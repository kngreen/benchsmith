#!/usr/bin/env python3
"""Regression fixtures for benchsmith.

Every case here is a failure that actually happened — in a reported run, in a
review of this skill, or in another loop's incident log. A case with no incident
behind it does not belong in this file.

    python3 lib/selftest.py [-v]

Exit 0 all pass, 1 a failure. No network, no platform, no task repo: everything
runs on scratch under $TMPDIR.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchsmith import gate as gate_mod  # noqa: E402
from benchsmith.adapter import Identity, IdentityMismatch, Platform, Surface  # noqa: E402
from benchsmith.bar import completeness, evaluate, single_gate_share, wilson  # noqa: E402
from benchsmith.journal import Journal, surface_hashes  # noqa: E402
from benchsmith.model import Kind, Measurement, Plan, Review, Row, SlotKey  # noqa: E402
from benchsmith.replacement import ReplacementRefused, apply, eligible, plan  # noqa: E402
from benchsmith.snapshot import classify, evidence, graded_pass, is_participant  # noqa: E402

VERBOSE = "-v" in sys.argv
PASSED = FAILED = 0


def check(name: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        if VERBOSE:
            print(f"  ok    {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}\n        want {want!r}\n        got  {got!r}")


def section(title: str) -> None:
    if VERBOSE:
        print(f"\n{title}")


def slots(family="gpt", build="b1", n=5, step="1", stage="agent"):
    return [SlotKey(stage, family, build, step, i) for i in range(n)]


def two_family_plan(n=5, steps=("1",)):
    s = [k for f, b in (("gpt", "b1"), ("opus", "b2")) for st in steps for k in slots(f, b, n, st)]
    return Plan(
        slots=tuple(s),
        strongest=(("gpt", "b1"), ("opus", "b2")),
        steps=tuple(steps),
        categories=("x", "y"),
    )


def measurement(passes: dict, *, plan_=None, one_category=False, steps=("1",)):
    p = plan_ or two_family_plan(steps=steps)
    rows = []
    for k in p.slots:
        kind = Kind.PASS if k.ordinal < passes[k.family] else Kind.A
        cat = "x" if (one_category or k.family == "gpt") else "y"
        rows.append(Row(slot=k, kind=kind, category=cat, decisions=(f"{k.family}{k.ordinal}",)))
    return Measurement(plan=p, rows=rows, active_sha="sha")


# ---------------------------------------------------------------- Wilson ----
section("Wilson — Wald is degenerate exactly where five-trial cohorts live")

check("0/5 lower", wilson(0, 5)[0], 0.0)
check("0/5 upper stays informative", round(wilson(0, 5)[1], 3), 0.434)
check("5/5 lower stays informative", round(wilson(5, 5)[0], 3), 0.566)
check("5/5 upper", wilson(5, 5)[1], 1.0)
check("3/10", tuple(round(x, 3) for x in wilson(3, 10)), (0.108, 0.603))
try:
    wilson(0, 0)
    check("no denominator raises", False, True)
except ValueError:
    check("no denominator raises", True, True)


# --------------------------------------------------- zero-row job / slots ----
section("Zero-row jobs — a planned slot with no rows is incomplete, not absent")

p = two_family_plan()
m = Measurement(plan=p, rows=[], active_sha="sha")
check("empty measurement is incomplete", completeness(m)[0], False)
check("every planned slot named", len(completeness(m)[1]), 10)
r = evaluate(m)
check("no rate emitted", r["verdict"], "NO RATE")
check("runsIncomplete populated", len(r["runsIncomplete"]), 10)
check("rate is None, not zero", r["rate"], None)

half = [Row(slot=k, kind=Kind.PASS) for k in p.slots if k.family == "gpt"]
r = evaluate(Measurement(plan=p, rows=half, active_sha="sha"))
check("half-arrived cohort does not shrink the denominator", r["verdict"], "NO RATE")
check("missing cohort reported", len(r["runsIncomplete"]), 5)

allf = Measurement(plan=p, rows=[Row(slot=k, kind=Kind.F) for k in p.slots], active_sha="sha")
check("all-infra is incomplete", completeness(allf)[0], False)
check("F named as non-calibration-bearing", "no calibration-bearing" in completeness(allf)[1][0], True)

dupe = Measurement(
    plan=Plan(slots=tuple(slots(n=1)), strongest=(("gpt", "b1"),), steps=("1",)),
    rows=[Row(slot=slots(n=1)[0], kind=Kind.PASS), Row(slot=slots(n=1)[0], kind=Kind.A)],
    active_sha="sha",
)
check("two counting rows for one slot is incomplete", completeness(dupe)[0], False)

try:
    Plan(slots=())
    check("empty plan refused", False, True)
except ValueError:
    check("empty plan refused", True, True)


# ------------------------------------------------------------ the band ------
section("The band")

check("in band, mixed, two families, two categories", evaluate(measurement({"gpt": 2, "opus": 2}))["verdict"], "HARD")
check("no findings when hard", evaluate(measurement({"gpt": 2, "opus": 2}))["findings"], [])

easy = evaluate(measurement({"gpt": 5, "opus": 4}))
check("9/10 is not hard", easy["verdict"], "NOT HARD")
check("band named", "band" in {f["code"] for f in easy["findings"]}, True)
check("saturation named", "strongest-not-mixed" in {f["code"] for f in easy["findings"]}, True)

split = evaluate(measurement({"gpt": 5, "opus": 0}))
check("in-band overall but split cohorts is not hard", split["verdict"], "NOT HARD")

check("above band with one member mixed -> MEDIUM", evaluate(measurement({"gpt": 3, "opus": 4}))["verdict"], "MEDIUM")
check(
    "hard-only refuses medium",
    evaluate(measurement({"gpt": 3, "opus": 4}), target="hard-only")["verdict"],
    "NOT HARD",
)

m = measurement({"gpt": 3, "opus": 4})
m.reviews = [Review(name="tbr", state="completed", verdict="fail", reviewed_sha="sha")]
check("a failing review is never downgraded to medium", evaluate(m)["verdict"], "NOT HARD")

m = measurement({"gpt": 2, "opus": 2})
m.rows[-1] = Row(slot=m.rows[-1].slot, kind=Kind.C, category="y")
check("an unrelated candidate failure blocks hard", evaluate(m)["verdict"], "NOT HARD")

p10 = Plan(slots=tuple(slots(n=10)), strongest=(("gpt", "b1"),), steps=("1",))
m = Measurement(
    plan=p10,
    rows=[Row(slot=k, kind=Kind.PASS if k.ordinal < 6 else Kind.A, category="x") for k in p10.slots],
    active_sha="sha",
)
check("0.6 is boundary-adjacent", evaluate(m)["boundaryAdjacent"], True)
check("distance reported", evaluate(m)["distanceToBand"], 0.1)


# ------------------------------------------------- D/E and single gate ------
section("Invalidation and single-gate kill")

p1 = Plan(slots=tuple(slots(n=5)), strongest=(("gpt", "b1"),), steps=("1",))
m = Measurement(
    plan=p1,
    rows=[Row(slot=k, kind=Kind.E if k.ordinal == 0 else Kind.A, note="ambiguous") for k in p1.slots],
    active_sha="sha",
)
check("one E invalidates", evaluate(m)["verdict"], "NO RATE")
check("and says why", evaluate(m)["findings"][0]["code"], "invalidated")

p = two_family_plan()
same = Measurement(
    plan=p,
    rows=[
        Row(slot=k, kind=Kind.PASS if k.ordinal < 2 else Kind.A, category="x", decisions=("one",))
        for k in p.slots
    ],
    active_sha="sha",
)
check("one decision explains everything", single_gate_share(same), 1.0)
check("single-gate kill fires", "single-gate" in {f["code"] for f in evaluate(same)["findings"]}, True)
check(
    "one category cannot satisfy two",
    "one-category" in {f["code"] for f in evaluate(measurement({"gpt": 2, "opus": 2}, one_category=True))["findings"]},
    True,
)

st = evaluate(measurement({"gpt": 2, "opus": 2}, steps=("1", "2")))
check("two steps both covered", st["verdict"], "HARD")


# --------------------------------------------------------- stale gates ------
section("Stale reviews — absence of a verdict is never a weak pass")

base = measurement({"gpt": 2, "opus": 2})
# The real contract: TBR's passing verdict is the literal "pass" from
# task.tbdReviewStatus -- NOT "Accept", which belongs to the separate AI quality
# assessment. Conflating them produced a false green on live data.
good = [
    Review(name="tbr", state="completed", verdict="pass", reviewed_sha="sha"),
    Review(name="agentic-full-task", state="completed", verdict="GOOD", reviewed_sha="sha"),
    Review(name="quality", state="completed", verdict="Accept", reviewed_sha="sha"),
]
base.reviews = [Review(**r.__dict__) for r in good]
check("green reviews on the active sha", evaluate(base)["verdict"], "HARD")

for mutate, label in (
    ({"reviewed_sha": "older"}, "review on another sha"),
    ({"selection": "fallback"}, "fallback report"),
    ({"state": "pending"}, "pending"),
    ({"state": "errored"}, "errored"),
    ({"stale": True}, "stale"),
    ({"verdict": "BAD"}, "BAD verdict"),
    ({"verdict": "GOOD but 16/17"}, "16 of 17"),
):
    m = measurement({"gpt": 2, "opus": 2})
    m.reviews = [Review(**r.__dict__) for r in good]
    for k, v in mutate.items():
        setattr(m.reviews[1], k, v)
    check(f"{label} is not a pass", evaluate(m)["verdict"], "NOT HARD")

m = measurement({"gpt": 2, "opus": 2})
m.reviews = [Review(name="agentic-full-task", state="absent")]
check("an absent review is not silence", evaluate(m)["verdict"], "NOT HARD")


# -------------------------------------------------- name collision / CLI ----
section("Name collisions and CLI drift")

surface = Surface(binary="x", task_show=("api", "tasks", "show"), jobs_list=("api", "jobs", "list"))
pf = Platform(surface, Identity(task_name="ollo-auth", task_id="42", task_uuid="u-42"))
try:
    pf.check_identity({"id": "99", "uuid": "u-99"})
    check("id mismatch raises", False, True)
except IdentityMismatch as e:
    check("id mismatch raises", "expected task id 42" in str(e), True)

try:
    pf.check_identity({"id": "42", "uuid": "u-99"})
    check("uuid mismatch raises", False, True)
except IdentityMismatch:
    check("uuid mismatch raises", True, True)

check("matching identity passes", pf.check_identity({"id": "42", "uuid": "u-42"}), None)

bare = Platform(surface, Identity(task_name="ollo-auth"))
try:
    bare.check_identity({"id": "1"})
    check("name-only lookup with no bound id refuses", False, True)
except IdentityMismatch as e:
    check("name-only lookup with no bound id refuses", "cannot be trusted" in str(e), True)

# CLI drift: --no-cache absent must be recorded, never silently assumed.
drifted = Surface(binary="x", task_show=("task", "show"), supports_no_cache=False)
drifted.notes.append("no --no-cache flag; reads may be served from cache")
check("missing --no-cache is recorded", any("no-cache" in n for n in drifted.notes), True)
check("legacy and new shapes both representable", drifted.task_show, ("task", "show"))


# ------------------------------------------------------------- snapshot ----
section("Snapshot — participant admission, classification, evidence")

check("oracle job excluded", is_participant({"stage": "oracle", "model": "gpt"})[0], False)
check("review job excluded", is_participant({"config": {"env": {"__validation_stage": "agentic-review"}}, "model": "x"})[0], False)
check("modelless job excluded", is_participant({"stage": "agent"})[0], False)
check("agent job admitted", is_participant({"stage": "agent", "model": "gpt/b1"})[0], True)

check("partial credit is not a pass", graded_pass({"reward": 0.9}), False)
check("full reward is a pass", graded_pass({"reward": 1.0}), True)
check("dict reward needs every key", graded_pass({"reward": {"a": 1.0, "b": 0.5}}), False)
check("empty dict reward is not a pass", graded_pass({"reward": {}}), False)
check("no reward is not a pass", graded_pass({}), False)

check("errored trial is F", classify({"status": "errored"}, graded_ok=False)[0], Kind.F)
check("timeout is F", classify({"status": "failed", "error": "worker timed out"}, graded_ok=False)[0], Kind.F)
check("spec defect is E", classify({"specDefect": True}, graded_ok=False)[0], Kind.E)
check("valid alternative is D", classify({"validAlternative": True}, graded_ok=False)[0], Kind.D)
check("E outranks F", classify({"specDefect": True, "status": "errored"}, graded_ok=False)[0], Kind.E)
check("graded pass is PASS", classify({"status": "ok"}, graded_ok=True)[0], Kind.PASS)
check("verifier reached is A", classify({"status": "failed", "reachedVerifier": True}, graded_ok=False)[0], Kind.A)
check("unattributed failure is G", classify({"status": "failed"}, graded_ok=False)[0], Kind.G)

# Empty and unknown are different answers.
ev = evidence([{"reward": 0.0, "failingTests": ["a", "b"]}, {"reward": 0.0, "failingTests": ["a"]}])
check("shared failures computed", ev["sharedFailures"], ["a"])
check("discriminator set computed", ev["discriminatorSet"], ["b"])
check("evidence complete", ev["evidenceComplete"], True)

ev = evidence([{"reward": 0.0, "failingTests": ["a"]}, {"reward": 0.0}])
check("unnamed failures make the set unknowable", ev["sharedFailures"], None)
check("not an empty list", ev["sharedFailures"] is None, True)
check("evidenceComplete false", ev["evidenceComplete"], False)

# topFailure is the sole-blocker argmax, not the frequency leader.
ev = evidence(
    [
        {"reward": 0.0, "failingTests": ["freq", "other"]},
        {"reward": 0.0, "failingTests": ["freq", "other2"]},
        {"reward": 0.0, "failingTests": ["sole"]},
    ]
)
check("frequency leader is not topFailure", ev["topFailure"], "sole")
check("frequency table still available", ev["failureFrequency"]["freq"], 2)


# ------------------------------------------------------- job selection -----
section("Job selection — the headline fields lie by omission")

from benchsmith.snapshot import agent_name, job_stage, select_jobs  # noqa: E402

check("agentName is the cohort identity", agent_name({"config": {"agentName": "codex"}}), "codex")
check("reviewIdentity.stage is the stage", job_stage({"reviewIdentity": {"stage": "agentic-review"}}), "agentic-review")
check(
    "agentic-review excluded via reviewIdentity",
    is_participant({"reviewIdentity": {"stage": "agentic-review"}, "config": {"agentName": "x"}})[0],
    False,
)

# The measured case: ollo-behavior-log-anonymization @ 2e4a9850. Headline fields
# expose claude-code 2/5 and metacode 0/5 and have NO codex field, reading 20%
# pooled. Three cohorts actually ran; codex went 5/5, so the true SHA-scoped rate
# is 7/15 = 46% — in-band by the headline, saturated-cohort reject in truth.
SHA = "2e4a9850"
jobs = [
    {"id": "j-agent", "status": "completed", "config": {"agentName": "claude-code", "commitSha": SHA},
     "createdAt": "2026-09-08T01:00:00Z", "attempts": 5},
    {"id": "j-meta", "status": "completed", "config": {"agentName": "metacode", "commitSha": SHA},
     "createdAt": "2026-09-08T01:00:00Z", "attempts": 5},
    {"id": "j-codex", "status": "completed", "config": {"agentName": "codex", "commitSha": SHA},
     "createdAt": "2026-09-08T01:00:00Z", "attempts": 5},
    {"id": "j-review", "status": "completed", "reviewIdentity": {"stage": "agentic-review"},
     "config": {"agentName": "reviewer", "commitSha": SHA}, "createdAt": "2026-09-08T02:00:00Z", "attempts": 1},
    {"id": "j-stale", "status": "completed", "config": {"agentName": "codex", "commitSha": "0ldc0mm1"},
     "createdAt": "2026-09-07T01:00:00Z", "attempts": 5},
    {"id": "j-running", "status": "running", "config": {"agentName": "codex", "commitSha": SHA},
     "createdAt": "2026-09-08T03:00:00Z", "attempts": 5},
]
chosen, notes = select_jobs(jobs, SHA)
check("three participant cohorts selected", sorted(agent_name(j) for j in chosen), ["claude-code", "codex", "metacode"])
from benchsmith.snapshot import family_of  # noqa: E402
check("harness names normalise to model families", sorted(family_of(j) for j in chosen), ["avocado", "gpt", "opus"])
check("codex cohort is not lost", "codex" in {agent_name(j) for j in chosen}, True)
check("agentic-review dropped", "j-review" not in {j["id"] for j in chosen}, True)
check("off-SHA job dropped", any("0ldc0mm1" in n for n in notes), True)
check("non-completed job dropped", any("'running'" in n for n in notes), True)
check("every exclusion is explained", len(notes), 3)

# Newest batch per cohort: an earlier batch at the same SHA is superseded, not extra trials.
rerun = jobs[:3] + [
    {"id": "j-codex-2", "status": "completed", "config": {"agentName": "codex", "commitSha": SHA},
     "createdAt": "2026-09-08T09:00:00Z", "attempts": 5}
]
chosen2, notes2 = select_jobs(rerun, SHA)
check("one job per cohort after a rerun", len(chosen2), 3)
check("newest batch wins", "j-codex-2" in {j["id"] for j in chosen2}, True)
check("older batch named as dropped", any("older batch" in n for n in notes2), True)

# End to end: the omitted cohort must reach the denominator.
trials = {
    "j-agent": [{"reward": 1.0 if i < 2 else 0.0, "reachedVerifier": True} for i in range(5)],
    "j-meta": [{"reward": 0.0, "reachedVerifier": True} for _ in range(5)],
    "j-codex": [{"reward": 1.0} for _ in range(5)],
}
from benchsmith.snapshot import build as build_measurement  # noqa: E402

m = build_measurement(
    {"validationCommitSha": SHA},
    jobs,
    trials,
    # Strongest set is written in model families, which is what family_of yields:
    # agentName "codex" is the harness, family "gpt" is what §5 talks about.
    strongest=[("gpt", "codex"), ("opus", "claude-code")],
    steps=("1",),
    active_sha=SHA,
)
check("denominator is 15, not 10", len(m.plan.slots), 15)
res = evaluate(m)
check("pooled rate counts codex", res["rate"], {"passes": 7, "slots": 15, "p": 0.4667})
check("saturated codex cohort is caught", "strongest-not-mixed" in {f["code"] for f in res["findings"]}, True)
check("verdict is not hard", res["verdict"], "NOT HARD")
check("selection trail retained", len(m.selection_notes), 3)


# ---------------------------------------------------------- replacement ----
section("Replacement — one wave, F only, superseded by slot key")

p = Plan(slots=tuple(slots(n=3) + slots("opus", "b2", 3)), strongest=(("gpt", "b1"),), steps=("1",))
rows = [
    Row(slot=k, kind=(Kind.F if (k.family == "gpt" and k.ordinal == 0) else Kind.A), trial_id=f"t{i}")
    for i, k in enumerate(p.slots)
]
m = Measurement(plan=p, rows=rows, active_sha="sha")
check("only F is eligible", len(eligible(m)), 1)

w = plan(m, capabilities=("slot", "cohort", "generation"), reason="worker loss")
check("narrowest scope chosen", w.scope, "slot")
check("wave is frozen with a digest", len(w.digest), 16)

try:
    plan(m, capabilities=("slot",), reason="again", prior_wave=w)
    check("second wave refused", False, True)
except ReplacementRefused:
    check("second wave refused", True, True)

allA = Measurement(plan=p, rows=[Row(slot=k, kind=Kind.A) for k in p.slots], active_sha="s")
try:
    plan(allA, capabilities=("slot",), reason="reroll")
    check("A/B/C reroll refused", False, True)
except ReplacementRefused as e:
    check("A/B/C reroll refused", "authoritative" in str(e), True)

apply(m, w, [Row(slot=w.slots[0], kind=Kind.PASS)])
check("replacement completes the slot", completeness(m)[0], True)
check("original superseded", sum(1 for r in m.rows if r.superseded), 1)
check("denominator unchanged", evaluate(m)["rate"]["slots"], 6)

m2 = Measurement(
    plan=p,
    rows=[Row(slot=k, kind=(Kind.F if k.family == "gpt" else Kind.A)) for k in p.slots],
    active_sha="s",
)
w2 = plan(m2, capabilities=("cohort", "generation"), reason="cohort loss")
check("falls back to cohort", w2.scope, "cohort")
check("cohort supersedes the whole cohort", len(w2.superseded_slots), 3)

try:
    apply(m2, w2, [Row(slot=SlotKey("agent", "other", "b9", "1", 0), kind=Kind.PASS)])
    check("a wave cannot widen after dispatch", False, True)
except ReplacementRefused:
    check("a wave cannot widen after dispatch", True, True)


# -------------------------------------------------- journal and the gate ----
section("Journal, gate and receipts")


def scratch_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "mytask" / "tests").mkdir(parents=True)
    (repo / "mytask" / "task.toml").write_text(
        'difficulty = "hard"\n[metadata]\ntags = ["aai-labs","aai-labs-ollo",'
        '"benchsmith-v1","semi-synthetic","private_repos_1p"]\n'
    )
    (repo / "mytask" / "tests" / "config.json").write_text(
        json.dumps({"patch": "a", "test_patch": "b", "fail_to_pass": ["t1"], "pass_to_pass": ["t2"]})
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    repo = scratch_repo(tmp)
    task_dir = repo / "mytask"

    h = surface_hashes(task_dir)
    check("graded surface hashed", len(h["gradedHash"]), 32)
    check("absent spec is zero bytes", h["specBytes"], 0)

    j = Journal.open(repo, "mytask")
    j.record(
        task_dir=task_dir,
        sha="aaa",
        cls="too-easy",
        fix="lever",
        measurement={"rate": {"passes": 9, "slots": 10, "p": 0.9}, "distanceToBand": 0.4},
        hardening=True,
    )
    check("measured hardening spends budget", j.data["hardeningSpent"], 1)

    e = j.record(
        task_dir=task_dir,
        sha="bbb",
        cls="in-band",
        fix="none",
        signals={"validationCommitSha": "zzz"},
        measurement={"rate": {"passes": 3, "slots": 10, "p": 0.3}},
    )
    check("cross-commit numbers rewritten", e["class"], "not-measured")
    check("original class preserved", e["classOverriddenFrom"], "in-band")
    check("counts blanked", e["rate"], None)

    e = j.record(task_dir=task_dir, sha="ccc", cls="too-easy", fix="none", hardening=True)
    check("unmeasured difficulty class rewritten", e["class"], "not-measured")
    check("and spends no budget", j.data["hardeningSpent"], 1)

    # `corrective` is outside the {too-easy, in-band} set that the second guard
    # also rewrites, so this isolates the cross-commit guard itself. Without it a
    # mutant that removes that guard survives, masked by the redundant one.
    e = j.record(
        task_dir=task_dir,
        sha="eee",
        cls="corrective",
        fix="fixture",
        signals={"validationCommitSha": "other", "validationStatus": "passing"},
        measurement={"rate": {"passes": 4, "slots": 10, "p": 0.4}, "verdict": "HARD"},
        evidence={"sharedFailures": ["t9"], "evidenceComplete": True},
    )
    check("cross-commit guard fires on any class", e["class"], "not-measured")
    check("and records what it overrode", e["classOverriddenFrom"], "corrective")
    check("and blanks the evidence too", e["failing"], None)
    check("and marks the mismatch", e["measurementMatchesSha"], False)

    try:
        j.set_status("abandoned")
        check("abandon refused with budget left", False, True)
    except ValueError as exc:
        check("abandon refused with budget left", "under-hardened" in str(exc), True)
    j.set_status("escalated")
    check("escalated allowed", j.data["status"], "escalated")

    try:
        j.record(task_dir=task_dir, sha="d", cls="nonsense", fix="x")
        check("unknown class refused", False, True)
    except ValueError:
        check("unknown class refused", True, True)

    j.save()
    other = Journal.open(repo, "mytask")
    check("journal reopens with its rounds", len(other.rounds), 4)

    rep = gate_mod.run(repo_root=repo, task_dir=task_dir, task_name="mytask")
    check("gate passes on a sound tree", rep.ok, True)
    check("NOT_RUN reported, not hidden", "oracle" in rep.as_dict()["notRun"], True)

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "j"], check=True, capture_output=True)
    receipt = gate_mod.write_receipt(repo, "mytask", rep)
    check("receipt written on a clean passing tree", receipt.get("ok"), True)
    check("receipt verifies", gate_mod.verify_receipt(repo, "mytask")[0], True)

    (task_dir / "task.toml").write_text((task_dir / "task.toml").read_text() + "\n# drift\n")
    check("dirty tree invalidates the receipt", gate_mod.verify_receipt(repo, "mytask")[0], False)
    check(
        "and declines to write a new one",
        gate_mod.write_receipt(repo, "mytask", rep).get("state"),
        "not_written",
    )

    # config-integrity traps the oracle cannot catch
    (task_dir / "tests" / "config.json").write_text(
        json.dumps({"patch": "same", "test_patch": "same", "fail_to_pass": ["t1"]})
    )
    rep = gate_mod.run(repo_root=repo, task_dir=task_dir, task_name="mytask")
    detail = next(c.detail for c in rep.checks if c.name == "config-integrity")
    check("patch == test_patch caught", "byte-identical" in detail, True)
    check("gate fails on it", rep.ok, False)

    (task_dir / "task.toml").write_text('[metadata]\ntags = ["aai-labs"]\n')
    rep = gate_mod.run(repo_root=repo, task_dir=task_dir, task_name="mytask")
    tags = next(c.detail for c in rep.checks if c.name == "tags")
    check("missing team tag caught", "aai-labs-<project>" in tags, True)
    check("missing recipe tag caught", "benchsmith-v1" in tags, True)


# ------------------------------------------------- reading foreign receipts ----
section("Foreign receipts — a green status is not evidence")

from benchsmith.receipts import predicate_is_permissive, read_job, scan_log  # noqa: E402

# Observed: a deploy log claiming health verification whose own text says it skipped.
dep = scan_log("pushing image...\nno ALB found — skipping health verification\nrefresh complete")
check("deploy log skip marker found", dep["skipped"], True)
check("and the log is UNPROVEN", dep["verdict"], "UNPROVEN")
check("a clean log has no markers", scan_log("all checks executed")["skipped"], False)

# Observed: an e2e job green via its skip path, every step skipped.
e2e = {"conclusion": "success", "steps": [{"conclusion": "skipped"} for _ in range(6)]}
r = read_job(e2e)
check("all-skipped job is not a pass", r["verdict"], "UNPROVEN")
check("and says why", "all 6 steps skipped" in r["reason"], True)

real = {"conclusion": "success", "steps": [{"conclusion": "success"}, {"conclusion": "skipped"}]}
check("a job with real steps passes", read_job(real)["verdict"], "PASS")
check("a failed job fails", read_job({"conclusion": "failure", "steps": []})["verdict"], "FAIL")
check("status with no corroboration is unproven",
      read_job({"conclusion": "success"})["verdict"], "UNPROVEN")
check("a passing status with a skipping log is unproven",
      read_job(real, log="no ALB found — skipping health verification")["verdict"], "UNPROVEN")

# Observed: assert_ok accepting everything except 403.
deny = 'assert_ok() { [ "$code" != 403 ] || fail; }'
pd = predicate_is_permissive(deny)
check("denylist positive control detected", pd["permissive"], True)
check("and named", "accepts every other code" in pd["findings"][0], True)
allow = 'assert_ok() { [ "$code" == 200 ] || fail; }'
check("allowlist control is not flagged", predicate_is_permissive(allow)["permissive"], False)


# ---------------------------------------------------------- repair mode ----
section("Repair mode — closure terminates, not budget")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); repo = scratch_repo(tmp); task_dir = repo / "mytask"
    j = Journal.open(repo, "mytask")
    j.set_mode("repair")
    check("mode is settable", j.mode, "repair")
    try:
        j.set_mode("nonsense"); check("unknown mode refused", False, True)
    except ValueError: check("unknown mode refused", True, True)

    # An acceptance test is mandatory at OPEN time.
    try:
        j.open_finding("F1", "grader pins file identity", "")
        check("opening without an acceptance test is refused", False, True)
    except ValueError as e:
        check("opening without an acceptance test is refused", "acceptance test is required" in str(e), True)

    j.open_finding("F1", "grader pins file identity", "p3 refactor scores 1.0")
    j.open_finding("F2", "spec ambiguous on ordering", "reviewer confirms wording")
    check("two findings open", sorted(j.open_findings()), ["F1", "F2"])
    check("repair does not stop with findings open", j.stop_reason(), None)

    try:
        j.close_finding("F1", "abc", ""); check("closing without evidence is refused", False, True)
    except ValueError as e:
        check("closing without evidence is refused", "requires evidence" in str(e), True)
    try:
        j.close_finding("NOPE", "abc", "x"); check("closing an unopened finding is refused", False, True)
    except ValueError: check("closing an unopened finding is refused", True, True)

    j.close_finding("F1", "abc123", "p3-struct-backfill-result.sh scores 1.0")
    check("one closed, one open", j.closure_summary(), "1/2 findings closed")
    check("still not terminal", j.stop_reason(), None)

    j.close_finding("F2", "def456", "reviewer thread resolved")
    stop = j.stop_reason()
    check("closure terminates repair", stop is not None and "repair complete" in stop, True)
    check("and says hardening needs a new ask", "new ask" in (stop or ""), True)

    # Budget-driven stop must NOT fire in repair mode with findings open.
    k = Journal.open(repo, "other")
    k.set_mode("repair")
    k.open_finding("G1", "x", "y")
    k.data["hardeningBudgetExhausted"] = True
    check("spent hardening budget does not end a repair", k.stop_reason(), None)
    k.data["probesSpent"] = 99
    stop = k.stop_reason()
    check("probe budget does end it", stop is not None and "probe budget" in stop, True)

    # Harden mode is unchanged.
    h = Journal.open(repo, "third")
    check("default mode is harden", h.mode, "harden")
    h.data["hardeningBudgetExhausted"] = True
    check("harden still stops on budget", "budget" in (h.stop_reason() or ""), True)


# ------------------------------------------------------- smoke layer -------
section("Smoke — every subcommand reachable from bin/benchsmith is invoked")

# Why this exists, separately from the fixtures above: three defects shipped in
# one day that the fixtures could not see, because a fixture that supplies a
# field cannot notice code reading a different one. Fail-open single_gate_share,
# the plan/row build-label mismatch, and a NameError in discover() that made
# `probe` and `read` crash outright. All three were reachable from the CLI and
# none was reachable from a unit fixture. This layer calls every subcommand with
# the external boundary stubbed, so a path that cannot execute at all fails here.

import subprocess as _sp  # noqa: E402
from benchsmith import adapter as _ad  # noqa: E402
from benchsmith import cli as _cli  # noqa: E402

_LEGACY_ROOT = """Usage: codimango [OPTIONS] COMMAND [ARGS]...

Options:
  --site [vanilla|nest]  Which site.
  --help                 Show this message and exit.

Commands:
  api       Codimango API commands (setup, tasks, jobs, trials, keys).
  assets    Upload and manage the large task assets a trial downloads.
  bench     Benchmarking commands.
  task      Task scaffolding commands.
"""
_LEGACY_API = """Usage: codimango api [OPTIONS] COMMAND [ARGS]...

Commands:
  tasks   Task endpoints.
  jobs    Job endpoints.
  trials  Trial endpoints.
"""
_NEW_ROOT = """Usage: codimango [OPTIONS] COMMAND [ARGS]...

Commands:
  task    Task commands.
  job     Job commands.
  trial   Trial commands.
  bench   Benchmarking commands.
"""
_SHOW_HELP = "Options:\n  --json\n  --no-cache\n"
_JOBS_HELP = "Options:\n  --json\n  --limit <N>\n  --include-agentic-review [latest|all|none]\n"


def _stub_cli(root, api="", show=_SHOW_HELP, jobs=_JOBS_HELP):
    """Pretend a codimango binary exists and answers --help a given way."""
    calls = []

    def fake_help(binary, *words):
        calls.append(words)
        if not words:
            return root
        if words == ("api",):
            return api
        if words and words[-1] == "show":
            return show
        if words and words[-1] == "list":
            return jobs
        return ""

    return fake_help, calls


_real_help, _real_which = _ad._help, __import__("shutil").which
try:
    __import__("shutil").which = lambda b: f"/usr/bin/{b}"

    # legacy surface -- the shape this box actually has
    _ad._help, _ = _stub_cli(_LEGACY_ROOT, _LEGACY_API)
    s = _ad.discover(binary="codimango")
    check("discover resolves the legacy api surface", s.task_show, ("api", "tasks", "show"))
    check("legacy detects --no-cache", s.supports_no_cache, True)
    check("legacy detects agentic-review", s.supports_agentic_review, True)
    check("legacy has no --offset", s.supports_offset, False)

    # replacement surface -- bare subcommands, no api
    _ad._help, _ = _stub_cli(_NEW_ROOT, "")
    s = _ad.discover(binary="codimango")
    check("discover resolves the bare surface", s.task_show, ("task", "show"))

    # the substring trap: "task" appears only in a DESCRIPTION
    _ad._help, _ = _stub_cli(
        "Usage: x\n\nCommands:\n  assets  Manage the large task assets a trial downloads.\n", ""
    )
    try:
        _ad.discover(binary="codimango")
        check("a description mentioning 'task' does not resolve a command", False, True)
    except _ad.Unresolved:
        check("a description mentioning 'task' does not resolve a command", True, True)
finally:
    _ad._help, __import__("shutil").which = _real_help, _real_which

# A partial `api` surface must not resolve: `api tasks` without jobs/trials
# would bind three subcommands, two of which fail only at call time.
_real_help2, _real_which2 = _ad._help, __import__("shutil").which
try:
    __import__("shutil").which = lambda b: f"/usr/bin/{b}"
    _ad._help, _ = _stub_cli(_LEGACY_ROOT, "Commands:\n  tasks   Task endpoints.\n")
    try:
        s = _ad.discover(binary="codimango")
        check("partial api surface does not resolve to api", s.task_show, ("task", "show"))
    except _ad.Unresolved:
        check("partial api surface refuses rather than half-binding", True, True)
finally:
    _ad._help, __import__("shutil").which = _real_help2, _real_which2

# Pagination: a second page must actually be requested, not refetched.
class _FakePlatform(_ad.Platform):
    def __init__(self, surface, identity, pages):
        super().__init__(surface, identity)
        self.pages, self.seen = pages, []

    def _json(self, argv):
        self.seen.append(argv)
        return self.pages[min(len(self.seen) - 1, len(self.pages) - 1)]


_ident = _ad.Identity(task_name="t", task_id="1", task_uuid="u")
_surf = _ad.Surface(binary="c", jobs_list=("job", "list"), supports_offset=True)
fp = _FakePlatform(_surf, _ident, [
    {"jobs": [{"id": "a"}], "total": 2, "hasMore": True, "readTruncated": False},
    {"jobs": [{"id": "b"}], "total": 2, "hasMore": False, "readTruncated": False},
])
got = fp.jobs(page_size=1)
check("pagination collects both pages", [j["id"] for j in got], ["a", "b"])
check("second call carries --offset", any("--offset" in a for a in fp.seen[1]), True)

# Without an --offset flag it must refuse, not silently refetch page one.
_surf_no = _ad.Surface(binary="c", jobs_list=("job", "list"), supports_offset=False)
fp2 = _FakePlatform(_surf_no, _ident, [
    {"jobs": [{"id": "a"}], "total": 9, "hasMore": True, "readTruncated": False},
])
try:
    fp2.jobs(page_size=1)
    check("no --offset flag refuses to page", False, True)
except _ad.Unresolved as e:
    check("no --offset flag refuses to page", "no --offset" in str(e), True)

# Every subcommand actually executes.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    repo = scratch_repo(tmp)
    payload = tmp / "payload.json"
    # Shaped from verified live payloads: config.agentName / config.commitSha /
    # config.nAttempts, reviewIdentity.stage, ctrfResults.summary. A degenerate
    # empty payload exercises none of the parsing and hides whether it works.
    SMOKE_SHA = "0012092313ae180a663829e037869a2bbd6e16b1"
    payload.write_text(json.dumps({
        "task": {"validationCommitSha": SMOKE_SHA, "oracleStatus": "validated",
                 "tbdReviewStatus": "pass", "qualitativeResult": {"overall": "GOOD", "verdict": "Accept"}},
        "jobs": [
            {"id": "1", "status": "completed", "createdAt": "2026-09-10T01:00:00Z",
             "config": {"agentName": "codex", "commitSha": SMOKE_SHA, "nAttempts": 5,
                        "modelName": "gpt-5.5", "env": {"__validation_stage": "codex"}},
             "stats": {"passed": 2, "failed": 3, "errored": 0, "completed": 5}},
            {"id": "2", "status": "completed", "createdAt": "2026-09-10T01:00:00Z",
             "config": {"agentName": "claude-code", "commitSha": SMOKE_SHA, "nAttempts": 5,
                        "modelName": "claude-opus-5", "env": {"__validation_stage": "agent"}},
             "stats": {"passed": 2, "failed": 3, "errored": 0, "completed": 5}},
            {"id": "9", "status": "completed", "createdAt": "2026-09-10T02:00:00Z",
             "reviewIdentity": {"attempt": 1, "stage": "agentic-review"},
             "config": {"agentName": "codex", "commitSha": SMOKE_SHA, "nAttempts": 1},
             "agenticReview": {"verdict": "GOOD", "rubrics": {"summary": {"nPassed": 17, "nTotal": 17},
                                                              "items": []}}},
        ],
        "trials": {
            "1": [{"id": f"t1{i}", "status": "completed", "reward": 1.0 if i < 2 else 0.0,
                   "modelName": "gpt-5.5",
                   "ctrfResults": {"summary": {"tests": 11, "passed": 11 if i < 2 else 10,
                                               "failed": 0 if i < 2 else 1}}} for i in range(5)],
            "2": [{"id": f"t2{i}", "status": "completed", "reward": 1.0 if i < 2 else 0.0,
                   "modelName": "claude-opus-5",
                   "ctrfResults": {"summary": {"tests": 11, "passed": 11 if i < 2 else 10,
                                               "failed": 0 if i < 2 else 1}}} for i in range(5)],
        },
        "strongest": [["gpt", "gpt-5.5"], ["opus", "claude-opus-5"]],
        "steps": ["1"], "activeSha": SMOKE_SHA,
    }))

    invocations = [
        (["preflight", "--json"], {0, 1}),
        (["hash", "--repo", str(repo), "--task", "mytask"], {0}),
        (["gate", "--repo", str(repo), "--task", "mytask", "--json"], {0, 1}),
        (["record", "--repo", str(repo), "--task", "mytask", "--sha", "abc",
          "--class", "corrective", "--fix", "smoke"], {0}),
        (["bar", str(payload)], {0, 1}),
        (["trailers", "--run-id", "r1", "--workflow", "smoke"], {0}),
        (["install-hooks", "--repo", str(repo)], {0}),
    ]
    for argv, ok in invocations:
        try:
            rc = _cli.main(argv)
            check(f"`benchsmith {argv[0]}` executes (rc={rc})", rc in ok, True)
        except SystemExit as e:
            check(f"`benchsmith {argv[0]}` executes", e.code in ok, True)
        except Exception as e:  # noqa: BLE001 - a crash is the thing we are hunting
            check(f"`benchsmith {argv[0]}` executes", f"{type(e).__name__}: {e}", "no exception")

    # rc==0 from install-hooks proves nothing: the hook it wrote must point at a
    # binary that exists. The first version resolved to lib/bin/benchsmith, one
    # directory too deep, and every gate it was supposed to enforce silently
    # never ran.
    hook = repo / ".git" / "hooks" / "pre-push"
    check("pre-push hook was written", hook.is_file(), True)
    if hook.is_file():
        import re as _re
        m = _re.search(r'exec python3 "([^"]+)"', hook.read_text())
        check("hook names an exec target", bool(m), True)
        if m:
            check("hook target exists on disk", Path(m.group(1)).is_file(), True)


print(f"\nbenchsmith selftest: {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
