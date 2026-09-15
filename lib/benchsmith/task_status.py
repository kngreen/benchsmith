"""Durable, Markdown-friendly state for a Benchsmith fleet.

The JSON document is the source of truth and the current Markdown file is its projection.
Each mutable file is atomically replaced under one exclusive lock, and every semantic revision
also publishes an immutable Markdown snapshot. Observing the same state twice does not churn the
row's ``updatedAt`` value or ask a coordinator to repost the table.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 1
STATE_DIR = Path(".benchsmith") / "fleet"
STATE_FILE = "task-status.json"
MARKDOWN_FILE = "task-status.md"
HISTORY_DIR = "history"
LOCK_FILE = ".task-status.lock"
ANNOUNCED_FILE = ".task-status.announced"

SUBMISSION_URL = "https://codimango.internalmeta.com/submissions/{id}"
SESSION_URL = "https://agentcloud.internalmeta.com/{sid}"

NEXT_IN_QUEUE = "next in queue"
REVISION_HARDENING = "revision: hardening"
REVISION_DOCUMENTATION = "revision: documentation"
REVISION_REVIEW = "revision: review findings"
READY_TO_PUBLISH = "ready to publish"
READY_GREEN = "ready to submit / green"
VALIDATING = "validating"
AWAITING_AGENTIC_REVIEW = "awaiting agentic review"
AWAITING_HUMAN_REVIEW = "awaiting human review"
BLOCKED_INFRA = "blocked: infra"
BLOCKED_HUMAN = "blocked: needs human"
BLOCKED_WORKER = "blocked: worker failed"

_HIDDEN_TABLE_STATUSES = frozenset(
    {
        "terminal: accepted",
        "terminal: training",
        "platform: being reviewed",
        "awaiting reviewers",
        "held: preserved original",
        "intake: killed",
        "no change",
        "worker: finished",
    }
)

ROW_FIELDS = (
    "task",
    "submissionId",
    "submissionUrl",
    "status",
    "workerSession",
    "workerUrl",
    "sha",
    "validation",
    "review",
    "evidenceUrl",
    "evidenceLabel",
)
_EVIDENCE_FIELDS = (
    "status",
    "sha",
    "validation",
    "review",
    "evidenceUrl",
    "evidenceLabel",
)
_STATE_FIELDS = ROW_FIELDS + ("statusSource", "statusSha")
_SOURCE_PRIORITY = {
    "routing": 10,
    "worker": 20,
    "publish": 30,
    "platform": 40,
    "journal": 40,
    "manual": 50,
}

_STATUS_ORDER = {
    BLOCKED_INFRA: 10,
    BLOCKED_HUMAN: 11,
    BLOCKED_WORKER: 12,
    READY_GREEN: 20,
    READY_TO_PUBLISH: 21,
    AWAITING_HUMAN_REVIEW: 30,
    AWAITING_AGENTIC_REVIEW: 31,
    VALIDATING: 40,
    REVISION_REVIEW: 50,
    REVISION_DOCUMENTATION: 51,
    REVISION_HARDENING: 52,
    NEXT_IN_QUEUE: 90,
}

_INFRA = re.compile(
    r"\b(infra(?:structure)?|platform|network|offline|timeout|timed out|auth|credential|"
    r"lease|worker host|not imported|orphaned|unreadable)\b",
    re.IGNORECASE,
)
_DOCUMENTATION = re.compile(
    r"\b(documentation|document|docs?|readme|prose|metadata|wording)\b",
    re.IGNORECASE,
)
_PASSING_VALIDATION = frozenset({"completed", "passed", "passing"})
_INFRA_CLASSES = frozenset({"infra", "not-measured", "platform-stale"})


class StatusTableError(RuntimeError):
    """The durable status document could not be read or written safely."""


def _timestamp(now: datetime | float | int | str | None = None) -> str:
    if isinstance(now, str):
        return now
    if isinstance(now, (float, int)):
        current = datetime.fromtimestamp(now, timezone.utc)
    elif isinstance(now, datetime):
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    else:
        current = datetime.now(timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_common_root(repo: Path) -> Path:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return repo
    if result.returncode != 0 or not result.stdout.strip():
        return repo
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common.parent if common.name == ".git" else repo


def status_root(
    repo: str | Path,
    *,
    task: str = "",
    status_repo: str | Path | None = None,
) -> Path:
    """Resolve every worktree for one fleet back to the coordinator's store."""
    if status_repo:
        return Path(status_repo).expanduser().resolve()
    root = Path(repo).expanduser().resolve()
    if task:
        assignment = root / ".benchsmith" / "assignments" / f"{task}.json"
        try:
            recorded = json.loads(assignment.read_text(encoding="utf-8")).get(
                "status_repo"
            )
        except (OSError, ValueError, TypeError):
            recorded = None
        if recorded:
            return Path(recorded).expanduser().resolve()
    return _git_common_root(root)


def paths(
    repo: str | Path,
    *,
    task: str = "",
    status_repo: str | Path | None = None,
) -> tuple[Path, Path, Path]:
    directory = status_root(repo, task=task, status_repo=status_repo) / STATE_DIR
    return directory / STATE_FILE, directory / MARKDOWN_FILE, directory / LOCK_FILE


def _snapshot_path(state_path: Path, revision: int) -> Path:
    return state_path.parent / HISTORY_DIR / f"task-status-r{revision}.md"


def snapshot_path(
    repo: str | Path,
    revision: int,
    *,
    task: str = "",
    status_repo: str | Path | None = None,
) -> Path:
    state_path, _, _ = paths(repo, task=task, status_repo=status_repo)
    return _snapshot_path(state_path, revision)


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_snapshot(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise StatusTableError(f"cannot read revision snapshot {path}: {error}") from error
    else:
        if existing != data:
            raise StatusTableError(
                f"revision snapshot {path} already exists with different content"
            )
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as error:
                raise StatusTableError(
                    f"cannot read revision snapshot {path}: {error}"
                ) from error
            if existing != data:
                raise StatusTableError(
                    f"revision snapshot {path} already exists with different content"
                )
            return
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _empty() -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "revision": 0, "rows": {}}


def _load(path: Path) -> dict:
    if not path.exists():
        return _empty()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StatusTableError(f"cannot read {path}: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != SCHEMA_VERSION
    ):
        raise StatusTableError(
            f"{path} has unsupported schema {document.get('schemaVersion') if isinstance(document, dict) else None!r}"
        )
    if not isinstance(document.get("rows"), dict):
        raise StatusTableError(f"{path} has no row map")
    try:
        document["revision"] = int(document.get("revision") or 0)
    except (TypeError, ValueError) as error:
        raise StatusTableError(f"{path} has an invalid revision") from error
    return document


def read(
    repo: str | Path,
    *,
    task: str = "",
    status_repo: str | Path | None = None,
) -> dict:
    state_path, _, lock_path = paths(repo, task=task, status_repo=status_repo)
    with _locked(lock_path):
        return _load(state_path)


def _semantic(row: dict) -> dict:
    return {field: str(row.get(field) or "") for field in _STATE_FIELDS}


def _evidence_source(row: dict) -> str:
    source = str(row.get("statusSource") or "")
    if source in _SOURCE_PRIORITY:
        return source
    status = str(row.get("status") or "")
    sha = str(row.get("statusSha") or row.get("sha") or "")
    validation = str(row.get("validation") or "").lower()
    if sha and (
        validation in _PASSING_VALIDATION
        or status
        in {
            AWAITING_AGENTIC_REVIEW,
            AWAITING_HUMAN_REVIEW,
            READY_GREEN,
        }
        or status.startswith("terminal:")
        or status == "revision: validation findings"
    ):
        return "platform"
    if status == VALIDATING:
        return "publish"
    if status == READY_TO_PUBLISH:
        return "worker"
    return "routing"


def _accept_evidence(old: dict, patch: dict) -> tuple[bool, bool, str, str]:
    """Decide whether a patch may replace the row's SHA-bound status evidence."""
    incoming_source = str(patch.get("statusSource") or "manual")
    old_source = _evidence_source(old)
    old_sha = str(old.get("statusSha") or old.get("sha") or "")
    incoming_sha = (
        str(patch.get("sha") or "") if "sha" in patch else old_sha
    )
    has_old_evidence = any(str(old.get(field) or "") for field in _EVIDENCE_FIELDS)
    if not has_old_evidence:
        return True, False, incoming_source, incoming_sha

    advances_candidate = bool(
        patch.get("_advancesCandidate")
        and old_sha
        and incoming_sha
        and incoming_sha != old_sha
    )
    if old_sha and incoming_sha and incoming_sha != old_sha:
        return advances_candidate, advances_candidate, incoming_source, incoming_sha
    accepted = _SOURCE_PRIORITY.get(incoming_source, 0) >= _SOURCE_PRIORITY.get(
        old_source, 0
    )
    return accepted, False, incoming_source, incoming_sha


def _normalise(old: dict, patch: dict) -> dict:
    task = str(patch.get("task") or old.get("task") or "").strip()
    if not task:
        raise StatusTableError("a status row needs a task name")
    merged = {field: str(old.get(field) or "") for field in _STATE_FIELDS}
    merged["task"] = task
    touches_evidence = any(field in patch for field in _EVIDENCE_FIELDS)
    accepted, advances_candidate, incoming_source, incoming_sha = _accept_evidence(
        old, patch
    )
    if advances_candidate:
        for field in _EVIDENCE_FIELDS:
            merged[field] = ""
    for field in ROW_FIELDS:
        if field == "task" or field not in patch or patch[field] is None:
            continue
        if field in _EVIDENCE_FIELDS and not accepted:
            continue
        merged[field] = str(patch[field]).strip()
    if touches_evidence and accepted:
        merged["statusSource"] = incoming_source
        merged["statusSha"] = incoming_sha

    if "submissionId" in patch and "submissionUrl" not in patch:
        submission_id = merged["submissionId"]
        merged["submissionUrl"] = (
            SUBMISSION_URL.format(id=quote(submission_id, safe=""))
            if submission_id
            else ""
        )
    if "workerSession" in patch and "workerUrl" not in patch:
        session = merged["workerSession"]
        merged["workerUrl"] = (
            SESSION_URL.format(sid=quote(session, safe="")) if session else ""
        )
    return merged


def _cell(value: object) -> str:
    text = str(value or "").replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r\n", "<br>").replace("\n", "<br>") or "—"


def _link(label: str, url: str) -> str:
    if not url:
        return _cell(label)
    safe_label = _cell(label).replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_label}]({url})"


def render(document: dict) -> str:
    rows = [
        row
        for row in (document.get("rows") or {}).values()
        if str(row.get("status") or "") not in _HIDDEN_TABLE_STATUSES
    ]
    rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(str(row.get("status") or ""), 70),
            str(row.get("task") or ""),
        )
    )
    if not rows:
        return "_No Benchsmith task status has been recorded._\n"

    lines = [
        "| Task | Status | Worker | Validation | Review | Handoff / evidence | Updated (UTC) |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        task = _link(str(row.get("task") or ""), str(row.get("submissionUrl") or ""))
        session = str(row.get("workerSession") or "")
        worker = (
            _link(f"session {session[:8]}", str(row.get("workerUrl") or ""))
            if session
            else "—"
        )
        evidence_url = str(row.get("evidenceUrl") or "")
        evidence_label = str(row.get("evidenceLabel") or "evidence")
        if evidence_url:
            evidence = _link(evidence_label, evidence_url)
        elif row.get("submissionUrl"):
            evidence = _link("submission", str(row["submissionUrl"]))
        elif row.get("workerUrl"):
            evidence = _link("handoff", str(row["workerUrl"]))
        else:
            evidence = "—"
        lines.append(
            "| "
            + " | ".join(
                (
                    task,
                    _cell(row.get("status")),
                    worker,
                    _cell(row.get("validation")),
                    _cell(row.get("review")),
                    evidence,
                    f"`{_cell(row.get('updatedAt'))}`",
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def update_many(
    repo: str | Path,
    patches: list[dict],
    *,
    task: str = "",
    status_repo: str | Path | None = None,
    now: datetime | float | int | str | None = None,
    announce: bool = True,
) -> dict:
    state_path, markdown_path, lock_path = paths(
        repo, task=task, status_repo=status_repo
    )
    announced_path = state_path.parent / ANNOUNCED_FILE
    changed_tasks: list[str] = []
    changed_rows: list[dict] = []
    with _locked(lock_path):
        document = _load(state_path)
        rows = document["rows"]
        stamp = _timestamp(now)
        for patch in patches:
            name = str(patch.get("task") or "").strip()
            old = rows.get(name) or {}
            merged = _normalise(old, patch)
            if _semantic(old) == _semantic(merged):
                continue
            merged["updatedAt"] = stamp
            rows[name] = merged
            if name not in changed_tasks:
                changed_tasks.append(name)
            changed_rows.append(merged.copy())

        table_markdown = None
        if changed_tasks:
            document["schemaVersion"] = SCHEMA_VERSION
            document["revision"] = int(document.get("revision") or 0) + 1
            table_markdown = render(document)
            revision_snapshot_path = _snapshot_path(
                state_path, document["revision"]
            )
            # Publish the immutable snapshot before the mutable pointers. A watcher
            # cannot observe the new JSON revision until its exact Markdown exists;
            # any interrupted retry that disagrees with it fails closed.
            _write_snapshot(revision_snapshot_path, table_markdown)
            _atomic_write(markdown_path, table_markdown)
            _atomic_write(
                state_path, json.dumps(document, indent=2, sort_keys=True) + "\n"
            )
        else:
            revision_snapshot_path = _snapshot_path(
                state_path, document["revision"]
            )

        should_announce = False
        if announce and rows:
            try:
                announced_revision = int(announced_path.read_text().strip())
            except (OSError, ValueError):
                announced_revision = -1
            should_announce = announced_revision != document["revision"]
            if should_announce:
                table_markdown = table_markdown or render(document)
                _atomic_write(announced_path, f"{document['revision']}\n")

    return {
        # `changed` is the caller contract: post `markdown` exactly when true.
        # `rowChanged` distinguishes a worker-side durable write from a later
        # coordinator call that announces that already-written revision.
        "changed": should_announce,
        "rowChanged": bool(changed_tasks),
        "revision": document["revision"],
        "changedTasks": changed_tasks,
        "changedRows": changed_rows,
        "statePath": str(state_path),
        "markdownPath": str(markdown_path),
        "snapshotPath": (
            str(revision_snapshot_path) if revision_snapshot_path.is_file() else None
        ),
        "announcementPath": str(announced_path),
        "markdown": table_markdown if should_announce else None,
    }


def update(
    repo: str | Path,
    patch: dict,
    *,
    task: str = "",
    status_repo: str | Path | None = None,
    now: datetime | float | int | str | None = None,
    announce: bool = True,
) -> dict:
    return update_many(
        repo,
        [patch],
        task=task or str(patch.get("task") or ""),
        status_repo=status_repo,
        now=now,
        announce=announce,
    )


def revision_status(mode: str, detail: str = "") -> str:
    if _DOCUMENTATION.search(detail or "") or mode == "documentation":
        return REVISION_DOCUMENTATION
    if mode in {"repair", "revise"}:
        return REVISION_REVIEW
    if mode == "scaffold":
        return "revision: authoring"
    return REVISION_HARDENING


def blocked_status(detail: str = "", *, human: bool = False) -> str:
    if human:
        return BLOCKED_HUMAN
    return BLOCKED_INFRA if _INFRA.search(detail or "") else "blocked"


def review_summary(reviews: list[dict] | None) -> str:
    parts = []
    for row in reviews or []:
        name = str(row.get("name") or "review")
        state = str(row.get("state") or "unknown")
        verdict = str(row.get("verdict") or "")
        parts.append(f"{name}: {state}" + (f"/{verdict}" if verdict else ""))
    return "; ".join(parts)


def evidence_url(document: dict | None) -> str:
    document = document or {}
    for key in ("evidence_url", "evidenceUrl", "handoff_url", "handoffUrl"):
        value = document.get(key)
        if isinstance(value, str) and value.startswith(
            ("https://", "http://", "?dock=")
        ):
            return value
    evidence = document.get("evidence")
    if isinstance(evidence, dict):
        for key in ("url", "evidenceUrl", "artifactUrl"):
            value = evidence.get(key)
            if isinstance(value, str) and value.startswith(
                ("https://", "http://", "?dock=")
            ):
                return value
    return ""


def transition_patch(
    event: str,
    task: str,
    *,
    mode: str = "",
    state: str = "",
    journal_status: str = "",
    class_name: str = "",
    detail: str = "",
    submission_id: str | None = None,
    session: str | None = None,
    sha: str | None = None,
    validation: str | None = None,
    review: str | None = None,
    evidence: str | None = None,
    status_source: str = "",
    ok: bool | None = None,
    orphaned: bool = False,
    needs_regate: bool = False,
) -> dict:
    """Map one coordinator event onto the stable, operator-facing vocabulary."""
    patch: dict = {"task": task}
    if submission_id is not None:
        patch["submissionId"] = submission_id
    if session is not None:
        patch["workerSession"] = session
    if sha is not None:
        patch["sha"] = sha
    if validation is not None:
        patch["validation"] = validation
    if review is not None:
        patch["review"] = review
    if evidence:
        patch.update(evidenceUrl=evidence, evidenceLabel="evidence")

    state = str(state or "")
    validation_text = str(validation or "").lower()
    detail_text = str(detail or "")

    patch["statusSource"] = status_source or {
        "queued": "routing",
        "worker-start": "worker",
        "handoff": "worker",
        "collect": "worker",
        "publish": "publish",
        "watch": "platform",
        "record": "journal",
    }.get(event, "")
    if event == "worker-start":
        patch["_advancesCandidate"] = True
    elif event in {"handoff", "collect"} and state in {
        "ready_to_publish",
        "awaiting_validation",
    }:
        patch["_advancesCandidate"] = True
    elif event == "publish" and (ok or state == "rebased"):
        patch["_advancesCandidate"] = True

    if event == "queued":
        patch.update(
            status=NEXT_IN_QUEUE, workerSession="", evidenceUrl="", evidenceLabel=""
        )
    elif event == "worker-start":
        patch.update(
            status=revision_status(mode, detail_text),
            evidenceUrl="",
            evidenceLabel="",
        )
        if mode in {"repair", "revise"} and review is None:
            patch["review"] = "changes requested"
    elif event in {"handoff", "collect"}:
        if state == "ready_to_publish":
            patch["status"] = READY_TO_PUBLISH
        elif state == "awaiting_validation":
            patch.update(status=VALIDATING, validation=validation or "pending")
        elif state == "blocked":
            patch["status"] = blocked_status(detail_text)
        elif state == "needs_human":
            patch["status"] = BLOCKED_HUMAN
        elif state == "failed" or state == "finished-without-handoff":
            patch["status"] = BLOCKED_WORKER
        elif state == "unreadable":
            patch["status"] = BLOCKED_INFRA
        elif state == "no_change":
            patch["status"] = "no change"
        elif state in {"running", "starting", "working"}:
            patch["status"] = revision_status(mode, detail_text)
        else:
            patch["status"] = (
                f"worker: {state}" if state else revision_status(mode, detail_text)
            )
    elif event == "publish":
        lowered = detail_text.lower()
        if state == "rebased":
            patch["status"] = "revision: re-gate" if needs_regate else READY_TO_PUBLISH
        elif ok:
            patch.update(status=VALIDATING, validation=validation or "pending")
        elif "accepted" in lowered or "training" in lowered:
            patch["status"] = "terminal: accepted"
        elif "reviewer" in lowered or "review hold" in lowered:
            patch.update(status=AWAITING_HUMAN_REVIEW, review="human review pending")
        elif "remote moved" in lowered or "rebase" in lowered:
            patch["status"] = "revision: rebase"
        elif state == "unknown":
            patch["status"] = BLOCKED_INFRA
        else:
            patch["status"] = blocked_status(detail_text)
    elif event == "watch":
        if state == "absent" and orphaned:
            patch.update(status=BLOCKED_INFRA, validation="not imported")
        elif state == "absent":
            patch.update(status=VALIDATING, validation="not imported")
        elif state == "running":
            patch.update(status=VALIDATING, validation=validation or "running")
        elif state == "terminal" and validation_text in _PASSING_VALIDATION:
            patch.update(
                status=AWAITING_AGENTIC_REVIEW,
                review=review or "agentic review pending",
            )
        elif state == "terminal":
            patch.update(status="revision: validation findings", review="")
        elif state == "unknown":
            patch["status"] = BLOCKED_INFRA
        else:
            patch["status"] = f"validation: {state}" if state else VALIDATING
    elif event == "record":
        if journal_status == "converged":
            patch.update(status=READY_GREEN, validation=validation or "green")
        elif journal_status == "awaiting-review":
            patch.update(
                status=AWAITING_HUMAN_REVIEW, review=review or "human review pending"
            )
        elif journal_status == "blocked-on-platform":
            patch["status"] = BLOCKED_INFRA
        elif journal_status in {"blocked", "escalated"}:
            patch["status"] = (
                BLOCKED_HUMAN
                if journal_status == "escalated"
                else blocked_status(detail_text)
            )
        elif journal_status == "abandoned":
            patch["status"] = "terminal: rejected"
        elif journal_status in {"accepted", "used_in_training"}:
            patch["status"] = "terminal: accepted"
        elif class_name in _INFRA_CLASSES:
            patch["status"] = BLOCKED_INFRA
        else:
            patch["status"] = revision_status(mode, detail_text)
    else:
        raise StatusTableError(f"unknown task-status event {event!r}")
    return patch


def transition(
    repo: str | Path,
    event: str,
    task: str,
    *,
    status_repo: str | Path | None = None,
    now: datetime | float | int | str | None = None,
    announce: bool = True,
    **fields,
) -> dict:
    patch = transition_patch(event, task, **fields)
    return update(
        repo,
        patch,
        task=task,
        status_repo=status_repo,
        now=now,
        announce=announce,
    )
