"""Has the wave for this exact SHA landed yet?

Codimango is the largest wall-clock cost in the loop, and a worker that sits
through a wave holds a slot for tens of minutes to hours while learning nothing
it could not read on arrival. The coordinator can ask the same question for the
price of one uncached read, and start a fresh worker when the answer changes.

The states are kept apart on purpose. "Not imported yet", "running", "terminal"
and "I could not tell" each imply a different next action, and collapsing any of
them into another is how a loop either waits forever or acts on nothing.
"""

from __future__ import annotations

import json
import subprocess
import time

ABSENT, RUNNING, TERMINAL, UNKNOWN = "absent", "running", "terminal", "unknown"

TERMINAL_STATES = {"completed", "failed", "passing", "passed", "error", "cancelled", "canceled"}
PENDING_STATES = {"pending", "running", "queued", "in_progress", "validating"}


def _read(task: str, binary: str = "codimango") -> tuple[dict, str]:
    from .adapter import discover

    try:
        s = discover(binary)
        argv = [binary, *s.site, *s.task_show, task, "--json"]
        if s.supports_no_cache:
            argv.append("--no-cache")
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return {}, f"{type(e).__name__}: {e}"
    if r.returncode != 0 or "{" not in r.stdout:
        return {}, (r.stderr or "no JSON").strip()[:160]
    try:
        doc = json.loads(r.stdout[r.stdout.index("{"):])
    except ValueError as e:
        return {}, str(e)
    return (doc.get("task") or doc), ""


def _context(record: dict) -> dict:
    review = record.get("agenticReviewStatus") or record.get("agenticReview") or ""
    if isinstance(review, dict):
        state = str(review.get("state") or review.get("status") or "")
        verdict = str(review.get("verdict") or "")
        review = state + (f"/{verdict}" if verdict else "")
    return {
        "submissionId": str(record.get("id") or ""),
        "review": str(review or ""),
    }


def state(task: str, sha: str, *, binary: str = "codimango", pushed_at: float | None = None,
          orphan_after: int = 45 * 60) -> dict:
    """Where the platform is with this exact commit."""
    rec, why = _read(task, binary)
    if why:
        # Not knowing is its own state. Treating it as "still running" waits
        # forever; treating it as terminal acts on evidence that does not exist.
        return {"state": UNKNOWN, "sha": sha, "reason": f"could not read the task: {why}"}

    context = _context(rec)
    seen = str(rec.get("validationCommitSha") or "")
    validation = str(rec.get("validationStatus") or "").lower()

    if seen != sha:
        waited = (time.time() - pushed_at) if pushed_at else None
        orphaned = bool(waited and waited > orphan_after)
        return {"state": ABSENT, "sha": sha, "platformSha": seen or None,
                "waitedSeconds": int(waited) if waited else None, "orphaned": orphaned,
                "reason": ("the platform has not imported this commit"
                           + (f"; {int(waited / 60)}m with no progress, which is orphaned"
                              if orphaned else "")), **context}

    if validation in PENDING_STATES:
        return {"state": RUNNING, "sha": sha, "validation": validation,
                "reason": f"validation is {validation}", **context}
    if validation in TERMINAL_STATES:
        return {"state": TERMINAL, "sha": sha, "validation": validation,
                "reason": f"validation is {validation}; re-read every signal", **context}
    return {"state": UNKNOWN, "sha": sha, "validation": validation or None,
            "reason": f"unrecognised validation status {validation!r}; treat as unresolved",
            **context}
