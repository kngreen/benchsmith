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

# A trial rejected BEFORE the grader ran is not a failure -- it is a graded
# surface refusing a candidate on something other than behaviour. Measured on a
# live task: 9 of 15 trials never reached the grader because the base test
# pinned a function signature, so any candidate refactoring it could not compile.
# Reported rate 4/15 = 26.7% "in band"; true rate 4/6 = 66.7%, above band. Wrong,
# and flattering -- these inflate the denominator and depress the rate.
_PRE_GRADE = re.compile(
    r"\b(compile|compilation|build fail|does not compile|cannot find|undefined:|"
    r"digest|checksum|manifest mismatch|pinned|pin reject|refus(ed|ing) submitted|"
    r"pre-?grade|never reached the (grader|verifier))\b",
    re.I,
)

_ERRORED = re.compile(
    r"\b(error|errored|infra|timeout|timed.?out|cancell?ed|worker|harness|rate.?limit|"
    r"unavailable|outage|preempt)\b",
    re.I,
)


# `config.agentName` names the HARNESS ("codex", "claude-code", "metacode"), not
# the model family a strongest set is written in terms of ("GPT/Codex",
# "Opus/Claude"). Without this map a frozen strongest set matches nothing.
FAMILY_ALIASES = {
    "codex": "gpt",
    "claude-code": "opus",
    "metacode": "avocado",
    "agent": "opus",
}


def family_of(job: dict) -> str:
    raw = agent_name(job).split("/")[0]
    return FAMILY_ALIASES.get(raw, raw) or "unknown"


def agent_name(job: dict) -> str:
    """The cohort identity. `config.agentName` is the observed field."""
    cfg = job.get("config") or {}
    return str(cfg.get("agentName") or job.get("agentName") or job.get("model") or job.get("family") or "")


def job_stage(job: dict) -> str:
    """Validation stage. `reviewIdentity.stage` is the observed field."""
    review_identity = job.get("reviewIdentity") or {}
    env = (job.get("config") or {}).get("env") or {}
    return str(
        review_identity.get("stage")
        or env.get("__validation_stage")
        or job.get("stage")
        or job.get("validationStage")
        or ""
    ).lower()


def job_commit(job: dict) -> str:
    cfg = job.get("config") or {}
    return str(cfg.get("commitSha") or job.get("commitSha") or "")


def stats_errored(job: dict) -> int:
    """Authoritative errored count. Trial rows cannot be trusted for this.

    A live trial that died in infra often still reports `status: "completed"`
    with `reward: 0`, indistinguishable at trial level from a genuine failure.
    The job's own `stats` block counts them correctly.
    """
    st = job.get("stats") or {}
    try:
        return int(st.get("errored") or 0)
    except (TypeError, ValueError):
        return 0


def dead_cohort(job: dict) -> bool:
    """Every trial errored -- the cohort measured nothing at all."""
    st = job.get("stats") or {}
    completed = st.get("completed") or 0
    return bool(completed) and stats_errored(job) >= completed


def suspect_infra(trial: dict) -> str:
    """A tell that a 'completed' zero-reward trial never actually ran.

    Two signals, both weak alone and strong together: the trial scored exactly
    the untouched baseline, and it made no tool calls. Neither proves infra --
    a no-op agent looks the same -- so this returns a REASON, and the caller
    classifies G (unknown), never A. Banking an unexamined zero as difficulty is
    the failure this whole module exists to prevent; guessing the other way and
    calling it infra would be the same mistake mirrored.
    """
    hrs = trial.get("harborResultSummary") or {}
    calls = hrs.get("agentToolCalls")
    if calls is None:
        calls = trial.get("n_tool_calls")
    baseline = trial.get("baselineResult")
    reward = trial.get("reward")
    tells = []
    if calls == 0:
        tells.append("zero tool calls")
    if baseline is not None and reward is not None and baseline == reward:
        tells.append("scored exactly the baseline")
    return " and ".join(tells) if len(tells) >= 2 else ""


def is_participant(job: dict) -> tuple[bool, str]:
    """Admit a job as participant, or say which predicate rejected it."""
    blob = " ".join((job_stage(job), str(job.get("kind") or job.get("type") or "").lower(),
                     str(job.get("name") or "").lower()))
    for marker in NON_PARTICIPANT:
        if marker in blob:
            return (False, f"stage/kind matches {marker!r}")
    if not agent_name(job):
        return (False, "no participant agent on the job")
    return (True, "")


def select_jobs(jobs: list[dict], validation_sha: str) -> tuple[list[dict], list[str]]:
    """The participant jobs for one exact SHA — newest batch per cohort.

    **Never derive cohort rates from the task record's headline fields.** Those
    carry `agentPassCount/Rate`, `metacodePassCount/Rate` and `avocadoPass*` and
    have **no codex field at all**, so a task where codex ran and saturated reads
    as though that cohort never existed. Measured on
    `ollo-behavior-log-anonymization @ 2e4a9850`: the headline fields give
    2/5 + 0/5 = 20% pooled, while the SHA-scoped jobs give 7/15 = 46% because
    codex went 5/5. That is the difference between "in band" and "a saturated
    strongest cohort, reject".

    Returns the selected jobs plus notes about what was dropped and why.
    """
    notes: list[str] = []
    selected: dict[str, dict] = {}
    live: list[dict] = []
    for job in jobs:
        ok, why = is_participant(job)
        if not ok:
            notes.append(f"excluded {job.get('id')}: {why}")
            continue
        if str(job.get("status") or "").lower() != "completed":
            notes.append(f"excluded {job.get('id')}: status {job.get('status')!r}, not completed")
            continue
        commit = job_commit(job)
        if validation_sha and commit and commit != validation_sha:
            notes.append(f"excluded {job.get('id')}: commit {commit[:8]} != {validation_sha[:8]}")
            continue
        live.append(job)

    if not live:
        return ([], notes)

    # The platform dispatches whole WAVES keyed by config.revalidateRunId, not
    # cohort by cohort. Deduping per-cohort on createdAt can pair cohort A's
    # wave 2 with cohort B's wave 1 -- mixing populations and halving the sample
    # without saying so. Pick one wave, then one job per cohort inside it.
    waves: dict[str, list[dict]] = defaultdict(list)
    for job in live:
        waves[str((job.get("config") or {}).get("revalidateRunId") or "")].append(job)

    if len(waves) > 1 or (len(waves) == 1 and "" not in waves):
        def rank(item):
            wid, jobs_in = item
            return (len({agent_name(j) for j in jobs_in}), max(str(j.get("createdAt") or "") for j in jobs_in))

        wave_id, chosen_wave = max(waves.items(), key=rank)
        for wid, jobs_in in waves.items():
            if wid != wave_id:
                for j in jobs_in:
                    notes.append(f"excluded {j.get('id')}: wave {wid or '<none>'} superseded by {wave_id}")
        live = chosen_wave

    for job in live:
        name = agent_name(job)
        prior = selected.get(name)
        if prior is None or str(job.get("createdAt") or "") > str(prior.get("createdAt") or ""):
            if prior is not None:
                notes.append(f"excluded {prior.get('id')}: older batch for cohort {name}")
            selected[name] = job
        else:
            notes.append(f"excluded {job.get('id')}: older batch for cohort {name}")
    return (list(selected.values()), notes)


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
    # Live trials carry `exceptionInfo` at top level and the detail under
    # `harborResultSummary`. Reading only failureReason/error meant _ERRORED
    # never matched and every infra abort classified as a semantic failure --
    # precisely the failed-vs-never-ran collapse this module opens by warning of.
    hrs = trial.get("harborResultSummary") or {}
    reason = " ".join(
        str(x)
        for x in (
            trial.get("exceptionInfo"),
            hrs.get("exceptionType"),
            hrs.get("exceptionMessage"),
            trial.get("failureReason"),
            trial.get("error"),
        )
        if x
    )

    if trial.get("specDefect") or trial.get("ambiguity"):
        return (Kind.E, "spec ambiguity or defect")
    if trial.get("validAlternative") or trial.get("graderFalseNegative"):
        return (Kind.D, "valid alternative rejected")
    if trial.get("preGradeRejected") or _PRE_GRADE.search(reason):
        # The grader refused the candidate on something other than behaviour.
        # D, not A: it invalidates the measurement rather than counting as
        # difficulty, because the denominator it sits in is not a denominator.
        return (Kind.D, f"rejected before grading: {reason[:100]}")
    if _ERRORED.search(status) or _ERRORED.search(reason):
        return (Kind.F, f"infrastructure: {status or reason}"[:120])
    if graded_ok:
        return (Kind.PASS, "every graded assertion passed")
    # Nothing passed at all, on a suite that includes preserved-guarantee tests
    # which pass at base by construction. If those are failing the package did not
    # build -- three trials once read as total capability failure when the gold
    # file had simply broken its own compilation.
    ctrf_sum = ((trial.get("ctrfResults") or {}).get("summary")) or {}
    if ctrf_sum.get("tests") and ctrf_sum.get("passed") == 0:
        return (
            Kind.F,
            f"0 of {ctrf_sum['tests']} tests passed: preserved-guarantee tests pass at base, "
            "so a total zero is a build failure, not behaviour — check compile stderr",
        )

    tell = suspect_infra(trial)
    if tell:
        return (Kind.G, f"zero reward but {tell}: attribution unresolved, investigate")
    if trial.get("reachedVerifier") or reward is not None or trial.get("ctrfResults"):
        return (Kind.A, "verifier ran and reported wrong behaviour")
    if status in {"failed", "fail", "error"} and not reason:
        return (Kind.G, "no attribution available")
    if status:
        return (Kind.B, f"candidate failure before the verifier: {status}"[:120])
    return (Kind.G, "no status")


def failing_tests(trial: dict) -> list[str] | None:
    """Named failures for one trial, or None when they are unknowable.

    Live shape is `ctrfResults.tests[]`; the downloaded artifact is
    `verifier/output.json` with UPPERCASE statuses and no summary key. None means
    the trial reported failures but named none -- never an empty list.
    """
    ctrf = trial.get("ctrfResults") or {}
    tests = ctrf.get("tests") or trial.get("tests")
    summary = ctrf.get("summary") or {}
    declared = summary.get("failed")
    if not tests:
        return None
    out = []
    for entry in tests:
        name = entry.get("name") or entry.get("id")
        status = str(entry.get("status") or "").upper()
        if name and status in {"FAIL", "FAILED", "ERROR"}:
            out.append(str(name))
    # `trial list`'s ctrfResults is a PROJECTION: the summary counts are real but
    # every per-test status can come back "skipped". Observed live -- summary
    # {tests:11, passed:10, failed:1} with not one FAILED entry in tests[].
    # Returning [] there would publish "no test failed" as a fact, which is the
    # empty-vs-unknown rule this module opens with, violated in its own code.
    # The names live in the artifact: trial artifacts <id> --key verifier/ctrf.json.
    if declared and not out:
        return None
    return out


def blocker_concentration(trials: list[dict]) -> dict:
    """Do the failing trials all die on the same small number of assertions?

    Computable from ctrf SUMMARIES alone, so it works even when the projection
    hides per-test names. A cohort where every failure is `N-1 of N` is the
    signature of one assertion gating the cohort -- not of a capability gap.
    Observed live: five gpt trials, each 10 of 11, binary reward zero for all
    five, while avocado passed 5/5 on the same graded surface.
    """
    shapes, near_miss, failing = [], 0, 0
    for t in trials:
        summary = ((t.get("ctrfResults") or {}).get("summary")) or {}
        total, failed = summary.get("tests"), summary.get("failed")
        if not total or not failed:
            continue
        failing += 1
        shapes.append((int(failed), int(total)))
        if int(failed) <= max(1, int(total) // 10):
            near_miss += 1
    if not failing:
        return {"failingTrials": 0, "uniformShape": None, "nearMissShare": 0.0, "concentrated": False}
    uniform = shapes[0] if len(set(shapes)) == 1 else None
    share = near_miss / failing
    return {
        "failingTrials": failing,
        "uniformShape": (f"{uniform[0]}/{uniform[1]} failed" if uniform else None),
        "nearMissShare": round(share, 4),
        # Every failing trial missing by a hair, all with the same shape, is a
        # blocker signal rather than a difficulty signal.
        "concentrated": bool(uniform) and share == 1.0,
    }


def graded_pass(trial: dict) -> bool:
    """PASS means every element of the graded set passed. Partial credit is not a pass."""
    if trial.get("gradedPass") is not None:
        return bool(trial["gradedPass"])
    ctrf = trial.get("ctrfResults") or {}
    summary = ctrf.get("summary") or {}
    if summary:
        return bool(summary.get("tests")) and not (summary.get("failed") or 0)
    reward = trial.get("reward")
    if isinstance(reward, dict):
        return bool(reward) and all(float(v) >= 1.0 for v in reward.values())
    if reward is None:
        return False
    try:
        return float(reward) >= 1.0
    except (TypeError, ValueError):
        return False


def model_build(job: dict, trial: dict | None = None) -> str:
    """The exact build. Prefer the TRIAL's modelName over the job's.

    They differ on point releases: a job declaring `meta/avocado-code-flex` runs
    trials of `meta/avocado-code-flex-5.16`. §5 freezes exact builds and pooling
    requires identical ones, so taking the job's value collapses two releases
    into one slot key.
    """
    if trial and trial.get("modelName"):
        return str(trial["modelName"])
    return _job_build(job)


def _job_build(job: dict) -> str:
    """The exact build. `config.modelName` is the live field.

    agentName and modelName differ -- agentName 'metacode' runs modelName
    'meta/avocado-code-flex' -- so family comes from the agent and build from the
    model. Collapsing them merges distinct builds into one slot.
    """
    cfg = job.get("config") or {}
    return str(cfg.get("modelName") or job.get("modelVersion") or agent_name(job) or "unknown")


def generation_of(job: dict) -> int:
    """Which dispatch this row belongs to. Absent as `generation` on live jobs."""
    cfg = job.get("config") or {}
    for key in (cfg.get("revalidateRunId"), job.get("supersededMultistepAttempt")):
        try:
            if key is not None:
                return int(key)
        except (TypeError, ValueError):
            return 1
    return 0


def slot_of(job: dict, trial: dict, ordinal: int) -> SlotKey:
    return SlotKey(
        stage=job_stage(job) or "agent",
        family=family_of(job),
        build=model_build(job, trial),
        step=str(trial.get("step") or trial.get("stepId") or "1"),
        ordinal=ordinal,
    )


def build_plan(jobs: list[dict], *, strongest, steps, categories=(), trials_by_job=None) -> Plan:
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
        cfg = job.get("config") or {}
        # `config.nAttempts` is the live field. Everything else here is a
        # fallback for other surfaces; probing the wrong name made build_plan
        # raise on every real job.
        # `plannedNAttempts` first: the doctrine is planned-vs-observed and
        # `nAttempts` is what the job ran. Both are needed -- observed on a real
        # swe-bench-pro task, completed participant jobs carry both (5/5), while
        # the agentic-review job has nAttempts=1 and plannedNAttempts null.
        attempts = (
            cfg.get("plannedNAttempts")
            or cfg.get("nAttempts")
            or job.get("plannedAttempts")
            or job.get("attempts")
            or cfg.get("numAttempts")
            or job.get("k")
        )
        if not attempts:
            raise ValueError(
                f"job {job.get('id')} does not declare its planned attempt count; "
                "cannot freeze a slot plan from it"
            )
        # Resolve the build label the same way rows will, or the plan and the
        # rows key on different strings and every slot reads as unobserved. The
        # COUNT still comes from config -- only the label comes from a trial.
        sample = (trials_by_job or {}).get(str(job.get("id")), [])
        build_label = model_build(job, sample[0] if sample else None)
        for step in steps or ("1",):
            for i in range(int(attempts)):
                slots.append(
                    SlotKey(
                        stage=job_stage(job) or "agent",
                        family=family_of(job),
                        build=build_label,
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
                    generation=generation_of(job),
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
            names = failing_tests(t)
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
            "blocker": blocker_concentration(trials),
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
        "blocker": blocker_concentration(trials),
        "trialsFound": len(trials),
        "sharedFailures": sorted(shared),
        "discriminatorSet": sorted(union - shared),
        "unionFailures": sorted(union),
        "topFailure": top,
        "topFailureSoleBlockerShare": round(share, 4),
        "concentrated": share > 0.5,
        "failureFrequency": dict(freq.most_common()),
    }


def infra_fraction(rows: list[Row], jobs: list[dict] | None = None) -> dict:
    """Scored vs errored. The published rate must quote the scored denominator."""
    live = [r for r in rows if not r.superseded]
    errored = [r for r in live if r.kind is Kind.F]
    unknown = [r for r in live if r.kind is Kind.G]
    scored = [r for r in live if r.counts]
    total = len(live)
    job_errored = sum(stats_errored(j) for j in (jobs or []))
    dead = [agent_name(j) for j in (jobs or []) if dead_cohort(j)]
    return {
        "jobStatsErrored": job_errored,
        "rowErrored": len(errored),
        # A disagreement means trial rows are hiding infra the job counted.
        "erroredDisagreement": bool(jobs) and job_errored != len(errored),
        "deadCohorts": dead,
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

REQUIRED_REVIEWS = ("tbr", "agentic-full-task", "quality", "review-critic")

REVIEW_PASSING = {
    "tbr": frozenset({"pass"}),
    "agentic-full-task": frozenset({"GOOD"}),
    "quality": frozenset({"GOOD", "Accept"}),
    # codimango-review-critic returns exactly one of Accept / Request changes /
    # Reject. It is the only required review the platform does not produce, so
    # its row is supplied by the operator rather than parsed from a payload.
    "review-critic": frozenset({"Accept"}),
}


def _tbr_row(task: dict, active_sha: str) -> Review:
    """TBR is `tbdReviewStatus` / `tbdReviewDetails` -- NOT `qualitativeResult`.

    Reading `qualitativeResult` here produced a false green on real data: a task
    whose true `tbdReviewStatus` was "fail" reported Accept, because the quality
    assessment was GOOD. That is worse than a crash, because it is silent.
    """
    status = str(task.get("tbdReviewStatus") or "")
    details = task.get("tbdReviewDetails") or {}
    # `instance_id` embeds the short SHA, which is the only revision binding this
    # payload offers. Without it the row cannot claim exact-head.
    instance = str(details.get("instance_id") or "")
    bound = bool(active_sha) and active_sha[:7] in instance
    return Review(
        name="tbr",
        state="completed" if status else "absent",
        verdict=status,
        reviewed_sha=active_sha if bound else "",
        selection="exact-head" if bound else "fallback",
        stale=bool(task.get("tbrNotFinalized")),
    )


def _quality_row(task: dict, active_sha: str) -> Review:
    """The AI quality assessment -- a real gate, but a different one from TBR."""
    q = task.get("qualitativeResult") or {}
    sha = str(task.get("validationCommitSha") or "")
    return Review(
        name="quality",
        state="completed" if q.get("overall") else ("errored" if q.get("error") else "absent"),
        verdict=str(q.get("verdict") or q.get("overall") or ""),
        reviewed_sha=sha,
        selection="exact-head" if sha and sha == active_sha else "fallback",
        # Deliberately not derived from task.headCommitSha: in a multi-task
        # monorepo that is the REPO branch head, which moves whenever any other
        # task commits. Using it marked nearly every task stale.
        stale=False,
    )


def _agentic_row(jobs: list[dict], active_sha: str) -> tuple[Review, list[str]]:
    """The Agentic Full-Task Review lives at `job.agenticReview` on a job row.

    Not `job.review`, not `job.validation.review` -- both absent on live jobs.
    Liveness is `job.status`, and the revision is `job.config.commitSha`; reading
    `job.state` and `job.task.commitSha` made a completed GOOD/exact-head review
    render as pending/fallback, so the gate could never go green.
    """
    found = None
    for job in jobs:
        if job_stage(job) == "agentic-review":
            found = job
            break
    if found is None:
        return (Review(name="agentic-full-task", state="absent"), [])

    review = found.get("agenticReview") or {}
    rubrics = review.get("rubrics") or {}
    summary = rubrics.get("summary") or {}
    n_pass, n_total = summary.get("nPassed"), summary.get("nTotal")
    verdict = str(review.get("verdict") or "")
    # A bare GOOD with a failed rubric is not a pass: the one FAIL is often
    # exactly the difficulty signal §5 cares about.
    failed = [
        f"{i.get('id')} ({i.get('focus') or i.get('dimension')})"
        for i in (rubrics.get("items") or [])
        if str(i.get("verdict") or "").upper() == "FAIL"
    ]
    if verdict == "GOOD" and n_total and n_pass != n_total:
        verdict = f"GOOD but {n_pass}/{n_total}"
    sha = str((found.get("config") or {}).get("commitSha") or "")
    return (
        Review(
            name="agentic-full-task",
            state=str(found.get("status") or "pending"),
            verdict=verdict,
            job_id=str(found.get("id") or ""),
            reviewed_sha=sha,
            selection="exact-head" if sha and sha == active_sha else "fallback",
            stale=bool(review.get("stale")),
        ),
        failed,
    )


def review_manifest(task: dict, jobs: list[dict], active_sha: str, required=REQUIRED_REVIEWS):
    """One row per required review. A missing row is a missing pass, not silence."""
    agentic, failed_rubrics = _agentic_row(jobs, active_sha)
    rows = [_tbr_row(task, active_sha), agentic, _quality_row(task, active_sha)]
    if failed_rubrics:
        agentic.verdict = f"{agentic.verdict} [FAIL: {', '.join(failed_rubrics)}]"
    known = {r.name for r in rows}
    rows.extend(Review(name=n, state="absent") for n in required if n not in known)
    return rows


def build(task: dict, jobs: list[dict], trials_by_job: dict, *, strongest, steps, active_sha, categories=()):
    """Assemble a Measurement from platform records.

    Job selection is SHA-scoped and cohort-deduplicated first: the task record's
    headline pass fields are never a source of rates (see `select_jobs`).
    """
    validation_sha = str(task.get("validationCommitSha") or active_sha or "")
    chosen, notes = select_jobs(jobs, validation_sha)
    plan = build_plan(
        chosen, strongest=strongest, steps=steps, categories=categories, trials_by_job=trials_by_job
    )
    rows = build_rows(chosen, trials_by_job, plan)
    m = Measurement(
        plan=plan,
        rows=rows,
        active_sha=active_sha,
        reviews=review_manifest(task, jobs, active_sha),
    )
    m.selection_notes = notes
    return m
