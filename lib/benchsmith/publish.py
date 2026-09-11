"""Stage 4: one push at a time, per repository, crash-safely.

Workers prepare in parallel; exactly one may publish. That is not throughput
squeamishness -- the platform validates the branch tip, so two loops pushing to
one repository invalidate each other's evidence, and the second push silently
turns the first one's measurement into somebody else's.

The hard part is not the lock, it is crashing while holding it. So the intent to
push is written down BEFORE the push happens. On restart, a recorded intent plus
the actual remote head answers the only question that matters -- did it land? --
and answering it is what makes a retry safe rather than a second push.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

LANE_TTL = 3600

LANDED = "landed"
NOT_LANDED = "not-landed"
DIVERGED = "diverged"
UNKNOWN = "unknown"


class PublishRefused(Exception):
    """Not safe to push. The message is the reason."""


def _git(repo: Path, *args, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


@dataclass
class Intent:
    task: str
    base_sha: str
    commit_sha: str
    remote: str
    branch: str
    key: str
    at: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Lane:
    """One publication lane per repository, not per task."""

    def __init__(self, repo_root: Path, run_id: str = "", ttl: int = LANE_TTL):
        self.root = Path(repo_root) / ".benchsmith"
        self.lock = self.root / "publish.lane"
        self.intent_path = self.root / "publish.intent.json"
        self.run_id = run_id or "local"
        self.ttl = ttl

    def _owner(self, task: str) -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{task}"

    def holder(self) -> dict | None:
        try:
            doc = json.loads(self.lock.read_text())
        except (OSError, ValueError):
            return None
        if time.time() - float(doc.get("at") or 0) > self.ttl:
            return None  # expired; reapable
        return doc

    def acquire(self, task: str) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        held = self.holder()
        if held:
            return held.get("task") == task  # re-entrant for the same task only
        # An expired lock file still exists on disk; clear it before claiming so
        # O_EXCL means what it says.
        if self.lock.exists():
            try:
                self.lock.unlink()
            except OSError:
                return False
        try:
            fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            json.dump({"task": task, "owner": self._owner(task), "at": time.time()}, f)
        return True

    def release(self, task: str) -> None:
        held = self.holder()
        if held and held.get("task") != task:
            return  # never release somebody else's lane
        self.lock.unlink(missing_ok=True)

    # --- crash safety ---

    def key(self, task: str, commit_sha: str) -> str:
        """Stable across restarts, so a retry is recognisably the same attempt."""
        return f"benchsmith:{self.run_id}:publish:{task}:{commit_sha}"

    def record_intent(self, intent: Intent) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.intent_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(intent.as_dict(), indent=1))
        os.replace(tmp, self.intent_path)  # atomic: a half-written intent is unreadable

    def pending(self) -> Intent | None:
        try:
            return Intent(**json.loads(self.intent_path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def clear_intent(self) -> None:
        self.intent_path.unlink(missing_ok=True)

    def reconcile(self, repo_root: Path, *, git=_git) -> dict:
        """Did the pending push land? The only safe question after a crash."""
        intent = self.pending()
        if intent is None:
            return {"state": "clean", "detail": "no pending push"}
        r = git(Path(repo_root), "ls-remote", intent.remote, intent.branch)
        if r.returncode != 0:
            # Not knowing is not the same as not landed. Pushing again here is
            # how a crash becomes a double push.
            return {"state": UNKNOWN, "intent": intent.as_dict(),
                    "detail": f"cannot read the remote: {r.stderr.strip()[:160]}"}
        head = (r.stdout.split() or [""])[0]
        if head == intent.commit_sha:
            return {"state": LANDED, "intent": intent.as_dict(),
                    "detail": "the push landed; clear the intent and record the round"}
        if head == intent.base_sha:
            return {"state": NOT_LANDED, "intent": intent.as_dict(),
                    "detail": "the remote is still at our base; the push can be retried"}
        return {"state": DIVERGED, "intent": intent.as_dict(),
                "detail": f"the remote moved to {head[:8]}; someone else published. Rebase and "
                          "re-gate before retrying — the prepared commit's evidence is stale"}


def publish(repo_root: Path, task: str, handoff: dict, *, remote: str = "origin",
            branch: str = "main", lane: Lane | None = None, apply: bool = False,
            git=_git, check_review: bool = True) -> dict:
    """Verify, claim the lane, record the intent, then push exactly once."""
    repo_root = Path(repo_root)
    lane = lane or Lane(repo_root)

    if str(handoff.get("state")) != "ready_to_publish":
        raise PublishRefused(f"handoff state is {handoff.get('state')!r}, not ready_to_publish")
    commit_sha = str(handoff.get("commit_sha") or "")
    base_sha = str(handoff.get("base_sha") or "")
    if not commit_sha:
        raise PublishRefused("no commit_sha; nothing to publish")
    if not handoff.get("gate_receipt"):
        # The receipt is the evidence the gate ran on this exact tree. Publishing
        # without it means the lane's one job -- only gated work reaches the
        # remote -- was never actually done.
        raise PublishRefused("no gate_receipt; an ungated commit may not be published")

    # Pushing to a task under review changes what the reviewer is looking at,
    # and their findings then cite a revision that no longer exists. Refused on
    # a positive read; noted, not blocked, when the platform cannot be reached,
    # because a deadlock on every push while offline is worse than the risk.
    review_note = ""
    if check_review:
        try:
            from .resolve import Unresolved, resolve as _resolve

            info = _resolve(task)
            if info.get("awaitingReview"):
                raise PublishRefused(
                    f"{task} is {info.get('awaitingReason')}. Pushing now changes what the "
                    "reviewer is looking at. Wait for their verdict; if they asked for changes "
                    "the status becomes needs_revision and the loop resumes on its own."
                )
        except PublishRefused:
            raise
        except Exception as e:  # noqa: BLE001
            review_note = f"could not confirm review status: {type(e).__name__}"

    pend = lane.reconcile(repo_root, git=git)
    if pend["state"] in (LANDED, DIVERGED, UNKNOWN):
        raise PublishRefused(f"resolve the pending push first ({pend['state']}): {pend['detail']}")

    if not lane.acquire(task):
        held = lane.holder() or {}
        raise PublishRefused(
            f"the publication lane for this repository is held by {held.get('task', 'another task')}; "
            "one publisher per repository is what keeps exact-head evidence valid"
        )

    try:
        r = git(repo_root, "ls-remote", remote, branch)
        if r.returncode != 0:
            raise PublishRefused(f"cannot read the remote: {r.stderr.strip()[:160]}")
        head = (r.stdout.split() or [""])[0]
        if base_sha and head and head != base_sha:
            raise PublishRefused(
                f"the remote is at {head[:8]} but this commit was prepared on {base_sha[:8]}; "
                "rebase and re-gate — pushing now would measure a tree nobody gated"
            )

        intent = Intent(task=task, base_sha=base_sha or head, commit_sha=commit_sha,
                        remote=remote, branch=branch,
                        key=lane.key(task, commit_sha), at=time.time())
        if not apply:
            return {"planned": intent.as_dict(), "applied": False,
                    "reviewNote": review_note or None,
                    "hint": "re-run with apply=True to actually push"}

        # Written before the push, so a crash between here and the next line is
        # recoverable rather than ambiguous.
        lane.record_intent(intent)
        pr = git(repo_root, "push", remote, f"{commit_sha}:refs/heads/{branch}", timeout=600)
        if pr.returncode != 0:
            return {"ok": False, "task": task, "key": intent.key,
                    "error": pr.stderr.strip()[:400],
                    "hint": "the intent is recorded; run reconcile before retrying"}
        lane.clear_intent()
        return {"ok": True, "task": task, "key": intent.key, "commit": commit_sha,
                "remote": remote, "branch": branch}
    finally:
        lane.release(task)
