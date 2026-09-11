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


print(f"\nbenchsmith selftest: {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
