"""Did the last hardening change do anything?

"In band" and "in band because of what we did" are different claims. A task can
land in band because a change worked, or because a five-trial sample moved on
its own — and only the first is a reason to stop.

This does not decide the band; §5 does. It answers the narrower question of
whether the most recent difficulty-moving round has evidence behind it, so a
terminal verdict can say which of the two happened instead of implying the
former.

The honest answer is often "cannot tell". A five-trial cohort moves ~6.7 points
per flipped trial, so a change smaller than that is inside the noise and saying
otherwise would be inventing a causal story.
"""

from __future__ import annotations

from dataclasses import dataclass

# One flipped trial in three cohorts of five. A movement smaller than this is
# not distinguishable from the sample landing differently.
ONE_TRIAL = 1.0 / 15.0

CONFIRMED, UNMOVED, WRONG_WAY, INDISTINGUISHABLE, UNKNOWN = (
    "confirmed", "unmoved", "wrong-way", "indistinguishable", "unknown")


@dataclass(frozen=True)
class Verdict:
    state: str
    before: float | None
    after: float | None
    delta: float | None
    detail: str

    @property
    def useful(self) -> bool:
        return self.state == CONFIRMED

    def as_dict(self) -> dict:
        return {"state": self.state, "before": self.before, "after": self.after,
                "delta": None if self.delta is None else round(self.delta, 3),
                "useful": self.useful, "detail": self.detail}


def _rate(entry: dict) -> float | None:
    m = (entry or {}).get("measurement") or {}
    for key in ("pooledRate", "overallRate", "rate"):
        v = m.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def assess(rounds: list, *, band: tuple = (0.20, 0.50)) -> Verdict:
    """Compare the last difficulty-moving round with the one before it."""
    hardening = [r for r in (rounds or []) if r.get("hardening")]
    if not hardening:
        return Verdict(UNKNOWN, None, None, None,
                       "no hardening round recorded; nothing to attribute the band to")
    last = hardening[-1]
    after = _rate(last)
    if after is None:
        return Verdict(UNKNOWN, None, None, None,
                       "the last hardening round recorded no rate; it cannot be shown to have "
                       "helped, and an unmeasured change is not a proven one")

    idx = (rounds or []).index(last)
    before = next((_rate(r) for r in reversed((rounds or [])[:idx]) if _rate(r) is not None), None)
    if before is None:
        return Verdict(UNKNOWN, None, after, None,
                       "no measured round before the last hardening change; there is nothing to "
                       "compare it against")

    delta = after - before
    lo, hi = band
    # Which way SHOULD it have gone: down if it was too easy, up if too hard.
    wanted_down = before > hi
    wanted_up = before < lo
    if abs(delta) < ONE_TRIAL:
        return Verdict(INDISTINGUISHABLE, before, after, delta,
                       f"{before:.0%} → {after:.0%} is under one trial of movement; the change "
                       "cannot be distinguished from the sample landing differently")
    if (wanted_down and delta > 0) or (wanted_up and delta < 0):
        return Verdict(WRONG_WAY, before, after, delta,
                       f"{before:.0%} → {after:.0%} moved away from the band; the last change made "
                       "it worse, and reverting it is the cheapest next step")
    if not (wanted_down or wanted_up):
        return Verdict(UNMOVED, before, after, delta,
                       f"{before:.0%} was already in band, so the last hardening change had "
                       "nothing to fix; {after:.0%} is drift, not an effect")
    return Verdict(CONFIRMED, before, after, delta,
                   f"{before:.0%} → {after:.0%}: the last hardening change moved the rate "
                   f"{abs(delta):.0%} toward the band")
