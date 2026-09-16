from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchsmith import candidate
from benchsmith import cli
from benchsmith import dispatch
from benchsmith import gate
from benchsmith import publish
from benchsmith import task_status


class CandidateStackSafetyTest(unittest.TestCase):
    def setUp(self):
        self.safety = patch.object(
            gate.safety,
            "snapshot",
            side_effect=lambda component: {
                "component": component,
                "digest": f"sha256:{component}-test",
                "sourceHead": "a" * 40,
                "clean": True,
            },
        )
        self.safety_mock = self.safety.start()
        self.addCleanup(self.safety.stop)
        self.publication_verifier = patch.object(
            publish,
            "verify_publication_evidence",
            side_effect=lambda _repo, _task, sha, supplied, **_kwargs: {
                "schema_version": 1,
                "candidate_sha": sha,
                "gate": {"digest": str((supplied or {}).get("gate", {}).get("digest") or "receipt")},
                "reviews": {},
                "change": {"mode": "harden", "levers": ["graded"], "findings": []},
            },
        )
        self.publication_verifier.start()
        self.addCleanup(self.publication_verifier.stop)
        self.publication_builder = patch.object(
            publish,
            "publication_evidence",
            side_effect=lambda _repo, _task, sha, **_kwargs: {
                "schema_version": 1,
                "candidate_sha": sha,
                "gate": {"digest": gate._receipt_body(self.repo, "task-b")[0]["digest"]},
                "reviews": {},
                "change": {"mode": "harden", "levers": ["graded"], "findings": []},
            },
        )
        self.publication_builder_mock = self.publication_builder.start()
        self.addCleanup(self.publication_builder.stop)
        self.review_carry = patch(
            "benchsmith.critic_receipt.carry", return_value={"ok": True}
        )
        self.review_carry.start()
        self.addCleanup(self.review_carry.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "repo"
        self._run("git", "init", "--bare", "-q", "-b", "main", str(self.remote))
        self._run("git", "init", "-q", "-b", "main", str(self.repo))
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Benchsmith Test")
        for task in ("task-a", "task-b"):
            (self.repo / task / "tests").mkdir(parents=True)
            (self.repo / task / "instruction.md").write_text(f"{task} specification\n")
            (self.repo / task / "tests" / "test_task.py").write_text(
                "def test_task():\n    assert True\n"
            )
            (self.repo / task / "solve.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / ".gitignore").write_text(".benchsmith/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.sha("HEAD")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        dispatch.write_assignment(
            self.repo,
            "task-b",
            "session-test",
            "",
            controller_fingerprint="sha256:controller_dispatch-test",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_task_b_on_unmerged_task_a_parent_is_rejected_and_status_is_blocked(self):
        task_a = self.commit("task-a/instruction.md", "task A candidate\n", "task A")
        task_b = self.commit("task-b/instruction.md", "task B candidate\n", "task B")
        self.assertEqual(self.sha(f"{task_b}^"), task_a)
        handoff = self.handoff(task_b, task_a)

        with self.assertRaises(dispatch.CandidateHandoffRefused) as caught:
            dispatch.finalize_handoff(self.repo, "task-b", handoff)
        self.assertIn("changes outside task-b", str(caught.exception))
        self.assertIn("task-a/instruction.md", str(caught.exception))
        with self.assertRaises(publish.PublishRefused) as publish_error:
            publish.publish(
                self.repo,
                "task-b",
                handoff,
                check_review=False,
                check_hold=False,
            )
        self.assertIn("changes outside task-b", str(publish_error.exception))

        handoff_path = self.repo / dispatch.HANDOFF_DIR / "task-b.json"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(json.dumps(handoff))
        fleet = self.repo / ".benchsmith" / "fleet"
        fleet.mkdir(parents=True, exist_ok=True)
        (fleet / "current.json").write_text(
            json.dumps(
                {
                    "plans": [
                        {
                            "task": "task-b",
                            "workItem": "task-b",
                            "repo": str(self.repo),
                            "worktree": str(self.repo),
                            "mode": "harden",
                            "session": "session-b",
                            "taskId": "42",
                        }
                    ]
                }
            )
        )
        task_status.transition(
            self.repo,
            "handoff",
            "task-b",
            state="ready_to_publish",
            sha=task_b,
            announce=False,
        )
        self.assertEqual(
            task_status.read(self.repo)["rows"]["task-b"]["status"],
            task_status.READY_TO_PUBLISH,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.cmd_status(SimpleNamespace(repo=str(self.repo))), 0)
        live = json.loads(output.getvalue())
        self.assertEqual(live["workers"][0]["state"], "blocked")
        row = task_status.read(self.repo)["rows"]["task-b"]
        self.assertEqual(row["status"], "blocked")
        self.assertNotEqual(row["status"], task_status.READY_TO_PUBLISH)

    def test_clean_task_b_only_stack_is_finalized(self):
        first = self.commit("task-b/instruction.md", "task B first\n", "task B first")
        candidate_sha = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B second",
        )
        self.assertEqual(self.sha(f"{candidate_sha}^"), first)

        result = dispatch.finalize_handoff(
            self.repo, "task-b", self.handoff(candidate_sha, self.base)
        )

        self.assertTrue(result["ok"])
        stored = dispatch.parse_handoff(Path(result["path"]).read_text())
        self.assertEqual(stored["state"], "ready_to_publish")
        self.assertEqual(stored["commit_sha"], candidate_sha)

    def test_unregistered_status_resolution_uses_published_worktree(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B candidate"
        )
        handoff = self.handoff(candidate_sha, self.base)
        status = {
            "status": "unregistered",
            "registered": False,
            "awaitingReview": False,
        }

        with patch("benchsmith.resolve.resolve", return_value=status) as resolve_status:
            result = publish.publish(
                self.repo,
                "task-b",
                handoff,
                check_hold=False,
            )

        self.assertEqual(resolve_status.call_count, 1)
        resolve_status.assert_called_with("task-b", roots=[str(self.repo)])
        self.assertFalse(result["applied"])

    def test_byte_identical_rebase_carries_receipt_with_explicit_tree_proof(self):
        self.git("checkout", "-qb", "candidate", self.base)
        source_candidate = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B candidate",
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.assertTrue(receipt["ok"])

        self.git("checkout", "-q", "main")
        remote_head = self.commit(
            "task-a/instruction.md", "task A landed sibling\n", "task A sibling"
        )
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "candidate")
        handoff = self.handoff(source_candidate, self.base, receipt["digest"])
        lane = publish.Lane(self.repo, run_id="rebase-phases")
        review_fetch_lane_states = []
        hook_review_auth_lane_states = []
        build_evidence = self.publication_builder_mock.side_effect

        def review_requests(*_args, **_kwargs):
            review_fetch_lane_states.append(bool(lane.holder()))
            return {"requests": []}

        def publication_evidence(*args, **kwargs):
            hook_review_auth_lane_states.append(bool(lane.holder()))
            return build_evidence(*args, **kwargs)

        self.publication_builder_mock.side_effect = publication_evidence
        with (
            patch("benchsmith.reviews.requests", side_effect=review_requests),
            patch(
                "benchsmith.gate.check_control_manifest",
                side_effect=self.shadow_controls,
            ),
        ):
            result = publish.publish(
                self.repo,
                "task-b",
                handoff,
                rebase=True,
                lane=lane,
                check_review=False,
                check_hold=False,
            )

        self.assertEqual(result["state"], "rebased")
        self.assertFalse(result["needsRegate"], result)
        self.assertEqual(review_fetch_lane_states, [False, False])
        self.assertEqual(hook_review_auth_lane_states, [False])
        carried_receipt, problem = gate._receipt_body(self.repo, "task-b")
        self.assertEqual(problem, "")
        self.assertEqual(carried_receipt["repositoryHook"]["state"], "deferred")
        self.assertEqual(result["onto"], remote_head)
        self.assertEqual(result["stackProof"]["baseTaskTree"], self.tree(self.base, "task-b"))
        self.assertEqual(result["taskTree"], self.tree(source_candidate, "task-b"))
        carried = {**handoff, **result["handoffPatch"]}
        proof = candidate.verify_handoff(self.repo, "task-b", carried)
        self.assertEqual(proof.mode, "carry")
        self.assertEqual(proof.source_base_sha, self.base)
        self.assertEqual(proof.carried_from_sha, source_candidate)
        self.assertEqual(proof.task_tree, self.tree(result["newSha"], "task-b"))

    def test_another_lane_holder_prevents_rebase_or_worktree_mutation(self):
        self.git("checkout", "-qb", "candidate", self.base)
        source_candidate = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B candidate",
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.git("checkout", "-q", "main")
        self.commit("task-a/instruction.md", "sibling\n", "sibling")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "candidate")
        handoff = self.handoff(source_candidate, self.base, receipt["digest"])
        lane = publish.Lane(self.repo, run_id="blocked-rebase")
        self.assertTrue(lane.acquire("other-task"))
        before_head = self.sha("HEAD")
        before_status = self.git("status", "--porcelain").stdout

        with self.assertRaisesRegex(publish.PublishRefused, "held by other-task"):
            publish.publish(
                self.repo,
                "task-b",
                handoff,
                rebase=True,
                lane=lane,
                check_review=False,
                check_hold=False,
            )

        self.assertEqual(self.sha("HEAD"), before_head)
        self.assertEqual(self.git("status", "--porcelain").stdout, before_status)
        self.assertEqual(lane.holder()["task"], "other-task")
        lane.release("other-task")

    def test_remote_move_between_rebase_phases_refuses_without_push(self):
        self.git("checkout", "-qb", "candidate", self.base)
        source_candidate = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B candidate",
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.git("checkout", "-q", "main")
        remote_head = self.commit(
            "task-a/instruction.md", "first sibling\n", "first sibling"
        )
        remote_later = self.commit(
            "task-a/instruction.md", "second sibling\n", "second sibling"
        )
        self.git("push", "-q", "origin", f"{remote_head}:main")
        self.git("checkout", "-q", "candidate")
        handoff = self.handoff(source_candidate, self.base, receipt["digest"])
        lane = publish.Lane(self.repo, run_id="two-phase")
        publish_pushes = []

        def authenticate(_repo, _task, sha, **_kwargs):
            self.assertIsNone(lane.holder())
            self.git("push", "-q", "origin", f"{remote_later}:main")
            return {
                "schema_version": 1,
                "candidate_sha": sha,
                "gate": {"digest": gate._receipt_body(self.repo, "task-b")[0]["digest"]},
                "reviews": {},
                "change": {"mode": "harden", "levers": ["graded"], "findings": []},
            }

        def git(repo, *args, **kwargs):
            if args and args[0] == "push":
                publish_pushes.append(args)
            return publish._git(repo, *args, **kwargs)

        self.publication_builder_mock.side_effect = authenticate
        with (
            patch("benchsmith.reviews.requests", return_value={"requests": []}),
            patch(
                "benchsmith.gate.check_control_manifest",
                side_effect=self.shadow_controls,
            ),
            self.assertRaisesRegex(
                publish.PublishRefused, "moved between the rebase and publish phases"
            ),
        ):
            publish.publish(
                self.repo,
                "task-b",
                handoff,
                rebase=True,
                apply=True,
                lane=lane,
                git=git,
                check_review=False,
                check_hold=False,
            )

        self.assertEqual(publish_pushes, [])
        self.assertEqual(self.sha("origin/main"), remote_later)
        self.assertIsNone(lane.holder())

    def test_two_phase_rebase_pushes_only_after_reacquiring_lane(self):
        self.git("checkout", "-qb", "candidate", self.base)
        source_candidate = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B candidate",
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.git("checkout", "-q", "main")
        remote_head = self.commit(
            "task-a/instruction.md", "sibling\n", "sibling"
        )
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "candidate")
        handoff = self.handoff(source_candidate, self.base, receipt["digest"])
        lane = publish.Lane(self.repo, run_id="two-phase-success")
        phases = []
        build_evidence = self.publication_builder_mock.side_effect

        def authenticate(*args, **kwargs):
            phases.append(("authenticate", bool(lane.holder())))
            return build_evidence(*args, **kwargs)

        def git(repo, *args, **kwargs):
            if args and args[0] == "rebase":
                phases.append(("rebase", bool(lane.holder())))
            if args and args[0] == "push":
                phases.append(("push", bool(lane.holder())))
            return publish._git(repo, *args, **kwargs)

        self.publication_builder_mock.side_effect = authenticate
        with (
            patch("benchsmith.reviews.requests", return_value={"requests": []}),
            patch(
                "benchsmith.gate.check_control_manifest",
                side_effect=self.shadow_controls,
            ),
        ):
            result = publish.publish(
                self.repo,
                "task-b",
                handoff,
                rebase=True,
                apply=True,
                lane=lane,
                git=git,
                check_review=False,
                check_hold=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(phases[0], ("rebase", True))
        self.assertIn(("authenticate", False), phases)
        self.assertEqual(phases[-1], ("push", True))
        self.assertEqual(self.sha("origin/main"), result["commit"])
        self.assertNotEqual(result["commit"], source_candidate)
        self.assertEqual(
            self.git("merge-base", "--is-ancestor", remote_head, result["commit"]).returncode,
            0,
        )
        self.assertIsNone(lane.holder())

    def test_explicit_manual_carry_supports_divergent_histories(self):
        self.git("checkout", "-qb", "source", self.base)
        source_candidate = self.commit(
            "task-b/tests/test_task.py",
            "def test_task():\n    assert 1 == 1\n",
            "task B source candidate",
        )
        source_receipt = gate.write_receipt(
            self.repo, "task-b", self.passing_report()
        )
        self.assertTrue(source_receipt["ok"])

        self.git("checkout", "-q", "main")
        (self.repo / "task-a" / "instruction.md").write_text("divergent remote task A\n")
        self.git("add", "--", "task-a/instruction.md")
        target_tree = self.git("write-tree").stdout.strip()
        target_base = self.git("commit-tree", target_tree, "-m", "divergent root").stdout.strip()
        self.git("reset", "-q", "--hard", target_base)
        self.git("push", "-q", "--force", "origin", "HEAD:main")
        self.git("cherry-pick", source_candidate)
        target_candidate = self.sha("HEAD")

        with (
            patch("benchsmith.reviews.requests", return_value={"requests": []}),
            patch(
                "benchsmith.gate.check_control_manifest",
                side_effect=self.shadow_controls,
            ),
        ):
            carried_receipt = gate.carry_receipt(
                self.repo, "task-b", target_candidate
            )
        self.assertTrue(carried_receipt["ok"], carried_receipt)
        handoff = self.handoff(
            target_candidate, target_base, carried_receipt["receipt"]["digest"]
        )
        handoff.update(
            source_base_sha=self.base, carried_from_sha=source_candidate
        )

        proof = candidate.verify_handoff(self.repo, "task-b", handoff)

        self.assertEqual(proof.mode, "carry")
        self.assertEqual(proof.base_task_tree, self.tree(self.base, "task-b"))
        self.assertEqual(proof.task_tree, self.tree(source_candidate, "task-b"))

    def test_incomparable_declared_base_blocks_without_carry(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B"
        )
        root_tree = self.git("rev-parse", f"{self.base}^{{tree}}").stdout.strip()
        unrelated_base = self.git(
            "commit-tree", root_tree, "-m", "unrelated base"
        ).stdout.strip()
        handoff = self.handoff(candidate_sha, unrelated_base)

        with self.assertRaises(dispatch.CandidateHandoffRefused) as caught:
            dispatch.finalize_handoff(self.repo, "task-b", handoff)

        self.assertIn("no verified carry proof", str(caught.exception))

    def test_unknown_declared_base_blocks(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B"
        )
        handoff = self.handoff(candidate_sha, "f" * 40)
        with self.assertRaises(dispatch.CandidateHandoffRefused) as caught:
            dispatch.finalize_handoff(self.repo, "task-b", handoff)
        self.assertIn("not a resolvable local commit", str(caught.exception))

    def test_gate_fingerprint_drift_invalidates_receipt(self):
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.assertTrue(receipt["ok"])
        self.safety_mock.side_effect = lambda component: {
            "component": component,
            "digest": (
                "sha256:gate-changed"
                if component == "gate"
                else f"sha256:{component}-test"
            ),
            "sourceHead": "b" * 40,
            "clean": True,
        }

        ok, reason = gate.verify_receipt(self.repo, "task-b")

        self.assertFalse(ok)
        self.assertIn("gate implementation changed", reason)

    def test_publish_policy_drift_blocks_real_push(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B candidate"
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.assertTrue(receipt["ok"])
        handoff = self.handoff(candidate_sha, self.base, receipt["digest"])
        self.safety_mock.side_effect = lambda component: {
            "component": component,
            "digest": (
                "sha256:publish-policy-changed"
                if component == "publish_policy"
                else f"sha256:{component}-test"
            ),
            "sourceHead": "b" * 40,
            "clean": True,
        }
        with self.assertRaisesRegex(
            publish.PublishRefused, "publish policy changed"
        ):
            publish.publish(
                self.repo,
                "task-b",
                handoff,
                apply=True,
                check_review=False,
                check_hold=False,
            )

    def test_expensive_review_authentication_finishes_before_lane_acquisition(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B candidate"
        )
        receipt = gate.write_receipt(self.repo, "task-b", self.passing_report())
        self.assertTrue(receipt["ok"])
        handoff = self.handoff(candidate_sha, self.base, receipt["digest"])
        lane = publish.Lane(self.repo, run_id="race")
        phases = []

        def verify(_repo, _task, _sha, _handoff, *, authenticate_review=True, **_kwargs):
            phases.append(("live" if authenticate_review else "cheap", bool(lane.holder())))
            return handoff["publication_evidence"]

        def resolve_status(*_args, **_kwargs):
            phases.append(("status", bool(lane.holder())))
            return {"status": "draft"}

        def git(repo, *args, **kwargs):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args, 0, "", "")
            return publish._git(repo, *args, **kwargs)

        with (
            patch.object(publish, "verify_handoff_evidence", side_effect=verify),
            patch("benchsmith.resolve.resolve", side_effect=resolve_status),
        ):
            result = publish.publish(
                self.repo,
                "task-b",
                handoff,
                apply=True,
                lane=lane,
                git=git,
                check_hold=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(phases[0], ("live", False))
        self.assertIn(("status", True), phases)
        self.assertEqual(phases[-2:], [("cheap", True), ("cheap", True)])
        self.assertIsNone(lane.holder())

    def test_remote_tip_equal_candidate_returns_already_published_without_push(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B published\n", "task B published"
        )
        self.git("push", "-q", "origin", "HEAD:main")
        handoff = self.handoff(candidate_sha, self.base, receipt="")

        result = publish.publish(self.repo, "task-b", handoff, apply=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "already-published")
        self.assertEqual(result["commit"], candidate_sha)
        self.assertEqual(result["nextAction"], "watch-exact-sha")
        self.assertFalse((self.repo / ".benchsmith" / "publish.intent.json").exists())

    def test_candidate_ancestor_but_not_tip_is_not_already_published(self):
        candidate_sha = self.commit(
            "task-b/instruction.md", "task B candidate\n", "task B candidate"
        )
        self.git("push", "-q", "origin", "HEAD:main")
        self.commit("task-a/instruction.md", "task A later\n", "task A later")
        self.git("push", "-q", "origin", "HEAD:main")

        with self.assertRaisesRegex(
            publish.PublishRefused, "empty self-range"
        ):
            publish.inspect_candidate(
                self.repo, "task-b", self.handoff(candidate_sha, candidate_sha)
            )

    def test_already_published_still_requires_task_only_scope(self):
        task_a = self.commit("task-a/instruction.md", "task A candidate\n", "task A")
        task_b = self.commit("task-b/instruction.md", "task B candidate\n", "task B")
        self.git("push", "-q", "origin", "HEAD:main")

        with self.assertRaisesRegex(publish.PublishRefused, "outside task-b"):
            publish.publish(
                self.repo,
                "task-b",
                self.handoff(task_b, self.base, receipt=""),
                apply=True,
            )
        self.assertEqual(self.sha(f"{task_b}^"), task_a)

    def handoff(self, commit_sha: str, base_sha: str, receipt: str = "receipt") -> dict:
        change = {"mode": "harden", "levers": ["graded"], "findings": []}
        return {
            "work_item": "task-b",
            "state": "ready_to_publish",
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "gate_receipt": receipt,
            "publication_evidence": {
                "schema_version": 1,
                "candidate_sha": commit_sha,
                "gate": {"digest": receipt},
                "reviews": {},
                "change": change,
            },
            "change_evidence": change,
            "next_action": "publish",
            "session": "session-test",
        }

    @staticmethod
    def shadow_controls(repo_root, task_dir, task_name, report, **kwargs):
        report.artifacts["controlManifest"] = {"mode": "shadow"}
        report.add("control-manifest", gate.PASS, "not part of this regression")

    @staticmethod
    def passing_report() -> gate.Report:
        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            report.add(name, gate.PASS, "test evidence")
        return report

    def commit(self, relative: str, content: str, message: str) -> str:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", "--", relative)
        self.git("commit", "-qm", message)
        return self.sha("HEAD")

    def tree(self, revision: str, task: str) -> str:
        return self.git("rev-parse", f"{revision}:{task}").stdout.strip()

    def sha(self, revision: str) -> str:
        return self.git("rev-parse", revision).stdout.strip()

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return self._run("git", "-C", str(self.repo), *args)

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
