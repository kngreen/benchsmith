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

        resolve_status.assert_called_once_with("task-b", roots=[str(self.repo)])
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
                check_review=False,
                check_hold=False,
            )

        self.assertEqual(result["state"], "rebased")
        self.assertFalse(result["needsRegate"], result)
        self.assertEqual(result["onto"], remote_head)
        self.assertEqual(result["stackProof"]["baseTaskTree"], self.tree(self.base, "task-b"))
        self.assertEqual(result["taskTree"], self.tree(source_candidate, "task-b"))
        carried = {**handoff, **result["handoffPatch"]}
        proof = candidate.verify_handoff(self.repo, "task-b", carried)
        self.assertEqual(proof.mode, "carry")
        self.assertEqual(proof.source_base_sha, self.base)
        self.assertEqual(proof.carried_from_sha, source_candidate)
        self.assertEqual(proof.task_tree, self.tree(result["newSha"], "task-b"))

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

    def handoff(self, commit_sha: str, base_sha: str, receipt: str = "receipt") -> dict:
        return {
            "work_item": "task-b",
            "state": "ready_to_publish",
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "gate_receipt": receipt,
            "next_action": "publish",
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
