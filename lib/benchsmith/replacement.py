"""Infrastructure replacement: one frozen wave per SHA, superseded by slot key.

Three rules make this safe, and all three are enforced here rather than trusted:

**Only F is replaceable.** A candidate-caused failure is authoritative. Rerolling
an A, B or C until it comes out differently is sampling until you like the
answer, so the planner refuses those slots outright.

**Scope is chosen from causes, never from outcomes.** The narrowest supported
action wins, and a wider one is only reachable by proving the narrower is
unavailable. Rewards from calibration-bearing rows are not an input.

**The wave is frozen before dispatch and cannot escalate.** Once any replacement
result is visible the scope is fixed, and a second wave on the same SHA is
refused — there is no third unchanged-SHA sampling generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Kind, Measurement, Row, SlotKey

# Narrowest first. A wider scope requires proof that every narrower one is
# unavailable, recorded as a capability probe.
SCOPES = ("replay", "slot", "cohort", "generation")


class ReplacementRefused(Exception):
    """The requested wave is not permitted. The message is the reason."""


@dataclass
class Wave:
    sha: str
    scope: str
    slots: tuple[SlotKey, ...]
    reason: str
    capabilities: tuple[str, ...] = ()
    frozen_at: str = ""
    digest: str = ""
    dispatched: bool = False
    results_seen: bool = False
    superseded_slots: tuple[SlotKey, ...] = field(default_factory=tuple)

    def freeze(self) -> Wave:
        self.frozen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(
            {
                "sha": self.sha,
                "scope": self.scope,
                "slots": sorted(str(s) for s in self.slots),
                "reason": self.reason,
                "capabilities": sorted(self.capabilities),
                "frozenAt": self.frozen_at,
            },
            sort_keys=True,
        )
        self.digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return self

    def as_dict(self) -> dict:
        return {
            "sha": self.sha,
            "scope": self.scope,
            "slots": [str(s) for s in self.slots],
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            "frozenAt": self.frozen_at,
            "digest": self.digest,
            "dispatched": self.dispatched,
            "supersededSlots": [str(s) for s in self.superseded_slots],
        }


def eligible(m: Measurement) -> list[Row]:
    """Slots needing replacement: infrastructure-invalid, or unresolved at deadline.

    G is included only because an unresolved attribution at the bounded deadline
    is classified F for completeness. Never infer candidate fault from
    uncertainty, and never replace a slot because its outcome was unwelcome.
    """
    return [r for r in m.rows if not r.superseded and r.kind in (Kind.F, Kind.G)]


def plan(
    m: Measurement,
    *,
    capabilities: tuple[str, ...],
    reason: str,
    prior_wave: Wave | None = None,
) -> Wave:
    """Choose the narrowest supported scope for the slots that actually need it."""
    if prior_wave is not None:
        raise ReplacementRefused(
            f"a replacement wave ({prior_wave.scope}, {prior_wave.digest}) is already frozen for "
            f"{m.active_sha[:8]}; the budget is the original attempt set plus one wave per SHA"
        )

    bad = eligible(m)
    if not bad:
        raise ReplacementRefused(
            "no infrastructure-invalid slots; A, B and C are authoritative non-passes "
            "and are never replaced"
        )

    forbidden = [r for r in m.rows if not r.superseded and r.kind in (Kind.A, Kind.B, Kind.C)]
    slots = tuple(sorted({r.slot for r in bad}))

    supported = [s for s in SCOPES if s in capabilities]
    if not supported:
        raise ReplacementRefused(
            "no replacement capability resolved; probe the CLI and record what it offers "
            "before claiming a wider scope is necessary"
        )

    cohorts = {(s.family, s.build) for s in slots}
    if "slot" in supported:
        scope, chosen = "slot", slots
    elif "cohort" in supported:
        scope = "cohort"
        chosen = tuple(sorted(s for s in m.plan.slots if (s.family, s.build) in cohorts))
    else:
        scope, chosen = "generation", tuple(sorted(m.plan.slots))

    if "replay" in supported and all(r.trial_id for r in bad):
        scope, chosen = "replay", slots

    wave = Wave(
        sha=m.active_sha,
        scope=scope,
        slots=chosen,
        reason=reason,
        capabilities=tuple(supported),
    ).freeze()
    wave.superseded_slots = _supersedes(m, wave)
    if forbidden and scope in ("cohort", "generation"):
        # A wide scope inevitably re-runs authoritative rows. That is allowed --
        # they are superseded mechanically -- but it must be visible, because it
        # is also how a wide scope becomes a quiet reroll of an unwelcome result.
        wave.reason += (
            f" [note: this scope supersedes {len(forbidden)} authoritative row(s); "
            "their replacements bind regardless of outcome]"
        )
    return wave


def _supersedes(m: Measurement, wave: Wave) -> tuple[SlotKey, ...]:
    """Which existing rows this wave replaces -- by slot key, never by judgement."""
    if wave.scope in ("replay", "slot"):
        return tuple(sorted(set(wave.slots)))
    if wave.scope == "cohort":
        cohorts = {(s.family, s.build) for s in wave.slots}
        return tuple(sorted({r.slot for r in m.rows if (r.slot.family, r.slot.build) in cohorts}))
    return tuple(sorted({r.slot for r in m.rows}))


def apply(m: Measurement, wave: Wave, new_rows: list[Row]) -> Measurement:
    """Bind replacement rows and mark the originals superseded.

    A calibration-bearing replacement binds regardless of outcome: a candidate
    failure that arrives in place of an infrastructure error is authoritative and
    is never rerolled. An infrastructure-invalid replacement binds nothing and
    leaves the slot incomplete, which is the honest state.
    """
    if wave.results_seen:
        raise ReplacementRefused("this wave has already been applied; it cannot escalate")

    targets = set(wave.superseded_slots)
    for row in m.rows:
        if row.slot in targets and not row.superseded:
            row.superseded = True

    generation = max((r.generation for r in m.rows), default=0) + 1
    for row in new_rows:
        if row.slot not in targets:
            raise ReplacementRefused(
                f"{row.slot} is not in the frozen wave; a wave cannot widen after dispatch"
            )
        row.generation = generation
        row.superseded = False
        m.rows.append(row)

    wave.dispatched = True
    wave.results_seen = True
    m.plan.replacement_used = True
    return m
