"""Exact-SHA canonical + critic receipts anchored to terminal AgentCloud transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .identifiers import validate_sha, validate_task_path

MARKER = "BENCHSMITH_CRITIC_RECEIPT="
DECISIONS = frozenset({"Accept", "Request changes", "Reject"})
CANONICAL_DECISIONS = frozenset({"Accept"})
VERSION = 2


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def path(repo_root: Path, task: str, sha: str = "") -> Path:
    task = validate_task_path(task)
    repo_key = hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:12]
    root = Path(
        os.environ.get(
            "BENCHSMITH_CRITIC_RECEIPT_DIR",
            Path.home() / ".cache" / "benchsmith" / "critic-receipts",
        )
    )
    if sha:
        sha = validate_sha(sha)
        return root / repo_key / task / f"{sha}.json"
    return root / repo_key / f"{task}.json"


def _event_text(event: dict) -> str:
    value = event.get("event")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if not isinstance(value, dict):
        return ""
    block = value.get("block") or {}
    return str(block.get("text") or block.get("content") or "")


def _extract(events: list[dict]) -> dict | None:
    decoder = json.JSONDecoder()
    for event in reversed(events):
        text = _event_text(event)
        offset = text.rfind(MARKER)
        if offset < 0:
            continue
        try:
            document, _ = decoder.raw_decode(text, offset + len(MARKER))
        except ValueError:
            continue
        if isinstance(document, dict):
            return document
    return None


def _canonical(document: dict) -> tuple[dict | None, list[str]]:
    value = document.get("canonical_review")
    errors: list[str] = []
    if not isinstance(value, dict):
        return None, ["canonical_review is missing; old critic receipts cannot clear publication"]
    name = str(value.get("name") or "")
    decision = str(value.get("decision") or "")
    evidence_digest = str(value.get("evidence_digest") or "")
    if not name:
        errors.append("canonical_review.name is missing")
    if decision not in CANONICAL_DECISIONS:
        errors.append("canonical_review.decision must be Accept")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
        errors.append("canonical_review.evidence_digest must be a SHA-256 hex digest")
    return ({"name": name, "decision": decision, "evidence_digest": evidence_digest}, errors)


def _fetch(session_id: str, poller=None) -> tuple[list[dict] | None, str]:
    from .dispatch import poll_session

    events, problem = poll_session(session_id, runner=poller)
    if problem:
        return None, problem
    if not any(
        str(event.get("type")) in {"run_finished", "session_archived"}
        for event in events
    ):
        return None, "critic session is not terminal"
    return events, ""


def _from_events(
    repo_root: Path,
    task: str,
    sha: str,
    session_id: str,
    events: list[dict],
) -> tuple[dict | None, str]:
    try:
        task = validate_task_path(task)
        sha = validate_sha(sha)
    except ValueError as error:
        return None, str(error)
    supplied = _extract(events)
    if supplied is None:
        return None, f"session contains no {MARKER} block"

    errors: list[str] = []
    canonical, canonical_errors = _canonical(supplied)
    errors.extend(canonical_errors)
    if str(supplied.get("session_id") or "") != session_id:
        errors.append("session_id does not match the fetched session")
    if str(supplied.get("sha") or "") != sha:
        errors.append("receipt SHA does not match the requested SHA")
    if not str(supplied.get("task_id") or ""):
        errors.append("task_id is missing")
    if str(supplied.get("decision") or "") not in DECISIONS:
        errors.append("decision is missing or invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(supplied.get("evidence_digest") or "")):
        errors.append("evidence_digest must be a SHA-256 hex digest")
    if not str(supplied.get("critic_version") or ""):
        errors.append("critic_version is missing")
    try:
        receipt_at = datetime.fromisoformat(str(supplied.get("timestamp") or ""))
        committed = subprocess.run(
            ["git", "-C", str(repo_root), "show", "-s", "--format=%cI", sha],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        commit_at = datetime.fromisoformat(committed.stdout.strip())
        if receipt_at < commit_at:
            errors.append("critic receipt predates the commit")
        now = datetime.now(receipt_at.tzinfo)
        if receipt_at > now + timedelta(minutes=5):
            errors.append("critic receipt timestamp is in the future")
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        errors.append("timestamp or commit binding is unreadable")
    if errors:
        return None, "; ".join(errors)

    transcript_digest = _digest(events)
    return {
        "version": VERSION,
        "task": task,
        "task_id": str(supplied["task_id"]),
        "sha": sha,
        "canonical_review": canonical,
        "critic_version": str(supplied["critic_version"]),
        "session_id": session_id,
        "decision": str(supplied["decision"]),
        "evidence_digest": transcript_digest,
        "reported_evidence_digest": str(supplied["evidence_digest"]),
        "timestamp": str(supplied["timestamp"]),
        "session_evidence_digest": transcript_digest,
        "source": "agentcloud-session-transcript",
    }, ""


def _write(repo_root: Path, task: str, sha: str, body: dict) -> dict:
    task = validate_task_path(task)
    sha = validate_sha(sha)
    body = dict(body)
    body["digest"] = _digest(body)
    destination = path(repo_root, task, sha)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(body, indent=2) + "\n")
    os.replace(temporary, destination)
    return {"ok": True, "receipt": body, "path": str(destination)}


def ingest(
    repo_root: Path, task: str, sha: str, session_id: str, *, poller=None
) -> dict:
    """Fetch the terminal session and cache its validated exact-SHA receipt."""
    events, problem = _fetch(session_id, poller=poller)
    if events is None:
        return {"ok": False, "reason": problem}
    body, problem = _from_events(repo_root, task, sha, session_id, events)
    if body is None:
        return {"ok": False, "reason": problem}
    return _write(repo_root, task, sha, body)


def load(repo_root: Path, task: str, sha: str) -> tuple[dict | None, str]:
    try:
        task = validate_task_path(task)
        sha = validate_sha(sha)
        receipt = path(repo_root, task, sha)
    except ValueError as error:
        return None, str(error)
    if not receipt.is_file():
        legacy = path(repo_root, task)
        if legacy.is_file():
            return None, "critic receipt predates exact-SHA schema v2; rerun and ingest the review"
        return None, "no trusted critic receipt for this exact SHA"
    try:
        body = json.loads(receipt.read_text())
    except (OSError, ValueError) as error:
        return None, f"critic receipt is unreadable: {error}"
    unsigned = {key: value for key, value in body.items() if key != "digest"}
    if body.get("digest") != _digest(unsigned):
        return None, "critic receipt digest does not match its contents"
    if body.get("version") != VERSION:
        return None, "critic receipt predates canonical-review schema v2; rerun and ingest the review"
    if body.get("source") not in {
        "agentcloud-session-transcript", "tree-identical-review-carry"
    }:
        return None, "critic receipt has no trusted session provenance"
    if body.get("task") != task or body.get("sha") != sha:
        return None, "critic receipt is for another task or SHA"
    if body.get("decision") not in DECISIONS:
        return None, "critic receipt decision is invalid"
    if not str(body.get("critic_version") or ""):
        return None, "critic receipt version is missing"
    canonical, errors = _canonical(body)
    if errors or canonical != body.get("canonical_review"):
        return None, "; ".join(errors or ["canonical review receipt is malformed"])
    if body.get("source") == "tree-identical-review-carry":
        if not body.get("derived_from_sha") or not body.get("derived_from_digest"):
            return None, "carried review receipt has no source binding"
        try:
            source_sha = validate_sha(str(body["derived_from_sha"]), "source review SHA")
            tree = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", f"{sha}:{task}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout.strip()
        except (ValueError, OSError, subprocess.SubprocessError):
            return None, "carried review task tree is unreadable"
        if tree != body.get("task_tree"):
            return None, "carried review receipt task tree changed"
        if source_sha == sha:
            return None, "carried review receipt points to itself"
    return body, ""


def verify_live(
    repo_root: Path,
    task: str,
    sha: str,
    cached: dict,
    *,
    poller=None,
) -> tuple[dict | None, str]:
    """Authenticate a cache by re-fetching the terminal transcript it names."""
    if cached.get("source") == "tree-identical-review-carry":
        source_sha = str(cached.get("derived_from_sha") or "")
        source, problem = load(repo_root, task, source_sha)
        if source is None:
            return None, f"carried review source is invalid: {problem}"
        if str(source.get("digest") or "") != str(cached.get("derived_from_digest") or ""):
            return None, "carried review source digest does not match"
        inherited = (
            "version", "task", "task_id", "canonical_review", "critic_version",
            "session_id", "decision", "evidence_digest", "reported_evidence_digest",
            "session_evidence_digest",
        )
        if any(cached.get(field) != source.get(field) for field in inherited):
            return None, "carried review cache changes source review evidence"
        try:
            source_tree = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", f"{source_sha}:{task}"],
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout.strip()
            target_tree = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", f"{sha}:{task}"],
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None, "carried review source or target tree is unreadable"
        if not source_tree or source_tree != target_tree or target_tree != cached.get("task_tree"):
            return None, "carried review task tree is not byte-identical to its source"
        verified, problem = verify_live(repo_root, task, source_sha, source, poller=poller)
        if verified is None:
            return None, problem
        return cached, ""

    session_id = str(cached.get("session_id") or "")
    if not session_id:
        return None, "critic cache has no session_id to authenticate"
    events, problem = _fetch(session_id, poller=poller)
    if events is None:
        return None, f"critic transcript could not be re-fetched: {problem}"
    fresh, problem = _from_events(repo_root, task, sha, session_id, events)
    if fresh is None:
        return None, f"critic transcript no longer validates: {problem}"
    expected = {key: value for key, value in cached.items() if key != "digest"}
    if fresh != expected:
        stable = {
            key: value
            for key, value in fresh.items()
            if key not in {"evidence_digest", "session_evidence_digest"}
        }
        expected_stable = {
            key: value
            for key, value in expected.items()
            if key not in {"evidence_digest", "session_evidence_digest"}
        }
        if stable == expected_stable:
            return (
                None,
                "critic session advanced after receipt ingest; re-ingest receipt and "
                "regenerate the handoff",
            )
        return None, "critic cache does not match the re-fetched terminal session transcript"
    return cached, ""


def carry(repo_root: Path, task: str, source_sha: str, target_sha: str) -> dict:
    """Derive an exact-target receipt only across byte-identical task trees."""
    try:
        task = validate_task_path(task)
        source_sha = validate_sha(source_sha, "source review SHA")
        target_sha = validate_sha(target_sha, "target review SHA")
    except ValueError as error:
        return {"ok": False, "reason": str(error)}
    source, problem = load(repo_root, task, source_sha)
    if source is None:
        return {"ok": False, "reason": problem}
    if source.get("decision") != "Accept":
        return {"ok": False, "reason": "critic did not accept the source candidate"}
    try:
        before = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"{source_sha}:{task}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        after = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"{target_sha}:{task}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "reason": f"review carry task tree is unreadable: {error}"}
    if not before or before != after:
        return {"ok": False, "reason": "reviewed task tree changed; rerun canonical and critic reviews"}
    body = {
        key: value
        for key, value in source.items()
        if key not in {
            "digest", "sha", "timestamp", "source", "derived_from_sha",
            "derived_from_digest", "task_tree",
        }
    }
    body.update(
        version=VERSION,
        sha=target_sha,
        timestamp=datetime.now(timezone.utc).isoformat(),
        source="tree-identical-review-carry",
        derived_from_sha=source_sha,
        derived_from_digest=str(source["digest"]),
        task_tree=after,
    )
    return _write(repo_root, task, target_sha, body)
