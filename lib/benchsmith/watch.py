"""Has the wave and its real exact-SHA platform signals reached a terminal state?"""

from __future__ import annotations

import json
import subprocess
import time

from .identifiers import (
    VALIDATION_PASSING_STATES as PASSING_VALIDATION,
    VALIDATION_PENDING_STATES as PENDING_STATES,
    VALIDATION_TERMINAL_STATES as TERMINAL_STATES,
)

ABSENT, RUNNING, TERMINAL, UNKNOWN = "absent", "running", "terminal", "unknown"
RETRY_AFTER_SECONDS = 5 * 60
EXTERNAL_SIGNAL_HOLD_SECONDS = 45 * 60
REVIEW_TERMINAL_STATES = frozenset({
    "accept", "accepted", "cancelled", "canceled", "completed", "error", "fail", "failed",
    "good", "pass", "reject", "rejected", "request changes", "request_changes",
})


def _read(task: str, binary: str = "codimango") -> tuple[dict, list[dict] | None, str]:
    """Read the task plus real job rows carrying Agentic review evidence."""
    from .adapter import Identity, Platform, discover

    try:
        surface = discover(binary)
        argv = [binary, *surface.site, *surface.task_show, task, "--json"]
        if surface.supports_no_cache:
            argv.append("--no-cache")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except Exception as error:  # noqa: BLE001
        return {}, None, f"{type(error).__name__}: {error}"
    if result.returncode != 0 or "{" not in result.stdout:
        return {}, None, (result.stderr or "no JSON").strip()[:160]
    try:
        document = json.loads(result.stdout[result.stdout.index("{"):])
    except ValueError as error:
        return {}, None, str(error)
    record = document.get("task") or document
    if not isinstance(record, dict):
        return {}, None, "task read returned no task object"
    task_id = str(record.get("id") or record.get("taskId") or "")
    task_uuid = str(record.get("uuid") or record.get("taskUuid") or "")
    if not (task_id or task_uuid):
        return record, None, "task read has no platform identity for the job query"
    try:
        platform = Platform(
            surface,
            Identity(task_name=task, task_id=task_id, task_uuid=task_uuid),
        )
        jobs = platform.jobs()
    except Exception as error:  # noqa: BLE001
        return record, None, f"job read failed: {type(error).__name__}: {error}"
    return record, jobs, ""


def _job_stage(job: dict) -> str:
    from .snapshot import job_stage

    return job_stage(job)


def _agentic_signal(jobs: list[dict] | None, sha: str, problem: str) -> dict:
    if jobs is None:
        return {"state": UNKNOWN, "detail": problem or "Agentic job rows are unavailable"}
    agentic = [job for job in jobs if _job_stage(job) == "agentic-review"]
    exact = [
        job
        for job in agentic
        if str((job.get("config") or {}).get("commitSha") or "") == sha
    ]
    if not exact:
        stale = sorted({
            str((job.get("config") or {}).get("commitSha") or "")[:12]
            for job in agentic
            if (job.get("config") or {}).get("commitSha")
        })
        return {
            "state": UNKNOWN,
            "detail": (
                "no Agentic review job exists for the exact SHA"
                + (f"; stale rows: {', '.join(stale[:4])}" if stale else "")
            ),
        }
    job = exact[-1]
    status = str(job.get("status") or "").lower()
    review = job.get("agenticReview") or {}
    verdict = str(review.get("verdict") or "")
    if status in PENDING_STATES:
        return {"state": RUNNING, "status": status, "jobId": str(job.get("id") or "")}
    if status not in REVIEW_TERMINAL_STATES:
        return {"state": UNKNOWN, "detail": f"unrecognised Agentic job status {status!r}"}
    if not verdict:
        return {"state": UNKNOWN, "detail": "terminal Agentic job has no verdict"}
    return {
        "state": TERMINAL,
        "status": status,
        "verdict": verdict,
        "jobId": str(job.get("id") or ""),
        "sha": sha,
    }


def _tbr_signal(record: dict, sha: str) -> dict:
    status = str(record.get("tbdReviewStatus") or "").lower()
    details = record.get("tbdReviewDetails") or {}
    instance = str(details.get("instance_id") or "") if isinstance(details, dict) else ""
    if not status:
        return {"state": UNKNOWN, "detail": "TBR status is missing"}
    if not sha or sha[:7] not in instance:
        return {"state": UNKNOWN, "detail": "TBR status is not bound to the exact SHA"}
    if status in PENDING_STATES:
        return {"state": RUNNING, "status": status, "sha": sha}
    if status not in REVIEW_TERMINAL_STATES:
        return {"state": UNKNOWN, "detail": f"unrecognised TBR status {status!r}"}
    return {"state": TERMINAL, "status": status, "sha": sha}


def _elapsed(pushed_at: float | None) -> float | None:
    return max(0.0, time.time() - pushed_at) if pushed_at else None


def _unknown_signal_result(
    record: dict,
    sha: str,
    validation: str,
    signals: dict,
    *,
    pushed_at: float | None,
    signal_hold: int,
) -> dict:
    waited = _elapsed(pushed_at)
    hold_expired = waited is None or waited >= signal_hold
    details = [
        f"{name}: {value.get('detail') or value.get('status') or value.get('state')}"
        for name, value in signals.items()
        if value.get("state") != TERMINAL
    ]
    return {
        "state": UNKNOWN,
        "sha": sha,
        "validation": validation,
        "platformSignals": signals,
        "waitedSeconds": int(waited) if waited is not None else None,
        "retryable": True,
        "retryAfterSeconds": RETRY_AFTER_SECONDS,
        "signalHoldSeconds": signal_hold,
        "clearEligible": hold_expired,
        "blocksFreshIntake": not hold_expired,
        "statusClear": (
            f"benchsmith status-clear --task {record.get('name') or record.get('id') or '<task>'} "
            f"--sha {sha} --reason 'external exact-SHA signal unavailable' --apply"
        ),
        "reason": "exact-SHA platform signal is unresolved: " + "; ".join(details),
        **_context(record, signals),
    }


def _context(record: dict, signals: dict | None = None) -> dict:
    agentic = (signals or {}).get("agentic") or {}
    review = str(agentic.get("status") or agentic.get("state") or "")
    verdict = str(agentic.get("verdict") or "")
    return {
        "submissionId": str(record.get("id") or record.get("taskId") or ""),
        "review": review + (f"/{verdict}" if verdict else ""),
    }


def classify(
    record: dict,
    sha: str,
    *,
    jobs: list[dict] | None = None,
    jobs_problem: str = "",
    pushed_at: float | None = None,
    orphan_after: int = 45 * 60,
    signal_hold: int = EXTERNAL_SIGNAL_HOLD_SECONDS,
) -> dict:
    """Classify platform state using task validation/TBR and real Agentic job rows."""
    seen = str(record.get("validationCommitSha") or "")
    validation = str(record.get("validationStatus") or "").lower()

    if seen != sha:
        waited = _elapsed(pushed_at)
        orphaned = bool(waited is not None and waited > orphan_after)
        return {
            "state": ABSENT,
            "sha": sha,
            "platformSha": seen or None,
            "waitedSeconds": int(waited) if waited is not None else None,
            "orphaned": orphaned,
            "reason": (
                "the platform has not imported this commit"
                + (f"; {int(waited / 60)}m with no progress, which is orphaned" if orphaned else "")
            ),
            **_context(record),
        }

    if validation in PENDING_STATES:
        return {
            "state": RUNNING,
            "sha": sha,
            "validation": validation,
            "reason": f"validation is {validation}",
            **_context(record),
        }
    if validation in TERMINAL_STATES:
        if validation not in PASSING_VALIDATION:
            return {
                "state": TERMINAL,
                "sha": sha,
                "validation": validation,
                "platformSignals": {"validation": {"state": TERMINAL, "status": validation}},
                "reason": "validation is terminal; re-read every signal",
                **_context(record),
            }
        signals = {
            "tbr": _tbr_signal(record, sha),
            "agentic": _agentic_signal(jobs, sha, jobs_problem),
        }
        nonterminal = [value for value in signals.values() if value.get("state") != TERMINAL]
        if nonterminal:
            if any(value.get("state") == UNKNOWN for value in nonterminal):
                return _unknown_signal_result(
                    record,
                    sha,
                    validation,
                    signals,
                    pushed_at=pushed_at,
                    signal_hold=signal_hold,
                )
            waited = _elapsed(pushed_at)
            if waited is None or waited >= signal_hold:
                return _unknown_signal_result(
                    record,
                    sha,
                    validation,
                    signals,
                    pushed_at=pushed_at,
                    signal_hold=signal_hold,
                )
            return {
                "state": RUNNING,
                "sha": sha,
                "validation": validation,
                "platformSignals": signals,
                "waitedSeconds": int(waited),
                "blocksFreshIntake": True,
                "reason": "validation is terminal; exact-SHA platform reviews are still running",
                **_context(record, signals),
            }
        return {
            "state": TERMINAL,
            "sha": sha,
            "validation": validation,
            "platformSignals": signals,
            "reason": "validation and required exact-SHA platform signals are terminal",
            **_context(record, signals),
        }
    return {
        "state": UNKNOWN,
        "sha": sha,
        "validation": validation or None,
        "retryable": True,
        "retryAfterSeconds": RETRY_AFTER_SECONDS,
        "blocksFreshIntake": False,
        "reason": f"unrecognised validation status {validation!r}; treat as unresolved",
        **_context(record),
    }


def state(
    task: str,
    sha: str,
    *,
    binary: str = "codimango",
    pushed_at: float | None = None,
    orphan_after: int = 45 * 60,
    signal_hold: int = EXTERNAL_SIGNAL_HOLD_SECONDS,
) -> dict:
    """Read and classify the platform state for one exact commit."""
    record, jobs, problem = _read(task, binary)
    if not record:
        return {
            "state": UNKNOWN,
            "sha": sha,
            "retryable": True,
            "retryAfterSeconds": RETRY_AFTER_SECONDS,
            "blocksFreshIntake": False,
            "reason": f"could not read the task: {problem}",
        }
    return classify(
        record,
        sha,
        jobs=jobs,
        jobs_problem=problem,
        pushed_at=pushed_at,
        orphan_after=orphan_after,
        signal_hold=signal_hold,
    )
