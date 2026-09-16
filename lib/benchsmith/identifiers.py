"""Shared validation and lifecycle constants for safety-sensitive paths."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
TASK_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

VALIDATION_TERMINAL_STATES = frozenset(
    {"completed", "failed", "passing", "passed", "error", "cancelled", "canceled"}
)
VALIDATION_PENDING_STATES = frozenset(
    {"pending", "running", "queued", "in_progress", "validating"}
)
VALIDATION_PASSING_STATES = frozenset({"completed", "passing", "passed"})


def validate_sha(value: str, label: str = "commit SHA") -> str:
    candidate = str(value or "").strip().lower()
    if not FULL_SHA.fullmatch(candidate):
        raise ValueError(f"{label} must be one full lowercase commit SHA")
    return candidate


def validate_task_path(value: str) -> str:
    """Return one canonical safe relative task path or raise ``ValueError``."""
    task = str(value or "")
    path = PurePosixPath(task)
    if (
        not task
        or task != task.strip()
        or task.startswith("/")
        or task.endswith("/")
        or path.is_absolute()
        or path.as_posix() != task
        or "\\" in task
        or any(not TASK_SEGMENT.fullmatch(segment) for segment in path.parts)
    ):
        raise ValueError(
            f"task path {value!r} must contain only safe relative slug segments"
        )
    return task
