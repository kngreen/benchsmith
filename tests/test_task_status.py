from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from benchsmith import cli
from benchsmith import dispatch
from benchsmith import task_status as status


SHA = "0123456789abcdef0123456789abcdef01234567"


class TaskStatusTableTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_row_is_written_once_and_only_changes_semantically(self):
        first = status.update(
            self.root,
            {
                "task": "task-one",
                "submissionId": "210976",
                "status": status.REVISION_HARDENING,
                "workerSession": "8bc65b1b-5b04-4656-96f8-9958e939c631",
                "sha": SHA,
                "validation": "pending",
                "review": "agentic review pending",
                "evidenceUrl": "https://www.internalfb.com/intern/paste/P1/",
            },
            now=datetime(2026, 9, 14, tzinfo=timezone.utc),
        )
        self.assertTrue(first["changed"])
        self.assertIn(
            "[task-one](https://codimango.internalmeta.com/submissions/210976)",
            first["markdown"],
        )
        self.assertIn(f"`{SHA}`", first["markdown"])
        state_path = Path(first["statePath"])
        markdown_path = Path(first["markdownPath"])
        before_state = state_path.read_bytes()
        before_markdown = markdown_path.read_bytes()

        unchanged = status.update(
            self.root,
            {
                "task": "task-one",
                "submissionId": "210976",
                "status": status.REVISION_HARDENING,
                "workerSession": "8bc65b1b-5b04-4656-96f8-9958e939c631",
                "sha": SHA,
                "validation": "pending",
                "review": "agentic review pending",
                "evidenceUrl": "https://www.internalfb.com/intern/paste/P1/",
            },
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
        self.assertFalse(unchanged["changed"])
        self.assertIsNone(unchanged["markdown"])
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(markdown_path.read_bytes(), before_markdown)

        changed = status.update(
            self.root,
            {"task": "task-one", "validation": "passing"},
            now="2026-09-15T03:04:05Z",
        )
        self.assertTrue(changed["changed"])
        row = status.read(self.root)["rows"]["task-one"]
        self.assertEqual(row["updatedAt"], "2026-09-15T03:04:05Z")
        self.assertEqual(row["validation"], "passing")

    def test_markdown_escapes_cells_and_keeps_links_clickable(self):
        update = status.update(
            self.root,
            {
                "task": "task|two",
                "submissionId": "42",
                "status": "blocked: a|b",
                "workerSession": "session-2",
                "sha": SHA,
                "review": "line one\nline two",
            },
            now="2026-09-14T00:00:00Z",
        )
        self.assertIn("task\\|two", update["markdown"])
        self.assertIn("blocked: a\\|b", update["markdown"])
        self.assertIn("line one<br>line two", update["markdown"])
        self.assertIn(
            "https://agentcloud.internalmeta.com/session-2", update["markdown"]
        )

    def test_worktree_updates_the_primary_checkout(self):
        main = self.root / "main"
        worktree = self.root / "worker"
        main.mkdir()
        self._git(main, "init", "-q", "-b", "main")
        self._git(main, "config", "user.email", "test@example.com")
        self._git(main, "config", "user.name", "Test")
        (main / "README").write_text("base\n")
        self._git(main, "add", "README")
        self._git(main, "commit", "-qm", "base")
        self._git(main, "worktree", "add", "-q", "-b", "worker", str(worktree))

        result = status.update(
            worktree, {"task": "from-worker", "status": status.NEXT_IN_QUEUE}
        )
        expected = main / status.STATE_DIR / status.STATE_FILE
        self.assertEqual(Path(result["statePath"]), expected)
        self.assertTrue(expected.is_file())
        self.assertFalse((worktree / status.STATE_DIR / status.STATE_FILE).exists())

    def test_required_lifecycle_vocabulary(self):
        cases = {
            "agentic": status.transition_patch(
                "watch", "t", state="terminal", validation="passing"
            )["status"],
            "hardening": status.transition_patch("worker-start", "t", mode="harden")[
                "status"
            ],
            "documentation": status.transition_patch(
                "worker-start", "t", mode="repair", detail="README documentation"
            )["status"],
            "queued": status.transition_patch("queued", "t")["status"],
            "green": status.transition_patch("record", "t", journal_status="converged")[
                "status"
            ],
            "infra": status.transition_patch(
                "handoff", "t", state="blocked", detail="platform timeout"
            )["status"],
            "review-wait": status.transition_patch(
                "publish", "t", detail="a reviewer has it now", ok=False
            )["status"],
            "accepted": status.transition_patch(
                "publish", "t", detail="task is already accepted", ok=False
            )["status"],
        }
        self.assertEqual(
            cases,
            {
                "agentic": "awaiting agentic review",
                "hardening": "revision: hardening",
                "documentation": "revision: documentation",
                "queued": "next in queue",
                "green": "ready to submit / green",
                "infra": "blocked: infra",
                "review-wait": "awaiting human review",
                "accepted": "terminal: accepted",
            },
        )

    def test_handoff_watch_and_terminal_transitions_keep_exact_state(self):
        self.assertEqual(
            status.transition_patch("handoff", "t", state="ready_to_publish")["status"],
            "ready to publish",
        )
        running = status.transition_patch(
            "watch", "t", state="running", validation="queued", sha=SHA
        )
        self.assertEqual(
            (running["status"], running["validation"], running["sha"]),
            ("validating", "queued", SHA),
        )
        failed = status.transition_patch(
            "watch", "t", state="terminal", validation="failed"
        )
        self.assertEqual(failed["status"], "revision: validation findings")
        waiting = status.transition_patch(
            "record", "t", journal_status="awaiting-review"
        )
        self.assertEqual(waiting["status"], "awaiting human review")
        abandoned = status.transition_patch("record", "t", journal_status="abandoned")
        self.assertEqual(abandoned["status"], "terminal: rejected")

    def test_review_summary_preserves_each_observed_state(self):
        self.assertEqual(
            status.review_summary(
                [
                    {
                        "name": "agentic-full-task",
                        "state": "completed",
                        "verdict": "GOOD",
                    },
                    {"name": "review-critic", "state": "pending", "verdict": ""},
                ]
            ),
            "agentic-full-task: completed/GOOD; review-critic: pending",
        )

    def test_fleet_start_and_queue_transition_are_persisted(self):
        for task in ("active-task", "queued-task"):
            (self.root / task).mkdir()
            (self.root / task / "task.toml").write_text("[metadata]\n")
        task_rows = [
            {
                "name": "active-task",
                "id": "101",
                "status": "draft",
                "validationStatus": "failed",
                "validationCommitSha": SHA,
            },
            {
                "name": "queued-task",
                "id": "102",
                "status": "draft",
                "validationStatus": "pending",
                "validationCommitSha": "f" * 40,
            },
        ]
        saved_discover = cli.sources.discover
        saved_resolve = cli.resolve_mod.resolve
        saved_run = cli.dispatch_mod.run
        saved_capability = cli.passatk_mod.capability
        try:
            cli.sources.discover = lambda **kwargs: {"tasks": task_rows, "notes": []}
            cli.resolve_mod.resolve = lambda task, rows=None: {
                "task": task,
                "id": next(row["id"] for row in task_rows if row["name"] == task),
                "repo": str(self.root),
                "mode": "harden",
                "sha": next(
                    row["validationCommitSha"]
                    for row in task_rows
                    if row["name"] == task
                ),
                "validation": next(
                    row["validationStatus"] for row in task_rows if row["name"] == task
                ),
            }
            cli.dispatch_mod.run = lambda plan, apply: {
                "ok": True,
                "stdout": '{"session_id":"session-active"}',
                "stderr": "",
            }
            cli.passatk_mod.capability = lambda path: {
                "applicable": False,
                "ready": True,
            }
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.cmd_fleet(
                    SimpleNamespace(
                        repo=str(self.root),
                        gsd_project="",
                        workers=1,
                        no_gsd=True,
                        target="hard-preferred",
                        apply=True,
                        no_snooze=True,
                        shared_tree=True,
                        no_remote_lease=True,
                        max_runtime=24.0,
                    )
                )
        finally:
            cli.sources.discover = saved_discover
            cli.resolve_mod.resolve = saved_resolve
            cli.dispatch_mod.run = saved_run
            cli.passatk_mod.capability = saved_capability
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["taskStatus"]["changed"])
        rows = status.read(self.root)["rows"]
        self.assertEqual(rows["active-task"]["status"], "revision: hardening")
        self.assertEqual(rows["active-task"]["workerSession"], "session-active")
        self.assertEqual(rows["active-task"]["submissionId"], "101")
        self.assertEqual(rows["queued-task"]["status"], "next in queue")

    def test_handoff_and_collect_do_not_repost_an_unchanged_table(self):
        dispatch.write_assignment(
            self.root,
            "task-one",
            "session-one",
            "",
            status_repo=str(self.root),
            submission_id="210976",
        )
        handoff_path = self.root / "handoff-input.json"
        handoff_path.write_text(
            json.dumps(
                {
                    "work_item": "task-one",
                    "state": "ready_to_publish",
                    "base_sha": "b" * 40,
                    "commit_sha": SHA,
                    "gate_receipt": "receipt",
                    "next_action": "publish",
                    "note": "gated and ready",
                    "evidence_url": "https://www.internalfb.com/intern/paste/P1/",
                }
            )
        )
        first_output = io.StringIO()
        with redirect_stdout(first_output):
            handoff_code = cli.cmd_handoff(
                SimpleNamespace(
                    repo=str(self.root),
                    task="task-one",
                    input=str(handoff_path),
                    remote="origin",
                )
            )
        self.assertEqual(handoff_code, 0)
        first = json.loads(first_output.getvalue())
        self.assertTrue(first["taskStatus"]["rowChanged"])
        self.assertFalse(first["taskStatus"]["changed"])
        self.assertIsNone(first["taskStatus"]["markdown"])

        collect_output = io.StringIO()
        with redirect_stdout(collect_output):
            collect_code = cli.cmd_collect(
                SimpleNamespace(
                    repo=str(self.root),
                    task="task-one",
                    session_id="session-one",
                    status_repo="",
                )
            )
        self.assertEqual(collect_code, 0)
        collected = json.loads(collect_output.getvalue())
        self.assertFalse(collected["taskStatus"]["rowChanged"])
        self.assertTrue(collected["taskStatus"]["changed"])
        self.assertIsNotNone(collected["taskStatus"]["markdown"])

        repeated_output = io.StringIO()
        with redirect_stdout(repeated_output):
            self.assertEqual(
                cli.cmd_collect(
                    SimpleNamespace(
                        repo=str(self.root),
                        task="task-one",
                        session_id="session-one",
                        status_repo="",
                    )
                ),
                0,
            )
        repeated = json.loads(repeated_output.getvalue())
        self.assertFalse(repeated["taskStatus"]["changed"])
        self.assertIsNone(repeated["taskStatus"]["markdown"])

    def test_watch_emits_markdown_once_for_each_observed_transition(self):
        saved_state = cli.watch_mod.state
        try:
            cli.watch_mod.state = lambda task, sha, pushed_at=None: {
                "state": "running",
                "sha": sha,
                "validation": "queued",
                "reason": "validation is queued",
            }
            args = SimpleNamespace(
                repo=str(self.root),
                task="task-one",
                sha=SHA,
                pushed_at=0.0,
                status_repo="",
            )
            first_output = io.StringIO()
            with redirect_stdout(first_output):
                self.assertEqual(cli.cmd_watch(args), 1)
            first = json.loads(first_output.getvalue())
            self.assertTrue(first["taskStatus"]["changed"])

            second_output = io.StringIO()
            with redirect_stdout(second_output):
                self.assertEqual(cli.cmd_watch(args), 1)
            second = json.loads(second_output.getvalue())
            self.assertFalse(second["taskStatus"]["changed"])
            self.assertIsNone(second["taskStatus"]["markdown"])

            cli.watch_mod.state = lambda task, sha, pushed_at=None: {
                "state": "terminal",
                "sha": sha,
                "validation": "passing",
                "reason": "validation is passing; re-read every signal",
            }
            third_output = io.StringIO()
            with redirect_stdout(third_output):
                self.assertEqual(cli.cmd_watch(args), 0)
            third = json.loads(third_output.getvalue())
            self.assertEqual(
                third["taskStatus"]["changedRows"][0]["status"],
                "awaiting agentic review",
            )
        finally:
            cli.watch_mod.state = saved_state

    def test_publish_and_record_drive_validation_and_review_waiting(self):
        handoff_path = self.root / "publish-handoff.json"
        handoff_path.write_text(
            json.dumps(
                {
                    "work_item": "task-one",
                    "state": "ready_to_publish",
                    "base_sha": "b" * 40,
                    "commit_sha": SHA,
                    "gate_receipt": "receipt",
                    "submission_id": "210976",
                }
            )
        )
        saved_publish = cli.publish_mod.publish
        try:
            cli.publish_mod.publish = lambda *args, **kwargs: {
                "ok": True,
                "commit": SHA,
                "state": "published",
            }
            publish_output = io.StringIO()
            with redirect_stdout(publish_output):
                publish_code = cli.cmd_publish(
                    SimpleNamespace(
                        repo=str(self.root),
                        task="task-one",
                        handoff=str(handoff_path),
                        remote="origin",
                        branch="main",
                        run_id="test",
                        apply=True,
                        rebase=False,
                        allow_review_status="",
                        no_remote_lease=True,
                    )
                )
        finally:
            cli.publish_mod.publish = saved_publish
        self.assertEqual(publish_code, 0)
        published = json.loads(publish_output.getvalue())
        self.assertEqual(
            published["taskStatus"]["changedRows"][0]["status"], "validating"
        )

        task_dir = self.root / "task-one"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text("[metadata]\n")
        record_output = io.StringIO()
        with redirect_stdout(record_output):
            record_code = cli.cmd_record(
                SimpleNamespace(
                    repo=str(self.root),
                    task="task-one",
                    input="",
                    sha=SHA,
                    cls="infra",
                    fix="platform retry",
                    explain="",
                    hardening=False,
                    mode="harden",
                    open_finding="",
                    close_finding="",
                    status="awaiting-review",
                    oracle_failing=False,
                )
            )
        self.assertEqual(record_code, 0)
        recorded = json.loads(record_output.getvalue())
        self.assertEqual(
            recorded["taskStatus"]["changedRows"][0]["status"],
            "awaiting human review",
        )

    def test_status_backfills_old_fleet_metadata_once(self):
        fleet_dir = self.root / ".benchsmith" / "fleet"
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "current.json").write_text(
            json.dumps(
                {
                    "plans": [
                        {
                            "task": "legacy-task",
                            "repo": str(self.root),
                            "mode": "harden",
                            "session": "legacy-session",
                        }
                    ]
                }
            )
        )
        saved_collect = cli.dispatch_mod.collect
        saved_health = cli.dispatch_mod.health
        saved_fetch = cli.sources.fetch_codimango
        try:
            cli.dispatch_mod.collect = lambda *args, **kwargs: {
                "state": "running",
                "reason": "still working",
            }
            cli.dispatch_mod.health = lambda *args, **kwargs: {
                "state": "working",
                "reason": "last activity 0 minutes ago",
                "idleSeconds": 0,
                "errorEvents": 0,
                "overRuntime": False,
            }
            cli.sources.fetch_codimango = lambda: (
                [
                    {
                        "name": "legacy-task",
                        "id": "303",
                        "validationCommitSha": SHA,
                        "validationStatus": "pending",
                    }
                ],
                [],
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    cli.cmd_status(SimpleNamespace(repo=str(self.root))), 0
                )
        finally:
            cli.dispatch_mod.collect = saved_collect
            cli.dispatch_mod.health = saved_health
            cli.sources.fetch_codimango = saved_fetch
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["workers"][0]["state"], "running")
        row = status.read(self.root)["rows"]["legacy-task"]
        self.assertEqual(row["submissionId"], "303")
        self.assertEqual(row["sha"], SHA)
        self.assertEqual(row["status"], "revision: hardening")

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
