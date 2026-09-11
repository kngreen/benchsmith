"""Re-measure the same commit, when the measurement failed rather than the task.

A third of rounds are not the task's fault. The wrong response to one is another
edit: it changes the tree, so whatever the infrastructure was hiding is now
hidden behind a different tree as well, and the round that would have told you
something is gone.

The right response is to measure the same commit again. The dangerous version of
that is rerolling until the numbers look better, so the rule is narrow: a rerun
is authorised only by an infrastructure-shaped classification, never by a
disappointing but valid result.
"""

from __future__ import annotations

import json
import subprocess

# Classifications that mean the platform failed, not the task.
INFRA = frozenset({"infra", "not-measured", "platform-stale"})

# Classifications that mean the measurement was real. A rerun here is rerolling
# the dice, and the numbers it produces are not evidence of anything.
NEVER = frozenset({"in-band", "too-easy", "dominant-blocker", "grader-false-negative",
                   "contract", "contract-disagreement", "suspect-golden", "revert"})


class RerunRefused(Exception):
    """Not an authorised rerun. The message says why."""


def authorised(last_class: str, *, budget_left: int = 1) -> tuple[bool, str]:
    cls = (last_class or "").strip()
    if not cls:
        return False, "no recorded classification for the last round; classify it first"
    if cls in NEVER:
        return False, (f"the last round was classified {cls!r}, which is a real measurement. "
                       "Re-running it is rerolling for a better sample, not repairing evidence")
    if cls not in INFRA:
        return False, f"{cls!r} is not an infrastructure classification; no rerun is authorised"
    if budget_left <= 0:
        return False, "the rerun budget for this commit is spent; escalate at blocked-on-platform"
    return True, f"last round was {cls}: the platform failed, not the task"


# A wave that has not progressed in this long is orphaned, not slow. Waiting on
# one indefinitely is how a loop spends a day producing nothing.
ORPHAN_SECONDS = 45 * 60


def orphaned(pending_seconds: float | None, *, local_defect: bool = False) -> tuple[bool, str]:
    """Should a stuck wave be replaced with a fresh one?

    Not when there is a deterministic local defect outstanding: re-running the
    same commit cannot clear one, so the rerun would burn a wave and return the
    identical failure. Fix the local thing first.
    """
    if local_defect:
        return False, ("a deterministic local defect is outstanding; re-running the same commit "
                       "cannot clear it. Repair that first, then push")
    if pending_seconds is None:
        return False, "no pending wave"
    if pending_seconds < ORPHAN_SECONDS:
        return False, (f"the wave has been pending {int(pending_seconds / 60)} minutes; "
                       f"orphaned is {int(ORPHAN_SECONDS / 60)}")
    return True, (f"the wave has made no progress for {int(pending_seconds / 60)} minutes; "
                  "request one fresh wave rather than waiting indefinitely")


def trigger(task: str, *, binary: str = "codimango", apply: bool = False) -> dict:
    """Ask the platform to validate the same commit again."""
    argv = [binary, "api", "tasks", "rerun", task, "--json"]
    if not apply:
        return {"planned": argv, "applied": False,
                "hint": "re-run with apply=True to actually trigger it"}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "task": task, "error": f"{type(e).__name__}: {e}"}
    body = r.stdout[r.stdout.find("{"):] if "{" in r.stdout else ""
    doc = {}
    if body:
        try:
            doc = json.loads(body)
        except ValueError:
            doc = {}
    # The server joins an in-flight revalidation rather than starting a second.
    # That is a success, and reporting it as a failure would prompt an edit.
    joined = "already running" in (r.stdout + r.stderr)
    return {"ok": r.returncode == 0, "task": task, "joined": joined,
            "detail": doc or (r.stdout or r.stderr).strip()[:200]}
