"""Turn platform records into a Measurement.

This is where most false greens are born, so the module is written around three
distinctions that are easy to collapse and expensive to get wrong:

* **failed vs never ran.** An errored trial sits in the denominator and reads as
  a harder task. It is `F`, it is replaced, and it is not difficulty.
* **empty vs unknown.** A trial that reported failures but named none makes the
  shared set *unknowable*. That is `None`, not `[]`, and the two must never be
  rendered alike.
* **planned vs observed.** A planned slot with no row is incomplete. Shrinking
  the denominator to what arrived turns a broken cohort into a flattering rate.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .model import Kind, Measurement, Plan, Review, Row, SlotKey

# Job/trial identities that are never a participant attempt, whatever model ran
# them. Everything here is excluded before a denominator is formed.
NON_PARTICIPANT = (
    "oracle",
    "reference",
    "verifier",
    "test-patch",
    "structural",
    "provenance",
    "contamination",
    "qualitative",
    "agentic-review",
    "full-task",
    "code-review",
    "collector",
)

_ERRORED = re.compile(
    r"\b(error|errored|infra|timeout|timed.?out|cancell?ed|worker|harness|rate.?limit|"
    r"unavailable|outage|preempt)\b",
    re.I,
)


def is_participant(job: dict) -> tuple[bool, str]:
    """Admit a job as participant, or say which predicate rejected it."""
    stage = str(job.get("stage") or job.get("validationStage") or "").lower()
    env = (job.get("config") or {}).get("env") or {}
    stage = str(env.get("__validation_stage") or stage).lower()
    kind = str(job.get("kind") or job.get("type") or "").lower()
    name = str(job.get("name") or "").lower()
    blob = " ".join((stage, kind, name))
    for marker in NON_PARTICIPANT:
        if marker in blob:
            return (False, f"stage/kind matches {marker!r}")
    if not job.get("model") and not job.get("family"):
        return (False, "no participant model on the job")
    return (True, "")


def classify(trial: dict, *, graded_ok: bool) -> tuple[Kind, str]:
    """The disjoint ordered classifier. First match wins; order is precedence.

    E and D outrank F and C deliberately: a spec defect or a rejected valid
    alternative invalidates the measurement, and reading either as difficulty is
    how a grader bug becomes a hardness claim.
    """
    cause = str(trial.get("causeClass") or trial.get("attribution") or "").upper()
    if cause in {"E", "D", "F", "C", "A", "B", "G", "PASS"}:
        return (Kind[cause] if cause != "PASS" else Kind.PASS, "explicit attribution")

    status = str(trial.get("status") or "").lower()
    reward = trial.get("reward")
    reason = str(trial.get("failureReason") or trial.get("error") or "")

    if trial.get("specDefect") or trial.get("ambiguity"):
        return (Kind.E, "spec ambiguity or defect")
    if trial.get("validAlternative") or trial.get("graderFalseNegative"):
        return (Kind.D, "valid alternative rejected")
    if _ERRORED.search(status) or _ERRORED.search(reason):
        return (Kind.F, f"infrastructure: {status or reason}"[:120])
    if graded_ok:
        return (Kind.PASS, "every graded assertion passed")
    if trial.get("reachedVerifier") or reward is not None:
        return (Kind.A, "verifier ran and reported wrong behaviour")
    if status in {"failed", "fail", "error"} and not reason:
        return (Kind.G, "no attribution available")
    if status:
        return (Kind.B, f"candidate failure before the verifier: {status}"[:120])
    return (Kind.G, "no status")


def graded_pass(trial: dict) -> bool:
    """PASS means every element of the graded set passed. Partial credit is not a pass."""
    if trial.get("gradedPass") is not None:
        return bool(trial["gradedPass"])
    reward = trial.get("reward")
    if isinstance(reward, dict):
        return bool(reward) and all(float(v) >= 1.0 for v in reward.values())
    if reward is None:
        return False
    try:
        return float(reward) >= 1.0
    except (TypeError, ValueError):
        return False


def slot_of(job: dict, trial: dict, ordinal: int) -> SlotKey:
    env = (job.get("config") or {}).get("env") or {}
    return SlotKey(
        stage=str(env.get("__validation_stage") or job.get("stage") or "agent"),
        family=str(job.get("family") or job.get("model") or "unknown").split("/")[0],
        build=str(job.get("modelBuild") or job.get("model") or "unknown"),
        step=str(trial.get("step") or trial.get("stepId") or "1"),
        ordinal=ordinal,
    )


def build_plan(jobs: list[dict], *, strongest, steps, categories=()) -> Plan:
    """Derive the planned slot set from job configuration, not from results.

    `attempts` comes from the job's own configuration. When the platform will
    not say how many attempts a cohort plans, that cohort cannot be accepted as
    calibration -- the missing field is reported as infrastructure rather than
    silently becoming "however many arrived".
    """
    slots: list[SlotKey] = []
    for job in jobs:
        ok, _ = is_participant(job)
        if not ok:
            continue
        attempts = job.get("plannedAttempts") or job.get("attempts") or job.get("k")
        if not attempts:
            raise ValueError(
                f"job {job.get('id')} does not declare its planned attempt count; "
                "cannot freeze a slot plan from it"
            )
        for step in steps or ("1",):
            for i in range(int(attempts)):
                slots.append(
                    SlotKey(
                        stage=str(
                            ((job.get("config") or {}).get("env") or {}).get("__validation_stage")
                            or job.get("stage")
                            or "agent"
                        ),
                        family=str(job.get("family") or job.get("model") or "unknown").split("/")[0],
                        build=str(job.get("modelBuild") or job.get("model") or "unknown"),
                        step=str(step),
                        ordinal=i,
                    )
                )
    return Plan(
        slots=tuple(slots),
        strongest=tuple(strongest),
        steps=tuple(steps),
        categories=tuple(categories),
    )


def build_rows(jobs: list[dict], trials_by_job: dict[str, list[dict]], plan: Plan) -> list[Row]:
    rows: list[Row] = []
    for job in jobs:
        ok, _ = is_participant(job)
        if not ok:
            continue
        seen: Counter[tuple] = Counter()
        for trial in trials_by_job.get(str(job.get("id")), []):
            base = (job.get("id"), trial.get("step") or "1")
            ordinal = seen[base]
            seen[base] += 1
            slot = slot_of(job, trial, ordinal)
            kind, note = classify(trial, graded_ok=graded_pass(trial))
            rows.append(
                Row(
                    slot=slot,
                    kind=kind,
                    job_id=str(job.get("id") or ""),
                    trial_id=str(trial.get("id") or ""),
                    generation=int(job.get("generation") or 0),
                    superseded=bool(trial.get("superseded")),
                    decisions=tuple(sorted(trial.get("decisions") or ())),
                    category=str(trial.get("category") or ""),
                    note=note,
                )
            )
    return rows


# --- evidence ---------------------------------------------------------------


def evidence(trials: list[dict]) -> dict:
    """Failing-test evidence across trials.

    `sharedFailures` and `discriminatorSet` are `None` -- not `[]` -- whenever
    any failing trial did not name its failures, because the unnamed ones might
    be exactly the tests every other failing trial shares. Publishing `[]` there
    states as fact something nothing measured.
    """
    failing_sets: list[set[str]] = []
    complete = True
    scored = 0
    for t in trials:
        if graded_pass(t):
            scored += 1
            continue
        names = t.get("failingTests")
        if names is None:
            complete = False
            continue
        scored += 1
        failing_sets.append(set(names))

    if not complete or not failing_sets:
        return {
            "evidenceComplete": complete and bool(failing_sets),
            "scored": scored,
            "trialsFound": len(trials),
            "sharedFailures": None,
            "discriminatorSet": None,
            "unionFailures": None,
            "topFailure": None,
            "topFailureSoleBlockerShare": 0.0,
            "concentrated": False,
            "failureFrequency": {},
        }

    shared = set.intersection(*failing_sets)
    union: set[str] = set().union(*failing_sets)
    freq = Counter(name for s in failing_sets for name in s)

    # topFailure is the SOLE-BLOCKER argmax, not the frequency leader. A test can
    # head the frequency table and never once be the only thing between a trial
    # and a pass; the sole blocker is the finding every time.
    sole = Counter(next(iter(s)) for s in failing_sets if len(s) == 1)
    top, share = (None, 0.0)
    if sole:
        top, n = sole.most_common(1)[0]
        share = n / len(failing_sets)

    return {
        "evidenceComplete": True,
        "scored": scored,
        "trialsFound": len(trials),
        "sharedFailures": sorted(shared),
        "discriminatorSet": sorted(union - shared),
        "unionFailures": sorted(union),
        "topFailure": top,
        "topFailureSoleBlockerShare": round(share, 4),
        "concentrated": share > 0.5,
        "failureFrequency": dict(freq.most_common()),
    }


def infra_fraction(rows: list[Row]) -> dict:
    """Scored vs errored. The published rate must quote the scored denominator."""
    live = [r for r in rows if not r.superseded]
    errored = [r for r in live if r.kind is Kind.F]
    unknown = [r for r in live if r.kind is Kind.G]
    scored = [r for r in live if r.counts]
    total = len(live)
    return {
        "trialsFound": total,
        "scored": len(scored),
        "errored": len(errored),
        "unknown": len(unknown),
        "infraFraction": round(len(errored) / total, 4) if total else 0.0,
        # A measurement where nothing was scored is not a rate of zero; it is the
        # absence of a measurement, and must never gate.
        "balanceIsMeasurement": bool(scored),
    }


# --- reviews ----------------------------------------------------------------

REQUIRED_REVIEWS = ("tbr", "agentic-full-task")


def review_manifest(task: dict, jobs: list[dict], active_sha: str, required=REQUIRED_REVIEWS) -> list[Review]:
    """One row per required review. A missing row is a missing pass, not a silence."""
    rows: list[Review] = []

    tbr = task.get("qualitativeResult") or {}
    rows.append(
        Review(
            name="tbr",
            state="completed" if tbr.get("overall") else ("errored" if tbr.get("error") else "absent"),
            verdict=str(tbr.get("verdict") or tbr.get("overall") or ""),
            job_id=str(tbr.get("jobId") or ""),
            reviewed_sha=str(tbr.get("commitSha") or task.get("validationCommitSha") or ""),
            selection="exact-head"
            if str(tbr.get("commitSha") or task.get("validationCommitSha") or "") == active_sha
            else "fallback",
            stale=str(task.get("headCommitSha") or "") != active_sha,
        )
    )

    full = None
    for job in jobs:
        env = (job.get("config") or {}).get("env") or {}
        if str(env.get("__validation_stage") or "").lower() == "agentic-review":
            full = job
            break
    review = ((full or {}).get("validation") or {}).get("review") or (full or {}).get("review") or {}
    summary = (review.get("rubrics") or {}).get("summary") or {}
    n_pass, n_total = summary.get("nPassed"), summary.get("nTotal")
    verdict = str(review.get("verdict") or "")
    if verdict == "GOOD" and not (n_pass == n_total and n_total):
        verdict = f"GOOD but {n_pass}/{n_total}"
    rows.append(
        Review(
            name="agentic-full-task",
            state=str((full or {}).get("state") or ("absent" if full is None else "pending")),
            verdict=verdict,
            job_id=str((full or {}).get("id") or ""),
            reviewed_sha=str(((full or {}).get("task") or {}).get("commitSha") or ""),
            selection="exact-head"
            if str(((full or {}).get("task") or {}).get("commitSha") or "") == active_sha
            else "fallback",
            stale=bool(review.get("stale")),
        )
    )

    known = {r.name for r in rows}
    rows.extend(Review(name=n, state="absent") for n in required if n not in known)
    return rows


def build(task: dict, jobs: list[dict], trials_by_job: dict, *, strongest, steps, active_sha, categories=()):
    plan = build_plan(jobs, strongest=strongest, steps=steps, categories=categories)
    rows = build_rows(jobs, trials_by_job, plan)
    return Measurement(
        plan=plan,
        rows=rows,
        active_sha=active_sha,
        reviews=review_manifest(task, jobs, active_sha),
    )
