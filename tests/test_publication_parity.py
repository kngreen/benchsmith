from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchsmith import cli, critic_receipt, dispatch, gate, publish, sources, task_status, watch
from benchsmith.queue import build_queue, fresh_intake_blockers


class PublicationParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.receipts = self.root / "receipts"
        self.reviews = self.root / "reviews"
        self._run("git", "init", "--bare", "-q", "-b", "main", str(self.remote))
        self._run("git", "init", "-q", "-b", "main", str(self.repo))
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Benchsmith Test")
        (self.repo / "task" / "tests").mkdir(parents=True)
        (self.repo / "task" / "qa").mkdir()
        (self.repo / "task" / "task.toml").write_text("[metadata]\n")
        (self.repo / "task" / "instruction.md").write_text("base\n")
        (self.repo / "task" / "tests" / "test_task.py").write_text(
            "def test_task():\n    assert True\n"
        )
        (self.repo / ".gitignore").write_text(".benchsmith/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.sha("HEAD")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        (self.repo / "task" / "instruction.md").write_text("candidate\n")
        self.git("add", "task/instruction.md")
        self.git("commit", "-qm", "candidate")
        self.candidate = self.sha("HEAD")
        self.review_events: list[dict] = []
        self.live_transcript = patch.object(
            dispatch,
            "poll_session",
            side_effect=lambda _session, runner=None: (self.review_events, ""),
        )
        self.live_transcript.start()
        self.addCleanup(self.live_transcript.stop)
        self.env = patch.dict(
            os.environ,
            {
                "BENCHSMITH_RECEIPT_DIR": str(self.receipts),
                "BENCHSMITH_CRITIC_RECEIPT_DIR": str(self.reviews),
                "GATE_RECEIPT": str(self.root / "repo-hook-receipt.json"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
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
        self.safety.start()
        self.addCleanup(self.safety.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def passing_report(self) -> gate.Report:
        report = gate.Report()
        for name in gate.PUSH_REQUIRED:
            report.add(name, gate.PASS, "test evidence")
        report.artifacts["changeEvidence"] = {
            "mode": "harden",
            "levers": ["spec"],
            "findings": [],
        }
        report.artifacts["artifactTransfer"] = {"required": False}
        return report

    def ingest_review(self, *, canonical: str = "Accept", critic: str = "Accept") -> dict:
        document = {
            "task_id": "task-1",
            "sha": self.candidate,
            "canonical_review": {
                "name": "review-task-swebench-v2@1",
                "decision": canonical,
                "evidence_digest": "b" * 64,
            },
            "critic_version": "critic-v1",
            "session_id": "session-1",
            "decision": critic,
            "evidence_digest": "c" * 64,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        events = [
            {
                "type": "block",
                "event": {
                    "block": {
                        "text": critic_receipt.MARKER + json.dumps(document)
                    }
                },
            },
            {"type": "run_finished", "event": {}},
        ]

        self.review_events = events

        def poller(_argv):
            return 0, json.dumps(events) + "\n" + json.dumps({"has_more": "no"}), ""

        return critic_receipt.ingest(
            self.repo, "task", self.candidate, "session-1", poller=poller
        )

    def current_handoff(self) -> dict:
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(receipt.get("ok"), receipt)
        review = self.ingest_review()
        self.assertTrue(review.get("ok"), review)
        evidence = publish.publication_evidence(self.repo, "task", self.candidate)
        return {
            "state": "ready_to_publish",
            "base_sha": self.base,
            "commit_sha": self.candidate,
            "gate_receipt": receipt["digest"],
            "publication_evidence": evidence,
            "change_evidence": evidence["change"],
            "review": "this prose is display-only",
        }

    def test_publication_rejects_legacy_or_prose_only_handoff(self) -> None:
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(self.ingest_review().get("ok"))
        old = {
            "state": "ready_to_publish",
            "base_sha": self.base,
            "commit_sha": self.candidate,
            "gate_receipt": receipt["digest"],
            "review": "Accept",
        }
        with self.assertRaisesRegex(publish.PublishRefused, "legacy ready_to_publish"):
            publish.publish(
                self.repo,
                "task",
                old,
                check_review=False,
                check_hold=False,
            )

    def test_handoff_finalization_rejects_legacy_publication_contract(self) -> None:
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(self.ingest_review().get("ok"))
        dispatch.write_assignment(
            self.repo,
            "task",
            "session-1",
            "",
            controller_fingerprint="sha256:controller_dispatch-test",
        )
        old = {
            "state": "ready_to_publish",
            "session": "session-1",
            "base_sha": self.base,
            "commit_sha": self.candidate,
            "gate_receipt": receipt["digest"],
            "review": "Accept",
        }
        with self.assertRaisesRegex(
            dispatch.CandidateHandoffRefused, "legacy ready_to_publish"
        ):
            dispatch.finalize_handoff(self.repo, "task", old)

    def test_handoff_finalization_rejects_empty_gate_receipt_even_with_evidence(self) -> None:
        handoff = self.current_handoff()
        handoff.update(session="session-empty-gate", gate_receipt="")
        dispatch.write_assignment(
            self.repo,
            "task",
            "session-empty-gate",
            "",
            controller_fingerprint="sha256:controller_dispatch-test",
        )
        with self.assertRaisesRegex(
            dispatch.CandidateHandoffRefused, "no gate_receipt"
        ):
            dispatch.finalize_handoff(self.repo, "task", handoff)

    def test_exact_structured_gate_and_reviews_plan_publication(self) -> None:
        result = publish.publish(
            self.repo,
            "task",
            self.current_handoff(),
            check_review=False,
            check_hold=False,
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["planned"]["commit_sha"], self.candidate)

    def test_canonical_request_changes_cannot_mint_clearance(self) -> None:
        result = self.ingest_review(canonical="Request changes")
        self.assertFalse(result["ok"])
        self.assertIn("canonical_review.decision", result["reason"])

    def test_live_hook_content_change_invalidates_cached_gate(self) -> None:
        hooks = self.repo / ".git" / "hooks"
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(receipt.get("ok"), receipt)
        self.assertTrue(gate.verify_receipt(self.repo, "task")[0])

        hook.write_text("#!/bin/sh\necho 'missing canary / G21 / oracle' >&2\nexit 21\n")
        hook.chmod(0o755)
        ok, reason = gate.verify_receipt(self.repo, "task")
        self.assertFalse(ok)
        self.assertIn("live pre-push hook changed", reason)
        self.git("commit", "--allow-empty", "-qm", "tree-identical rebase")
        carried = gate.carry_receipt(self.repo, "task", self.sha("HEAD"))
        self.assertFalse(carried["ok"])
        self.assertIn("live pre-push hook changed", carried["reason"])
        rerun = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertEqual(rerun["state"], "not_written")
        self.assertIn("missing canary", rerun["reason"])

    def test_live_hook_reentrancy_defers_only_nested_verify_arm(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-push"
        binary = Path(gate.__file__).resolve().parents[2] / "bin" / "benchsmith"
        marker = self.root / "chain-marker"
        cases = {
            "canonical": gate.PRE_PUSH.replace("$BENCHSMITH_BIN", str(binary)),
            "commented-chain": (
                "#!/bin/sh\nset -eu\n# wrapper around benchsmith pre-push\n"
                f"{binary} gate --repo \"$(git rev-parse --show-toplevel)\" "
                "--task \"$BENCHSMITH_TASK\" --verify-receipt\n"
                f"printf chained > {marker}\n"
            ),
            "recursive-verifier": (
                "#!/bin/sh\nset -eu\n"
                f"exec {binary} gate --repo \"$(git rev-parse --show-toplevel)\" "
                "--task \"$BENCHSMITH_TASK\" --verify-receipt\n"
            ),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                hook.write_text(body)
                hook.chmod(0o755)
                receipt = gate.write_receipt(self.repo, "task", self.passing_report())
                self.assertTrue(receipt.get("ok"), receipt)
                self.assertEqual(receipt["repositoryHook"]["state"], "passed")
        self.assertEqual(marker.read_text(), "chained")

        with patch.dict(
            os.environ,
            {
                gate.OUTER_HOOK_AUTHORITY: "forged",
                gate.OUTER_HOOK_STATE: str(self.root / "missing-authority"),
            },
        ):
            self.assertFalse(gate.nested_hook_is_authorized(self.repo, "task"))

        forged_dir = self.root / "forged-authority"
        forged_dir.mkdir(mode=0o700)
        forged_state = forged_dir / "authority.json"
        forged_state.write_text(json.dumps({
            "token": "self-issued",
            "repo": str(self.repo.resolve()),
            "task": "task",
            "head": self.candidate,
            "pid": os.getpid(),
        }))
        forged_state.chmod(0o600)
        forged_receipt = forged_dir / "receipt.json"
        forged_receipt.write_text("{}")
        forged_receipt.chmod(0o600)
        with patch.dict(
            os.environ,
            {
                gate.OUTER_HOOK_AUTHORITY: "self-issued",
                gate.OUTER_HOOK_STATE: str(forged_state),
                "GATE_RECEIPT": str(forged_receipt),
            },
        ):
            self.assertFalse(gate.nested_hook_is_authorized(self.repo, "task"))

    def test_hook_receipt_is_private_and_removed_on_success_and_failure(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-push"
        observed = self.root / "observed-hook-path"
        hook.write_text(
            "#!/bin/sh\nset -eu\n"
            'test "$(stat -c %a "$GATE_RECEIPT")" = 600\n'
            'test "$(stat -c %a "$(dirname "$GATE_RECEIPT")")" = 700\n'
            f"printf '%s' \"$GATE_RECEIPT\" > {observed}\n"
        )
        hook.chmod(0o755)
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(receipt.get("ok"), receipt)
        ephemeral = Path(observed.read_text())
        self.assertFalse(ephemeral.exists())
        self.assertNotIn("gate-receipt-task.json", str(ephemeral))

        hook.write_text(hook.read_text() + "exit 9\n")
        failed = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertEqual(failed["state"], "not_written")
        self.assertFalse(Path(observed.read_text()).exists())

    def test_gate_refuses_missing_remote_branch_even_without_hook(self) -> None:
        result = gate.write_receipt(
            self.repo, "task", self.passing_report(), branch="does-not-exist"
        )
        self.assertEqual(result["state"], "not_written")
        self.assertIn("does not exist", result["reason"])

    def test_gate_infers_master_as_remote_default_branch(self) -> None:
        root = self.root / "master-case"
        repo = root / "repo"
        remote = root / "remote.git"
        self._run("git", "init", "--bare", "-q", "-b", "master", str(remote))
        self._run("git", "init", "-q", "-b", "master", str(repo))
        self._run("git", "-C", str(repo), "config", "user.email", "test@example.com")
        self._run("git", "-C", str(repo), "config", "user.name", "Benchsmith Test")
        (repo / "task").mkdir(parents=True)
        (repo / "task" / "task.toml").write_text("[metadata]\n")
        self._run("git", "-C", str(repo), "add", ".")
        self._run("git", "-C", str(repo), "commit", "-qm", "base")
        self._run("git", "-C", str(repo), "remote", "add", "origin", str(remote))
        self._run("git", "-C", str(repo), "push", "-q", "-u", "origin", "master")

        receipt = gate.write_receipt(repo, "task", self.passing_report())

        self.assertTrue(receipt.get("ok"), receipt)
        self.assertEqual(receipt["repositoryHook"]["branch"], "master")
        self.assertEqual(receipt["repositoryHook"]["state"], "not-applicable")

    def test_gate_without_hook_or_remote_keeps_target_not_applicable(self) -> None:
        repo = self.root / "no-origin"
        self._run("git", "init", "-q", "-b", "trunk", str(repo))
        self._run("git", "-C", str(repo), "config", "user.email", "test@example.com")
        self._run("git", "-C", str(repo), "config", "user.name", "Benchsmith Test")
        (repo / "task").mkdir(parents=True)
        (repo / "task" / "task.toml").write_text("[metadata]\n")
        self._run("git", "-C", str(repo), "add", ".")
        self._run("git", "-C", str(repo), "commit", "-qm", "base")

        receipt = gate.write_receipt(repo, "task", self.passing_report())

        self.assertTrue(receipt.get("ok"), receipt)
        self.assertEqual(receipt["repositoryHook"]["state"], "not-applicable")
        self.assertEqual(receipt["repositoryHook"]["branch"], "")

    def test_legacy_review_receipt_fails_with_migration_error(self) -> None:
        legacy = {
            "version": 1,
            "task": "task",
            "sha": self.candidate,
            "decision": "Accept",
            "source": "agentcloud-session-transcript",
        }
        legacy["digest"] = critic_receipt._digest(legacy)
        destination = critic_receipt.path(self.repo, "task")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(legacy))

        loaded, reason = critic_receipt.load(self.repo, "task", self.candidate)
        self.assertIsNone(loaded)
        self.assertIn("predates exact-SHA schema v2", reason)

    def test_authenticated_critic_receipt_without_digest_is_rejected(self) -> None:
        written = self.ingest_review()
        self.assertTrue(written["ok"])
        current = dict(written["receipt"])
        self.assertIsInstance(current.pop("digest"), str)
        Path(written["path"]).write_text(json.dumps(current))

        loaded, problem = critic_receipt.load(
            self.repo, "task", self.candidate
        )

        self.assertIsNone(loaded)
        self.assertEqual(
            problem, "critic receipt digest does not match its contents"
        )

    def test_terminal_session_append_requires_reingest(self) -> None:
        ingested = self.ingest_review()
        self.assertTrue(ingested["ok"])
        cached, problem = critic_receipt.load(self.repo, "task", self.candidate)
        self.assertEqual(problem, "")
        self.review_events.append({
            "type": "block",
            "event": {"block": {"text": "post-terminal bookkeeping"}},
        })

        verified, problem = critic_receipt.verify_live(
            self.repo, "task", self.candidate, cached
        )

        self.assertIsNone(verified)
        self.assertEqual(
            problem,
            "critic session advanced after receipt ingest; re-ingest receipt and "
            "regenerate the handoff",
        )

    def test_hand_written_self_digested_review_cache_is_rejected_by_live_transcript(self) -> None:
        ingested = self.ingest_review()
        self.assertTrue(ingested["ok"])
        receipt = gate.write_receipt(self.repo, "task", self.passing_report())
        self.assertTrue(receipt.get("ok"), receipt)
        original_evidence = publish.publication_evidence(
            self.repo, "task", self.candidate
        )
        destination = Path(ingested["path"])
        forged = json.loads(destination.read_text())
        forged["critic_version"] = "forged-local-review"
        unsigned = {key: value for key, value in forged.items() if key != "digest"}
        forged["digest"] = critic_receipt._digest(unsigned)
        destination.write_text(json.dumps(forged))

        with self.assertRaisesRegex(
            publish.PublishRefused, "does not match the re-fetched terminal session"
        ):
            publish.publication_evidence(self.repo, "task", self.candidate)

        dispatch.write_assignment(
            self.repo,
            "task",
            "session-forged",
            "",
            controller_fingerprint="sha256:controller_dispatch-test",
        )
        handoff = {
            "work_item": "task",
            "state": "ready_to_publish",
            "session": "session-forged",
            "base_sha": self.base,
            "commit_sha": self.candidate,
            "gate_receipt": receipt["digest"],
            "publication_evidence": original_evidence,
            "change_evidence": original_evidence["change"],
        }
        with self.assertRaisesRegex(
            dispatch.CandidateHandoffRefused,
            "does not match the re-fetched terminal session",
        ):
            dispatch.finalize_handoff(self.repo, "task", handoff)

    def test_review_receipt_carries_only_across_identical_task_tree(self) -> None:
        self.assertTrue(self.ingest_review().get("ok"))
        self.git("commit", "--allow-empty", "-qm", "rebased identity")
        target = self.sha("HEAD")
        carried = critic_receipt.carry(self.repo, "task", self.candidate, target)
        self.assertTrue(carried.get("ok"), carried)
        loaded, problem = critic_receipt.load(self.repo, "task", target)
        self.assertEqual(problem, "")
        self.assertEqual(loaded["derived_from_sha"], self.candidate)

        (self.repo / "task" / "instruction.md").write_text("different tree\n")
        self.git("add", "task/instruction.md")
        self.git("commit", "-qm", "different task")
        rejected = critic_receipt.carry(
            self.repo, "task", self.candidate, self.sha("HEAD")
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("task tree changed", rejected["reason"])

    def test_separate_verifier_requires_executable_artifact_transfer_contract(self) -> None:
        (self.repo / "task" / "tests" / "Dockerfile").write_text("FROM scratch\n")
        report = gate.Report()
        gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.NOT_RUN)

        contract = self.repo / "task" / "qa" / "artifact-transfer"
        contract.write_text("#!/bin/sh\nexit 0\n")
        report = gate.Report()
        gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.NOT_RUN)

        contract.chmod(0o755)
        absolute = self.repo / "task" / "absolute-repo-link"
        absolute.symlink_to("/candidate/repo")
        contract.write_text(
            "#!/bin/sh\n"
            "target=$(readlink absolute-repo-link)\n"
            "case \"$target\" in /*) echo 'absolute symlink blocks /app export' >&2; exit 9;; esac\n"
        )
        self.git(
            "add",
            "task/tests/Dockerfile",
            "task/qa/artifact-transfer",
            "task/absolute-repo-link",
        )
        self.git("commit", "-qm", "failing artifact transfer contract")
        report = gate.Report()
        gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.FAIL)
        self.assertIn("absolute symlink", report.checks[-1].detail)

        absolute.unlink()
        absolute.symlink_to("instruction.md")
        self.git("add", "task/absolute-repo-link")
        self.git("commit", "-qm", "artifact transfer contract")
        transfer_sha = self.sha("HEAD")
        report = gate.Report()
        gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.PASS)
        self.assertEqual(
            report.artifacts["artifactTransfer"]["candidateSha"], transfer_sha
        )
        for name in gate.PUSH_REQUIRED:
            if name != "artifact-transfer":
                report.add(name, gate.PASS, "test evidence")
        report.artifacts["changeEvidence"] = {
            "mode": "harden",
            "levers": ["graded"],
            "findings": [],
        }
        receipt = gate.write_receipt(self.repo, "task", report)
        self.assertTrue(receipt.get("ok"), receipt)
        self.assertEqual(
            receipt["artifacts"]["artifactTransfer"]["candidateSha"], transfer_sha
        )

    def test_task_paths_are_validated_before_state_paths_are_built(self) -> None:
        for task in ("../escape", "task//nested", ".hidden", "task\\windows", "bad space"):
            with self.subTest(task=task):
                with self.assertRaises((ValueError, dispatch.DispatchRefused)):
                    dispatch.assignment_path(self.repo, task)
                with self.assertRaises(ValueError):
                    gate.receipt_path(self.repo, task)
                with self.assertRaises(ValueError):
                    critic_receipt.path(self.repo, task, self.candidate)

    def test_artifact_transfer_env_is_minimal_and_source_mutation_is_attributed(self) -> None:
        dockerfile = self.repo / "task" / "tests" / "Dockerfile"
        contract = self.repo / "task" / "qa" / "artifact-transfer"
        dockerfile.write_text("FROM scratch\n")
        contract.write_text(
            "#!/bin/sh\n"
            'test -z "${SECRET_TOKEN+x}"\n'
            'test ! -e "$BENCHSMITH_REPO/.git"\n'
            "exit 0\n"
        )
        contract.chmod(0o755)
        self.git("add", "task/tests/Dockerfile", "task/qa/artifact-transfer")
        self.git("commit", "-qm", "isolated transfer contract")
        with patch.dict(os.environ, {"SECRET_TOKEN": "must-not-leak"}):
            report = gate.Report()
            gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.PASS)
        self.assertNotIn("SECRET_TOKEN", report.artifacts["artifactTransfer"]["environment"])

        source_file = self.repo / "task" / "instruction.md"
        contract.write_text(
            "#!/bin/sh\n"
            f"printf mutation >> {source_file}\n"
            "exit 0\n"
        )
        contract.chmod(0o755)
        self.git("add", "task/qa/artifact-transfer")
        self.git("commit", "-qm", "source mutation probe")
        report = gate.Report()
        gate.check_artifact_transfer(self.repo / "task", report)
        self.assertEqual(report.checks[-1].state, gate.FAIL)
        self.assertIn("mutated the source checkout", report.checks[-1].detail)
        self.git("reset", "-q", "--hard")

    def sha(self, revision: str) -> str:
        return self.git("rev-parse", revision).stdout.strip()

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return self._run("git", "-C", str(self.repo), *args)

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, check=True, capture_output=True, text=True)


class QueueAndWatchParityTest(unittest.TestCase):
    def test_existing_platform_work_holds_fresh_board_dispatch(self) -> None:
        items = build_queue(
            [{"name": "revision", "status": "needs_revision", "validationStatus": "failed"}],
            ideas=[{"name": "T1", "kind": "idea", "title": "new seed"}],
        )
        by_name = {item.task: item for item in items}
        self.assertTrue(by_name["revision"].dispatchable)
        self.assertFalse(by_name["T1"].dispatchable)
        self.assertIn("fresh intake held", by_name["T1"].skip)
        self.assertEqual(fresh_intake_blockers(items), ["revision"])

    def test_publication_and_validation_rows_are_explicit_intake_blockers(self) -> None:
        rows = {
            "ready": {"status": "ready to publish"},
            "validating": {"status": "validating"},
            "reviewing": {"status": "awaiting agentic review"},
        }
        self.assertEqual(
            fresh_intake_blockers([], rows),
            ["ready", "reviewing", "validating"],
        )

    def test_passing_validation_waits_for_exact_sha_platform_signals(self) -> None:
        sha = "a" * 40
        pending = watch.classify(
            {"validationCommitSha": sha, "validationStatus": "passing"},
            sha,
            jobs=None,
            jobs_problem="job service unavailable",
            pushed_at=__import__("time").time() - 60,
        )
        self.assertEqual(pending["state"], watch.UNKNOWN)
        self.assertTrue(pending["retryable"])
        self.assertTrue(pending["blocksFreshIntake"])
        self.assertEqual(
            set(pending["platformSignals"]), {"tbr", "agentic"}
        )

        terminal = watch.classify(
            {
                "validationCommitSha": sha,
                "validationStatus": "passing",
                "tbdReviewStatus": "pass",
                "tbdReviewDetails": {"instance_id": f"tbr-{sha[:7]}"},
            },
            sha,
            jobs=[{
                "id": "job-1",
                "status": "completed",
                "stage": "agentic-review",
                "config": {"commitSha": sha},
                "agenticReview": {"verdict": "GOOD"},
            }],
        )
        self.assertEqual(terminal["state"], watch.TERMINAL)

    def test_task_row_agentic_shape_is_ignored_and_stale_job_is_unknown(self) -> None:
        sha = "a" * 40
        record = {
            "name": "task",
            "id": "9",
            "validationCommitSha": sha,
            "validationStatus": "passing",
            "tbdReviewStatus": "pass",
            "tbdReviewDetails": {"instance_id": f"tbr-{sha[:7]}"},
            # This field was the old fabricated source and must not clear Agentic.
            "agenticReview": {"status": "completed", "commitSha": sha, "verdict": "GOOD"},
        }
        stale = watch.classify(
            record,
            sha,
            jobs=[{
                "id": "old-job",
                "status": "completed",
                "stage": "agentic-review",
                "config": {"commitSha": "b" * 40},
                "agenticReview": {"verdict": "GOOD"},
            }],
            pushed_at=time.time() - 60,
        )
        self.assertEqual(stale["state"], watch.UNKNOWN)
        self.assertIn("stale rows", stale["reason"])
        self.assertTrue(stale["blocksFreshIntake"])

        expired = watch.classify(
            record,
            sha,
            jobs=None,
            jobs_problem="job service unreadable",
            pushed_at=time.time() - watch.EXTERNAL_SIGNAL_HOLD_SECONDS - 1,
        )
        self.assertEqual(expired["state"], watch.UNKNOWN)
        self.assertTrue(expired["clearEligible"])
        self.assertFalse(expired["blocksFreshIntake"])
        self.assertIn("status-clear", expired["statusClear"])

    def test_sources_attach_real_job_rows_for_passing_tasks(self) -> None:
        task = {
            "name": "task",
            "id": "9",
            "status": "draft",
            "validationStatus": "passing",
            "currentUserIsTaskOwner": True,
        }
        config = SimpleNamespace(
            configured=False, sections={}, assignee="", as_dict=lambda: {}
        )
        jobs = [{"id": "job-1", "stage": "agentic-review"}]
        with (
            patch.object(sources, "fetch_codimango", return_value=([task], [])),
            patch.object(sources, "fetch_codimango_jobs", return_value=(jobs, "")) as fetch,
        ):
            result = sources.discover(cfg=config, with_gsd=False)
        fetch.assert_called_once_with(task, binary="codimango")
        self.assertEqual(result["jobsByTask"], {"task": jobs})
        self.assertEqual(result["jobProblems"], {})

    def test_watch_read_uses_platform_job_rows_for_agentic(self) -> None:
        from benchsmith.adapter import Surface

        sha = "a" * 40
        record = {"id": "9", "name": "task", "validationCommitSha": sha}
        result = subprocess.CompletedProcess(
            ["codimango"], 0, json.dumps({"task": record}), ""
        )
        jobs = [{
            "id": "job-1",
            "stage": "agentic-review",
            "status": "completed",
            "config": {"commitSha": sha},
            "agenticReview": {"verdict": "GOOD"},
        }]
        surface = Surface(
            binary="codimango", task_show=("task", "show"), jobs_list=("job", "list")
        )
        with (
            patch("benchsmith.adapter.discover", return_value=surface),
            patch.object(watch.subprocess, "run", return_value=result),
            patch("benchsmith.adapter.Platform.jobs", return_value=jobs) as read_jobs,
        ):
            observed, observed_jobs, problem = watch._read("task")
        self.assertEqual(problem, "")
        self.assertEqual(observed, record)
        self.assertEqual(observed_jobs, jobs)
        read_jobs.assert_called_once_with()

    def test_status_clear_is_exact_sha_and_releases_only_intake_hold(self) -> None:
        root = Path(tempfile.mkdtemp())
        sha = "c" * 40
        task_status.update(
            root,
            {
                "task": "task",
                "status": task_status.BLOCKED_INFRA,
                "statusSource": "platform",
                "sha": sha,
                "validation": "passing",
            },
        )
        with self.assertRaisesRegex(task_status.StatusTableError, "does not match"):
            task_status.clear_external_signal_hold(
                root, "task", "d" * 40, "review service unavailable", apply=True
            )
        result = task_status.clear_external_signal_hold(
            root, "task", sha, "review service unavailable", apply=False
        )
        self.assertFalse(result["applied"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.cmd_status_clear(SimpleNamespace(
                repo=str(root),
                task="task",
                sha=sha,
                reason="review service unavailable",
                status_repo="",
                apply=True,
            ))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        row = task_status.read(root)["rows"]["task"]
        self.assertEqual(row["status"], task_status.BLOCKED_INFRA)
        self.assertEqual(row["sha"], sha)
        self.assertEqual(row["statusSource"], "platform")
        self.assertEqual(row[task_status.INTAKE_HOLD_CLEARED_SHA], sha)
        self.assertEqual(
            row[task_status.INTAKE_HOLD_CLEAR_REASON],
            "review service unavailable",
        )
        self.assertEqual(
            fresh_intake_blockers(
                [build_queue([{
                    "name": "task", "status": "draft", "validationStatus": "passing",
                    "validationCommitSha": sha,
                }])[0]],
                {"task": row},
            ),
            [],
        )
        self.assertEqual(
            fresh_intake_blockers(
                [build_queue([{
                    "name": "task", "status": "draft", "validationStatus": "passing",
                }])[0]],
                {"task": row},
            ),
            ["task"],
        )
        task_status.transition(
            root,
            "watch",
            "task",
            state="terminal",
            sha=sha,
            validation="passing",
        )
        self.assertEqual(
            task_status.read(root)["rows"]["task"]["status"],
            task_status.AWAITING_AGENTIC_REVIEW,
        )
        new_sha = "e" * 40
        task_status.transition(
            root,
            "publish",
            "task",
            state="published",
            ok=True,
            sha=new_sha,
            validation="pending",
        )
        advanced = task_status.read(root)["rows"]["task"]
        self.assertEqual(advanced["status"], task_status.VALIDATING)
        self.assertEqual(advanced[task_status.INTAKE_HOLD_CLEARED_SHA], "")
        self.assertEqual(
            fresh_intake_blockers([], {"task": advanced}), ["task"]
        )

    def test_failed_validation_is_terminal_without_post_validation_reviews(self) -> None:
        sha = "b" * 40
        result = watch.classify(
            {"validationCommitSha": sha, "validationStatus": "failed"}, sha
        )
        self.assertEqual(result["state"], watch.TERMINAL)


if __name__ == "__main__":
    unittest.main()
