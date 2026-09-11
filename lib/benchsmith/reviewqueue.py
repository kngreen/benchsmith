"""The other queue: tasks waiting on ME to review them.

An author's backlog and a reviewer's backlog are different work with different
urgency. A task sitting in `being_reviewed` is blocking somebody else, and it
has a deadline; a task in `needs_revision` is the author's move and appears in
their queue, not this one.

Reviewing is never automated to a verdict. A worker gathers the evidence and
drafts the feedback; submitting it stays a human decision, the same way
publishing a task does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Lower sorts first.
TIER_OVERDUE = 10   # the SLA has already passed
TIER_DUE = 20       # assigned, with a deadline ahead
TIER_ASSIGNED = 30  # assigned, no deadline recorded
TIER_PRE = 40       # a draft I am down to review, already TBD-clean

TIER_NAMES = {
    TIER_OVERDUE: "review · overdue",
    TIER_DUE: "review · due",
    TIER_ASSIGNED: "review · assigned",
    TIER_PRE: "review · pre-submission",
}

# Why a task in the reviewing list is NOT mine to act on. Named rather than
# dropped: "my review queue lost a task" is a worse experience than a line
# saying whose move it is.
NOT_MINE = {
    "needs_revision": "I asked for changes; it is the author's move",
    "accepted": "already accepted",
    "used_in_training": "accepted and in training",
}


@dataclass
class ReviewItem:
    task: str
    tier: int
    reason: str
    track: str = ""
    tbd: str = ""
    validation: str = ""
    due: str = ""
    skip: str = ""

    @property
    def dispatchable(self) -> bool:
        return not self.skip

    def as_dict(self) -> dict:
        return {"task": self.task, "tier": self.tier, "tierName": TIER_NAMES.get(self.tier, "?"),
                "reason": self.reason, "track": self.track, "tbd": self.tbd,
                "validation": self.validation, "due": self.due, "skip": self.skip,
                "dispatchable": self.dispatchable}


def _overdue(deadline: str, now: datetime | None = None) -> bool | None:
    """None when there is no readable deadline -- absent is not 'plenty of time'."""
    if not deadline:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            d = datetime.strptime(deadline, fmt).replace(tzinfo=timezone.utc)
            return (now or datetime.now(timezone.utc)) > d
        except ValueError:
            continue
    return None


def build(rows: list, *, now: datetime | None = None) -> tuple:
    """Order a reviewer's backlog. Pure: same rows, same order."""
    items, notes = [], []
    for r in rows:
        name = str(r.get("name") or "")
        if not name or not r.get("currentUserIsReviewer"):
            continue
        status = str(r.get("status") or "")
        if status in NOT_MINE:
            notes.append(f"{name}: {NOT_MINE[status]}")
            continue

        due = str(r.get("reviewSlaDeadline") or "")
        tbd = str(r.get("tbdReviewStatus") or "")
        common = dict(track=str(r.get("track") or ""), tbd=tbd,
                      validation=str(r.get("validationStatus") or ""), due=due)

        if status == "being_reviewed":
            late = _overdue(due, now)
            if late:
                items.append(ReviewItem(name, TIER_OVERDUE, f"SLA passed {due[:10]}", **common))
            elif late is False:
                items.append(ReviewItem(name, TIER_DUE, f"due {due[:10]}", **common))
            else:
                items.append(ReviewItem(name, TIER_ASSIGNED, "assigned, no deadline recorded",
                                        **common))
        elif status == "draft" and tbd == "pass":
            items.append(ReviewItem(name, TIER_PRE, "draft, TBD-clean; a look before submission",
                                    **common))
        else:
            notes.append(f"{name}: status {status!r} is not review-ready")
            continue

        # A review of a task whose validation is still moving reads evidence
        # that is about to change.
        if items and items[-1].validation == "pending":
            items[-1].skip = "validation still pending; the evidence will move under the review"

    # Deadline within tier, then name, so the order is total and reproducible.
    items.sort(key=lambda i: (i.tier, i.due or "9999", i.task))
    return items, notes


def render(items: list, notes: list | None = None) -> str:
    if not items:
        return "Review queue is empty — nothing is assigned to you."
    counts: dict = {}
    for i in items:
        counts[i.tier] = counts.get(i.tier, 0) + 1
    head = ", ".join(f"{n} {TIER_NAMES.get(t, t)}" for t, n in sorted(counts.items()))
    out = [f"**Review queue — {len(items)} task(s):** {head}", ""]
    for i in items:
        mark = "  " if i.dispatchable else "· "
        why = "" if i.dispatchable else f"  ({i.skip})"
        out.append(f"{mark}`{TIER_NAMES.get(i.tier, '?'):<24}` {i.task}  [{i.track}]{why}")
    if notes:
        out += ["", f"Not mine to act on: {len(notes)}"]
    return "\n".join(out)
