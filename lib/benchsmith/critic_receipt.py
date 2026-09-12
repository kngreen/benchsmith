"""Exact-SHA critic receipts ingested from a completed AgentCloud transcript."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

MARKER = "BENCHSMITH_CRITIC_RECEIPT="
DECISIONS = frozenset({"Accept", "Request changes", "Reject"})
VERSION = 1


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def path(repo_root: Path, task: str) -> Path:
    repo_key = hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:12]
    root = Path(
        os.environ.get(
            "BENCHSMITH_CRITIC_RECEIPT_DIR",
            Path.home() / ".cache" / "benchsmith" / "critic-receipts",
        )
    )
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


def ingest(
    repo_root: Path, task: str, sha: str, session_id: str, *, poller=None
) -> dict:
    """Fetch the session ourselves; operator-supplied receipt content is never accepted."""
    from .dispatch import poll_session

    events, problem = poll_session(session_id, runner=poller)
    if problem:
        return {"ok": False, "reason": problem}
    terminal = any(
        str(event.get("type")) in {"run_finished", "session_archived"}
        for event in events
    )
    if not terminal:
        return {"ok": False, "reason": "critic session is not terminal"}
    supplied = _extract(events)
    if supplied is None:
        return {"ok": False, "reason": f"session contains no {MARKER} block"}

    errors = []
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
        return {"ok": False, "reason": "; ".join(errors)}

    transcript_digest = _digest(events)
    body = {
        "version": VERSION,
        "task": task,
        "task_id": str(supplied["task_id"]),
        "sha": sha,
        "critic_version": str(supplied["critic_version"]),
        "session_id": session_id,
        "decision": str(supplied["decision"]),
        "evidence_digest": transcript_digest,
        "reported_evidence_digest": str(supplied["evidence_digest"]),
        "timestamp": str(supplied["timestamp"]),
        "session_evidence_digest": transcript_digest,
        "source": "agentcloud-session-transcript",
    }
    body["digest"] = _digest(body)
    destination = path(repo_root, task)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(body, indent=2) + "\n")
    os.replace(temporary, destination)
    return {"ok": True, "receipt": body, "path": str(destination)}


def load(repo_root: Path, task: str, sha: str) -> tuple[dict | None, str]:
    receipt = path(repo_root, task)
    if not receipt.is_file():
        return None, "no trusted critic receipt"
    try:
        body = json.loads(receipt.read_text())
    except (OSError, ValueError) as error:
        return None, f"critic receipt is unreadable: {error}"
    unsigned = {key: value for key, value in body.items() if key != "digest"}
    if body.get("digest") != _digest(unsigned):
        return None, "critic receipt digest does not match its contents"
    if body.get("source") != "agentcloud-session-transcript":
        return None, "critic receipt has no trusted session provenance"
    if body.get("task") != task or body.get("sha") != sha:
        return None, "critic receipt is for another task or SHA"
    if body.get("decision") not in DECISIONS:
        return None, "critic receipt decision is invalid"
    return body, ""
