from __future__ import annotations

import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchsmith import cli
from benchsmith import controller
from benchsmith import task_status


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.repo = root / "repo"
        self.status = root / "status"
        self.repo.mkdir()
        self.status.mkdir()

    def acquire(self, session: str = "session-a", now: float = 100.0) -> dict:
        return controller.acquire(
            self.repo,
            self.status,
            session,
            ttl_seconds=120,
            now=now,
            epoch_factory=lambda: "epoch-a",
        )

    def test_same_owner_must_renew_with_the_epoch(self) -> None:
        first = self.acquire()
        with self.assertRaisesRegex(controller.ControllerRefused, "use renew"):
            controller.acquire(
                self.repo,
                self.status,
                "session-a",
                ttl_seconds=120,
                now=110,
                epoch_factory=lambda: "must-not-replace",
            )
        renewed = controller.renew(
            self.repo,
            self.status,
            "session-a",
            "epoch-a",
            ttl_seconds=120,
            now=120,
        )

        self.assertFalse(first["reused"])
        self.assertEqual(renewed["epoch"], "epoch-a")
        self.assertEqual(renewed["expiresAt"], 240)

    def test_live_foreign_owner_is_not_replaced(self) -> None:
        self.acquire()
        with self.assertRaisesRegex(controller.ControllerRefused, "owned by live"):
            controller.acquire(
                self.repo,
                self.status,
                "session-b",
                now=110,
                inspector=lambda _owner: self.fail("live lease needs no inspect"),
            )

    def test_expired_run_open_or_unreadable_owner_is_not_reaped(self) -> None:
        self.acquire()
        cases = (
            ("session-b", True, "still run-open"),
            ("session-b", None, "could not be inspected"),
            ("session-a", True, "still run-open"),
        )
        for session, state, message in cases:
            with self.subTest(session=session, state=state):
                with self.assertRaisesRegex(controller.ControllerRefused, message):
                    controller.acquire(
                        self.repo,
                        self.status,
                        session,
                        now=221,
                        inspector=lambda _owner, state=state: state,
                    )

    def test_expired_explicitly_terminal_owner_gets_new_epoch(self) -> None:
        self.acquire()
        result = controller.acquire(
            self.repo,
            self.status,
            "session-b",
            now=221,
            inspector=lambda _owner: False,
            epoch_factory=lambda: "epoch-b",
        )

        self.assertEqual(result["sessionId"], "session-b")
        self.assertEqual(result["epoch"], "epoch-b")
        with self.assertRaises(controller.ControllerRefused):
            controller.verify(
                self.repo, self.status, "session-a", "epoch-a", now=222
            )

    def test_expired_takeover_resamples_time_after_inspection(self) -> None:
        self.acquire()
        with patch.object(controller.time, "time", side_effect=[221.0, 400.0]):
            result = controller.acquire(
                self.repo,
                self.status,
                "session-b",
                inspector=lambda _owner: False,
                epoch_factory=lambda: "epoch-b",
                ttl_seconds=120,
            )
        self.assertEqual(result["acquiredAt"], 400)
        self.assertEqual(result["expiresAt"], 520)

    def test_wrong_identity_is_rejected_for_every_mutating_verb(self) -> None:
        self.acquire()
        calls = (
            lambda: controller.verify(
                self.repo, self.status, "session-a", "wrong", now=101
            ),
            lambda: controller.renew(
                self.repo, self.status, "session-b", "epoch-a", now=101
            ),
            lambda: controller.release(
                self.repo, self.status, "session-a", "wrong", now=101
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(controller.ControllerRefused):
                    call()

    def test_exact_epoch_can_renew_and_release_after_expiry(self) -> None:
        self.acquire()
        renewed = controller.renew(
            self.repo,
            self.status,
            "session-a",
            "epoch-a",
            ttl_seconds=120,
            now=221,
        )
        self.assertEqual(renewed["expiresAt"], 341)
        released = controller.release(
            self.repo, self.status, "session-a", "epoch-a", now=500
        )
        self.assertTrue(released["released"])

    def test_post_commit_sync_failures_are_indeterminate(self) -> None:
        state_path, _ = controller.paths(self.status)
        real_open = controller.os.open

        def fail_parent_open(path, *args, **kwargs):
            if Path(path) == state_path.parent:
                raise OSError("directory open failed")
            return real_open(path, *args, **kwargs)

        with patch.object(controller.os, "open", side_effect=fail_parent_open):
            with self.assertRaises(controller.ControllerIndeterminate):
                self.acquire()
        self.assertTrue(state_path.exists())

        state_path.unlink()
        self.acquire()
        with patch.object(controller.os, "open", side_effect=fail_parent_open):
            with self.assertRaises(controller.ControllerIndeterminate):
                controller.release(
                    self.repo, self.status, "session-a", "epoch-a", now=101
                )
        self.assertFalse(state_path.exists())

    def test_canonical_repo_and_status_root_are_fenced(self) -> None:
        self.acquire()
        other_repo = self.repo.parent / "other-repo"
        other_repo.mkdir()
        with self.assertRaisesRegex(controller.ControllerRefused, "canonicalRepo"):
            controller.verify(
                other_repo, self.status, "session-a", "epoch-a", now=101
            )

    def test_write_fence_blocks_epoch_takeover_until_mutation_finishes(self) -> None:
        self.acquire()
        entered = threading.Event()
        finished = threading.Event()
        result: dict[str, object] = {}

        def takeover() -> None:
            entered.set()
            result.update(
                controller.acquire(
                    self.repo,
                    self.status,
                    "session-b",
                    now=221,
                    inspector=lambda _owner: False,
                    epoch_factory=lambda: "epoch-b",
                )
            )
            finished.set()

        with patch.dict(os.environ, {"USER": "alice"}):
            with controller.write_fence(
                self.repo,
                self.status,
                "session-a",
                "epoch-a",
                now=101,
                hold_reader=lambda _repo: {
                    "readable": True,
                    "held": True,
                    "holder": "alice",
                },
            ):
                thread = threading.Thread(target=takeover)
                thread.start()
                self.assertTrue(entered.wait(1))
                time.sleep(0.02)
                self.assertFalse(finished.is_set())

        thread.join(1)
        self.assertTrue(finished.is_set())
        self.assertEqual(result["epoch"], "epoch-b")

    def test_admission_is_epoch_hold_disk_then_real_container_start(self) -> None:
        self.acquire()
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.dict(os.environ, {"USER": "alice"}):
            result = controller.admit(
                self.repo,
                self.status,
                "session-a",
                "epoch-a",
                canary_image="sha256:canary",
                min_free_gb=25,
                now=101,
                hold_reader=lambda _repo: {
                    "readable": True,
                    "held": True,
                    "holder": "alice",
                    "sha": "hold-token",
                },
                disk_usage=lambda _path: SimpleNamespace(free=30 * 1024**3),
                runner=runner,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], ["docker", "image", "inspect", "sha256:canary"])
        self.assertEqual(
            calls[1],
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/bin/true",
                "sha256:canary",
            ],
        )

    def test_unreadable_hold_or_low_disk_refuses_before_container_start(self) -> None:
        self.acquire()
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(controller.ControllerRefused, "offline"):
            controller.admit(
                self.repo,
                self.status,
                "session-a",
                "epoch-a",
                canary_image="canary",
                now=101,
                hold_reader=lambda _repo: {
                    "readable": False,
                    "reason": "offline",
                },
                runner=runner,
            )
        self.assertEqual(calls, [])

        with patch.dict(os.environ, {"USER": "alice"}):
            with self.assertRaisesRegex(controller.ControllerRefused, "GiB free"):
                controller.admit(
                    self.repo,
                    self.status,
                    "session-a",
                    "epoch-a",
                    canary_image="canary",
                    min_free_gb=25,
                    now=101,
                    hold_reader=lambda _repo: {
                        "readable": True,
                        "held": True,
                        "holder": "alice",
                    },
                    disk_usage=lambda _path: SimpleNamespace(free=24 * 1024**3),
                    runner=runner,
                )
        self.assertEqual(calls, [])

    def test_container_inspect_or_start_failure_is_blocking(self) -> None:
        self.acquire()

        def hold_reader(_repo):
            return {"readable": True, "held": True, "holder": "alice"}

        with patch.dict(os.environ, {"USER": "alice"}):
            with self.assertRaisesRegex(controller.ControllerRefused, "unavailable"):
                controller.admit(
                    self.repo,
                    self.status,
                    "session-a",
                    "epoch-a",
                    canary_image="missing",
                    now=101,
                    hold_reader=hold_reader,
                    disk_usage=lambda _path: SimpleNamespace(free=30 * 1024**3),
                    runner=lambda argv, **_kwargs: subprocess.CompletedProcess(
                        argv, 1, "", "no such image"
                    ),
                )

            attempts = 0

            def start_fails(argv, **_kwargs):
                nonlocal attempts
                attempts += 1
                return subprocess.CompletedProcess(
                    argv,
                    0 if attempts == 1 else 125,
                    "",
                    "" if attempts == 1 else "runc start failed",
                )

            with self.assertRaisesRegex(controller.ControllerRefused, "start canary"):
                controller.admit(
                    self.repo,
                    self.status,
                    "session-a",
                    "epoch-a",
                    canary_image="present",
                    now=101,
                    hold_reader=hold_reader,
                    disk_usage=lambda _path: SimpleNamespace(free=30 * 1024**3),
                    runner=start_fails,
                )
            self.assertEqual(attempts, 2)

    @staticmethod
    def fleet_args(repo: Path, status_repo: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            repo=str(repo),
            status_repo=status_repo,
            session_id="session-a",
            controller_epoch="epoch-a",
            container_canary_image="canary",
            min_free_gb=25.0,
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

    def test_fleet_active_status_owner_blocks_redispatch_without_lease(self) -> None:
        task = "task-owned"
        (self.repo / task).mkdir()
        task_status.update(
            self.repo,
            {
                "task": task,
                "status": task_status.REVISION_HARDENING,
                "workerSession": "existing-session",
            },
            status_repo=self.status,
        )
        args = self.fleet_args(self.repo, str(self.status))
        row = {
            "name": task,
            "id": "105",
            "status": "draft",
            "validationStatus": "failed",
            "validationCommitSha": "e" * 40,
        }
        output = io.StringIO()
        with (
            patch.object(cli.controller_mod, "admit", return_value={"ok": True}),
            patch.object(
                cli.controller_mod,
                "write_fence",
                return_value=nullcontext({"ok": True}),
            ),
            patch.object(
                cli.sources, "discover", return_value={"tasks": [row], "notes": []}
            ),
            patch.object(
                cli.dispatch_mod,
                "run",
                side_effect=AssertionError("owned task must not dispatch"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_fleet(args), 0)

        payload = __import__("json").loads(output.getvalue())
        self.assertEqual(payload["dispatchable"], 0)
        self.assertEqual(payload["plans"], [])
        stored = task_status.read(self.repo, status_repo=self.status)["rows"][task]
        self.assertEqual(stored["workerSession"], "existing-session")

    def test_fleet_requires_canonical_status_root_before_discovery(self) -> None:
        args = self.fleet_args(self.repo)
        with patch.object(
            cli.sources,
            "discover",
            side_effect=AssertionError("discovery must not run"),
        ):
            with self.assertRaisesRegex(controller.ControllerRefused, "status-repo"):
                cli.cmd_fleet(args)

    def test_fleet_admission_precedes_discovery_and_persists_canonically(self) -> None:
        args = self.fleet_args(self.repo, str(self.status))
        order: list[str] = []

        def admit(*_args, **_kwargs):
            order.append("admit")
            return {"ok": True}

        @contextmanager
        def fence(*_args, **_kwargs):
            order.append("fence")
            yield {"ok": True}

        def discover(**_kwargs):
            order.append("discover")
            return {"tasks": [], "notes": []}

        output = io.StringIO()
        with (
            patch.object(cli.controller_mod, "admit", side_effect=admit),
            patch.object(cli.controller_mod, "write_fence", side_effect=fence),
            patch.object(cli.sources, "discover", side_effect=discover),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_fleet(args), 0)

        self.assertEqual(order[0:3], ["admit", "fence", "discover"])
        payload = __import__("json").loads(output.getvalue())
        self.assertEqual(payload["admission"], {"ok": True})
        self.assertTrue(
            str(Path(payload["taskStatus"]["statePath"])).startswith(
                str(self.status)
            )
        )

    def test_fleet_holds_epoch_fence_through_dispatch_and_status_write(self) -> None:
        task = "task-fenced"
        (self.repo / task).mkdir()
        (self.repo / task / "task.toml").write_text("[metadata]\n")
        args = self.fleet_args(self.repo, str(self.status))
        inside_fence = False
        dispatches: list[str] = []

        @contextmanager
        def fence(*_args, **_kwargs):
            nonlocal inside_fence
            inside_fence = True
            try:
                yield {"ok": True}
            finally:
                inside_fence = False

        def dispatch(_plan, apply):
            self.assertTrue(apply)
            self.assertTrue(inside_fence)
            dispatches.append(task)
            return {
                "ok": True,
                "stdout": '{"session_id":"new-worker"}',
                "stderr": "",
            }

        task_row = {
            "name": task,
            "id": "101",
            "status": "draft",
            "validationStatus": "failed",
            "validationCommitSha": "a" * 40,
        }
        with (
            patch.object(cli.controller_mod, "admit", return_value={"ok": True}),
            patch.object(cli.controller_mod, "write_fence", side_effect=fence),
            patch.object(
                cli.controller_mod, "verify_target", return_value={"ok": True}
            ),
            patch.object(
                cli.sources,
                "discover",
                return_value={"tasks": [task_row], "notes": []},
            ),
            patch.object(
                cli.resolve_mod,
                "resolve",
                return_value={
                    "task": task,
                    "id": "101",
                    "repo": str(self.repo),
                    "mode": "harden",
                    "sha": "a" * 40,
                    "validation": "failed",
                },
            ),
            patch.object(
                cli.passatk_mod,
                "capability",
                return_value={"applicable": False, "ready": True},
            ),
            patch.object(cli.dispatch_mod, "run", side_effect=dispatch),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_fleet(args), 0)

        self.assertFalse(inside_fence)
        self.assertEqual(dispatches, [task])
        rows = __import__("json").loads(
            (
                self.status
                / ".benchsmith"
                / "fleet"
                / "task-status.json"
            ).read_text()
        )["rows"]
        self.assertEqual(rows[task]["workerSession"], "new-worker")
    def test_dispatch_exception_releases_unbound_lease_and_worktree(self) -> None:
        task = "task-cleanup"
        (self.repo / task).mkdir()
        (self.repo / task / "task.toml").write_text("[metadata]\n")
        worktree = self.repo.parent / "task-cleanup-worktree"
        worktree.mkdir()
        args = self.fleet_args(self.repo, str(self.status))
        args.no_remote_lease = False
        args.shared_tree = False
        released: list[str] = []
        worktrees_released: list[str] = []

        class FakeLease:
            ref = "refs/benchsmith/task-cleanup"
            sha = "lease-sha"

            def __init__(self, task_name, _repo, known=""):
                self.task = task_name

            def acquire(self):
                return {"held": True}

            def release(self):
                released.append(self.task)
                return {"released": True}

        task_row = {
            "name": task,
            "id": "102",
            "status": "draft",
            "validationStatus": "failed",
            "validationCommitSha": "b" * 40,
        }
        with (
            patch.object(cli.controller_mod, "admit", return_value={"ok": True}),
            patch.object(
                cli.controller_mod,
                "write_fence",
                return_value=nullcontext({"ok": True}),
            ),
            patch.object(
                cli.controller_mod, "verify_target", return_value={"ok": True}
            ),
            patch.object(
                cli.sources,
                "discover",
                return_value={"tasks": [task_row], "notes": []},
            ),
            patch.object(
                cli.resolve_mod,
                "resolve",
                return_value={
                    "task": task,
                    "id": "102",
                    "repo": str(self.repo),
                    "mode": "harden",
                    "sha": "b" * 40,
                    "validation": "failed",
                },
            ),
            patch.object(
                cli.passatk_mod,
                "capability",
                return_value={"applicable": False, "ready": True},
            ),
            patch.object(cli.rlease_mod, "states", return_value={}),
            patch.object(cli.rlease_mod, "RemoteLease", FakeLease),
            patch.object(
                cli.wt_mod, "ensure", return_value=SimpleNamespace(path=str(worktree))
            ),
            patch.object(
                cli.wt_mod,
                "release",
                side_effect=lambda _repo, name: worktrees_released.append(name),
            ),
            patch.object(
                cli.dispatch_mod,
                "plan",
                return_value=SimpleNamespace(shell="planned"),
            ),
            patch.object(
                cli.dispatch_mod, "run", side_effect=OSError("create failed")
            ),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(OSError, "create failed"):
                cli.cmd_fleet(args)

        self.assertEqual(released, [task])
        self.assertEqual(worktrees_released, [task])
    def test_secondary_repo_admission_blocks_before_dispatch(self) -> None:
        task = "task-secondary"
        secondary = self.repo.parent / "secondary"
        (secondary / task).mkdir(parents=True)
        (secondary / task / "task.toml").write_text("[metadata]\n")
        args = self.fleet_args(self.repo, str(self.status))
        task_row = {
            "name": task,
            "id": "103",
            "status": "draft",
            "validationStatus": "failed",
            "validationCommitSha": "c" * 40,
        }
        with (
            patch.object(cli.controller_mod, "admit", return_value={"ok": True}),
            patch.object(
                cli.controller_mod,
                "write_fence",
                return_value=nullcontext({"ok": True}),
            ),
            patch.object(
                cli.controller_mod,
                "verify_target",
                side_effect=controller.ControllerRefused("secondary disk low"),
            ) as target_check,
            patch.object(
                cli.sources,
                "discover",
                return_value={"tasks": [task_row], "notes": []},
            ),
            patch.object(
                cli.resolve_mod,
                "resolve",
                return_value={
                    "task": task,
                    "id": "103",
                    "repo": str(secondary),
                    "mode": "harden",
                    "sha": "c" * 40,
                    "validation": "failed",
                },
            ),
            patch.object(
                cli.passatk_mod,
                "capability",
                return_value={"applicable": False, "ready": True},
            ),
            patch.object(
                cli.dispatch_mod,
                "run",
                side_effect=AssertionError("dispatch must not run"),
            ),
        ):
            with self.assertRaisesRegex(controller.ControllerRefused, "secondary"):
                cli.cmd_fleet(args)
        target_check.assert_called_once_with(str(secondary), min_free_gb=25.0)
    def test_success_without_session_id_retains_lease_and_worktree(self) -> None:
        task = "task-indeterminate"
        (self.repo / task).mkdir()
        args = self.fleet_args(self.repo, str(self.status))
        args.no_remote_lease = False
        args.shared_tree = False
        worktree = self.repo.parent / "task-indeterminate-worktree"
        worktree.mkdir()
        released: list[str] = []
        worktrees_released: list[str] = []

        class FakeLease:
            ref = "refs/benchsmith/task-indeterminate"
            sha = "lease-sha"

            def __init__(self, task_name, _repo, known=""):
                self.task = task_name

            def acquire(self):
                return {"held": True}

            def release(self):
                released.append(self.task)

        row = {
            "name": task,
            "id": "104",
            "status": "draft",
            "validationStatus": "failed",
            "validationCommitSha": "d" * 40,
        }
        with (
            patch.object(cli.controller_mod, "admit", return_value={"ok": True}),
            patch.object(
                cli.controller_mod,
                "write_fence",
                return_value=nullcontext({"ok": True}),
            ),
            patch.object(
                cli.controller_mod, "verify_target", return_value={"ok": True}
            ),
            patch.object(
                cli.sources, "discover", return_value={"tasks": [row], "notes": []}
            ),
            patch.object(
                cli.resolve_mod,
                "resolve",
                return_value={
                    "task": task,
                    "id": "104",
                    "repo": str(self.repo),
                    "mode": "harden",
                    "sha": "d" * 40,
                    "validation": "failed",
                },
            ),
            patch.object(
                cli.passatk_mod,
                "capability",
                return_value={"applicable": False, "ready": True},
            ),
            patch.object(cli.rlease_mod, "states", return_value={}),
            patch.object(cli.rlease_mod, "RemoteLease", FakeLease),
            patch.object(
                cli.wt_mod, "ensure", return_value=SimpleNamespace(path=str(worktree))
            ),
            patch.object(
                cli.wt_mod,
                "release",
                side_effect=lambda _repo, name: worktrees_released.append(name),
            ),
            patch.object(
                cli.dispatch_mod,
                "plan",
                return_value=SimpleNamespace(shell="planned"),
            ),
            patch.object(
                cli.dispatch_mod,
                "run",
                return_value={"ok": True, "stdout": "{}", "stderr": ""},
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_fleet(args), 2)

        self.assertEqual(released, [])
        self.assertEqual(worktrees_released, [])


if __name__ == "__main__":
    unittest.main()
