from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchsmith import cli
from benchsmith import dispatch


class DispatchPlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name).resolve()
        (self.repo / "sample-task" ).mkdir()
        (self.repo / "sample-task" / "task.toml").write_text("[metadata]\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_agentcloud_plan_pins_host_and_exact_checkout(self) -> None:
        plan = dispatch.plan("sample-task", str(self.repo), bootstrap=True)

        self.assertEqual(plan.argv[0:2], ["agentcloudctl", "create"])
        self.assertEqual(plan.argv[plan.argv.index("--node-id") + 1], dispatch.HOST)
        self.assertEqual(plan.argv[plan.argv.index("--workspace") + 1], str(self.repo))
        prompt = plan.argv[plan.argv.index("--prompt") + 1]
        self.assertIn(str(self.repo), prompt)
        self.assertIn("Do not clone Benchsmith", prompt)
        self.assertNotIn("clone benchsmith themselves", prompt.lower())

    def test_worker_prompt_threads_resolved_publication_branch(self) -> None:
        with patch(
            "benchsmith.gate.resolve_publication_target",
            return_value={
                "ok": True,
                "configured": True,
                "remote": "origin",
                "branch": "master",
            },
        ):
            plan = dispatch.plan("sample-task", str(self.repo), bootstrap=False)

        prompt = plan.argv[plan.argv.index("--prompt") + 1]
        self.assertIn("gate --remote origin --branch master --json", prompt)
        self.assertIn("publication-evidence --remote origin --branch master", prompt)
        self.assertIn("handoff --repo", prompt)
        self.assertIn("--branch master", prompt)

    def test_review_plan_allows_isolated_read_only_checkout(self) -> None:
        review_repo = self.repo / ".benchsmith-review-checkouts" / "repo"
        review_repo.mkdir(parents=True)
        plan = dispatch.plan(
            "review-task",
            str(review_repo),
            mode="review",
            bootstrap=True,
            idea={"track": "t-bench"},
        )

        self.assertEqual(
            plan.argv[plan.argv.index("--workspace") + 1], str(review_repo.resolve())
        )
        prompt = plan.argv[plan.argv.index("--prompt") + 1]
        self.assertIn("behavioral read-only review contract", prompt)
        self.assertIn("repository is READ-ONLY", prompt)

    def test_local_codex_plan_has_no_host_binding(self) -> None:
        plan = dispatch.plan("sample-task", str(self.repo), backend="codex")

        self.assertEqual(plan.argv[0:2], ["codex", "exec"])
        self.assertNotIn("--node-id", plan.argv)
        self.assertNotIn("This work runs only", plan.argv[-1])

    def test_standalone_dispatch_apply_is_refused(self) -> None:
        args = SimpleNamespace(
            task="sample-task",
            repo=str(self.repo),
            backend="agentcloud",
            harness="",
            skills="",
            mode="harden",
            target="hard-preferred",
            bootstrap=False,
            card="",
            apply=True,
        )
        output = io.StringIO()
        with (
            patch.object(
                dispatch,
                "run",
                side_effect=AssertionError("standalone dispatch must not create a worker"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_dispatch(args), 2)
        self.assertIn("fleet --workers 1 --apply", output.getvalue())

    def test_missing_or_malformed_assignment_refuses_handoff(self) -> None:
        document = {"state": "no_change", "session": "session-one"}
        with self.assertRaisesRegex(dispatch.DispatchRefused, "assignment is missing"):
            dispatch.finalize_handoff(self.repo, "sample-task", document)

        path = dispatch.assignment_path(self.repo, "sample-task")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n")
        with self.assertRaisesRegex(dispatch.DispatchRefused, "assignment is unreadable"):
            dispatch.finalize_handoff(self.repo, "sample-task", document)

    def test_stale_worker_cannot_inherit_current_assignment(self) -> None:
        dispatch.write_assignment(
            self.repo,
            "sample-task",
            "new-session",
            "new-lease",
            controller_fingerprint="sha256:controller",
        )
        with self.assertRaisesRegex(dispatch.DispatchRefused, "session does not match"):
            dispatch.finalize_handoff(
                self.repo,
                "sample-task",
                {"state": "no_change", "session": "old-session"},
            )

    def test_handoff_rejects_controller_fingerprint_drift(self) -> None:
        dispatch.write_assignment(
            self.repo,
            "sample-task",
            "session-one",
            "",
            controller_fingerprint="sha256:old",
        )
        with patch.object(
            dispatch.safety,
            "snapshot",
            return_value={"digest": "sha256:new", "clean": True},
        ):
            with self.assertRaisesRegex(
                dispatch.DispatchRefused, "implementation changed"
            ):
                dispatch.finalize_handoff(
                    self.repo,
                    "sample-task",
                    {
                        "state": "no_change",
                        "session": "session-one",
                    },
                )

    def test_awaiting_validation_requires_full_sha(self) -> None:
        with self.assertRaisesRegex(
            dispatch.CandidateHandoffRefused, "requires a full commit_sha"
        ):
            dispatch.parse_handoff(
                '{"state":"awaiting_validation","commit_sha":"abc"}'
            )


if __name__ == "__main__":
    unittest.main()
