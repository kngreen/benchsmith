"""How long to wait before reading the platform again.

A third of rounds are not the task's fault -- infrastructure, stale platform
state, a commit that was never measured. Classifying those correctly is only
half the job: a loop that classifies `infra` and then immediately re-reads
spends its whole budget confirming the same outage, and in a fleet it spends
every other worker's slot too.

So the wait grows with the streak, and a long enough streak stops the loop
rather than burning the budget. Nothing here decides whether a round WAS
infrastructure; it only decides what to do once the classifier has said so.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Rounds where the platform, not the task, produced the outcome.
NOT_THE_TASK = frozenset({"infra", "not-measured", "platform-stale"})

BASE_SECONDS = 60
CAP_SECONDS = 1800
# Beyond this the loop is not iterating, it is polling an outage. Stop and say
# so -- `blocked-on-platform` exists precisely so this is never reported as
# either a pass or a task defect.
STREAK_STOP = 5


@dataclass(frozen=True)
class Advice:
    streak: int
    delay_seconds: float
    stop: bool
    reason: str

    def as_dict(self) -> dict:
        return {"streak": self.streak, "delaySeconds": round(self.delay_seconds, 1),
                "stop": self.stop, "reason": self.reason}


def consecutive(rounds: list[dict]) -> int:
    """How many of the most recent rounds in a row were not the task's fault."""
    n = 0
    for r in reversed(rounds or []):
        if str(r.get("class") or r.get("cls") or "") in NOT_THE_TASK:
            n += 1
        else:
            break
    return n


def delay(streak: int, *, rng: random.Random | None = None) -> float:
    """Exponential, capped, with jitter.

    Jitter is not decoration. Several workers that hit the same outage in the
    same minute would otherwise retry in the same minute forever, which is the
    thundering herd that keeps a rate-limited registry rate-limited.
    """
    if streak <= 0:
        return 0.0
    base = min(BASE_SECONDS * (2 ** (streak - 1)), CAP_SECONDS)
    r = rng or random.Random()
    return base * (0.5 + r.random() * 0.5)


def advise(rounds: list[dict], *, rng: random.Random | None = None) -> Advice:
    streak = consecutive(rounds)
    if streak == 0:
        return Advice(0, 0.0, False, "last round was about the task; no backoff")
    if streak >= STREAK_STOP:
        return Advice(streak, 0.0, True,
                      f"{streak} consecutive rounds were not the task's fault; "
                      "stop at blocked-on-platform rather than polling an outage")
    d = delay(streak, rng=rng)
    return Advice(streak, d, False, f"{streak} consecutive platform rounds; waiting {d:.0f}s")
