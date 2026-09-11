"""Stage 2: the read-only planner, and the lease that makes dispatch idempotent.

Read-only by construction. `build_queue` takes data and returns an ordering; it
opens no sockets and writes no files. Claims are the only thing here that
mutates, and they are separate so a plan can always be produced without taking
anything.

Priority is deterministic, not heuristic: the same inputs must always produce
the same order, or a restarted coordinator will disagree with itself about what
to do next.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

# Lower sorts first. Cheapest evidence first: a reviewer who already said what is
# wrong beats a measurement you have not taken yet.
TIER_REVISION = 10       # a human named the defect
TIER_DRAFT_FAILED = 20   # broken, and the evidence is already on the SHA
TIER_DRAFT_PENDING = 30  # needs a round before anything is knowable
TIER_DRAFT_PASSING = 40  # green but unaccepted; usually a difficulty question
TIER_IDEA = 50           # most expensive: intake, screen, scaffold

TIER_NAMES = {
    TIER_REVISION: "needs_revision",
    TIER_DRAFT_FAILED: "draft/validation-failed",
    TIER_DRAFT_PENDING: "draft/validation-pending",
    TIER_DRAFT_PASSING: "draft/validation-passing",
    TIER_IDEA: "idea",
}

# A journal status that means the worker is finished with this task.
TERMINAL = frozenset({"converged", "abandoned"})
# ...and one that means a human must look before anything else happens.
NEEDS_HUMAN = frozenset({"escalated", "blocked-on-platform", "blocked"})

DEFAULT_LEASE_TTL = 3600


@dataclass
class Item:
    task: str
    tier: int
    reason: str
    status: str = ""
    validation: str = ""
    journal_status: str = ""
    claimed_by: str | None = None
    skip: str = ""

    @property
    def dispatchable(self) -> bool:
        return not self.skip and self.claimed_by is None

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "tier": self.tier,
            "tierName": TIER_NAMES.get(self.tier, str(self.tier)),
            "reason": self.reason,
            "status": self.status,
            "validation": self.validation,
            "journalStatus": self.journal_status,
            "claimedBy": self.claimed_by,
            "skip": self.skip,
            "dispatchable": self.dispatchable,
        }


def _tier(status: str, validation: str) -> tuple[int, str] | None:
    if status == "needs_revision":
        return (TIER_REVISION, "a reviewer named the defect")
    if status == "draft":
        if validation == "failed":
            return (TIER_DRAFT_FAILED, "validation failed; evidence is on the SHA")
        if validation in ("pending", "", None):
            return (TIER_DRAFT_PENDING, "no measurement yet")
        return (TIER_DRAFT_PASSING, "green but unaccepted")
    return None


def read_journals(repo_root: Path) -> dict[str, str]:
    """Map task -> journal status. Unreadable is not absent."""
    out: dict[str, str] = {}
    d = Path(repo_root) / ".benchsmith"
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".receipt.json"):
            continue
        try:
            doc = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            # A journal that will not parse is not a task with no journal: the
            # counters behind it are unknown, so it must not look dispatchable.
            out[p.stem] = "unreadable"
            continue
        if isinstance(doc, dict) and doc.get("task"):
            out[str(doc["task"])] = str(doc.get("status") or "running")
    return out


def build_queue(tasks: list[dict], journals: dict[str, str] | None = None,
                leases: dict[str, str] | None = None, ideas: list[dict] | None = None) -> list[Item]:
    """Deterministic prioritised backlog. Pure: no IO, no clock, no network."""
    journals, leases = journals or {}, leases or {}
    items: list[Item] = []

    for t in tasks:
        name = str(t.get("name") or t.get("id") or "")
        if not name:
            continue
        status = str(t.get("status") or "")
        validation = str(t.get("validationStatus") or "")
        tiered = _tier(status, validation)
        if tiered is None:
            continue
        tier, reason = tiered
        jstatus = journals.get(name, "")
        item = Item(task=name, tier=tier, reason=reason, status=status,
                    validation=validation, journal_status=jstatus,
                    claimed_by=leases.get(name))
        if jstatus in TERMINAL:
            item.skip = f"journal says {jstatus}"
        elif jstatus in NEEDS_HUMAN:
            item.skip = f"journal says {jstatus}: needs a human before redispatch"
        elif jstatus == "unreadable":
            item.skip = "journal will not parse; counters unknown"
        items.append(item)

    for idea in ideas or []:
        name = str(idea.get("name") or idea.get("id") or "")
        if not name:
            continue
        # Deduplicate against a linked Codimango task before dispatching.
        if any(i.task == name for i in items) or name in journals:
            continue
        items.append(Item(task=name, tier=TIER_IDEA, reason="unscaffolded idea",
                          claimed_by=leases.get(name)))

    # Stable and total: tier, then name. Never insertion order -- a coordinator
    # that restarts must compute the identical plan.
    return sorted(items, key=lambda i: (i.tier, i.task))


# --- leases -----------------------------------------------------------------


@dataclass
class Leases:
    """One lease file per task. Creation is O_EXCL, so exactly one claimant wins."""

    root: Path
    ttl: int = DEFAULT_LEASE_TTL
    _now: object = field(default=time.time)

    def dir(self) -> Path:
        d = Path(self.root) / ".benchsmith" / "leases"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, task: str) -> Path:
        return self.dir() / f"{task}.lease"

    def owner(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}"

    def active(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self.dir().glob("*.lease"):
            try:
                doc = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if float(doc.get("expires", 0)) > self._now():
                out[p.stem] = str(doc.get("owner") or "?")
            else:
                # An expired lease is not a claim. Reap it so a crashed worker
                # does not park a task forever.
                p.unlink(missing_ok=True)
        return out

    def claim(self, task: str) -> dict:
        self.active()  # reap first
        p = self._path(task)
        body = json.dumps({"task": task, "owner": self.owner(),
                           "claimed": self._now(), "expires": self._now() + self.ttl})
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = json.loads(p.read_text())
            return {"ok": False, "task": task, "heldBy": existing.get("owner"),
                    "reason": "already claimed"}
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return {"ok": True, "task": task, "owner": self.owner()}

    def release(self, task: str) -> dict:
        p = self._path(task)
        if not p.is_file():
            return {"ok": False, "task": task, "reason": "not claimed"}
        p.unlink()
        return {"ok": True, "task": task}
