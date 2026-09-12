"""Crash-safe compare-and-swap updates for remote Git refs."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


CONFIRMED = "confirmed"
REJECTED = "rejected"
UNKNOWN = "unknown"
DEFAULT_DELAYS = (0, 1, 2, 4, 8, 16)


def _runner(repo: Path, timeout: int):
    return lambda argv: subprocess.run(
        ["git", "-C", str(repo), *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def read(repo: Path, remote: str, ref: str, *, timeout: int = 60, runner=None) -> dict:
    run = runner or _runner(Path(repo), timeout)
    try:
        result = run(["ls-remote", "--heads", remote, ref])
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"readable": False, "sha": "", "detail": f"{type(error).__name__}: {error}"}
    if result.returncode:
        return {
            "readable": False,
            "sha": "",
            "detail": (result.stderr or result.stdout).strip()[:160],
        }
    return {"readable": True, "sha": (result.stdout.split() or [""])[0], "detail": ""}


def update(
    repo: Path,
    remote: str,
    ref: str,
    target: str,
    *,
    expected: str | None = None,
    timeout: int = 60,
    delays=DEFAULT_DELAYS,
    runner=None,
    sleeper=time.sleep,
) -> dict:
    """Push one ref update and reconcile an ambiguous result.

    ``expected=None`` creates a new ref. A string performs a compare-and-swap.
    ``target=""`` deletes the ref. Success is reported only after the push
    returned zero or a subsequent read observed the requested state.
    """
    run = runner or _runner(Path(repo), timeout)
    args = ["push", "-q", "--no-verify"]
    if expected is not None:
        args.append(f"--force-with-lease={ref}:{expected}")
    args += [remote, f"{target}:{ref}"]

    push_error = ""
    try:
        pushed = run(args)
        if pushed.returncode == 0:
            return {"state": CONFIRMED, "attempts": 0, "sha": target, "detail": "push returned zero"}
        push_error = (pushed.stderr or pushed.stdout).strip()[:160]
    except (OSError, subprocess.TimeoutExpired) as error:
        push_error = f"{type(error).__name__}: {error}"

    attempts = 0
    last = ""
    for delay in delays:
        if delay:
            sleeper(delay)
        attempts += 1
        observed = read(Path(repo), remote, ref, timeout=timeout, runner=run)
        if not observed["readable"]:
            last = observed["detail"]
            continue
        sha = observed["sha"]
        if sha == target:
            return {
                "state": CONFIRMED,
                "attempts": attempts,
                "sha": sha,
                "detail": "remote reached the requested state after an ambiguous push",
            }
        if expected is not None and sha not in (expected, target):
            return {
                "state": REJECTED,
                "attempts": attempts,
                "sha": sha,
                "detail": f"remote ref moved to {sha[:12] or 'missing'}",
            }
        if expected is None and sha:
            return {
                "state": REJECTED,
                "attempts": attempts,
                "sha": sha,
                "detail": f"remote ref is held by {sha[:12]}",
            }

    return {
        "state": UNKNOWN,
        "attempts": attempts,
        "sha": "",
        "detail": f"push was ambiguous after {attempts} reconciliation read(s): {last or push_error}",
    }
