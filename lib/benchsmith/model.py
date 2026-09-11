"""Normalized types for a measurement.

Deliberately stdlib-only: this runs from a skill directory on whatever Python a
devserver happens to have, with no install step and no virtualenv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    """Disjoint outcome classes. Order is the classifier's precedence order.

    Only PASS counts as a pass. Only A and B are hardness evidence. C is an
    authoritative non-pass that blocks a hard verdict. D and E invalidate the
    measurement rather than contributing to it. F needs replacement. G is
    unresolved and must never be silently read as candidate fault.
    """

    E = "E"  # spec ambiguity or defect        -> invalidates
    D = "D"  # valid alternative rejected      -> invalidates
    F = "F"  # infrastructure                  -> replace
    C = "C"  # unrelated candidate failure     -> counts, blocks hard
    PASS = "PASS"
    A = "A"  # intended semantic verifier failure
    B = "B"  # intended semantic pre-verifier failure
    G = "G"  # unknown attribution

    @property
    def is_pass(self) -> bool:
        return self is Kind.PASS

    @property
    def is_hardness_evidence(self) -> bool:
        return self in (Kind.A, Kind.B)

    @property
    def is_calibration_bearing(self) -> bool:
        """Contributes a denominator slot. D/E/F/G do not."""
        return self in (Kind.PASS, Kind.A, Kind.B, Kind.C)

    @property
    def invalidates_measurement(self) -> bool:
        return self in (Kind.D, Kind.E)


@dataclass(frozen=True, order=True)
class SlotKey:
    """Cross-generation identity of one planned attempt.

    Generation-config and platform slot IDs are recorded aliases elsewhere; they
    are never the identity, because a replacement generation must bind to the
    same slot as the row it supersedes.
    """

    stage: str
    family: str
    build: str
    step: str
    ordinal: int

    def __str__(self) -> str:
        return f"{self.stage}/{self.family}@{self.build}/{self.step}#{self.ordinal}"


@dataclass
class Row:
    """One observed participant attempt."""

    slot: SlotKey
    kind: Kind
    job_id: str = ""
    trial_id: str = ""
    generation: int = 0
    superseded: bool = False
    decisions: tuple[str, ...] = ()  # canonical decision IDs, sorted
    category: str = ""  # frozen behaviour category
    note: str = ""

    @property
    def counts(self) -> bool:
        return not self.superseded and self.kind.is_calibration_bearing


@dataclass
class Plan:
    """The slot plan, frozen before the first participant result exists.

    `slots` is the denominator. A planned slot with no row is incomplete, not
    absent -- the single most consequential bug this module exists to prevent.
    """

    slots: tuple[SlotKey, ...]
    strongest: tuple[tuple[str, str], ...] = ()  # (family, build)
    steps: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()  # frozen independent behaviour categories
    replacement_used: bool = False

    def __post_init__(self) -> None:
        if not self.slots:
            raise ValueError("a plan with no slots cannot measure anything")
        if len(set(self.slots)) != len(self.slots):
            raise ValueError("duplicate slot keys in plan")


@dataclass
class Review:
    """One row of the review manifest."""

    name: str
    state: str = "absent"  # completed | pending | errored | absent
    verdict: str = ""
    job_id: str = ""
    reviewed_sha: str = ""
    selection: str = "exact-head"  # exact-head | fallback
    stale: bool = False

    def green(self, active_sha: str, passing: frozenset[str]) -> bool:
        return (
            self.state == "completed"
            and self.reviewed_sha == active_sha
            and self.selection == "exact-head"
            and not self.stale
            and self.verdict in passing
        )


@dataclass
class Finding:
    """One reason the bar does not hold. Empty list == the bar holds."""

    code: str
    detail: str


@dataclass
class Measurement:
    """A plan plus the rows observed against it, for one exact SHA."""

    plan: Plan
    rows: list[Row] = field(default_factory=list)
    active_sha: str = ""
    reviews: list[Review] = field(default_factory=list)
    # Why each job was kept or dropped during SHA scoping — an auditable trail,
    # because a silently dropped cohort is indistinguishable from one that never ran.
    selection_notes: list[str] = field(default_factory=list)
