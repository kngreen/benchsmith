"""The §5 bar: does this measurement establish that the task is hard?

Every predicate is conjunctive and false on missing evidence. A rate is only
produced for a calibration-complete measurement -- an incomplete one has no
rate, not a provisional one, because a provisional rate is the failure mode this
whole module exists to prevent.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from .model import Finding, Kind, Measurement, Review, Row, SlotKey

Z = 1.96  # two-sided 95%, no continuity correction

BAND = (0.20, 0.50)  # pooled participant completion
MIXED = (0.20, 0.60)  # per strongest member
MEDIUM_BAND = (0.50, 0.80)
SINGLE_GATE_KILL = 0.80
MIN_FAMILIES = 2

# The 1P model the corpus exists to train. It is deliberately NOT in the frozen
# strongest set -- it never substitutes for a missing GPT/Opus member -- which
# left it entirely unchecked: a task where avocado passed every trial scored HARD
# with no findings at all. A task the model under test already solves carries no
# training signal, whatever the other cohorts do.
MODEL_UNDER_TEST = frozenset({"avocado", "metacode"})
MIN_CATEGORIES = 2

from .snapshot import REVIEW_PASSING  # noqa: E402  (single source of truth)


def wilson(passes: int, n: int) -> tuple[float, float]:
    """Two-sided 95% Wilson score interval.

    Wald is degenerate at 0/n and n/n, which is precisely where a five-trial
    cohort spends most of its time, so it is not offered as an option.
    """
    if n <= 0:
        raise ValueError("no denominator")
    p = passes / n
    d = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / d
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def distance_to_band(p: float, band: tuple[float, float] = BAND) -> float:
    """d(p): how far outside the band, 0.0 when inside."""
    lo, hi = band
    return max(lo - p, 0.0, p - hi)


def _live(m: Measurement) -> list[Row]:
    return [r for r in m.rows if not r.superseded]


def completeness(m: Measurement) -> tuple[bool, list[str]]:
    """Calibration-complete: every planned slot has exactly one counting row.

    A planned slot with zero rows is INCOMPLETE, never absent. Shrinking the
    denominator to the rows that happened to arrive turns a broken cohort into a
    flattering rate, which is how an unmeasured task reads as green.
    """
    by_slot: dict[SlotKey, list[Row]] = defaultdict(list)
    for r in _live(m):
        by_slot[r.slot].append(r)

    incomplete: list[str] = []
    for slot in m.plan.slots:
        rows = by_slot.get(slot, [])
        counting = [r for r in rows if r.counts]
        if not rows:
            incomplete.append(f"{slot}: no rows (planned, never observed)")
        elif not counting:
            kinds = ",".join(sorted({r.kind.value for r in rows}))
            incomplete.append(f"{slot}: no calibration-bearing outcome (saw {kinds})")
        elif len(counting) > 1:
            incomplete.append(f"{slot}: {len(counting)} counting rows, expected 1")

    extra = sorted(str(s) for s in by_slot if s not in set(m.plan.slots))
    incomplete.extend(f"{s}: row for an unplanned slot" for s in extra)
    return (not incomplete, incomplete)


def invalidating(m: Measurement) -> list[Row]:
    """D/E rows. Any one of these means the measurement has no rate at all."""
    return [r for r in _live(m) if r.kind.invalidates_measurement]


def rate(m: Measurement, subset: list[Row] | None = None) -> tuple[int, int]:
    rows = subset if subset is not None else [r for r in _live(m) if r.counts]
    return (sum(1 for r in rows if r.kind.is_pass), len(rows))


def by_family(m: Measurement) -> dict[tuple[str, str], list[Row]]:
    out: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in _live(m):
        if r.counts:
            out[(r.slot.family, r.slot.build)].append(r)
    return out


def by_step(m: Measurement) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in _live(m):
        if r.counts:
            out[r.slot.step].append(r)
    return out


def single_gate_share(m: Measurement) -> float:
    """Max share of strongest-member A/B trials explained by one decision ID.

    At or above 0.80 the task hinges on one decision: it cannot be tuned into
    band and must be redesigned rather than hardened.
    """
    strongest = set(m.plan.strongest)
    trials = [
        r
        for r in _live(m)
        if r.counts and r.kind.is_hardness_evidence and (r.slot.family, r.slot.build) in strongest
    ]
    if not trials:
        # Zero strongest-member semantic failures -- both cohorts saturated, say.
        # Returning 0.0 reads as "well spread" and passes the box with nothing
        # behind it. Same fail-open bug as the no-decisions branch below; the
        # sibling was fixed and this one was missed.
        return None
    counts: Counter[str] = Counter()
    for r in trials:
        for d in set(r.decisions):
            counts[d] += 1
    if not counts:
        # No decision IDs means the kill CANNOT be evaluated. Returning 0.0 here
        # reads as "well spread" and lets the check pass -- failing open, in a
        # module whose contract is that every predicate is false on missing
        # evidence. The caller must treat None as unverifiable, not as clean.
        return None
    return max(counts.values()) / len(trials)


def categories_covered(m: Measurement) -> set[str]:
    """A category counts with >=2 distinct A/B trials, >=1 from a strongest member.

    One trial contributes to exactly one category -- the one holding the
    lexicographically smallest decision ID in its canonical set -- so a single
    failure can never satisfy the two-category requirement by itself.
    """
    strongest = set(m.plan.strongest)
    buckets: dict[str, list[Row]] = defaultdict(list)
    for r in _live(m):
        if r.counts and r.kind.is_hardness_evidence and r.category:
            buckets[r.category].append(r)
    covered = set()
    for cat, rows in buckets.items():
        if len(rows) >= 2 and any((x.slot.family, x.slot.build) in strongest for x in rows):
            covered.add(cat)
    return covered


def evaluate(m: Measurement, target: str = "hard-preferred") -> dict:
    """Return the full verdict. `findings` empty == the bar holds at hard."""
    findings: list[Finding] = []

    complete, incomplete = completeness(m)
    bad = invalidating(m)
    if bad:
        findings.append(
            Finding("invalidated", f"{len(bad)} D/E rows: {', '.join(r.note or str(r.slot) for r in bad)}")
        )
    if not complete:
        findings.append(Finding("incomplete", "; ".join(incomplete)))

    # No rate exists for an incomplete or invalidated measurement. Emitting one
    # anyway is the boundary where every false green in this system began.
    if findings:
        return {
            "verdict": "NO RATE",
            "rate": None,
            "wilson": None,
            "findings": [f.__dict__ for f in findings],
            "runsIncomplete": incomplete,
        }

    passes, n = rate(m)
    p = passes / n
    lo, hi = wilson(passes, n)

    if not (BAND[0] <= p <= BAND[1]):
        findings.append(Finding("band", f"pooled {passes}/{n} = {p:.4f}, outside {BAND}"))

    if not m.plan.strongest:
        findings.append(Finding("no-strongest-set", "strongest set was never frozen"))
    for member in m.plan.strongest:
        rows = by_family(m).get(member, [])
        if not rows:
            findings.append(Finding("strongest-missing", f"{member[0]}@{member[1]} has no rows"))
            continue
        mp, mn = rate(m, rows)
        mrate = mp / mn
        if not (MIXED[0] <= mrate <= MIXED[1]):
            state = "saturated" if mrate > MIXED[1] else "starved"
            edge = MIXED[1] if mrate > MIXED[1] else MIXED[0]
            lo, hi = wilson(mp, mn)
            # The allowance is ONE-SIDED, and the asymmetry is the point.
            #
            # SATURATED near the edge (codex 4/5) is thin evidence for rejecting
            # a task that may be fine: one trial moves the rate 20 points, so 4/5
            # is not distinguishable from 3/5. Advisory.
            #
            # STARVED (a cohort at 0/5) stays BLOCKING, however close to the
            # floor. A cohort that never passed is the signature of a grader
            # over-constraining implementation freedom -- pinned file identity,
            # exact column names, a numeric margin, an error shape -- and waving
            # it through is how a broken grader reads as difficulty. A live run
            # reported exactly this shape and correctly treated it as a failure.
            if state == "saturated" and mn < 10 and abs(mrate - edge) <= (1.0 / mn) + 1e-9:
                findings.append(
                    Finding(
                        "strongest-boundary",
                        f"{member[0]}@{member[1]} {mp}/{mn} = {mrate:.4f} ({state}) is ONE TRIAL from "
                        f"{edge:.2f}, Wilson [{lo:.2f}, {hi:.2f}] — advisory at k={mn}. "
                        f"Resample at k>=10 before rejecting on this alone",
                    )
                )
            else:
                findings.append(
                    Finding("strongest-not-mixed", f"{member[0]}@{member[1]} {mp}/{mn} = {mrate:.4f} ({state})")
                )

    # The model under test must record at least one genuine failure.
    mut_rows = [r for r in _live(m) if r.counts and r.slot.family in MODEL_UNDER_TEST]
    if mut_rows:
        mut_fail = [r for r in mut_rows if r.kind.is_hardness_evidence]
        if not mut_fail:
            fam = sorted({r.slot.family for r in mut_rows})
            mp = sum(1 for r in mut_rows if r.kind.is_pass)
            findings.append(
                Finding(
                    "model-under-test-saturated",
                    f"{'/'.join(fam)} {mp}/{len(mut_rows)} with no genuine semantic failure: "
                    "the model this corpus trains already solves the task, so it carries no "
                    "training signal regardless of the other cohorts",
                )
            )

    fams = {
        r.slot.family for r in _live(m) if r.counts and r.kind.is_hardness_evidence
    }
    if len(fams) < MIN_FAMILIES:
        findings.append(Finding("one-family", f"semantic failures in {len(fams)} family/families"))

    cats = categories_covered(m)
    labelled = any(r.category for r in _live(m) if r.counts and r.kind.is_hardness_evidence)
    if not labelled:
        # Distinguish "measured, and it is all one category" from "nothing was
        # ever categorised". The second is unverified, and saying `one-category`
        # would blame the task for a gap in our own evidence.
        findings.append(
            Finding(
                "attribution-unavailable",
                "no behaviour category on any semantic failure: two-category coverage cannot be "
                "evaluated",
            )
        )
    elif len(cats) < MIN_CATEGORIES:
        findings.append(Finding("one-category", f"hardness in {len(cats)} category/categories: {sorted(cats)}"))

    share = single_gate_share(m)
    if share is None:
        findings.append(
            Finding(
                "attribution-unavailable",
                "no decision IDs on any strongest-member semantic failure: the single-gate kill "
                "cannot be evaluated. Supply attribution (trajectory read or reviewer input) or "
                "the hardness claim is unverified, not clean",
            )
        )
    elif share >= SINGLE_GATE_KILL:
        findings.append(Finding("single-gate", f"one decision explains {share:.0%} of strongest failures"))

    for step in m.plan.steps:
        rows = by_step(m).get(step, [])
        if not any(r.kind.is_pass for r in rows):
            findings.append(Finding("step-no-pass", f"step {step} has no genuine pass"))
        if not any(r.kind.is_hardness_evidence for r in rows):
            findings.append(Finding("step-no-failure", f"step {step} has no genuine semantic failure"))

    unrelated = [r for r in _live(m) if r.counts and r.kind is Kind.C]
    if unrelated:
        findings.append(
            Finding("unrelated-nonpass", f"{len(unrelated)} C rows in the denominator block a hard verdict")
        )

    for rv in m.reviews:
        passing = REVIEW_PASSING.get(rv.name, frozenset({"GOOD", "Accept"}))
        if not rv.green(m.active_sha, passing):
            findings.append(
                Finding(
                    "review",
                    f"{rv.name}: state={rv.state} verdict={rv.verdict or '-'} "
                    f"sha={rv.reviewed_sha[:8] or '-'} selection={rv.selection} stale={rv.stale}",
                )
            )

    # `strongest-boundary` is advisory: reported, never decisive on its own.
    decisive = [f for f in findings if f.code != "strongest-boundary"]
    verdict = _verdict(decisive, p, m, target)
    return {
        "verdict": verdict,
        "rate": {"passes": passes, "slots": n, "p": round(p, 4)},
        "wilson": {"lo": round(lo, 4), "hi": round(hi, 4)},
        "distanceToBand": round(distance_to_band(p), 4),
        "boundaryAdjacent": 0 < distance_to_band(p) < 1 / n,
        "categories": sorted(cats),
        "singleGateShare": None if share is None else round(share, 4),
        "findings": [f.__dict__ for f in findings],
        "runsIncomplete": [],
    }


def _verdict(findings: list[Finding], p: float, m: Measurement, target: str) -> str:
    """HARD is the full conjunction; MEDIUM is its own predicate, not a near-miss.

    Inferring medium from "the band was the only complaint" is stricter than the
    spec -- it silently requires every strongest member mixed, when medium asks
    for one -- so the medium conditions are evaluated directly.
    """
    if not findings:
        return "HARD"
    if target == "hard-only":
        return "NOT HARD"

    codes = {f.code for f in findings}

    # Never downgrade a correctness problem into a pass. These say the
    # measurement or the task is unsound, not that it is merely easier.
    blocking = {
        "invalidated",
        "incomplete",
        "attribution-unavailable",
        "review",
        "single-gate",
        "unrelated-nonpass",
        "no-strongest-set",
        "strongest-missing",
        "model-under-test-saturated",
        "step-no-pass",
    }
    if codes & blocking:
        return "NOT HARD"

    in_medium_band = MEDIUM_BAND[0] < p <= MEDIUM_BAND[1]
    # The low branch: below the hard floor is medium only when a strongest member
    # genuinely passed, i.e. the task is hard rather than broken.
    strongest_rows = {k: v for k, v in by_family(m).items() if k in set(m.plan.strongest)}
    any_strongest_pass = any(any(r.kind.is_pass for r in rows) for rows in strongest_rows.values())
    below_floor = p < BAND[0] and any_strongest_pass

    if not (in_medium_band or below_floor):
        return "NOT HARD"

    # At least one strongest member mixed, and at least one family failing for a
    # real semantic reason.
    any_mixed = False
    for rows in strongest_rows.values():
        if not rows:
            continue
        mp, mn = rate(m, rows)
        if MIXED[0] <= mp / mn <= MIXED[1]:
            any_mixed = True
            break
    any_semantic = any(r.kind.is_hardness_evidence for r in _live(m) if r.counts)

    return "MEDIUM" if any_mixed and any_semantic else "NOT HARD"
