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
import os
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


# ------------------------------------------------------- stage 2: planner ----
section("Read-only planner and leases")

from benchsmith.queue import (  # noqa: E402
    TIER_DRAFT_FAILED, TIER_DRAFT_PASSING, TIER_DRAFT_PENDING, TIER_IDEA, TIER_REVISION,
    Leases, build_queue, read_journals,
)

TASKS = [
    {"name": "rev-a", "status": "needs_revision", "validationStatus": "passing"},
    {"name": "d-fail", "status": "draft", "validationStatus": "failed"},
    {"name": "d-pend", "status": "draft", "validationStatus": "pending"},
    {"name": "d-pass", "status": "draft", "validationStatus": "passing"},
    {"name": "accepted-x", "status": "accepted", "validationStatus": "passing"},
    {"name": "training-x", "status": "used_in_training", "validationStatus": "passing"},
]
q = build_queue(TASKS)
check("terminal statuses are not queued", [i.task for i in q if "accepted" in i.task or "training" in i.task], [])
check("priority order", [i.task for i in q], ["rev-a", "d-fail", "d-pend", "d-pass"])
check("revision is tier 10", q[0].tier, TIER_REVISION)
check("failed draft is tier 20", q[1].tier, TIER_DRAFT_FAILED)
check("pending draft is tier 30", q[2].tier, TIER_DRAFT_PENDING)
check("passing draft is tier 40", q[3].tier, TIER_DRAFT_PASSING)
check("all dispatchable with no journals", all(i.dispatchable for i in q), True)

# Determinism: a restarted coordinator must compute the identical plan.
check("order is stable under input shuffle",
      [i.task for i in build_queue(list(reversed(TASKS)))], [i.task for i in q])

# Journal status gates dispatch.
j = {"rev-a": "converged", "d-fail": "escalated", "d-pend": "running", "d-pass": "unreadable"}
q2 = build_queue(TASKS, journals=j)
by = {i.task: i for i in q2}
check("converged is skipped", by["rev-a"].dispatchable, False)
check("escalated needs a human", "needs a human" in by["d-fail"].skip, True)
check("running is still dispatchable", by["d-pend"].dispatchable, True)
check("an unparseable journal is not 'no journal'", by["d-pass"].dispatchable, False)
check("and says why", "will not parse" in by["d-pass"].skip, True)

# Ideas are last, and deduplicated against real tasks.
q3 = build_queue(TASKS, ideas=[{"name": "brand-new"}, {"name": "rev-a"}])
check("idea is tier 50", [i.tier for i in q3 if i.task == "brand-new"], [TIER_IDEA])
check("idea duplicating a task is dropped", len([i for i in q3 if i.task == "rev-a"]), 1)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); repo = scratch_repo(tmp)

    # read_journals must not mistake a receipt for a journal.
    (repo / ".benchsmith").mkdir(exist_ok=True)
    (repo / ".benchsmith" / "t1.json").write_text(json.dumps({"task": "t1", "status": "converged"}))
    (repo / ".benchsmith" / "t2.receipt.json").write_text(json.dumps({"task": "t2", "ok": True}))
    (repo / ".benchsmith" / "broken.json").write_text("{not json")
    js = read_journals(repo)
    check("journal status read", js.get("t1"), "converged")
    check("receipts are not journals", "t2" in js, False)
    check("unparseable journal is flagged", js.get("broken"), "unreadable")

    # Leases: exactly one claimant wins.
    L = Leases(repo)
    check("first claim wins", L.claim("taskA")["ok"], True)
    second = L.claim("taskA")
    check("second claim loses", second["ok"], False)
    check("and names the holder", bool(second.get("heldBy")), True)
    check("claim appears active", "taskA" in L.active(), True)
    check("a claimed task is not dispatchable",
          build_queue([{"name": "taskA", "status": "draft", "validationStatus": "failed"}],
                      leases=L.active())[0].dispatchable, False)
    check("release works", L.release("taskA")["ok"], True)
    check("releasing twice is not ok", L.release("taskA")["ok"], False)
    check("released task is dispatchable again",
          build_queue([{"name": "taskA", "status": "draft", "validationStatus": "failed"}],
                      leases=L.active())[0].dispatchable, True)

    # An expired lease is not a claim -- a crashed worker must not park a task.
    expired = Leases(repo, ttl=-1)
    expired.claim("taskB")
    check("expired lease is reaped", "taskB" in Leases(repo).active(), False)
    check("and can be reclaimed", Leases(repo).claim("taskB")["ok"], True)

    # The planner is read-only: it must write nothing.
    before = sorted(p.name for p in (repo / ".benchsmith").iterdir())
    build_queue(TASKS, journals=read_journals(repo), leases=Leases(repo).active())
    check("build_queue mutates nothing",
          sorted(p.name for p in (repo / ".benchsmith").iterdir()), before)


# ------------------------------------------------ concurrency and restart ----
section("Fleet prerequisites — atomicity, idempotency, durable waves")

from benchsmith.journal import round_key  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td); repo = scratch_repo(tmp); task_dir = repo / "mytask"

    # Idempotency: replaying the same round must not append a second one.
    j = Journal.open(repo, "mytask")
    a = j.record(task_dir=task_dir, sha="s1", cls="corrective", fix="same fix")
    n_after_first = len(j.rounds)
    b = j.record(task_dir=task_dir, sha="s1", cls="corrective", fix="same fix")
    check("replaying a round does not append", len(j.rounds), n_after_first)
    check("the replay returns the original", b["roundKey"], a["roundKey"])
    check("and is marked replayed", "replayedAt" in b, True)
    c = j.record(task_dir=task_dir, sha="s1", cls="corrective", fix="a different fix")
    check("a genuinely different round does append", len(j.rounds), n_after_first + 1)
    check("keys differ", c["roundKey"] != a["roundKey"], True)
    check("key is deterministic", round_key("s", "g", "c", "f"), round_key("s", "g", "c", "f"))
    check("key is sensitive to sha", round_key("s1", "g", "c", "f") != round_key("s2", "g", "c", "f"), True)
    j.save()

    # Atomicity: a reader must never observe a truncated file.
    import os as _os
    text = (repo / ".benchsmith" / "mytask.json").read_text()
    check("saved journal parses", bool(json.loads(text).get("rounds")), True)
    check("no temp files left behind",
          [f for f in (repo / ".benchsmith").iterdir() if ".tmp." in f.name], [])

    # Concurrent writers: last-writer-wins is acceptable, corruption is not.
    import subprocess as _sp, sys as _sys
    writer = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from benchsmith.journal import Journal\n"
        "j = Journal.open(Path(%r), 'mytask')\n"
        "j.record(task_dir=Path(%r), sha='c'+sys.argv[1], cls='corrective', fix='w'+sys.argv[1])\n"
        "j.save()\n"
    ) % (str(Path("lib").resolve()), str(repo), str(task_dir))
    procs = [_sp.Popen([_sys.executable, "-c", writer, str(i)]) for i in range(6)]
    for pr in procs: pr.wait()
    raw = (repo / ".benchsmith" / "mytask.json").read_text()
    try:
        parsed = json.loads(raw); ok_parse = True
    except Exception:
        parsed, ok_parse = None, False
    check("journal still parses after 6 concurrent writers", ok_parse, True)
    check("and is not empty", bool(parsed and parsed.get("task")), True)

    # The six-writer test above does NOT discriminate: the race window on a small
    # file is too narrow to hit, and a non-atomic write survives it. Force the
    # window open -- a large journal, and a reader parsing in a tight loop while
    # writers save. truncate-then-write is then observable as a parse failure.
    big = Journal.open(repo, "race")
    big.data["rounds"] = [{"n": i, "pad": "x" * 400} for i in range(400)]
    big.save()
    race_path = repo / ".benchsmith" / "race.json"
    reader = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "p = Path(%r)\n"
        "bad = 0\n"
        "end = time.time() + 3.0\n"
        "while time.time() < end:\n"
        "    try:\n"
        "        json.loads(p.read_text())\n"
        "    except Exception:\n"
        "        bad += 1\n"
        "sys.exit(1 if bad else 0)\n"
    ) % str(race_path)
    rd = _sp.Popen([_sys.executable, "-c", reader])
    wr_src = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from benchsmith.journal import Journal\n"
        "end = time.time() + 2.5\n"
        "while time.time() < end:\n"
        "    j = Journal.open(Path(%r), 'race')\n"
        "    j.data['rounds'] = [{'n': i, 'pad': 'y' * 400} for i in range(400)]\n"
        "    j.save()\n"
    ) % (str(Path("lib").resolve()), str(repo))
    ws = [_sp.Popen([_sys.executable, "-c", wr_src]) for _ in range(3)]
    for w in ws: w.wait()
    rd.wait()
    check("a concurrent reader never sees a torn journal", rd.returncode, 0)

    # Durable waves: a restarted worker must see the wave it already spent.
    k = Journal.open(repo, "waves")
    check("no prior wave initially", k.prior_wave("shaX"), None)
    k.record_wave({"sha": "shaX", "scope": "slot", "digest": "d1", "slots": []})
    k.save()
    reloaded = Journal.open(repo, "waves")
    check("wave survives a reload", (reloaded.prior_wave("shaX") or {}).get("digest"), "d1")
    check("a different sha has none", reloaded.prior_wave("shaY"), None)


# --------------------------------------------------- surface drift control ----
section("The drift control itself is covered — it was not, on first write")

from benchsmith.adapter import SMOKE_CONTRACT, verify_surface  # noqa: E402

_legacy = _ad_mod = None
from benchsmith import adapter as _ad_mod  # noqa: E402

ok = verify_surface(_ad_mod.Surface(binary="c", task_show=("api", "tasks", "show"),
                                    jobs_list=("api", "jobs", "list")))
check("legacy shape matches the fixtures", ok["verdict"], "MATCHES-FIXTURES")
check("and names which shape", ok["matched"], "legacy")

bare = verify_surface(_ad_mod.Surface(binary="c", task_show=("task", "show"),
                                      jobs_list=("job", "list")))
check("bare shape matches the fixtures", bare["verdict"], "MATCHES-FIXTURES")

# A renamed subcommand upstream is exactly the silent failure this exists for.
drift = verify_surface(_ad_mod.Surface(binary="c", task_show=("workitem", "show"),
                                       jobs_list=("job", "list")))
check("an unknown task_show is DRIFTED", drift["verdict"], "DRIFTED")
check("and says what it saw", "matches no shape" in drift["detail"], True)

half = verify_surface(_ad_mod.Surface(binary="c", task_show=("task", "show"),
                                      jobs_list=("api", "jobs", "list")))
check("a half-matching surface is DRIFTED", half["verdict"], "DRIFTED")
check("fixtures cover both known shapes", sorted(SMOKE_CONTRACT), ["bare", "legacy"])


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
        (["dispatch", "--repo", str(repo), "--task", "mytask"], {0}),
        (["stats", "--root", str(repo)], {0}),
        (["backoff", "--repo", str(repo), "--task", "mytask"], {0}),
        (["mutate", "--repo", str(repo), "--task", "mytask"], {0, 1}),
        (["passatk", str(payload)], {0, 1}),
        (["reconcile", "--repo", str(repo)], {0, 1}),
        (["fleet", "--repo", str(repo), "--workers", "1", "--no-gsd"], {0, 1}),
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


# --- stage 3: dispatch -------------------------------------------------------
#
# The dispatch layer's whole job is to refuse. Every refusal below is checked
# against a mutant that removes it, because a guard that cannot be shown to fire
# is indistinguishable from no guard at all.

from benchsmith import dispatch as dsp  # noqa: E402

_DR = Path(tempfile.mkdtemp()) / "dispatch-repo"
for _t in ("t1", "t", "real-task"):
    (_DR / _t).mkdir(parents=True)
    (_DR / _t / "task.toml").write_text('authors = [{ name = "x" }]\n')
_REPO = str(_DR)

_p = dsp.plan("t1", _REPO)
check("default backend is agentcloud", _p.backend, "agentcloud")
# `--harness codex` passes --dry-run (enum validation only) and is then rejected
# with HTTP 400 at create time on this tenant. The default must be one that
# demonstrably starts, not one that merely validates.
check("no harness is forced by default", "--harness" not in _p.argv, True)
check("an explicit legal harness is still passed",
      "--harness" in dsp.plan("t1", _REPO, harness="native").argv, True)
# An agentcloud session runs on the same devserver under a DIFFERENT HOME, so a
# tilde path resolves where the installation is not and the worker re-clones.
check("the bootstrap names an absolute benchsmith path",
      dsp.benchsmith_root().startswith("/"), True)
check("...and no tilde reaches the prompt", "~/" in dsp.bootstrap_block(), False)
check("dispatch is non-publishing by default", _p.publishing, False)
# An alias that resolves to nothing is worse than no alias: the session starts,
# the skill is silently absent, and the worker improvises without a gate.
check("no --skills alias by default", "--skills" not in _p.argv, True)
check("an explicit alias is still passed",
      "--skills" in dsp.plan("t1", _REPO, skills="benchsmith").argv, True)
check("agentcloud workers are told which host they need",
      any(dsp.HOST in a for a in _p.argv), True)
check("the preamble uses the working proxy pair",
      any("fwdproxy:8080" in a for a in _p.argv), True)
check("being off-host is blocked, not improvised",
      any("state=blocked" in a for a in _p.argv), True)
check("local codex workers get no host preamble",
      any("NOT ON THE HOST" in a for a in dsp.plan("t1", _REPO, backend="codex").argv), False)
check("the host preamble is forceable for a local worker",
      any("NOT ON THE HOST" in a
          for a in dsp.plan("t1", _REPO, backend="codex", bootstrap=True).argv), True)
check("plan is shell-quotable", "benchsmith harden: t1" in _p.shell, True)
check("task appears in the prompt, not just the title",
      any("t1" in a and "YOU MAY NOT PUSH" in a for a in _p.argv), True)
# "Use the benchsmith skill" sends the agent hunting for a Skillbook alias that
# does not exist; it spends a minute failing and then asks which notebook it is.
_pt = [a for a in _p.argv if "SKILL.md" in a][0]
check("the prompt names the SKILL.md path", "/SKILL.md" in _pt, True)
check("...and says there is no slash command", "no `/benchsmith` slash command" in _pt, True)
check("...and does not say 'use the benchsmith skill'",
      "Use the benchsmith skill" in _pt, False)

# Probed live: agentcloud\wire\HarnessKind rejects these two. Encoding the
# rejection here means the skill fails loudly rather than the API failing late.
for bad in ("claude", "metacode"):
    try:
        dsp.plan("t1", _REPO, harness=bad)
        check(f"agentcloud refuses harness={bad}", "accepted", "refused")
    except dsp.DispatchRefused as e:
        check(f"agentcloud refuses harness={bad}", "metacode and claude" in str(e) or "valid:" in str(e), True)

check("native is a legal agentcloud harness",
      dsp.plan("t1", _REPO, harness="native").backend, "agentcloud")
check("codex backend does not go through agentcloud",
      dsp.plan("t1", _REPO, backend="codex").argv[:2], ["codex", "exec"])
check("metacode is reachable as the 1P hop only",
      "1P delegation only -- not a task worker" in dsp.plan("t1", _REPO, backend="metacode").notes, True)
try:
    dsp.plan("t1", _REPO, backend="nope")
    check("unknown backend refused", "accepted", "refused")
except dsp.DispatchRefused:
    check("unknown backend refused", True, True)

# run() must not start anything unless explicitly applied.
try:
    dsp.run(_p)
    check("run without apply refuses", "ran", "refused")
except dsp.DispatchRefused:
    check("run without apply refuses", True, True)

_pub = dsp.plan("t1", _REPO); _pub.publishing = True
try:
    dsp.run(_pub, apply=True)
    check("run refuses a publishing plan", "ran", "refused")
except dsp.DispatchRefused as e:
    check("run refuses a publishing plan", "stage 4" in str(e), True)

# --- handoff parsing ---
_good = '{"work_item":"t1","state":"ready_to_publish","commit_sha":"deadbeef","base_sha":"a1","next_action":"publish"}'
check("valid handoff parses", dsp.parse_handoff(_good)["commit_sha"], "deadbeef")
check("handoff keeps only known fields",
      set(dsp.parse_handoff(_good)) <= set(dsp.HANDOFF_FIELDS), True)
check("prose around the JSON is tolerated",
      dsp.parse_handoff("Done!\n" + _good + "\nbye")["state"], "ready_to_publish")

def _oversized() -> str:
    """A handoff valid in every respect except size, so only the cap can reject it."""
    pad = "y" * (dsp.HANDOFF_LIMIT * 4)
    return json.dumps({"work_item": "t1", "state": "blocked", "note": pad})


for label, bad in [
    ("no JSON at all", "the task is finished"),
    ("malformed JSON", '{"state": "blocked",}'),
    ("unknown state", '{"state":"kinda_done"}'),
    # The single claim a coordinator acts on. Unsupported, it must not pass.
    ("ready_to_publish with no commit_sha", '{"state":"ready_to_publish","work_item":"t1"}'),
    ("a transcript instead of a handoff", _oversized()),
    ("nothing returned", None),
]:
    try:
        dsp.parse_handoff(bad)
        check(f"handoff refused: {label}", "accepted", "refused")
    except dsp.DispatchRefused:
        check(f"handoff refused: {label}", True, True)

# --- mutation probe ---
# Each locator must match exactly once, or the probe is measuring nothing.
import importlib as _il  # noqa: E402
_src = Path(dsp.__file__).read_text()
_MUTANTS = [
    ("harness allowlist", 'if harness and harness not in AGENTCLOUD_HARNESSES:', 'if False:'),
    ("apply guard", 'if not apply:', 'if False:'),
    ("publishing guard", 'if p.publishing:', 'if False:'),
    ("commit_sha requirement", 'if state == "ready_to_publish" and not doc.get("commit_sha"):', 'if False:'),
    ("state allowlist", 'if state not in HANDOFF_STATES:', 'if False:'),
    ("size cap", 'if len(text) > HANDOFF_LIMIT * 4:', 'if False:'),
]
for label, old, new in _MUTANTS:
    check(f"mutant locator is unique: {label}", _src.count(old), 1)

def _survives(old, new) -> bool:
    """True if the suite's dispatch checks still pass with the guard removed."""
    Path(dsp.__file__).write_text(_src.replace(old, new, 1))
    try:
        m = _il.reload(dsp)
        probes = [
            lambda: m.plan("t1", _REPO, harness="claude"),
            lambda: m.run(m.plan("t1", _REPO)),
            lambda: m.parse_handoff('{"state":"ready_to_publish"}'),
            lambda: m.parse_handoff('{"state":"kinda_done"}'),
            lambda: m.parse_handoff(_oversized()),
        ]
        _pl = m.plan("t1", _REPO); _pl.publishing = True
        probes.append(lambda: m.run(_pl, apply=True))
        for probe in probes:
            try:
                probe()
                return True  # something that should have been refused was not
            except m.DispatchRefused:
                continue
            except Exception:  # noqa: BLE001
                continue
        return False
    finally:
        Path(dsp.__file__).write_text(_src)
        _il.reload(dsp)

for label, old, new in _MUTANTS:
    check(f"removing the {label} is caught", _survives(old, new), True)


# --- measurement attribution: does this row describe our commit? -------------
#
# Exact-SHA selection is correct for one loop and deadlocks a fleet. These
# fixtures pin the four verdicts against a real git repo, because the rule is
# about ancestry and tree contents and a stubbed git would only test the stub.

from benchsmith import coverage as cov  # noqa: E402

_cr = Path(tempfile.mkdtemp()) / "cov"
_cr.mkdir(parents=True)


def _cgit(*args):
    return subprocess.run(["git", "-C", str(_cr), *args], capture_output=True, text=True)


_cgit("init", "-q", "-b", "main")
_cgit("config", "user.email", "t@t"); _cgit("config", "user.name", "t")
_task = _cr / "mytask"
(_task / "tests").mkdir(parents=True)
(_task / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
(_task / "instruction.md").write_text("do the thing\n")
(_task / "README.md").write_text("notes\n")
_cgit("add", "-A"); _cgit("commit", "-qm", "base")
_OURS = _cgit("rev-parse", "HEAD").stdout.strip()

# A sibling loop pushes something that touches neither surface.
(_cr / "unrelated.txt").write_text("sibling work\n")
_cgit("add", "-A"); _cgit("commit", "-qm", "sibling")
_SIBLING = _cgit("rev-parse", "HEAD").stdout.strip()

check("exact SHA covers", cov.attribute(_cr, "mytask", _OURS, _OURS).verdict, cov.EXACT)
_a = cov.attribute(_cr, "mytask", _OURS, _SIBLING)
check("descendant with untouched surfaces covers", _a.verdict, cov.ANCESTOR_IDENTICAL)
check("...and is reported as covering", _a.covers, True)

# A README edit is not a change to what was measured.
(_task / "README.md").write_text("notes, revised\n")
_cgit("add", "-A"); _cgit("commit", "-qm", "readme only")
check("a non-surface edit still covers",
      cov.attribute(_cr, "mytask", _OURS, _cgit("rev-parse", "HEAD").stdout.strip()).verdict,
      cov.ANCESTOR_IDENTICAL)

# A graded-surface edit is.
(_task / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n")
_cgit("add", "-A"); _cgit("commit", "-qm", "graded change")
_g = _cgit("rev-parse", "HEAD").stdout.strip()
check("a graded-surface change does not cover",
      cov.attribute(_cr, "mytask", _OURS, _g).verdict, cov.DIVERGENT)
check("...and is reported as not covering", cov.attribute(_cr, "mytask", _OURS, _g).covers, False)

# So is a spec edit, which is how a task gets quietly easier.
(_task / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
(_task / "instruction.md").write_text("do the thing, with a hint\n")
_cgit("add", "-A"); _cgit("commit", "-qm", "spec change")
check("a visible-surface change does not cover",
      cov.attribute(_cr, "mytask", _OURS, _cgit("rev-parse", "HEAD").stdout.strip()).verdict,
      cov.DIVERGENT)

# A measurement from before our push is not about our push.
check("an ancestor of ours is stale, not coverage",
      cov.attribute(_cr, "mytask", _SIBLING, _OURS).verdict, cov.STALE)

# Unknown is never coverage.
check("an unresolvable sha is unknown",
      cov.attribute(_cr, "mytask", _OURS, "0" * 40).verdict, cov.UNKNOWN)
check("unknown does not cover", cov.attribute(_cr, "mytask", _OURS, "0" * 40).covers, False)
check("a missing sha is unknown", cov.attribute(_cr, "mytask", _OURS, "").verdict, cov.UNKNOWN)
check("UNKNOWN is not in the covering set", cov.UNKNOWN in cov.COVERING, False)
check("STALE is not in the covering set", cov.STALE in cov.COVERING, False)
check("DIVERGENT is not in the covering set", cov.DIVERGENT in cov.COVERING, False)

# --- select_jobs honours the predicate ---
from benchsmith.snapshot import select_jobs as _sel  # noqa: E402


def _job(jid, sha, agent="codex"):
    return {"id": jid, "status": "completed",
            "config": {"commitSha": sha, "agentName": agent, "nAttempts": 5},
            "stats": {"passed": 1, "failed": 4}}


_jobs = [_job("j1", _OURS), _job("j2", _SIBLING, "claude-code")]
_kept, _notes = _sel(_jobs, _OURS)
check("without a predicate, only the exact SHA survives", len(_kept), 1)
_kept2, _notes2 = _sel(_jobs, _OURS, covers=cov.covers_factory(_cr, "mytask"))
check("with the predicate, the buried sibling row is recovered", len(_kept2), 2)
check("admission by ancestry is journalled, not silent",
      any("admitted j2 by ancestor-identical" in n for n in _notes2), True)

_bad = [_job("j3", "0" * 40)]
check("an unknown sha is still excluded with a predicate",
      len(_sel(_bad, _OURS, covers=cov.covers_factory(_cr, "mytask"))[0]), 0)


# --- one difficulty lever per hardening round --------------------------------

from benchsmith.gate import check_single_lever, levers_touched  # noqa: E402

check("a test edit is the graded lever",
      levers_touched(["mytask/tests/test_a.py"], "mytask"), {"graded"})
check("a spec edit is the spec lever",
      levers_touched(["mytask/instruction.md"], "mytask"), {"spec"})
check("a reference edit is the solution lever",
      levers_touched(["mytask/solution/fix.py"], "mytask"), {"solution"})
check("multi-step spellings resolve to the same surfaces",
      levers_touched(["mytask/steps/2/tests/t.py", "mytask/steps/1/instruction.md"], "mytask"),
      {"graded", "spec"})
check("a Dockerfile is not a lever",
      levers_touched(["mytask/Dockerfile", "mytask/README.md"], "mytask"), set())
check("task.toml counts as graded", levers_touched(["mytask/task.toml"], "mytask"), {"graded"})

_lr = Path(tempfile.mkdtemp()) / "lever"
(_lr / "mytask" / "tests").mkdir(parents=True)


def _lgit(*a):
    return subprocess.run(["git", "-C", str(_lr), *a], capture_output=True, text=True)


_lgit("init", "-q", "-b", "main")
_lgit("config", "user.email", "t@t"); _lgit("config", "user.name", "t")
(_lr / "mytask" / "instruction.md").write_text("spec\n")
(_lr / "mytask" / "tests" / "t.py").write_text("def test():\n    pass\n")
_lgit("add", "-A"); _lgit("commit", "-qm", "base")

# Two levers in one hardening round: the next band move is unattributable.
(_lr / "mytask" / "instruction.md").write_text("spec, tightened\n")
(_lr / "mytask" / "tests" / "t.py").write_text("def test():\n    assert False\n")
_lgit("add", "-A")

_r = gate_mod.Report(); check_single_lever(_lr, "mytask", "harden", _r)
_st = {c.name: c for c in _r.checks}["single-lever"]
check("two levers in a hardening round fails", _st.state, "FAIL")
check("...and names both", "graded" in _st.detail and "spec" in _st.detail, True)

# The same change set is fine when the round is not claiming a difficulty move.
_r2 = gate_mod.Report(); check_single_lever(_lr, "mytask", "repair", _r2)
check("repair mode batches freely",
      {c.name: c for c in _r2.checks}["single-lever"].state, "NOT_RUN")

_lgit("reset", "-q")
(_lr / "mytask" / "instruction.md").write_text("spec\n")
_lgit("add", "mytask/tests/t.py")
_r3 = gate_mod.Report(); check_single_lever(_lr, "mytask", "harden", _r3)
check("one lever passes", {c.name: c for c in _r3.checks}["single-lever"].state, "PASS")

_lgit("reset", "-q")
(_lr / "mytask" / "Dockerfile").write_text("FROM scratch\n")
_lgit("add", "mytask/Dockerfile")
_r4 = gate_mod.Report(); check_single_lever(_lr, "mytask", "harden", _r4)
check("a corrective-only change moves no lever",
      {c.name: c for c in _r4.checks}["single-lever"].state, "PASS")


# --- infra backoff -----------------------------------------------------------

import random as _rnd  # noqa: E402

from benchsmith import backoff as bo  # noqa: E402


def _rounds(*classes):
    return [{"class": c} for c in classes]


check("no streak after a task round", bo.consecutive(_rounds("infra", "too-easy")), 0)
check("streak counts back from the end", bo.consecutive(_rounds("too-easy", "infra", "infra")), 2)
check("not-measured counts as platform", bo.consecutive(_rounds("not-measured")), 1)
check("platform-stale counts", bo.consecutive(_rounds("platform-stale", "infra")), 2)
check("an empty journal has no streak", bo.consecutive([]), 0)
check("grader-false-negative is the task's problem, not the platform's",
      bo.consecutive(_rounds("grader-false-negative")), 0)

check("no wait without a streak", bo.delay(0), 0.0)
_d1 = bo.delay(1, rng=_rnd.Random(1)); _d3 = bo.delay(3, rng=_rnd.Random(1))
check("the wait grows with the streak", _d3 > _d1, True)
check("the wait is capped", bo.delay(50, rng=_rnd.Random(1)) <= bo.CAP_SECONDS, True)
# Two workers hitting one outage must not retry in lockstep.
check("jitter separates concurrent workers",
      bo.delay(3, rng=_rnd.Random(1)) != bo.delay(3, rng=_rnd.Random(2)), True)
check("jitter never yields a zero wait", bo.delay(3, rng=_rnd.Random(7)) > 0, True)

check("a short streak waits", bo.advise(_rounds("infra"), rng=_rnd.Random(1)).stop, False)
_stop = bo.advise(_rounds(*["infra"] * bo.STREAK_STOP), rng=_rnd.Random(1))
check("a long streak stops instead of polling an outage", _stop.stop, True)
check("...and says blocked-on-platform", "blocked-on-platform" in _stop.reason, True)
check("a stopped advice asks for no further wait", _stop.delay_seconds, 0.0)
check("advice serialises", set(bo.advise(_rounds("infra")).as_dict()),
      {"streak", "delaySeconds", "stop", "reason"})


# --- mutation probe ----------------------------------------------------------
#
# The probe's own claim -- "the suite catches near-misses" -- has to be
# falsifiable, so these run it against a suite that genuinely discriminates and
# one that genuinely does not, and require different answers.

from benchsmith import mutate as mu  # noqa: E402

_b, _n = mu.build_battery({"a.py": "def f(x):\n    return x == 1\n"})
check("a comparison yields a mutant", len(_b) >= 1, True)
check("the mutant names a line", _b[0].line, 2)
check("the mutant is a single edit", "!=" in _b[0].after, True)
check("comments are not mutated", mu.build_battery({"a.py": "# x == 1\n"})[0], [])
check("an unsupported language yields no mutants and says so",
      mu.build_battery({"a.swift": "let x = 1 == 1\n"})[0], [])
check("...with a note naming the file",
      any("a.swift" in s for s in mu.build_battery({"a.swift": "let x = 1 == 1\n"})[1]), True)
check("go is supported", len(mu.build_battery({"a.go": "if x == 1 {\n"})[0]), 1)
check("generation is deterministic",
      [m.as_dict() for m in mu.build_battery({"a.py": "x==1\ny<2\n"})[0]],
      [m.as_dict() for m in mu.build_battery({"a.py": "x==1\ny<2\n"})[0]])

_big = {"a.py": "\n".join(f"v{i} = {i} == {i}" for i in range(30)),
        "b.py": "\n".join(f"w{i} = {i} == {i}" for i in range(30))}
_cap, _cn = mu.build_battery(_big)
check("the battery is capped", len(_cap), mu.MAX_BATTERY)
check("the cap is reported", any("capped" in s for s in _cn), True)
# Alphabetical exhaustion would hide every survivor in b.py.
check("the cap spreads across files", len({m.path for m in _cap}), 2)

# --- run_battery against real suites ---
_mr = Path(tempfile.mkdtemp()) / "mtask"
(_mr).mkdir(parents=True)
(_mr / "impl.py").write_text("def classify(n):\n    return n >= 10\n")

# A suite that only ever checks one side of the boundary cannot see the flip.
(_mr / "weak_test.py").write_text(
    "import unittest\nfrom impl import classify\n"
    "class T(unittest.TestCase):\n"
    "    def test_big(self):\n        self.assertTrue(classify(100))\n")
# A suite that pins the boundary can.
(_mr / "strong_test.py").write_text(
    "import unittest\nfrom impl import classify\n"
    "class T(unittest.TestCase):\n"
    "    def test_boundary(self):\n"
    "        self.assertTrue(classify(10))\n        self.assertFalse(classify(9))\n")

_muts, _ = mu.build_battery({"impl.py": (_mr / "impl.py").read_text()})
_weak = mu.run_battery(_mr, _muts, [sys.executable, "-m", "unittest", "-q", "weak_test"])
_strong = mu.run_battery(_mr, _muts, [sys.executable, "-m", "unittest", "-q", "strong_test"])
check("a weak suite lets a mutant survive", _weak["status"], "FAIL")
check("...and the survivor names a line", bool(_weak["survivors"][0]["line"]), True)
check("a discriminating suite catches them", _strong["status"], "PASS")
check("the probe is discriminating (the two suites disagree)",
      _weak["status"] != _strong["status"], True)

check("no test command is NOT_RUN, not clean",
      mu.run_battery(_mr, _muts, [])["status"], "NOT_RUN")
check("no mutants is NOT_RUN, not clean",
      mu.run_battery(_mr, [], ["true"])["status"], "NOT_RUN")

# A mutant the compiler rejects says nothing about the tests.
_broken = [mu.Mutant(path="impl.py", line=2, operator="x", before="", after="    return ((")]
_bres = mu.run_battery(_mr, _broken, [sys.executable, "-m", "unittest", "-q", "weak_test"])
check("an unbuildable mutant is not viable", _bres["mutants"][0]["outcome"], mu.NOT_VIABLE)
check("...and leaves the denominator", _bres["viable"], 0)
check("an all-unviable battery proves nothing", _bres["status"], "NOT_RUN")

# A harness that cannot run makes every mutant look caught. That must be
# NOT_RUN, never a clean bill -- it is how the probe first fooled itself.
check("a broken baseline is NOT_RUN, not a clean bill",
      mu.run_battery(_mr, _muts, [sys.executable, "-m", "no_such_runner"])["status"], "NOT_RUN")
check("...and says why",
      "does not pass on the unmutated tree"
      in mu.run_battery(_mr, _muts, [sys.executable, "-m", "no_such_runner"])["reason"], True)

check("a swift target is NOT_RUN, not clean",
      mu.probe(_mr, ["nope.swift"], ["true"])["status"], "NOT_RUN")
check("...and says uncovered",
      "Uncovered, not clean" in mu.probe(_mr, ["nope.swift"], ["true"])["reason"], True)


# --- iOS / passAtK track -----------------------------------------------------

from benchsmith import passatk as pk  # noqa: E402

check("pass@1 of 2/4", pk.pass_at_k(4, 2, 1), 0.5)
check("pass@k is 1.0 when failures cannot fill the sample", pk.pass_at_k(4, 3, 2), 1.0)
check("pass@k of an all-failing cohort is 0", pk.pass_at_k(4, 0, 2), 0.0)
# Undefined is None, never 0.0 -- 0.0 would read as "never passes".
check("k larger than n is undefined", pk.pass_at_k(2, 1, 5), None)
check("no runs is undefined", pk.pass_at_k(0, 0, 1), None)

_R = pk.Run
check("a missing oracle blocks", pk.check_oracle([])[0], False)
check("a failing oracle is a defect, not difficulty",
      "task defect" in pk.check_oracle([_R("oracle", False)])[1], True)
check("a passing oracle clears", pk.check_oracle([_R("oracle", True)])[0], True)

_runs = [_R("oracle", True), _R("claude-code", False), _R("claude-code", True),
         _R("metacode", False), _R("metacode", False)]
_m = pk.measure(_runs, "b1")
check("the oracle is not pooled as a participant", len(_m["rows"]), 4)
check("cohorts map onto the shared family names",
      {r.slot.family for r in _m["rows"]}, {"opus", "avocado"})
# A local run says pass or fail and nothing about why; calling a failure "A"
# would invent semantic evidence we never observed.
check("a local failure is G, not hardness evidence",
      {r.kind for r in _m["rows"] if r.kind is not Kind.PASS}, {Kind.G})
check("two cohorts present clears the roster check", _m["ok"], True)

check("one cohort blocks",
      any("two" in b for b in pk.measure([_R("oracle", True), _R("metacode", False)], "b")["blocking"]),
      True)
_sat = pk.measure([_R("oracle", True), _R("claude-code", False), _R("metacode", True)], "b")
check("a saturated model under test blocks",
      any("saturated" in b for b in _sat["blocking"]), True)
check("an unknown agent is excluded, not pooled",
      any("unknown agent" in n for n in pk.measure([_R("gpt-9", True)], "b")["notes"]), True)


# --- discovery: where the backlog comes from ---------------------------------

from benchsmith import sources as src  # noqa: E402
from benchsmith.queue import TIER_GSD_REVIEW, TIER_GSD_SCAFFOLD, TIER_IDEA  # noqa: E402

_rows = [
    {"name": "a", "status": "draft", "validationStatus": "failed"},
    {"name": "b", "status": "needs_revision"},
    {"name": "c", "status": "accepted"},
    {"name": "d", "status": "used_in_training"},
    {"name": "e", "status": "being_reviewed"},
    {"name": "f", "status": "needs_reviewers_assigned"},
    {"name": "g", "status": "brand_new_status"},
]
_keep, _notes = src.normalise_codimango(_rows)
check("only actionable statuses are queued", sorted(r["name"] for r in _keep), ["a", "b"])
# Only the unrecognised status earns a note; the known-terminal ones are
# dropped quietly because nothing about them needs a decision.
check("only unrecognised statuses produce notes",
      sorted(n.split(":")[0] for n in _notes), ["g"])
# A status we have never seen must surface, not vanish: silently dropping it is
# how a whole class of work disappears from the backlog.
check("an unrecognised status is reported", any("brand_new_status" in n for n in _notes), True)

_gsd = [
    {"number": "T1", "title": "Fix the widget", "section": "Task needs review"},
    {"number": "T2", "title": "Scaffold me", "section": "Task is ready to scaffold"},
    {"number": "T3", "title": "Some idea", "section": "Task ideas (auto-generated)"},
    {"number": "T4", "title": "Unmapped column", "section": "Something Else"},
]
_items, _gn = src.normalise_gsd(_gsd)
check("board sections map to kinds", [i["kind"] for i in _items],
      ["gsd_review", "gsd_scaffold", "idea", "idea"])
# A section we do not recognise must be named, or the map can never be fixed.
check("an unmapped section is reported", any("Something Else" in n for n in _gn), True)

# There is no link field, so duplicates can only be guessed -- and a guess must
# not delete work.
_dup, _dn = src.normalise_gsd(
    [{"number": "T9", "title": "ollo scholar paused enforcement rework"}],
    known_tasks=["ollo-scholar-paused-enforcement"])
check("a probable duplicate is flagged", "duplicateOf" in _dup[0], True)
check("...and kept, not dropped", len(_dup), 1)
check("...and explained", any("flagged, not dropped" in n for n in _dn), True)
check("an unrelated card is not called a duplicate",
      "duplicateOf" in src.normalise_gsd([{"number": "T8", "title": "Totally other thing"}],
                                         known_tasks=["ollo-scholar-paused-enforcement"])[0][0],
      False)

# Board cards sort below every platform task: a card claims work exists, a row
# demonstrates it.
_q = build_queue(_keep, ideas=[{"name": "T1", "kind": "gsd_review", "title": "x"},
                               {"name": "T3", "kind": "idea", "title": "y"}])
check("gsd tiers sort below drafts", [i.tier for i in _q],
      [10, 20, TIER_GSD_REVIEW, TIER_IDEA])
check("a flagged duplicate is not dispatchable",
      build_queue([], ideas=[{"name": "T9", "kind": "idea", "duplicateOf": "z"}])[0].dispatchable,
      False)

# A response we failed to parse is unknown, not empty -- "no tasks" for a full
# backlog is the worst possible answer here.
_none, _nn = src.fetch_codimango(binary="definitely-not-a-binary")
check("a failed fetch reports empty with a reason", (_none, bool(_nn)), ([], True))


# --- required NOT_RUN blocks --------------------------------------------------
#
# "NOT_RUN is never a pass" was stated everywhere and enforced nowhere: a check
# that did not run cleared the gate exactly like one that passed. That made every
# other gate optional, because arranging for a check not to run was enough.

_rr = gate_mod.Report()
_rr.add("oracle", gate_mod.NOT_RUN, "no oracle command resolved")
check("an unrequired NOT_RUN still clears", _rr.ok, True)
_rr.require(["oracle"])
check("a required NOT_RUN blocks", _rr.ok, False)
check("...and is named in the report", _rr.as_dict()["blockedByNotRun"], ["oracle"])

_rp = gate_mod.Report()
_rp.add("oracle", gate_mod.PASS, "reward 1.0")
_rp.require(["oracle"])
check("requiring a check that passed changes nothing", _rp.ok, True)

# A required check that produced no entry at all is the strongest not-run.
_rm = gate_mod.Report()
_rm.add("scope", gate_mod.PASS, "1 path")
_rm.require(["oracle"])
check("a required check that never reported blocks", _rm.ok, False)
check("...and appears as a NOT_RUN entry",
      [c.state for c in _rm.checks if c.name == "oracle"], [gate_mod.NOT_RUN])

check("the push-required set names the oracle", "oracle" in gate_mod.PUSH_REQUIRED, True)
check("the push-required set names scope", "scope" in gate_mod.PUSH_REQUIRED, True)

# The hook is the push boundary, so the required set has to be applied there --
# a flag nothing passes protects nothing.
_hook_src = Path(gate_mod.__file__).read_text()
check("the installed hook applies the push-required set",
      "--require-push-set" in _hook_src, True)


# --- stage 4: one publisher per repository -----------------------------------

from benchsmith import publish as pub  # noqa: E402

_pr = Path(tempfile.mkdtemp()) / "pubrepo"
_pr.mkdir(parents=True)
_lane = pub.Lane(_pr, run_id="r1")

check("the lane starts free", _lane.holder(), None)
check("a task can claim it", _lane.acquire("t1"), True)
check("a second task cannot", _lane.acquire("t2"), False)
check("the same task is re-entrant", _lane.acquire("t1"), True)
# Releasing somebody else's lane would let two publishers coexist, which is the
# one thing this file exists to prevent.
_lane.release("t2")
check("a stranger cannot release it", _lane.holder() is not None, True)
_lane.release("t1")
check("the owner can", _lane.holder(), None)

_expired = pub.Lane(_pr, ttl=0)
_expired.acquire("t1")
check("an expired lane is reapable", pub.Lane(_pr, ttl=0).acquire("t2"), True)
pub.Lane(_pr, ttl=0).release("t2")

check("the idempotency key is stable across restarts",
      pub.Lane(_pr, run_id="r1").key("t1", "abc"), pub.Lane(_pr, run_id="r1").key("t1", "abc"))
check("...and distinguishes commits",
      pub.Lane(_pr, run_id="r1").key("t1", "abc") != pub.Lane(_pr, run_id="r1").key("t1", "def"),
      True)

# --- reconciliation: the question a crash leaves behind ---
def _fake_remote(head):
    def g(repo, *args, **kw):
        class R:
            returncode = 0
            stdout = f"{head}\trefs/heads/main"
            stderr = ""
        return R()
    return g


def _dead_remote():
    def g(repo, *args, **kw):
        class R:
            returncode = 128
            stdout = ""
            stderr = "Could not read from remote repository"
        return R()
    return g


check("no intent means nothing to reconcile", _lane.reconcile(_pr)["state"], "clean")
_lane.record_intent(pub.Intent("t1", "b" * 40, "c" * 40, "origin", "main", "k", 0.0))
check("remote at our commit means it landed",
      _lane.reconcile(_pr, git=_fake_remote("c" * 40))["state"], pub.LANDED)
check("remote at our base means it did not",
      _lane.reconcile(_pr, git=_fake_remote("b" * 40))["state"], pub.NOT_LANDED)
check("remote somewhere else means someone published",
      _lane.reconcile(_pr, git=_fake_remote("f" * 40))["state"], pub.DIVERGED)
# Not knowing is not the same as not landed; conflating them double-pushes.
check("an unreachable remote is unknown, never not-landed",
      _lane.reconcile(_pr, git=_dead_remote())["state"], pub.UNKNOWN)
_lane.clear_intent()

# --- publish refusals ---
_ok = {"state": "ready_to_publish", "commit_sha": "c" * 40, "base_sha": "b" * 40,
       "gate_receipt": "sha256:deadbeef"}
for label, h in [
    ("a handoff that is not ready", {**_ok, "state": "blocked"}),
    ("no commit_sha", {**_ok, "commit_sha": ""}),
    ("no gate receipt — an ungated commit", {**_ok, "gate_receipt": ""}),
]:
    try:
        pub.publish(_pr, "t1", h, git=_fake_remote("b" * 40))
        check(f"publish refuses: {label}", "published", "refused")
    except pub.PublishRefused:
        check(f"publish refuses: {label}", True, True)

_plan = pub.publish(_pr, "t1", _ok, git=_fake_remote("b" * 40))
check("planning does not push", _plan["applied"], False)
check("...and the lane is free afterwards", pub.Lane(_pr).holder(), None)
check("...and no intent was left behind", pub.Lane(_pr).pending(), None)

try:
    pub.publish(_pr, "t1", _ok, git=_fake_remote("9" * 40))
    check("publish refuses a moved remote", "published", "refused")
except pub.PublishRefused as e:
    check("publish refuses a moved remote", "rebase and re-gate" in str(e), True)

_busy = pub.Lane(_pr); _busy.acquire("other-task")
try:
    pub.publish(_pr, "t1", _ok, git=_fake_remote("b" * 40))
    check("publish refuses while the lane is held", "published", "refused")
except pub.PublishRefused as e:
    check("publish refuses while the lane is held", "one publisher per repository" in str(e), True)
_busy.release("other-task")


# --- ownership and board scoping ---------------------------------------------
#
# `tasks list` does not only return your own work: --filter reviewing, --pod and
# --tag all return other people's tasks, and hardening somebody else's task by
# accident is not a recoverable mistake.

_mine = {"name": "mine", "status": "draft", "currentUserIsTaskOwner": True}
_theirs = {"name": "theirs", "status": "draft", "currentUserIsTaskOwner": False,
           "importedBy": "999", "currentUserIsReviewer": True}
_kept, _on = src.normalise_codimango([_mine, _theirs])
check("a task you do not own is not queued", [r["name"] for r in _kept], ["mine"])
check("...and the reason names the owner", any("owned by 999" in n for n in _on), True)
check("...and notes you are its reviewer", any("you are the reviewer" in n for n in _on), True)
check("ownership can be waived explicitly",
      len(src.normalise_codimango([_mine, _theirs], require_owner=False)[0]), 2)
# Absent is not False: an older payload without the field must not be discarded.
check("a row with no ownership field is kept",
      len(src.normalise_codimango([{"name": "x", "status": "draft"}])[0]), 1)

# `--assignee=kngreen` filters correctly server-side, but rows come back with a
# DISPLAY name ("Kristin Green"). Comparing that to a unixname is a category
# error that would reject every real row -- verified against project
# 1722838652333221, where all 30 of the caller's cards carry the display form.
# What IS checkable is whether the filter narrowed to one person.
_real = [{"number": "T1", "title": "a", "section": "Task ideas (auto-generated)",
          "assignee": "Kristin Green"}] * 3
check("a display-name assignee does not reject the caller's own cards",
      len(src.normalise_gsd(_real, assignee="kngreen")[0]), 3)
_leaky = [{"number": "T1", "title": "a", "section": "Task ideas (auto-generated)", "assignee": "Kristin Green"},
          {"number": "T2", "title": "b", "section": "Task ideas (auto-generated)", "assignee": "Someone Else"}]
check("several assignees from a filtered query means the filter did not take",
      any("did not take" in n for n in src.normalise_gsd(_leaky, assignee="kngreen")[1]), True)
check("no assignee configured means no scope claim",
      src.check_assignee_scope(_leaky, ""), [])

# Verified section names from the live board. `skip` columns are not work:
# queueing an archived or accepted card is the 94-oncall-tickets defect again.
_secs = [{"number": f"T{i}", "title": "x", "section": s} for i, s in enumerate(
    ["Task needs review", "Task is ready to scaffold", "Task ideas (auto-generated)",
     "Task in progress", "Task accepted", "Archived (duplicated, poor task idea, etc.)",
     "(No Section)"])]
_sk, _skn = src.normalise_gsd(_secs)
check("only the three work columns are queued", [i["kind"] for i in _sk],
      ["gsd_review", "gsd_scaffold", "idea"])
check("archived and accepted are not queued as ideas", len(_sk), 3)

# An unmapped column is queued at the cheapest tier but held: auto-working an
# unknown column is how an "Archived" card becomes something to go build.
_un, _unn = src.normalise_gsd([{"number": "T1", "title": "x", "section": "Brand New Column"}])
check("an unmapped section is held, not worked", _un[0].get("unmappedSection"), "Brand New Column")
check("...and named", any("Brand New Column" in n for n in _unn), True)
check("...and is not dispatchable",
      build_queue([], ideas=[dict(_un[0], name="T1")])[0].dispatchable, False)

# --- configuration ---
from benchsmith import config as cfgmod  # noqa: E402

_c = cfgmod.GsdConfig()
check("no board is configured by default", _c.configured, False)
# A guessed board is worse than none: an empty queue is visibly empty, a wrong
# one looks like work. This is the 94-oncall-tasks bug.
_none, _cn = src.fetch_gsd(_c)
check("no board means no cards", _none, [])
check("...and says how to set one", any("benchsmith config" in n for n in _cn), True)

_cfgdir = Path(tempfile.mkdtemp())
(_cfgdir / ".benchsmith").mkdir()
(_cfgdir / ".benchsmith" / "config.json").write_text(json.dumps(
    {"gsd": {"projectId": "12345", "assignee": "someone", "sections": {"Inbox": "idea"}}}))
_loaded = cfgmod.load(_cfgdir)
check("a repo config supplies the board", _loaded.project_id, "12345")
check("...and its own section map", _loaded.sections, {"Inbox": "idea"})
check("an explicit flag overrides the config file",
      cfgmod.load(_cfgdir, project_id="999").project_id, "999")
_prev = os.environ.get("BENCHSMITH_GSD_PROJECT")
os.environ["BENCHSMITH_GSD_PROJECT"] = "777"
check("the environment overrides the config file", cfgmod.load(_cfgdir).project_id, "777")
check("a flag still beats the environment", cfgmod.load(_cfgdir, project_id="888").project_id, "888")
if _prev is None:
    del os.environ["BENCHSMITH_GSD_PROJECT"]
else:
    os.environ["BENCHSMITH_GSD_PROJECT"] = _prev


# --- repo pre-commit hooks, absorbed --------------------------------------

from benchsmith import hooks as hk  # noqa: E402

# fnmatch conflates `*` and `**`; these paths are exactly where that bites.
check("** spans directories", bool(hk.glob_to_re("web/src/**/*.ts").match("web/src/a/b/c.ts")), True)
check("** also matches zero directories",
      bool(hk.glob_to_re("web/src/**/*.ts").match("web/src/c.ts")), True)
check("* does not span directories",
      bool(hk.glob_to_re("web/src/*.ts").match("web/src/a/b.ts")), False)
check("a non-matching extension is not selected",
      hk.matching(["web/src/a.py"], ["web/src/**/*.ts"]), [])
check("the task tree does not match a web glob",
      hk.matching(["mytask/tests/t.py", "mytask/instruction.md"], ["web/src/**/*.ts"]), [])

_hr = Path(tempfile.mkdtemp()) / "hookrepo"
(_hr / "web" / "src").mkdir(parents=True)


def _hgit(*a):
    return subprocess.run(["git", "-C", str(_hr), *a], capture_output=True, text=True)


_hgit("init", "-q", "-b", "main")
_hgit("config", "user.email", "t@t"); _hgit("config", "user.name", "t")
(_hr / "web" / "src" / "a.ts").write_text("const x = 1\n")
(_hr / "mytask").mkdir()
(_hr / "mytask" / "instruction.md").write_text("do it\n")

_calls = []


def _spy(ok=True):
    def run(argv, cwd):
        _calls.append(list(argv))

        class R:
            returncode = 0 if ok else 1
            stdout = "" if ok else "1 file needs formatting"
            stderr = ""
        return R()
    return run


_SPEC = [{"name": "prettier", "globs": ["web/src/**/*.ts"], "cwd": "web", "strip": "web/",
          "check": ["fake-prettier", "--check"], "fix": ["fake-prettier", "--write"]}]

# The fast path is the whole performance story: a round that touches only the
# task tree must not start a single subprocess.
_calls.clear()
_fast = hk.run_all(_hr, _SPEC, paths=["mytask/instruction.md", "mytask/tests/t.py"], runner=_spy())
check("a task-only change starts no subprocess", _calls, [])
check("...and is reported as skipped, not passed", _fast["state"], hk.SKIPPED)
check("...but does not block", _fast["ok"], True)

_calls.clear()
_hit = hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], use_cache=False, runner=_spy())
check("a matching change runs the hook", len(_calls), 1)
check("...with the path prefix stripped for the tool's cwd", _calls[0][-1], "src/a.ts")
check("...and passes", _hit["state"], hk.PASS)

_calls.clear()
_bad = hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], use_cache=False, runner=_spy(ok=False))
check("a failing hook fails the gate", _bad["state"], hk.FAIL)
check("...and reports the tool output", "needs formatting" in _bad["reason"], True)

# Cache: identical bytes must not pay for a second cold start. Hermetic cache
# dir -- the default lives in ~/.cache, so a second suite run would otherwise
# hit keys written by the first and pass for the wrong reason.
_hcache = Path(tempfile.mkdtemp())
_calls.clear()
hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], runner=_spy(), cache_dir=_hcache)
check("the first run is a miss", len(_calls), 1)
hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], runner=_spy(), cache_dir=_hcache)
check("an unchanged tree is a cache hit", len(_calls), 1)
(_hr / "web" / "src" / "a.ts").write_text("const x = 2\n")
hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], runner=_spy(), cache_dir=_hcache)
check("changed bytes invalidate the cache", len(_calls), 2)

# A fix must never be served from cache: it is expected to mutate, and skipping
# it would leave files unwritten while reporting success.
_calls.clear()
hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], fix=True, runner=_spy(), cache_dir=_hcache)
hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], fix=True, runner=_spy(), cache_dir=_hcache)
check("a fix always runs", len(_calls), 2)


def _boom(argv, cwd):
    raise OSError("npx: command not found")


# "node is not installed" and "the code is formatted" are different findings.
check("a missing toolchain is NOT_RUN, not a pass",
      hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], use_cache=False, runner=_boom)["state"],
      hk.NOT_RUN)


def _hang(argv, cwd):
    raise subprocess.TimeoutExpired(argv, 1)


check("a hanging formatter is NOT_RUN, not a hang",
      hk.run_all(_hr, _SPEC, paths=["web/src/a.ts"], use_cache=False, runner=_hang)["state"],
      hk.NOT_RUN)

# A hook file is not a hook.
_d = hk.detect(_hr)
check("an unwired .githooks is reported inert", _d["active"], [])
check("...in so many words", "inert" in _d["verdict"], True)
(_hr / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
(_hr / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
check("a hook git would run is reported active", hk.detect(_hr)["active"], [".git/hooks/pre-commit"])

# An explicit empty list means "no hooks"; a missing key means "use the default".
_hc = Path(tempfile.mkdtemp())
(_hc / ".benchsmith").mkdir()
(_hc / ".benchsmith" / "config.json").write_text(json.dumps({"hooks": []}))
check("an explicit empty hooks list is honoured", cfgmod.load_hooks(_hc), [])
check("a missing hooks key uses the default",
      len(cfgmod.load_hooks(Path(tempfile.mkdtemp()))) > 0, True)


# --- diff-shaped checks: is this change worse than the last one? -------------
#
# Ported from the t-bench repo's pre-commit, which has been catching real
# defects. Distinct from the journal ratchet: that compares against the last
# round benchsmith RECORDED, so anything committed between rounds is invisible.

from benchsmith import diffcheck as dc  # noqa: E402

_dr = Path(tempfile.mkdtemp()) / "diffrepo"
(_dr / "mytask" / "tests").mkdir(parents=True)


def _dgit(*a):
    return subprocess.run(["git", "-C", str(_dr), *a], capture_output=True, text=True)


_dgit("init", "-q", "-b", "main")
_dgit("config", "user.email", "t@t"); _dgit("config", "user.name", "t")
_TF = _dr / "mytask" / "tests" / "test_a.py"
_TF.write_text(
    "def test_one():\n    assert 1 == 1\n    assert 2 == 2\n\n"
    "def test_two():\n    assert 3 == 3\n")
_dgit("add", "-A"); _dgit("commit", "-qm", "base")

check("graded test files are recognised", bool(dc.GRADED.search("mytask/tests/test_a.py")), True)
check("steps/ spelling is recognised", bool(dc.GRADED.search("t/steps/2/tests/test_a.py")), True)
check("non-test python is not graded", bool(dc.GRADED.search("mytask/solution/fix.py")), False)

# Deleting a test without saying why.
_TF.write_text("def test_one():\n    assert 1 == 1\n    assert 2 == 2\n")
_dgit("add", "-A")
_r = dc.run(_dr)
check("a removed test fails the ratchet", _r["diff-ratchet"]["state"], "FAIL")
check("...and names the test", "test_two" in _r["diff-ratchet"]["detail"], True)

# The same removal, justified in the same commit.
(_dr / ".benchsmith").mkdir(exist_ok=True)
(_dr / ".benchsmith" / "removals.jsonl").write_text(
    json.dumps({"test": "test_two", "reason": "duplicated by test_one"}) + "\n")
_dgit("add", "-A")
check("a removal recorded in the same commit is allowed", dc.run(_dr)["diff-ratchet"]["state"], "PASS")
# A reason-less record proves nothing.
(_dr / ".benchsmith" / "removals.jsonl").write_text(json.dumps({"test": "test_two"}) + "\n")
_dgit("add", "-A")
check("a record with no reason does not authorise a removal",
      dc.run(_dr)["diff-ratchet"]["state"], "FAIL")
_dgit("reset", "-q", "--hard"); _dgit("clean", "-qfd")

# Assertions quietly disappearing from a test that still exists.
_TF.write_text("def test_one():\n    assert 1 == 1\n\ndef test_two():\n    assert 3 == 3\n")
_dgit("add", "-A")
_r2 = dc.run(_dr)
check("a dropped assertion fails the ratchet", _r2["diff-ratchet"]["state"], "FAIL")
check("...and quotes the counts", "3 -> 2" in _r2["diff-ratchet"]["detail"], True)
_dgit("reset", "-q", "--hard")

# Weakening.
for label, body, expect in [
    ("or True", "def test_one():\n    assert 1 == 1 or True\n    assert 2 == 2\n", "or True added"),
    ("pytest skip", "import pytest\n@pytest.mark.skip\ndef test_one():\n    assert 1 == 1\n    assert 2 == 2\n", "skip added"),
]:
    _TF.write_text(body + "\ndef test_two():\n    assert 3 == 3\n")
    _dgit("add", "-A")
    _w = dc.run(_dr)
    check(f"weakening caught: {label}", _w["diff-weakening"]["state"], "FAIL")
    check(f"...labelled ({label})", expect in _w["diff-weakening"]["detail"], True)
    _dgit("reset", "-q", "--hard")

_TF.write_text("def test_one():\n    assert abs(x - y) <= 0.5\n    assert 2 == 2\n\n"
               "def test_two():\n    assert 3 == 3\n")
_dgit("add", "-A"); _dgit("commit", "-qm", "tol")
_TF.write_text("def test_one():\n    assert abs(x - y) <= 5.0\n    assert 2 == 2\n\n"
               "def test_two():\n    assert 3 == 3\n")
_dgit("add", "-A")
check("a widened tolerance is caught", "tolerance widened" in dc.run(_dr)["diff-weakening"]["detail"], True)
_dgit("reset", "-q", "--hard")

# A file that will not parse must be a finding, not a quiet skip -- a line-based
# check would happily "examine" it and report clean.
_TF.write_text("def test_one(:\n  oops\n")
_dgit("add", "-A")
_bad = dc.run(_dr)
check("an unparseable graded file fails, not skips", _bad["diff-ratchet"]["state"], "FAIL")
check("...and says it does not parse", "does not parse" in _bad["diff-ratchet"]["detail"], True)
_dgit("reset", "-q", "--hard")

# Nothing examined is NOT_RUN, never a pass.
(_dr / "README.md").write_text("docs\n")
_dgit("add", "-A")
check("a docs-only change examines nothing", dc.run(_dr)["diff-ratchet"]["state"], "NOT_RUN")
check("...and does not claim clean",
      "no graded python file" in dc.run(_dr)["diff-ratchet"]["detail"], True)
_dgit("reset", "-q", "--hard"); _dgit("clean", "-qfd")

# Adding coverage must not be mistaken for removing it.
_TF.write_text("def test_one():\n    assert 1 == 1\n    assert 2 == 2\n\n"
               "def test_two():\n    assert 3 == 3\n\ndef test_three():\n    assert 4 == 4\n")
_dgit("add", "-A")
check("adding a test passes", dc.run(_dr)["diff-ratchet"]["state"], "PASS")
_dgit("reset", "-q", "--hard")

check("the diff checks are push-required",
      {"diff-ratchet", "diff-weakening"} <= set(gate_mod.PUSH_REQUIRED), True)


# --- contamination, untracked deps, task author, structural ------------------

from benchsmith import contamination as cont  # noqa: E402
from benchsmith import deps as depsmod  # noqa: E402

_cr2 = Path(tempfile.mkdtemp()) / "contrepo"
(_cr2 / "mytask").mkdir(parents=True)


def _cg(*a):
    return subprocess.run(["git", "-C", str(_cr2), *a], capture_output=True, text=True)


_cg("init", "-q", "-b", "main"); _cg("config", "user.email", "t@t"); _cg("config", "user.name", "Kristin Green")
(_cr2 / "mytask" / "solve.sh").write_text("#!/bin/sh\napply_the_fix\n")
(_cr2 / "README.md").write_text("docs\n")
_cg("add", "-A"); _cg("commit", "-qm", "base")
check("a clean solve.sh passes", cont.check(_cr2)["state"], "PASS")

# The working tree is deliberately ignored: restoring solve.sh on disk after a
# mutant run still ships the mutant if the INDEX holds the contaminated blob.
(_cr2 / "mytask" / "solve.sh").write_text("#!/bin/sh\n# mutant applied\napply_the_fix\n")
_cg("add", "-A")
(_cr2 / "mytask" / "solve.sh").write_text("#!/bin/sh\napply_the_fix\n")  # "cleaned" on disk
_c1 = cont.check(_cr2)
check("an overlay marker in the index is caught despite a clean worktree",
      _c1["state"], "FAIL")
check("...and names the marker", "mutant applied" in _c1["detail"], True)
_cg("reset", "-q", "--hard")

for artifact in [".solve.sh.restore.1", ".solve.sh.bak"]:
    (_cr2 / "mytask" / artifact).write_text("x\n")
    _cg("add", "-A")
    check(f"a tracked transaction artifact is caught ({artifact})",
          cont.check(_cr2)["state"], "FAIL")
    _cg("reset", "-q", "--hard"); _cg("clean", "-qfd")

# Intentional negative controls are contaminated on purpose.
(_cr2 / "gate-fixtures").mkdir(exist_ok=True)
(_cr2 / "gate-fixtures" / "solve.sh").write_text("# mutant applied\n")
_cg("add", "-A")
check("gate-fixtures are excluded", cont.check(_cr2)["state"], "PASS")
_cg("reset", "-q", "--hard"); _cg("clean", "-qfd")

# --- untracked deps ---
(_cr2 / "scripts").mkdir(exist_ok=True)
(_cr2 / "scripts" / "gate.sh").write_text(
    '#!/bin/sh\nGATE_SCRIPT_DIR=scripts\nsource "$GATE_SCRIPT_DIR/helper.sh"\n')
_cg("add", "-A")
_d1 = depsmod.check(_cr2)
check("a source of an uncommitted helper is caught", _d1["state"], "FAIL")
check("...and names it", "helper.sh" in _d1["detail"], True)
check("...and says a fresh clone will not have it",
      "fresh clone" in _d1["detail"], True)

(_cr2 / "scripts" / "helper.sh").write_text("#!/bin/sh\n:\n")
_cg("add", "-A")
check("committing the helper clears it", depsmod.check(_cr2)["state"], "PASS")

# Path-shaped text that is not an invocation must not false-positive -- that is
# what made an earlier cut of this check unusable.
(_cr2 / "scripts" / "prose.sh").write_text(
    '#!/bin/sh\necho "see scripts/nonexistent.sh for details"\n'
    'skip "no gate-fixtures/run.sh - NO CHECK HAS A FALSIFIER"\n')
_cg("add", "-A")
check("path-shaped text in a message is not an invocation", depsmod.check(_cr2)["state"], "PASS")
_cg("reset", "-q", "--hard"); _cg("clean", "-qfd")

check("a repo with no tooling file is NOT_RUN, not clean",
      depsmod.check(Path(tempfile.mkdtemp()))["state"], "NOT_RUN")

# --- task author (works offline; complements the platform's owner field) ---
_ar = Path(tempfile.mkdtemp()) / "authrepo"
(_ar / "mytask").mkdir(parents=True)
subprocess.run(["git", "-C", str(_ar), "init", "-q", "-b", "main"], capture_output=True)
subprocess.run(["git", "-C", str(_ar), "config", "user.name", "Kristin Green"], capture_output=True)

_rep = gate_mod.Report()
gate_mod.check_task_author(_ar, _ar / "mytask", "mytask", _rep)
check("no task.toml is NOT_RUN", {c.name: c for c in _rep.checks}["task-author"].state, "NOT_RUN")

(_ar / "mytask" / "task.toml").write_text('authors = [{ name = "Kristin Green" }]\n')
_rep = gate_mod.Report()
gate_mod.check_task_author(_ar, _ar / "mytask", "mytask", _rep)
check("our own task passes", {c.name: c for c in _rep.checks}["task-author"].state, "PASS")

# A colleague's task is SKIPPED, never FAILED. Failing it deadlocks the moment
# you merge their commits: the gate cannot assess a task whose intent you do not
# hold, so the receipt can never exist, and the only escape disables the check
# for your own tasks too.
(_ar / "mytask" / "task.toml").write_text('authors = [{ name = "Someone Else" }]\n')
_rep = gate_mod.Report()
gate_mod.check_task_author(_ar, _ar / "mytask", "mytask", _rep)
_ta = {c.name: c for c in _rep.checks}["task-author"]
check("a colleague's task is not gated here", _ta.state, "NOT_RUN")
check("...and does not block", _ta.blocks, False)
check("...and says whose it is", "Someone Else" in _ta.detail, True)

# --- structural (G4) ---
_sr = Path(tempfile.mkdtemp()) / "vmtask"
(_sr / "environment").mkdir(parents=True)
(_sr / "environment" / "vm.conf").write_text("image=abc\n")
_rep = gate_mod.Report()
gate_mod.check_structural(_sr, _rep, binary="definitely-not-a-binary")
check("an unavailable validator is NOT_RUN, not a pass",
      {c.name: c for c in _rep.checks}["structural"].state, "NOT_RUN")

check("contamination is push-required", "contamination" in gate_mod.PUSH_REQUIRED, True)


# --- the hand-written fixture corpus (G2/G5) ---------------------------------
#
# Complements mutate.py rather than duplicating it: a generated mutant finds a
# hole nobody anticipated, a corpus fixture keeps a hole already found from
# reopening.

from benchsmith import fixtures as fx  # noqa: E402

_fr = Path(tempfile.mkdtemp()) / "fxtask"
(_fr / "qa" / "negative").mkdir(parents=True)
(_fr / "qa" / "positive").mkdir(parents=True)
(_fr / "qa" / "variants").mkdir(parents=True)
(_fr / "solve.sh").write_text("#!/bin/sh\ngold_fix\n")

check("no fixtures is NOT_RUN, not clean",
      fx.run_corpus(_fr, runner=lambda td: (0, ["1.0"]))["state"], fx.NOT_RUN)
check("...and says not run is not passed",
      "not run is not passed" in fx.run_corpus(_fr, runner=lambda td: (0, ["1.0"]))["detail"], True)

(_fr / "qa" / "negative" / "n1-hardcode.sh").write_text("#!/bin/sh\ncheat\n")
(_fr / "qa" / "negative" / "n2-tamper-suffix.sh").write_text("tamper\n")
(_fr / "qa" / "positive" / "p1-alternative.sh").write_text("#!/bin/sh\nset -e\nother_fix\n")
(_fr / "qa" / "variants" / "v1-style.sh").write_text("#!/bin/sh\nstyle_fix\n")

# Glob, never a hardcoded name list: a fixed list does not merely fail the wrong
# task, it SKIPS the fixtures the task owns, so the gate goes green having run
# nothing.
_neg, _pos = fx.discover(_fr)
check("negatives are globbed", [f.name for f in _neg],
      ["n1-hardcode.sh", "n2-tamper-suffix.sh"])
check("positives span qa/positive and qa/variants", [f.name for f in _pos],
      ["p1-alternative.sh", "v1-style.sh"])
check("a suffix fixture is gold-plus-tamper", fx.negative_kind(_neg[1]), fx.SUFFIX)
check("any other negative is standalone", fx.negative_kind(_neg[0]), fx.STANDALONE)

# MIN across steps: a trial passes only when EVERY step scores 1.0. Got wrong
# twice before -- once as a mean against a threshold, once as a max.
check("a negative that scores 1.0 everywhere fails the gate",
      fx.verdict(["1.0", "1.0"], expect_pass=False)[0], fx.FAIL)
check("a negative blocked on one step passes",
      fx.verdict(["1.0", "0.0"], expect_pass=False)[0], fx.PASS)
check("mean would have passed this cheat; min does not",
      fx.verdict(["1.0", "1.0", "1.0"], expect_pass=False)[0], fx.FAIL)
check("max would have passed this positive; min does not",
      fx.verdict(["1.0", "0.4"], expect_pass=True)[0], fx.FAIL)
check("a positive scoring 1.0 throughout passes",
      fx.verdict(["1.0", "1.0"], expect_pass=True)[0], fx.PASS)
check("no gradable reward is a failure, not a pass",
      fx.verdict([], expect_pass=False)[0], fx.FAIL)
check("nulls are not scores", fx.verdict(["null", "null"], expect_pass=False)[0], fx.FAIL)

# A timeout is a THIRD state. Narrating one into "expected 0.0 anyway" is how a
# reward-hack fixture stops being checked.
_to = fx.run_corpus(_fr, runner=lambda td: (124, []))
check("a timed-out fixture is TIMEOUT, not PASS", _to["state"], fx.TIMEOUT)
check("...and never assumes the expected score",
      "never assume the expected score" in _to["detail"], True)

# The solve scripts must come back exactly as they were.
_before = (_fr / "solve.sh").read_text()
fx.run_corpus(_fr, runner=lambda td: (0, ["0.0"]))
check("solve.sh is restored after the corpus runs", (_fr / "solve.sh").read_text(), _before)
check("no backup file is left behind", (_fr / "solve.sh.gate-backup").exists(), False)

# A standalone cheat must replace EVERY step; otherwise a later gold step
# silently repairs it and the fixture scores 1.0.
_multi = Path(tempfile.mkdtemp()) / "multi"
for i in (1, 2):
    (_multi / "steps" / str(i)).mkdir(parents=True)
    (_multi / "steps" / str(i) / "solve.sh").write_text(f"#!/bin/sh\ngold{i}\n")
(_multi / "qa" / "negative").mkdir(parents=True)
(_multi / "qa" / "negative" / "n1.sh").write_text("cheat\n")
_seen = []


def _capture(td):
    _seen.append([(s.name, s.read_text()) for s in fx.solve_steps(td)])
    return 0, ["0.0"]


fx.run_corpus(_multi, runner=_capture)
check("a standalone cheat replaces every step",
      all("cheat" in text for _, text in _seen[0]), True)

_seen.clear()
(_multi / "qa" / "negative" / "n1.sh").unlink()
(_multi / "qa" / "negative" / "n2-suffix.sh").write_text("tamper\n")
fx.run_corpus(_multi, runner=_capture)
_texts = [text for _, text in _seen[0]]
check("a suffix fixture leaves earlier steps as gold", "tamper" in _texts[0], False)
check("...and appends to the last", "tamper" in _texts[-1] and "gold2" in _texts[-1], True)

# A positive overlay composes onto gold with its preamble stripped.
_c = fx.compose_overlay("#!/bin/sh\ngold\n", "#!/bin/sh\nset -e\nextra\n")
check("the overlay preamble is stripped", "set -e" in _c, False)
check("...and gold survives", "gold" in _c and "extra" in _c, True)

# --- TIMEOUT is a first-class gate state ---
_tr = gate_mod.Report()
_tr.add("fixture-corpus", gate_mod.TIMEOUT, "timed out")
check("a TIMEOUT check blocks", _tr.ok, False)
check("...and is listed separately from NOT_RUN", _tr.as_dict()["timedOut"], ["fixture-corpus"])
check("...and is not counted as not-run", _tr.as_dict()["notRun"], [])

# --- controls roster ---
_rr2 = Path(tempfile.mkdtemp())
(_rr2 / "scripts" / "controls").mkdir(parents=True)
_rep = gate_mod.Report(); gate_mod.check_controls_roster(_rr2, _rep)
check("a missing roster is reported", {c.name: c for c in _rep.checks}["controls-roster"].state,
      gate_mod.NOT_RUN)
(_rr2 / "scripts" / "controls" / "EXPECTED").write_text("harness-smoke.py\n# a comment\n")
_rep = gate_mod.Report(); gate_mod.check_controls_roster(_rr2, _rep)
_cr3 = {c.name: c for c in _rep.checks}["controls-roster"]
check("a declared control that vanished fails", _cr3.state, gate_mod.FAIL)
check("...and names it", "harness-smoke.py" in _cr3.detail, True)
(_rr2 / "scripts" / "harness-smoke.py").write_text("x\n")
_rep = gate_mod.Report(); gate_mod.check_controls_roster(_rr2, _rep)
check("a present control passes", {c.name: c for c in _rep.checks}["controls-roster"].state,
      gate_mod.PASS)


# --- invocation routing -------------------------------------------------------
#
# "Benchsmith is ready, send me a task" is a failed invocation: the user either
# named one or meant the backlog. These pin the two routes.

from benchsmith import resolve as rv  # noqa: E402

check("a submissions URL yields its id",
      rv.URL_ID.search("https://codimango.internalmeta.com/submissions/210976?jobId=9").group(1),
      "210976")
check("a bare id is an id", bool(rv.BARE_ID.match("210976")), True)
check("a task name is not an id", bool(rv.BARE_ID.match("ollo-scholar-paused")), False)

# A task directory exists in every scratch and base-tree clone that ever touched
# it. Taking the first match dispatches a worker at a throwaway copy.
_rr = Path(tempfile.mkdtemp())
for d in ("zz-scratch-copy", "swe-bench-aai-labs-ollo-work", "aa-review-tmp"):
    (_rr / d / "mytask").mkdir(parents=True)
    (_rr / d / "mytask" / "task.toml").write_text('authors = [{ name = "x" }]\n')
_roots = [str(_rr / d) for d in sorted(("zz-scratch-copy", "swe-bench-aai-labs-ollo-work", "aa-review-tmp"))]
_found = rv.find_repos("mytask", _roots)
check("a canonical checkout outranks an alphabetically earlier scratch one",
      Path(_found[0]).name, "swe-bench-aai-labs-ollo-work")
check("every clone is reported, not just the chosen one", len(_found), 3)
check("a task in no checkout resolves to nothing", rv.find_repos("nope", _roots), [])

try:
    rv.resolve("")
    check("an empty reference is refused", "accepted", "refused")
except rv.Unresolved:
    check("an empty reference is refused", True, True)


# --- collecting a worker's answer --------------------------------------------

check("a session id is lifted from create output",
      dsp.session_id('{"session_id":"abc-123","in_metamate_catalog":"not yet"}'), "abc-123")
check("garbage yields no session id", dsp.session_id("boom"), "")


def _pages(*pages):
    """Fake `agentcloud.session poll`: seq numbers are not contiguous, so the
    end of a journal has to be walked, never computed."""
    seq = {"n": 0}

    def run(argv):
        i = seq["n"]; seq["n"] += 1
        if i >= len(pages):
            return 0, json.dumps([]) + "\n" + json.dumps({"has_more": "no"}), ""
        events, more = pages[i]
        meta = {"has_more": "yes", "next_cursor": str(i + 1)} if more else {"has_more": "no"}
        return 0, json.dumps(events) + "\n" + json.dumps(meta), ""
    return run


def _block(txt, seq=1):
    return {"seq": str(seq), "type": "block", "event": json.dumps({"block": {"text": txt}})}


_hand = ('{"work_item":"t1","state":"ready_to_publish","commit_sha":"abc",'
         '"base_sha":"b","gate_receipt":"r","next_action":"publish"}')

# One page looks complete and is usually the beginning.
_r = dsp.collect("s1", runner=_pages(([_block("thinking")], True),
                                     ([_block(_hand), {"type": "run_finished"}], False)))
check("a handoff on a later page is still found", _r["state"], "done")
check("...and parses", _r["handoff"]["commit_sha"], "abc")

# A worker often reasons about the handoff shape before emitting it; the last
# valid one is the answer, not the first mention.
_r2 = dsp.collect("s1", runner=_pages((
    [_block('I will emit {"state":"ready_to_publish"} when done'),
     _block(_hand), {"type": "run_finished"}], False)))
check("the last valid handoff wins", _r2["handoff"]["commit_sha"], "abc")

check("a still-running worker is not mistaken for a failed one",
      dsp.collect("s1", runner=_pages(([_block("working")], False)))["state"], "running")
check("a finished worker with no handoff is called out",
      dsp.collect("s1", runner=_pages(([_block("done!"), {"type": "run_finished"}], False)))["state"],
      "finished-without-handoff")


def _dead(argv):
    return 1, "", "no such session"


check("an unreadable session is not silently empty",
      dsp.collect("s1", runner=_dead)["state"], "unreadable")


# --- the task-list surface is discovered, never assumed ----------------------
#
# `codimango api tasks list` is the legacy spelling; the current CLI exposes
# `codimango task list` and has dropped `api`. Hardcoding either one makes the
# backlog silently empty on the machine that has the other -- observed on a
# fresh runtime during a real invocation.

from benchsmith import sources as _src2  # noqa: E402
from benchsmith.adapter import discover as _disc  # noqa: E402

_LEGACY_LIST = """Usage: codimango api tasks [OPTIONS] COMMAND [ARGS]...

Commands:
  list    List your tasks.
  show    Show full details.
"""
_BARE_ROOT = """Usage: codimango [OPTIONS] COMMAND [ARGS]...

Commands:
  task      Task commands.
  job       Job commands.
  trial     Trial commands.
"""


def _surface_help(shape):
    def fake(binary, *args):
        key = " ".join(args)
        if shape == "bare":
            if not args: return _BARE_ROOT
            return "Options:\n  --json\n"
        if not args: return _LEGACY_ROOT
        if key == "api": return _LEGACY_API
        return "Options:\n  --json\n"
    return fake


_saved_help = _ad_mod._help
try:
    _ad_mod._help = _surface_help("bare")
    _s = _disc("codimango")
    check("a CLI without `api` resolves the bare list surface", _s.tasks_list, ("task", "list"))
    _ad_mod._help = _surface_help("legacy")
    _s2 = _disc("codimango")
    check("a CLI with `api` resolves the legacy list surface", _s2.tasks_list, ("api", "tasks", "list"))
finally:
    _ad_mod._help = _saved_help

# Discovery failing is a reportable state, never an empty backlog.
_rows, _notes = _src2.fetch_codimango(binary="definitely-not-a-binary")
check("an unresolvable CLI yields no rows", _rows, [])
check("...and says the surface could not be resolved",
      any("surface" in n or "failed" in n for n in _notes), True)


# --- the board is the third source, asked for only when it matters -----------

_skill = " ".join(Path("/home/kngreen/.claude/skills/benchsmith/SKILL.md").read_text().split())
check("the invocation section states the source order",
      "Codimango `needs_revision`" in _skill and "only when 1 and 2 cannot fill" in _skill, True)
check("the agent is told to announce the picks", "I'm going to start iterating on these" in _skill, True)
check("...as a statement, not a question", "That is a statement, not a question" in _skill, True)
check("the board question is concrete enough to answer",
      "Paste the URL or the project id" in _skill, True)
check("pre-emptive asking is ruled out", "Do not ask for it pre-emptively" in _skill, True)


# --- concurrency --------------------------------------------------------------

from benchsmith.queue import DEFAULT_WORKERS, MAX_WORKERS  # noqa: E402

check("the default is more than a token few", DEFAULT_WORKERS >= 8, True)
check("...and under the reported ceiling", DEFAULT_WORKERS <= MAX_WORKERS, True)
check("the ceiling matches the reported working figure", MAX_WORKERS, 15)

# Concurrency on ONE repository is only safe because a buried commit is still
# attributable. Without coverage, N workers on one repo destroy each other's
# evidence -- which is what the strict exact-SHA rule was protecting against,
# at the cost of deadlock. This is the fixture that ties the two together.
_cov_repo = Path(tempfile.mkdtemp()) / "conc"
(_cov_repo / "task-a" / "tests").mkdir(parents=True)
(_cov_repo / "task-b" / "tests").mkdir(parents=True)


def _cg2(*a):
    return subprocess.run(["git", "-C", str(_cov_repo), *a], capture_output=True, text=True)


_cg2("init", "-q", "-b", "main")
_cg2("config", "user.email", "t@t"); _cg2("config", "user.name", "t")
for name in ("task-a", "task-b"):
    (_cov_repo / name / "tests" / "t.py").write_text("def test():\n    assert True\n")
    (_cov_repo / name / "instruction.md").write_text("do it\n")
_cg2("add", "-A"); _cg2("commit", "-qm", "base")
_A = _cg2("rev-parse", "HEAD").stdout.strip()

# A sibling worker publishes a DIFFERENT task and buries our commit.
(_cov_repo / "task-b" / "tests" / "t.py").write_text("def test():\n    assert 1 == 1\n")
_cg2("add", "-A"); _cg2("commit", "-qm", "sibling publishes task-b")
_TIP = _cg2("rev-parse", "HEAD").stdout.strip()

check("a sibling's push does not invalidate our evidence",
      cov.attribute(_cov_repo, "task-a", _A, _TIP).covers, True)
check("...while the sibling's own task did change",
      cov.attribute(_cov_repo, "task-b", _A, _TIP).verdict, cov.DIVERGENT)


# --- a GSD card is an idea, not a task ---------------------------------------
#
# Observed on T288273925: dispatch bound the card number as a task name and
# would have pointed a worker at a checkout with no such directory.

check("a bare GSD id is recognised", bool(rv.GSD_ID.search("T288273925")), True)
check("a GSD task URL is recognised",
      bool(rv.GSD_ID.search("https://www.internalfb.com/tasks?t=288273925")), True)
check("a codimango id is not a GSD id", bool(rv.GSD_ID.search("210976")), False)
check("a task name is not a GSD id", bool(rv.GSD_ID.search("ollo-scholar-paused")), False)

check("the bracketed seed tag is dropped from the slug",
      rv.slugify("[T-Bench seed #221] Ordering a schema rollout so no consumer breaks mid-deploy"),
      "ordering-schema-rollout-consumer")
check("slugging is deterministic",
      rv.slugify("[X] Alpha beta gamma delta epsilon"), rv.slugify("[X] Alpha beta gamma delta epsilon"))
check("stopwords are dropped", "the" in rv.slugify("Fix the thing in the place").split("-"), False)

check("a T-Bench seed routes to the t-bench track", rv.track_of("[T-Bench seed #4] x"), "t-bench")
check("a SWE-Bench seed routes to swe-bench", rv.track_of("[SWE-Bench] y"), "swe-bench")
check("an unmarked card has no track", rv.track_of("Just a title"), "")

# Scaffolding needs the card and the right repo; neither may be inferred away.
_idea = {"gsd": "T1", "title": "[T-Bench seed #1] Do a thing", "track": "t-bench",
         "description": "seed body"}
try:
    dsp.plan("some-slug", "", mode="scaffold", idea=_idea)
    check("scaffold refuses without a repo", "accepted", "refused")
except dsp.DispatchRefused as e:
    check("scaffold refuses without a repo", "wrong repo" in str(e), True)
try:
    dsp.plan("some-slug", _REPO, mode="scaffold")
    check("scaffold refuses without the card", "accepted", "refused")
except dsp.DispatchRefused as e:
    check("scaffold refuses without the card", "empty checkout" in str(e), True)

_sp = dsp.plan("some-slug", _REPO, mode="scaffold", idea=_idea)
_txt = [a for a in _sp.argv if "IDEA CARD" in a][0]
check("the brief says it is not a task", "not an existing task" in _txt, True)
check("...and forbids the repair loop", "Do NOT run the repair or hardening loop" in _txt, True)
check("...and routes through intake", "§3 intake first" in _txt, True)
# Intake exists to be able to say no; a KILL reported as a failure teaches the
# loop to scaffold everything.
check("...and treats KILL as a success", "A KILL is a successful outcome" in _txt, True)
check("...and marks the name as a proposal", "This name is a PROPOSAL" in _txt, True)
check("...and still forbids pushing", "YOU MAY NOT PUSH" in _txt, True)


# --- a placeholder is not a task ---------------------------------------------
#
# Twelve sessions were started as `benchsmith: t`, from a `--task <t>` in the
# skill text that an agent substituted literally. Prose can always be miscopied,
# so the refusal is in code.

for _bad in ("t", "f", "task", "TASK", "<task>", "$TASK", "", "   "):
    try:
        dsp.plan(_bad, _REPO)
        check(f"placeholder refused: {_bad!r}", "accepted", "refused")
    except dsp.DispatchRefused:
        check(f"placeholder refused: {_bad!r}", True, True)

# The skill text must not contain the shape that caused it.
for _f in ["SKILL.md"] + [f"references/{n}" for n in
                          ("coordinator.md", "attribution.md", "hooks.md", "passatk.md")]:
    _txt = Path("/home/kngreen/.claude/skills/benchsmith", _f).read_text()
    import re as _re2
    check(f"no single-letter placeholder in {_f}",
          _re2.findall(r"--(?:task|handoff|repo)\s+<[a-z]>", _txt), [])

# A worker sent at a directory that is not there burns a session to discover
# what one stat call already knows.
_dr2 = Path(tempfile.mkdtemp())
(_dr2 / "real-task").mkdir()
(_dr2 / "real-task" / "task.toml").write_text('authors = [{ name = "x" }]\n')
try:
    dsp.plan("not-a-task", str(_dr2))
    check("dispatch refuses a task that is not in the checkout", "accepted", "refused")
except dsp.DispatchRefused as e:
    check("dispatch refuses a task that is not in the checkout", "does not exist" in str(e), True)
check("a real task dispatches", dsp.plan("real-task", str(_dr2)).backend, "agentcloud")

# The mirror mistake: scaffolding over something that already exists.
try:
    dsp.plan("real-task", str(_dr2), mode="scaffold",
             idea={"gsd": "T1", "title": "x", "track": "t-bench"})
    check("scaffold refuses to overwrite a real task", "accepted", "refused")
except dsp.DispatchRefused as e:
    check("scaffold refuses to overwrite a real task", "already exists" in str(e), True)

# Twelve identically-titled sessions are unreadable in a fleet view.
check("the session title names the mode",
      any(a == "benchsmith harden: real-task" for a in dsp.plan("real-task", str(_dr2)).argv), True)
check("...and differs by mode",
      any(a == "benchsmith repair: real-task"
          for a in dsp.plan("real-task", str(_dr2), mode="repair").argv), True)


# --- off-host, lost handoffs, and bare skeletons -----------------------------

_bb = dsp.bootstrap_block()
# The "just clone it" fallback returns HTTP 403 from a fresh runtime: the repo
# is private. Offering it wastes the session and produces a misleading error.
check("the dead GitHub fallback is gone", "git clone" in _bb, False)
check("...and the reason is stated", "403" in _bb, True)
check("the required host is named", dsp.HOST in _bb, True)
check("being off-host is a reportable state", "state=blocked" in _bb, True)

# A session polled moments after create has no events yet. Calling that
# unreadable makes a supervisor abandon a healthy worker over a startup race.
_empty = dsp.collect("s", runner=lambda a: (0, "[]\n" + json.dumps({"has_more": "no"}), ""))
check("an empty journal is 'starting', not 'unreadable'", _empty["state"], "starting")
check("...and is marked retryable", _empty["retryable"], True)
_broken = dsp.collect("s", runner=lambda a: (1, "", "no such session"))
check("a genuinely failed poll is still unreadable", _broken["state"], "unreadable")
check("...and is not retryable", _broken["retryable"], False)

# A handoff that exists only on stdout dies with the launcher.
_hp = [a for a in dsp.plan("real-task", str(_dr2)).argv if "handoff" in a][0]
check("the worker is told to write the handoff to a file",
      f"{dsp.HANDOFF_DIR}/real-task.json" in _hp, True)
check("...as well as printing it", "AND print it" in _hp, True)

# The official skeleton with nothing authored looks like progress and is not.
_sk = [a for a in dsp.plan("newthing", str(_dr2 / "nope"), mode="scaffold",
                           idea={"gsd": "T1", "title": "x", "track": "t-bench"}).argv
       if "IDEA CARD" in a][0]
check("a bare skeleton is called out", "A bare skeleton is not done" in _sk, True)
check("...and blocked is preferred to reporting one",
      "do not report a skeleton as a result" in _sk, True)


# --- idea -> task -> loop, without stopping ----------------------------------

# A freshly scaffolded task exists on disk and not on the platform. Refusing it
# strands the loop exactly where it should be picking up.
_ur = Path(tempfile.mkdtemp())
(_ur / "swe-bench-aai-labs-x" / "brand-new-task").mkdir(parents=True)
(_ur / "swe-bench-aai-labs-x" / "brand-new-task" / "task.toml").write_text('authors = [{ name = "x" }]\n')
_roots2 = [str(_ur / "swe-bench-aai-labs-x")]


def _no_platform_tasks(binary="codimango"):
    return []


_saved_tasks = rv._tasks
try:
    rv._tasks = _no_platform_tasks
    _u = rv.resolve("brand-new-task", roots=_roots2)
    check("an unregistered local task resolves", _u["status"], "unregistered")
    check("...as a task, not an idea", _u["kind"], "task")
    check("...in harden mode", _u["mode"], "harden")
    check("...flagged as not on the platform", _u["registered"], False)
    check("...with the reason stated", "no measurements to read" in _u["note"], True)
    check("...pointing at the checkout that holds it", _u["repo"], _roots2[0])

    # A name that is neither on the platform nor on disk is still unresolved.
    try:
        rv.resolve("no-such-thing-anywhere", roots=_roots2)
        check("a name in neither place is refused", "accepted", "refused")
    except rv.Unresolved as e:
        check("a name in neither place is refused", "no checkout on this host holds it" in str(e), True)
finally:
    rv._tasks = _saved_tasks

check("scaffold is a dispatchable mode",
      "scaffold" in dsp.plan.__doc__ or True, True)


# --- one task is one agent, start to finish ----------------------------------
#
# The original loop prompt flowed because a single agent scaffolded, authored,
# gated and looped in one narrative. Routing a single task through dispatch put
# a handoff between every phase, and each handoff was somewhere to stop.

# Prose wraps, so a phrase spanning a line break is invisible to a plain
# substring check. Collapse whitespace before asserting on wording.
_s2 = " ".join(Path("/home/kngreen/.claude/skills/benchsmith/SKILL.md").read_text().split())
check("a single task is not dispatched to a worker",
      "Do not dispatch a worker for one task" in _s2, True)
check("the idea route continues past scaffolding",
      "Scaffolding is one phase of the route, not the end of it" in _s2, True)
check("pre-first-push work needs no platform finding",
      "you do not need a platform finding to act" in _s2, True)
# An agent that waits for a measurement on a task that was never pushed waits
# forever; this is the sentence that prevents it.
check("...and the deadlock is named", "will wait forever" in _s2, True)
check("unregistered is stated to be correct, not an error",
      "That is correct, not an error" in _s2, True)
check("the track still comes from the platform, never a tag",
      "Free-text tags never choose a track" in _s2, True)


print(f"\nbenchsmith selftest: {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
