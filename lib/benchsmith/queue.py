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

from .resolve import publication_eligibility, work_eligibility
from .watch import PENDING_STATES, TERMINAL_STATES

# Lower sorts first. Cheapest evidence first: a reviewer who already said what is
# wrong beats a measurement you have not taken yet.
TIER_REVISION = 10       # a human named the defect
TIER_DRAFT_FAILED = 20   # broken, and the evidence is already on the SHA
TIER_DRAFT_PENDING = 30  # needs a round before anything is knowable
TIER_DRAFT_PASSING = 40  # green but unaccepted; usually a difficulty question
TIER_GSD_REVIEW = 50     # a board card someone asked to have looked at
TIER_GSD_SCAFFOLD = 60   # screened, not yet a task tree
TIER_IDEA = 70           # most expensive: intake, screen, scaffold

# Board cards sort below every Codimango task on purpose. A card is a claim that
# work exists; a platform row is work that demonstrably exists.
GSD_TIERS = {"gsd_review": TIER_GSD_REVIEW, "gsd_scaffold": TIER_GSD_SCAFFOLD,
             "idea": TIER_IDEA}

TIER_NAMES = {
    TIER_REVISION: "needs_revision",
    TIER_DRAFT_FAILED: "draft/validation-failed",
    TIER_DRAFT_PENDING: "draft/validation-pending",
    TIER_DRAFT_PASSING: "draft/validation-passing",
    TIER_GSD_REVIEW: "gsd/needs-review",
    TIER_GSD_SCAFFOLD: "gsd/ready-to-scaffold",
    TIER_IDEA: "idea",
}

# A journal status that means the worker is finished with this task.
TERMINAL = frozenset({"converged", "abandoned"})
# ...and one that means a human must look before anything else happens.
NEEDS_HUMAN = frozenset({"escalated", "blocked-on-platform", "blocked"})

DEFAULT_LEASE_TTL = 3600

# Concurrency. A round is mostly spent waiting on the platform, so workers cost
# far less than their count suggests -- 12-15 loops on one devserver is a
# reported working figure, not a theoretical one.
#
# What made a low default necessary was the strict exact-SHA rule: with several
# workers pushing to one repository, each push buried the last and only the tip
# was ever measured. `coverage.py` removed that -- a later commit's run counts
# for an earlier one when the task's graded and visible surfaces are unchanged
# between them, and a worker only ever touches its own task directory. The
# publish lane is still one per repository, but it is held for the duration of a
# `git push` and released before validation, so it costs seconds per task rather
# than a validation cycle.
DEFAULT_WORKERS = 8
# Not a hard law, but past this the limit stops being benchsmith and starts
# being the devserver and the platform's validation capacity. Clamped with a
# warning rather than refused: the caller may know something this does not.
MAX_WORKERS = 15

PASSING_VALIDATION = frozenset({"completed", "passing", "passed"})
FAILING_VALIDATION = frozenset(TERMINAL_STATES) - PASSING_VALIDATION


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
    work_eligible: bool = True
    publication_eligible: bool = False
    publication_reason: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.work_eligible and not self.skip and self.claimed_by is None

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
            "workEligibility": {
                "eligible": self.work_eligible,
                "reason": self.reason if self.work_eligible else self.skip,
            },
            "publicationEligibility": {
                "eligible": self.publication_eligible,
                "reason": self.publication_reason,
            },
            "dispatchable": self.dispatchable,
        }


def _tier(status: str, validation: str) -> tuple[int, str] | None:
    if not work_eligibility(status).eligible:
        return None
    if status == "needs_revision":
        return (TIER_REVISION, "a reviewer named the defect")
    if status in {"draft", "unregistered"}:
        if validation in FAILING_VALIDATION:
            return (TIER_DRAFT_FAILED, f"validation is {validation}; evidence is on the SHA")
        if not validation or validation in PENDING_STATES:
            return (TIER_DRAFT_PENDING, "no terminal measurement yet")
        if validation in PASSING_VALIDATION:
            return (TIER_DRAFT_PASSING, "green but unaccepted")
        return (TIER_DRAFT_PENDING, f"unrecognised validation status {validation!r}")
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
        work = work_eligibility(status)
        publication = publication_eligibility(status)
        supplied_publication = t.get("publicationEligibility")
        if isinstance(supplied_publication, dict):
            publication = type(publication)(
                bool(supplied_publication.get("eligible")),
                str(supplied_publication.get("reason") or publication.reason),
            )
        jstatus = journals.get(name, "")
        item = Item(task=name, tier=tier, reason=reason, status=status,
                    validation=validation, journal_status=jstatus,
                    claimed_by=leases.get(name), work_eligible=work.eligible,
                    publication_eligible=publication.eligible,
                    publication_reason=publication.reason)
        if jstatus in TERMINAL:
            item.skip = f"journal says {jstatus}"
        elif jstatus in NEEDS_HUMAN:
            item.skip = f"journal says {jstatus}: needs a human before redispatch"
        elif jstatus == "unreadable":
            item.skip = "journal will not parse; counters unknown"
        elif (
            validation
            and validation not in PENDING_STATES
            and validation not in TERMINAL_STATES
        ):
            item.skip = f"unrecognised validation status {validation!r}; classify before acting"
        items.append(item)

    for idea in ideas or []:
        name = str(idea.get("name") or idea.get("id") or "")
        if not name:
            continue
        if str(idea.get("kind") or "idea") == "done":
            continue
        if any(i.task == name for i in items) or name in journals:
            continue
        tier = GSD_TIERS.get(str(idea.get("kind") or "idea"), TIER_IDEA)
        item = Item(task=name, tier=tier,
                    reason=str(idea.get("title") or "")[:80] or "unscaffolded idea",
                    claimed_by=leases.get(name))
        # A suspected duplicate is queued and marked, never dropped. There is no
        # link field between a board card and a platform task, so the match is a
        # guess -- and a wrong guess that deletes loses real work silently.
        if idea.get("duplicateOf"):
            item.skip = f"probably duplicates {idea['duplicateOf']}; confirm before dispatch"
        elif idea.get("unmappedSection"):
            item.skip = (f"section {idea['unmappedSection']!r} is not in the section map; "
                         "confirm it is work before dispatch")
        items.append(item)

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

    def active(self, *, reap_expired: bool = True) -> dict[str, str]:
        out: dict[str, str] = {}
        directory = Path(self.root) / ".benchsmith" / "leases"
        if not directory.is_dir():
            if not reap_expired:
                return out
            directory = self.dir()
        for p in directory.glob("*.lease"):
            try:
                doc = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if float(doc.get("expires", 0)) > self._now():
                out[p.stem] = str(doc.get("owner") or "?")
            elif reap_expired:
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


# --- reporting ---------------------------------------------------------------
#
# A queue nobody can see is one the operator has to ask about, and asking is the
# thing the whole loop is trying to remove. These render it for a person and say
# what moved since last time, so an update is posted when something changed
# rather than on every poll.

SHORT = {
    TIER_REVISION: "needs revision",
    TIER_DRAFT_FAILED: "draft · failing",
    TIER_DRAFT_PENDING: "draft · pending",
    TIER_DRAFT_PASSING: "draft · passing",
    TIER_GSD_REVIEW: "board · review",
    TIER_GSD_SCAFFOLD: "board · scaffold",
    TIER_IDEA: "board · idea",
}


def render(items: list, *, limit: int = 12, held: list | None = None) -> str:
    """The queue as a person would want to read it."""
    if not items:
        return "Queue is empty — nothing on the platform needs work."
    counts: dict[int, int] = {}
    for i in items:
        counts[i.tier] = counts.get(i.tier, 0) + 1
    head = ", ".join(f"{n} {SHORT.get(tier, str(tier))}"
                     for tier, n in sorted(counts.items()))
    lines = [f"**Queue — {len(items)} task(s):** {head}", ""]
    for i in items[:limit]:
        mark = "  " if i.dispatchable else "· "
        why = "" if i.dispatchable else f"  ({i.skip})"
        lines.append(f"{mark}`{SHORT.get(i.tier, str(i.tier)):<16}` {i.task}{why}")
    if len(items) > limit:
        lines.append(f"  … and {len(items) - limit} more")
    if held:
        lines.append("")
        lines.append(f"Held by reviewers: {len(held)} — " + ", ".join(held[:4])
                     + (" …" if len(held) > 4 else ""))
    return "\n".join(lines)


def fingerprint(items: list) -> dict:
    """Task -> tier. The thing worth noticing a change in."""
    return {i.task: i.tier for i in items}


def changes(before: dict, after: dict) -> dict:
    """What moved. Empty means nothing worth posting about."""
    added = sorted(k for k in after if k not in before)
    gone = sorted(k for k in before if k not in after)
    moved = sorted(k for k in after if k in before and before[k] != after[k])
    return {
        "added": [{"task": k, "tier": SHORT.get(after[k], str(after[k]))} for k in added],
        "gone": [{"task": k, "was": SHORT.get(before[k], str(before[k]))} for k in gone],
        "moved": [{"task": k, "from": SHORT.get(before[k], str(before[k])),
                   "to": SHORT.get(after[k], str(after[k]))} for k in moved],
        "changed": bool(added or gone or moved),
    }


def render_changes(ch: dict) -> str:
    if not ch.get("changed"):
        return ""
    bits = []
    for a in ch["added"]:
        bits.append(f"+ {a['task']} → {a['tier']}")
    for m in ch["moved"]:
        bits.append(f"~ {m['task']}: {m['from']} → {m['to']}")
    for g in ch["gone"]:
        # Left the queue: submitted, accepted, or converged. Not a loss.
        bits.append(f"- {g['task']} (was {g['was']}) left the queue")
    return "**Queue changed**\n" + "\n".join(f"  {b}" for b in bits)
