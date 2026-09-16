"""The pre-push gate.

Every check reports PASS, FAIL or NOT_RUN, and **NOT_RUN is never a pass**. A
check that could not run must not read like one that ran and was satisfied --
that distinction is the whole point of the tri-state.

Installed as a `commit-msg` + `pre-push` pair so it cannot be quietly skipped: a
rule the loop can break by forgetting is not a gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from . import safety
from .identifiers import validate_sha, validate_task_path
from .journal import Journal, surface_hashes

REQUIRED_TAGS = ("benchsmith-v1", "aai-labs", "semi-synthetic", "private_repos_1p")
TEAM_TAG_PREFIX = "aai-labs-"

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
# A fourth state, kept distinct on purpose. A timeout is not a pass, not a
# failure, and not "did not run" -- it is a verdict that was never reached.
# Folding it into NOT_RUN loses the one thing a reader needs: that the check
# was attempted and the answer is missing. Narrating a timed-out reward-hack
# fixture into "it was expected to score 0.0 anyway" is how that fixture stops
# being checked.
TIMEOUT = "TIMEOUT"


# Checks whose absence makes a push unsafe rather than merely unmeasured. These
# are the ones where NOT_RUN and FAIL have the same consequence: you do not know
# the thing you would have to know in order to push.
PUSH_REQUIRED = ("oracle", "artifact-transfer", "scope", "config-integrity", "tags",
                 "diff-ratchet", "diff-weakening", "contamination",
                 # A repair that addressed nothing is not a repair.
                 "review-findings")
CONTROL_REQUIRED = ("control-manifest", "obligation-witnesses", "mutation-adequacy",
                    "metamorphic-variation", "verifier-closure")
INAPPLICABLE_PUSH_CHECKS = frozenset({
    "artifact-transfer", "diff-ratchet", "diff-weakening",
})


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    blocking: bool = True
    required: bool = False

    @property
    def blocks(self) -> bool:
        # NOT_RUN does not block on its own -- at scaffold time half these checks
        # legitimately cannot run yet. But a check marked REQUIRED that did not
        # run does block, because at that point "we did not measure it" and "it
        # failed" have the same consequence. Without this, every gate the loop
        # depends on is skippable by arranging for it not to run, which is the
        # one hole that makes all the others optional.
        if not self.blocking:
            # Explicitly advisory. Requiring an advisory check was a
            # contradiction that silently won, and it produced a gate nobody
            # could satisfy.
            return False
        if self.state in (FAIL, TIMEOUT):
            return True
        return bool(self.required and self.state == NOT_RUN)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)

    def add(self, *a, **kw) -> None:
        self.checks.append(Check(*a, **kw))

    def require(self, names) -> None:
        """Mark applicable checks whose NOT_RUN must block."""
        wanted = set(names or ())
        for c in self.checks:
            if c.name in wanted and c.blocking:
                c.required = True
        missing = wanted - {c.name for c in self.checks}
        for name in sorted(missing):
            # A required check that produced no entry at all is the strongest
            # form of not-run: it did not even get as far as reporting.
            self.checks.append(Check(name, NOT_RUN, "required, but the check never ran",
                                     required=True))

    @property
    def ok(self) -> bool:
        return not any(c.blocks for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.__dict__ for c in self.checks],
            "notRun": [c.name for c in self.checks if c.state == NOT_RUN],
            "blockedByNotRun": [c.name for c in self.checks
                                if c.required and c.state == NOT_RUN],
            "timedOut": [c.name for c in self.checks if c.state == TIMEOUT],
            "artifacts": self.artifacts,
        }

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=0)
        lines = [f"  {c.state:<7} {c.name:<{width}}  {c.detail}".rstrip() for c in self.checks]
        lines.append("")
        lines.append("  GATE PASS — may push" if self.ok else "  GATE FAIL — fix before pushing")
        return "\n".join(lines)


def _toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# --- individual checks ------------------------------------------------------


def check_tags(task_dir: Path, report: Report) -> None:
    """Gate the full tag set before the FIRST push, not at the terminal check.

    A task that reaches its first cloud round untagged is already mis-attributed,
    and Labs cannot see it at all.
    """
    doc = _toml(Path(task_dir) / "task.toml")
    if not doc:
        report.add("tags", NOT_RUN, "task.toml unreadable")
        return
    tags = (doc.get("metadata") or {}).get("tags")
    if not isinstance(tags, list):
        report.add("tags", FAIL, "[metadata].tags is missing or not a list")
        return
    missing = [t for t in REQUIRED_TAGS if t not in tags]
    if not any(str(t).startswith(TEAM_TAG_PREFIX) and t != "aai-labs" for t in tags):
        missing.append(f"{TEAM_TAG_PREFIX}<project>")
    if missing:
        report.add("tags", FAIL, "missing " + ", ".join(missing))
    else:
        report.add("tags", PASS, f"{len(tags)} tags")


def check_difficulty(task_dir: Path, measured: str | None, report: Report) -> None:
    """Declared difficulty must not silently disagree with the measurement."""
    doc = _toml(Path(task_dir) / "task.toml")
    declared = str(doc.get("difficulty") or (doc.get("metadata") or {}).get("difficulty") or "")
    if not declared:
        report.add("difficulty", PASS, "not declared yet (correct while in flux)", blocking=False)
        return
    if measured is None:
        report.add("difficulty", NOT_RUN, f"declared {declared!r}, nothing measured this round")
        return
    want = {"HARD": "hard", "MEDIUM": "medium"}.get(measured)
    if want and declared != want:
        report.add("difficulty", FAIL, f"declared {declared!r} but measured {measured}")
    else:
        report.add("difficulty", PASS, f"{declared} matches {measured}")


def check_config_integrity(task_dir: Path, report: Report) -> None:
    """The traps the oracle cannot catch.

    A golden patch overwritten with a copy of `test_patch` still passes the
    oracle whenever solve.sh applies the real solution separately -- so oracle
    success does not cover any of this.
    """
    cfg_path = Path(task_dir) / "tests" / "config.json"
    if not cfg_path.is_file():
        report.add("config-integrity", PASS, "no tests/config.json; check is not applicable")
        return
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        report.add("config-integrity", FAIL, f"unparseable: {e}")
        return

    problems = []
    patch, test_patch = cfg.get("patch", ""), cfg.get("test_patch", "")
    if patch and test_patch and patch == test_patch:
        problems.append("patch is byte-identical to test_patch")
    for marker in ("tests/", "/tests", "test_patch"):
        if marker in str(patch):
            problems.append(f"patch references {marker!r}")
            break
    f2p = cfg.get("fail_to_pass") or cfg.get("FAIL_TO_PASS")
    p2p = cfg.get("pass_to_pass") or cfg.get("PASS_TO_PASS")
    if f2p is None:
        problems.append("no fail_to_pass set")
    if not problems and cfg.get("FAIL_TO_PASS") and not cfg.get("fail_to_pass"):
        problems.append("UPPERCASE keys against a lowercase reader")
    report.add(
        "config-integrity",
        FAIL if problems else PASS,
        "; ".join(problems) if problems else f"F2P {len(f2p or [])}, P2P {len(p2p or [])}",
    )


def check_test_ratchet(task_dir: Path, journal: Journal, report: Report) -> None:
    """Test count must not fall without a per-case invalidity proof.

    Never remove by batch, prefix or filename pattern: a name is not evidence of
    invalidity, and deleting a group to hit a target count destroys good coverage
    while leaving bad cases that happen to be named differently.
    """
    cfg = Path(task_dir) / "tests" / "config.json"
    count = None
    if cfg.is_file():
        try:
            doc = json.loads(cfg.read_text())
            count = len(doc.get("fail_to_pass") or []) + len(doc.get("pass_to_pass") or [])
        except (OSError, json.JSONDecodeError):
            count = None
    if count is None:
        files = list((Path(task_dir) / "tests").rglob("test_*.py"))
        count = len(files) or None
    if count is None:
        report.add("test-ratchet", NOT_RUN, "no countable graded set")
        return
    prior = [r.get("testCount") for r in journal.rounds if r.get("testCount") is not None]
    if not prior:
        report.add("test-ratchet", PASS, f"{count} (first observation)")
        return
    if count < prior[-1]:
        proof = (Path(task_dir) / ".benchsmith" / "removals.md").is_file()
        report.add(
            "test-ratchet",
            PASS if proof else FAIL,
            f"{prior[-1]} -> {count}" + ("" if proof else "; no per-case invalidity proof"),
        )
    else:
        report.add("test-ratchet", PASS, f"{prior[-1]} -> {count}")


# Test-framework and language noise that is never a production symbol.
_NOISE = frozenset("""
if for while switch case return func def class import from package var let const
t testing assert require expect error err nil None True False true false print
range len make new append string int float bool map struct interface go defer
Errorf Fatalf Fatal Error Run Helper Cleanup TempDir Setenv Skip Log Logf
""".split())

_CALL = __import__("re").compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def check_gold_symbols(task_dir: Path, report: Report) -> None:
    """Does the graded test reach past its stable entry point?

    The single highest-yield grader defect, by field count: a held-out test that
    encodes HOW the reference happens to be written rather than WHAT it must do.
    Seven defects on one task, every one of this shape, and each took a ~40-minute
    cloud round to find. The detector is one grep.

    A healthy gold file calls the task's stable entry point and nothing else --
    one real example converged on a single symbol called nine times. Every extra
    production symbol is a way for a correct-but-different implementation to fail.

    Declare the entry points in `.benchsmith/entrypoints` (one per line). Without
    that file this reports what it found and does not block, because it cannot
    know which symbol is the intended one.
    """
    tests = Path(task_dir) / "tests"
    if not tests.is_dir():
        report.add("gold-symbols", NOT_RUN, "no tests/ directory")
        return
    decl = Path(task_dir) / ".benchsmith" / "entrypoints"
    allowed = (
        {ln.strip() for ln in decl.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
        if decl.is_file()
        else set()
    )
    seen: dict[str, int] = {}
    for f in sorted(tests.rglob("*")):
        if not f.is_file() or f.suffix not in {".go", ".py", ".ts", ".js", ".java", ".kt", ".swift", ".rs"}:
            continue
        for name in _CALL.findall(f.read_text(errors="ignore")):
            if name in _NOISE or name.startswith(("Test", "test_", "Benchmark", "_")):
                continue
            seen[name] = seen.get(name, 0) + 1
    if not seen:
        report.add("gold-symbols", NOT_RUN, "no production symbols resolved from tests/")
        return
    extra = sorted(s for s in seen if s not in allowed)
    top = ", ".join(f"{s}x{seen[s]}" for s in sorted(seen, key=lambda k: -seen[k])[:6])
    if not allowed:
        report.add(
            "gold-symbols",
            NOT_RUN,
            f"{len(seen)} symbols called ({top}); declare .benchsmith/entrypoints to gate this",
            blocking=False,
        )
    elif extra:
        report.add(
            "gold-symbols",
            FAIL,
            f"graded tests reach past the declared entry point: {', '.join(extra[:8])}",
        )
    else:
        report.add("gold-symbols", PASS, f"only declared entry points ({top})")


def check_divergent_fixture(task_dir: Path, report: Report) -> None:
    """Gold-passes-and-base-fails is necessary and badly insufficient.

    All seven field defects satisfied it perfectly, because the grader was written
    against the reference. The cheap disproof is one positive fixture implementing
    the same behaviour differently -- renamed fields, restructured return type,
    reordered work. Two minutes to write; each one paid for itself immediately.
    """
    for pat in ("solution/variant*", "solution/divergent*", "tests/variants/*", ".benchsmith/variants/*"):
        if list(Path(task_dir).glob(pat)):
            report.add("divergent-fixture", PASS, f"found {pat}")
            return
    report.add(
        "divergent-fixture",
        NOT_RUN,
        "no divergent positive fixture; gold-pass/base-fail cannot detect a grader "
        "written against the reference",
        blocking=False,
    )


def check_journal(journal: Journal, report: Report) -> None:
    """The previous round must be recorded. A round that is not recorded did not happen."""
    if not journal.rounds:
        report.add("journal", PASS, "opening a first journal", blocking=False)
        return
    last = journal.rounds[-1]
    report.add("journal", PASS, f"round {last['n']} recorded, class {last['class']}")


def check_excursion(journal: Journal, report: Report) -> None:
    if not journal.rounds:
        report.add("excursion", NOT_RUN, "no rounds yet")
        return
    last = journal.rounds[-1]
    exc = last.get("excursion")
    if not exc:
        report.add("excursion", PASS, "none outstanding")
    elif last.get("explain"):
        report.add("excursion", PASS, f"{exc}, explained")
    else:
        report.add(
            "excursion",
            FAIL,
            f"{exc} unresolved — clear it with an explanation or move the surface forward",
        )


def check_budget(journal: Journal, report: Report) -> None:
    outcome = journal.stop_outcome()
    if outcome and outcome["blocking"]:
        report.add("budget", FAIL, outcome["detail"])
    elif outcome:
        report.add("budget", PASS, outcome["detail"])
    else:
        report.add(
            "budget",
            PASS,
            f"{journal.data['hardeningSpent']}/{journal.budget()} spent, "
            f"ineffective streak {journal.data['ineffectiveStreak']}",
        )


def check_scope(repo_root: Path, task_name: str, report: Report) -> None:
    """Every staged path must be inside the task directory."""
    from .diffcheck import changeset

    try:
        cs = changeset(Path(repo_root))
    except (subprocess.CalledProcessError, OSError) as e:
        report.add("scope", NOT_RUN, f"git unavailable: {e}")
        return
    if cs.empty:
        report.add("scope", NOT_RUN, f"nothing to review ({cs.source})")
        return
    stray = [p for p in cs.paths
             if not (p.startswith(f"{task_name}/") or p.startswith(".benchsmith/"))]
    report.add(
        "scope",
        FAIL if stray else PASS,
        ("reaches outside the task: " + ", ".join(stray[:5])) if stray
        else f"{len(cs.paths)} path(s) ({cs.source})",
    )


# A "lever" is a surface whose change moves the difficulty band. Corrective work
# (Dockerfiles, flake fixes, environment) touches none of them.
LEVERS = {
    "graded": ("tests/", "task.toml"),
    "spec": ("instruction.md",),
    "solution": ("solution/", "reference/", ".patch"),
}


def levers_touched(paths: list[str], task_name: str) -> set[str]:
    """Which difficulty-bearing surfaces a change set moves."""
    hit: set[str] = set()
    for raw in paths:
        rel = raw[len(task_name) + 1:] if raw.startswith(f"{task_name}/") else raw
        # steps/<n>/tests/... and steps/<n>/instruction.md are the multi-step
        # spellings of the same two surfaces.
        if rel.startswith("steps/"):
            rel = "/".join(rel.split("/")[2:])
        for name, markers in LEVERS.items():
            if any(m in rel if m.endswith("/") or m.startswith(".") else rel == m or rel.endswith("/" + m)
                   for m in markers):
                hit.add(name)
    return hit


def check_single_lever(repo_root: Path, task_name: str, mode: str, report: Report) -> None:
    """In hardening mode, move exactly one difficulty lever per round.

    Two levers in one round makes the next measurement unattributable: the band
    moved, and nothing in the record says which change moved it. That is not a
    style preference -- it is the difference between evidence and a coincidence,
    and it is the failure mode that produces a task nobody can tune.

    Corrective rounds are exempt and batch freely; they are not claiming to have
    moved anything.
    """
    change = report.artifacts.setdefault("changeEvidence", {})
    change.update(mode=mode, levers=[])
    if mode != "harden":
        report.add("single-lever", NOT_RUN, f"mode is {mode!r}; batching corrective work is allowed")
        return
    from .diffcheck import changeset

    try:
        cs = changeset(Path(repo_root))
    except (subprocess.CalledProcessError, OSError) as e:
        report.add("single-lever", NOT_RUN, f"git unavailable: {e}")
        return
    if cs.empty:
        report.add("single-lever", NOT_RUN, f"nothing to review ({cs.source})")
        return
    # A task being created for the first time legitimately establishes every
    # surface at once. There is no prior measurement to make unattributable,
    # so the one-lever rule has nothing to protect yet. Checked against the
    # BASELINE of this change, not always HEAD, so it holds for a scaffold that
    # has already been committed.
    existed = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{cs.old or 'HEAD'}:{task_name}/task.toml"],
        capture_output=True, text=True,
    )
    if existed.returncode != 0:
        report.add("single-lever", PASS, "initial scaffold; no measured round to attribute")
        return
    hit = levers_touched(cs.paths, task_name)
    change["levers"] = sorted(hit)
    if len(hit) > 1:
        report.add("single-lever", FAIL,
                   "moves " + ", ".join(sorted(hit)) + " in one hardening round; "
                   "split them so the next measurement is attributable")
    elif not hit:
        report.add("single-lever", PASS, "no difficulty lever moved")
    else:
        report.add("single-lever", PASS, f"one lever: {next(iter(hit))}")


TOML_AUTHOR = re.compile(
    r'authors\s*=\s*\[\s*\{\s*name\s*=\s*"([^"]*)"', re.S)


def check_controls_roster(repo_root: Path, report: Report) -> None:
    """Every control the repo declares must still be present.

    A control that quietly disappears leaves no trace: the gate stops running it
    and goes green faster. The roster is the only thing that notices, which is
    why a missing roster is itself a finding rather than a reason to skip.
    """
    roster = Path(repo_root) / "scripts" / "controls" / "EXPECTED"
    if not roster.is_file():
        report.add("controls-roster", NOT_RUN,
                   "no scripts/controls/EXPECTED; nothing declares which controls must exist",
                   blocking=False)
        return
    try:
        wanted = [l.strip() for l in roster.read_text().splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    except OSError as e:
        report.add("controls-roster", NOT_RUN, f"roster unreadable: {e}")
        return
    if not wanted:
        report.add("controls-roster", NOT_RUN, "roster is empty; it declares nothing")
        return
    missing = [n for n in wanted
               if not (Path(repo_root) / "scripts" / n).is_file()
               and not (Path(repo_root) / "scripts" / "controls" / n).is_file()]
    report.add("controls-roster", FAIL if missing else PASS,
               ("declared but absent: " + ", ".join(missing)) if missing
               else f"all {len(wanted)} declared control(s) present")


def check_fixture_corpus(task_dir: Path, report: Report, runner=None) -> None:
    """Known cheats must fail; correct alternatives must pass."""
    from . import fixtures as fx

    if runner is None:
        # Running the corpus means running the benchmark once per fixture, which
        # is minutes. It is opt-in rather than silently skipped, and reported as
        # NOT_RUN so nobody reads its absence as a clean bill.
        neg, pos = fx.discover(Path(task_dir))
        report.add("fixture-corpus", NOT_RUN,
                   f"{len(neg)} negative and {len(pos)} positive fixture(s) found, not run "
                   "(minutes per fixture; run `benchsmith corpus`)" if (neg or pos)
                   else "no fixture corpus under qa/negative, qa/positive or qa/variants",
                   blocking=False)
        return
    res = fx.run_corpus(Path(task_dir), runner=runner)
    report.add("fixture-corpus",
               {fx.PASS: PASS, fx.FAIL: FAIL, fx.TIMEOUT: TIMEOUT, fx.NOT_RUN: NOT_RUN}[res["state"]],
               res["detail"])


def check_findings(
    task_name: str,
    journal,
    report: Report,
    *,
    binary: str = "codimango",
    request_snapshot: dict | None = None,
) -> None:
    """A repair round must address what the reviewer asked for.

    The whole point of `needs_revision` is that a person named something wrong.
    Pushing a repair with no finding recorded means the loop cannot have
    verified the change it claims to have made -- it never wrote down what the
    change was, or how it would know it worked.
    """
    findings = journal.data.get("findings") or {}
    report.artifacts.setdefault("changeEvidence", {})["findings"] = [
        {
            "id": str(fid),
            "state": str(row.get("state") or ""),
            "closedBy": str(row.get("closedBy") or ""),
        }
        for fid, row in sorted(findings.items())
        if isinstance(row, dict)
    ]
    from .reviews import requests, unaddressed

    if request_snapshot is not None:
        req = request_snapshot
    else:
        try:
            req = requests(task_name, binary=binary)
        except Exception as e:  # noqa: BLE001
            # Unreadable is genuinely unknown, and non-blocking here on purpose:
            # `publish` already refuses outright when it cannot read a task's
            # status, so the needs_revision case stays fail-closed at the one place
            # it matters, without blocking every unrelated draft push.
            report.add("review-findings", NOT_RUN, f"could not read the reviews: {e}",
                       blocking=False)
            return
    if not req.get("requests"):
        # PASS, not NOT_RUN. "Every requested change is addressed" is true when
        # there are no requests, and calling it unmeasured made the push gate
        # unsatisfiable for every draft: the check is push-required, a required
        # NOT_RUN blocks, and no receipt could ever be issued.
        report.add("review-findings", PASS, "no outstanding revision request")
        return
    blocked, why = unaddressed(journal.data.get("findings") or {}, req)
    report.add("review-findings", FAIL if blocked else PASS, why)


def check_task_author(repo_root: Path, task_dir: Path, task_name: str, report: Report) -> None:
    """Is this our task to be changing?

    Complements the platform's `currentUserIsTaskOwner`, which needs the network
    and a credential. This works in a fresh clone with no connectivity, which is
    exactly where a worker is when it decides whether to touch a directory.

    Foreign tasks are SKIPPED, not failed, and the distinction is load-bearing.
    The original hook records why: demanding a green receipt for every touched
    task deadlocks the moment you merge a colleague's commits -- their directory
    appears in the range, the gate refuses to assess a task whose intent you do
    not hold, so the receipt can never exist, and the only way out is a bypass
    that disables the check for YOUR tasks too. A gate that cannot go green is
    not a gate.
    """
    toml = Path(task_dir) / "task.toml"
    if not toml.is_file():
        report.add("task-author", NOT_RUN, "no task.toml to read an author from")
        return
    try:
        m = TOML_AUTHOR.search(toml.read_text(errors="replace"))
    except OSError as e:
        report.add("task-author", NOT_RUN, f"could not read task.toml: {e}")
        return
    author = (m.group(1).strip() if m else "")
    if not author:
        report.add("task-author", NOT_RUN, "task.toml declares no author")
        return
    # Identity lives in three spellings and they are not interchangeable:
    # task.toml carries a unixname (`kngreen`), `git config user.name` carries a
    # display name (`Kristin Green`), and the email carries the unixname again.
    # Comparing one to another is a category error, not a check -- it reported
    # every one of the caller's own tasks as somebody else's and skipped them
    # all as out of scope.
    def _cfg(key: str) -> str:
        return subprocess.run(["git", "-C", str(repo_root), "config", key],
                              capture_output=True, text=True).stdout.strip()

    display = _cfg("user.name")
    email = _cfg("user.email")
    unix = email.split("@")[0] if "@" in email else ""
    # `104796296+kngreen@users.noreply.github.com` and the like.
    if "+" in unix:
        unix = unix.split("+", 1)[1]
    me = {x.lower() for x in (display, unix, os.environ.get("USER", "")) if x}
    if not me:
        report.add("task-author", NOT_RUN, "no git identity configured; cannot tell whose task this is")
        return
    if author.lower() not in me:
        report.add("task-author", NOT_RUN,
                   f"{task_name} is authored by {author}, not {display or unix} "
                   f"(checked {', '.join(sorted(me))}) — out of scope, not gated here",
                   blocking=False)
        return
    report.add("task-author", PASS, f"authored by {author}")


def check_contamination(repo_root: Path, report: Report) -> None:
    """Overlay markers and transaction artifacts, read from the git snapshot."""
    from . import contamination

    res = contamination.check(Path(repo_root))
    report.add("contamination", res["state"], res["detail"])


def check_untracked_deps(repo_root: Path, report: Report) -> None:
    """A committed file may not invoke a path that is not committed."""
    from . import deps

    res = deps.check(Path(repo_root))
    report.add("untracked-deps", res["state"], res["detail"])


def check_structural(task_dir: Path, report: Report, binary: str = "codimango") -> None:
    """Upstream structural validation (G4).

    Exemptions are named, never silent. An undocumented exemption is
    indistinguishable from a bug, and a gate that fires known false positives is
    one people learn to skim -- which is how a real finding gets missed.
    """
    is_vm = (Path(task_dir) / "environment" / "vm.conf").is_file()
    try:
        r = subprocess.run(
            [binary, "bench", "validate", "-p", str(task_dir), "--structural-only", "--json"],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        report.add("structural", NOT_RUN, f"could not run upstream validation: {e}")
        return
    body = r.stdout[r.stdout.find("{"):] if "{" in r.stdout else ""
    if not body:
        report.add("structural", NOT_RUN, "upstream validation returned no JSON")
        return
    try:
        checks = json.loads(body)["structural"]["checks"]
    except (ValueError, KeyError, TypeError) as e:
        report.add("structural", NOT_RUN, f"could not parse structural JSON: {e}")
        return

    failed, warned, exempted = [], [], []
    for c in checks:
        status = str(c.get("status", "")).lower()
        if status in ("pass", "ok", "passed"):
            continue
        detail = str(c.get("details") or "")
        # A macOS VM task has no Dockerfile by design -- the environment is
        # vm.conf plus an image digest, and the upstream check cannot tell the
        # difference. There is deliberately NO exemption for a missing *.pem:
        # an earlier one claimed the key was injected at build time; it is not,
        # and the build dies at that COPY.
        if is_vm and "Dockerfile" in detail and "missing" in detail.lower():
            exempted.append(f"{c.get('name')} (VM task: no Dockerfile by design)")
            continue
        item = f"{c.get('name')}: {detail[:100]}"
        if status in ("warn", "warning"):
            warned.append(item)
            continue
        failed.append(item)
    notes = []
    if warned:
        notes.append("warnings: " + "; ".join(warned[:4]))
    if exempted:
        notes.append("exempted: " + ", ".join(exempted))
    suffix = "; " + "; ".join(notes) if notes else ""
    report.add(
        "structural",
        FAIL if failed else PASS,
        ("; ".join(failed[:4]) + suffix)
        if failed
        else f"{len(checks) - len(warned)} upstream check(s) pass{suffix}",
    )


def check_control_manifest(repo_root: Path, task_dir: Path, task_name: str,
                           report: Report, *, mutation_runner=None,
                           closure_runner=None, metamorphic_runner=None) -> None:
    """Resolve declared, detected, and policy-required controls, then fire them."""
    from . import controls

    resolved = controls.resolve(repo_root, task_name, task_dir)
    report.artifacts["controlManifest"] = resolved
    if resolved["mode"] == "shadow" and not resolved.get("digest"):
        report.add("control-manifest", PASS,
                   resolved["reason"] + "; shadow-only, 0 obligations examined")
        for name in ("obligation-witnesses", "mutation-adequacy",
                     "metamorphic-variation", "verifier-closure"):
            report.add(name, NOT_RUN, "shadow-only legacy task; 0 inputs examined", blocking=False)
        return
    if not resolved["ok"]:
        state = FAIL if resolved["mode"] == "enforce" else NOT_RUN
        detail = "; ".join(resolved["errors"]) or resolved["reason"]
        if state == NOT_RUN:
            detail += "; shadow policy, 0 obligations examined"
        report.add("control-manifest", state, detail,
                   required=resolved["mode"] == "enforce", blocking=resolved["mode"] == "enforce")
        for name in ("obligation-witnesses", "mutation-adequacy",
                     "metamorphic-variation", "verifier-closure"):
            report.add(name, FAIL if state == FAIL else NOT_RUN,
                       "manifest unresolved" if state == FAIL else
                       "shadow policy; 0 inputs examined",
                       required=resolved["mode"] == "enforce",
                       blocking=resolved["mode"] == "enforce")
        return

    report.add("control-manifest", PASS,
               f"{resolved['examined']} obligation(s), effective {', '.join(resolved['effective'])}",
               required=True)
    witnesses = controls.obligation_witnesses(task_dir, resolved)
    report.artifacts["obligationWitnesses"] = witnesses
    report.add("obligation-witnesses", PASS if witnesses["ok"] else FAIL,
               witnesses["detail"] + f"; examined {witnesses['examined']}", required=True)
    mutation = controls.mutation_adequacy(task_dir, resolved, runner=mutation_runner)
    report.artifacts["mutationAdequacy"] = mutation
    report.add("mutation-adequacy", PASS if mutation["ok"] else
               (FAIL if resolved["mode"] == "enforce" else PASS),
               mutation["detail"] + f"; examined {mutation['examined']}", required=True)

    if "metamorphic_variation" in resolved["effective"]:
        metamorphic = controls.run_metamorphic(task_dir, resolved, runner=metamorphic_runner)
        report.artifacts["metamorphicVariation"] = metamorphic
        report.add("metamorphic-variation", PASS if metamorphic["ok"] else FAIL,
                   metamorphic["detail"] + f"; examined {metamorphic['examined']}", required=True)
    else:
        report.add("metamorphic-variation", PASS,
                   "not applicable by effective manifest; 0 inputs examined", required=True)

    if "candidate_execution" in resolved["effective"]:
        closure = controls.verifier_closure(task_dir, resolved, runner=closure_runner)
        report.artifacts["verifierClosure"] = closure
        report.add("verifier-closure", PASS if closure["ok"] else FAIL,
                   closure["detail"] + f"; examined {closure['examined']}", required=True)
    else:
        report.add("verifier-closure", PASS,
                   "no candidate-influenced execution detected; 0 probes examined", required=True)


def check_diff(repo_root: Path, report: Report) -> None:
    """Staged-vs-HEAD checks: is this change worse than the last one?

    Distinct from `check_test_ratchet`, which compares against the last round
    benchsmith RECORDED. Anything committed between rounds is invisible to that
    one, and `staged vs HEAD` is cheaply available only here.
    """
    from . import diffcheck

    for name, res in diffcheck.run(Path(repo_root)).items():
        report.add(
            name,
            res["state"],
            res["detail"],
            blocking=res.get("applicable", True),
        )


def check_formatting(repo_root: Path, report: Report, specs=None) -> None:
    """The repos' pre-commit hook, as a check.

    Check-only by design. The gate may not rewrite files: its surface hashes and
    its receipt are computed against the tree as read, and a formatter running
    inside it would attest to a tree that no longer exists. `benchsmith fmt`
    does the writing, before the commit.
    """
    from . import hooks as hooks_mod

    res = hooks_mod.run_all(Path(repo_root), specs)
    state = {hooks_mod.PASS: PASS, hooks_mod.FAIL: FAIL,
             hooks_mod.SKIPPED: PASS, hooks_mod.NOT_RUN: NOT_RUN}[res["state"]]
    detail = res["reason"]
    if res["results"]:
        detail += f" ({res['seconds']}s)"
    report.add("formatting", state, detail)


def check_hygiene(task_dir: Path, report: Report) -> None:
    junk = [str(p.relative_to(task_dir)) for p in Path(task_dir).rglob("*") if p.suffix == ".pyc"]
    junk += [str(p.relative_to(task_dir)) for p in Path(task_dir).rglob("__pycache__")]
    report.add("hygiene", FAIL if junk else PASS, ", ".join(junk[:4]) if junk else "clean")


def check_oracle(oracle_cmd: list[str] | None, report: Report) -> None:
    """A cloud round costs minutes; the oracle costs seconds. Never push on a red one."""
    if not oracle_cmd:
        report.add("oracle", NOT_RUN, "no oracle command resolved")
        return
    try:
        r = subprocess.run(oracle_cmd, capture_output=True, text=True, timeout=3600)
    except (subprocess.TimeoutExpired, OSError) as e:
        report.add("oracle", NOT_RUN, f"could not run: {e}")
        return
    report.add(
        "oracle",
        PASS if r.returncode == 0 else FAIL,
        "reward 1.0" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:160],
    )


def check_artifact_transfer(task_dir: Path, report: Report, runner=None) -> None:
    """Run a task-owned Harbor-equivalent smoke in an isolated exact-HEAD export."""
    task_dir = Path(task_dir).resolve()
    try:
        repo_root = Path(_git(task_dir, "rev-parse", "--show-toplevel")).resolve()
        task_name = validate_task_path(task_dir.relative_to(repo_root).as_posix())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        report.add("artifact-transfer", FAIL, f"task path is unsafe: {error}")
        return
    dockerfile = task_dir / "tests" / "Dockerfile"
    contract = task_dir / "qa" / "artifact-transfer"
    artifact = {"required": dockerfile.is_file(), "contract": "qa/artifact-transfer"}
    report.artifacts["artifactTransfer"] = artifact
    if not dockerfile.is_file():
        report.add(
            "artifact-transfer", NOT_RUN,
            "no tests/Dockerfile; separate-verifier transfer proof is not applicable",
            blocking=False,
        )
        return
    if contract.is_symlink():
        report.add(
            "artifact-transfer", FAIL,
            "qa/artifact-transfer must be a tracked task-local executable, not a symlink",
        )
        return
    if not contract.is_file():
        report.add(
            "artifact-transfer", NOT_RUN,
            "tests/Dockerfile requires executable qa/artifact-transfer to prove the task-local "
            "/app export/import/oracle lifecycle",
        )
        return
    if not os.access(contract, os.X_OK):
        report.add("artifact-transfer", NOT_RUN, "qa/artifact-transfer is not executable")
        return

    try:
        candidate_sha = _git(repo_root, "rev-parse", "HEAD")
        source_status = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as error:
        report.add("artifact-transfer", NOT_RUN, f"could not bind transfer contract: {error}")
        return

    result = None
    setup_problem = ""
    with tempfile.TemporaryDirectory(prefix="benchsmith-transfer-") as temporary:
        root = Path(temporary)
        snapshot = root / "repo"
        snapshot.mkdir(mode=0o700)
        archive = subprocess.Popen(
            ["git", "-C", str(repo_root), "archive", "--format=tar", candidate_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        extracted = subprocess.run(
            ["tar", "-xf", "-", "-C", str(snapshot)],
            stdin=archive.stdout,
            capture_output=True,
            timeout=300,
        )
        if archive.stdout is not None:
            archive.stdout.close()
        archive_stderr = archive.communicate(timeout=30)[1]
        if archive.returncode or extracted.returncode:
            detail = (archive_stderr or extracted.stderr or b"").decode(errors="replace")[:180]
            setup_problem = f"could not create disposable exact-SHA export: {detail}"
        else:
            snapshot_task = snapshot / task_name
            snapshot_contract = snapshot_task / "qa" / "artifact-transfer"
            if not snapshot_contract.is_file() or snapshot_contract.is_symlink():
                setup_problem = "qa/artifact-transfer is absent or a symlink in the committed snapshot"
            elif not os.access(snapshot_contract, os.X_OK):
                setup_problem = "qa/artifact-transfer is not executable in the committed snapshot"
            else:
                contract_digest = "sha256:" + hashlib.sha256(
                    snapshot_contract.read_bytes()
                ).hexdigest()
                home = root / "home"
                scratch = root / "tmp"
                home.mkdir(mode=0o700)
                scratch.mkdir(mode=0o700)
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(home),
                    "TMPDIR": str(scratch),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "BENCHSMITH_ARTIFACT_TRANSFER_SCHEMA": "1",
                    "BENCHSMITH_CANDIDATE_SHA": candidate_sha,
                    "BENCHSMITH_REPO": str(snapshot),
                    "BENCHSMITH_TASK": task_name,
                    "BENCHSMITH_TASK_DIR": str(snapshot_task),
                }
                run_contract = runner or subprocess.run
                try:
                    result = run_contract(
                        [str(snapshot_contract)], cwd=snapshot_task, env=env,
                        capture_output=True, text=True, timeout=3600,
                    )
                except subprocess.TimeoutExpired:
                    report.add("artifact-transfer", TIMEOUT, "qa/artifact-transfer timed out")
                except OSError as error:
                    setup_problem = f"could not execute qa/artifact-transfer: {error}"
                artifact.update(
                    candidateSha=candidate_sha,
                    contractDigest=contract_digest,
                    environment=sorted(env),
                )

    try:
        after_head = _git(repo_root, "rev-parse", "HEAD")
        after_status = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as error:
        report.add(
            "artifact-transfer", FAIL,
            f"could not verify source checkout after qa/artifact-transfer: {error}",
        )
        return
    if after_head != candidate_sha or after_status != source_status:
        report.add(
            "artifact-transfer", FAIL,
            "qa/artifact-transfer mutated the source checkout; the contract ran only against "
            "a disposable export and must not reach back into its source",
        )
        return
    if any(check.name == "artifact-transfer" for check in report.checks):
        return
    if setup_problem:
        report.add("artifact-transfer", NOT_RUN, setup_problem)
        return
    if result is None:
        report.add("artifact-transfer", NOT_RUN, "qa/artifact-transfer produced no result")
        return
    artifact["exitCode"] = result.returncode
    output = (result.stderr or result.stdout or "").strip().splitlines()
    detail = output[-1][:160] if output else ""
    report.add(
        "artifact-transfer",
        PASS if result.returncode == 0 else FAIL,
        (
            f"disposable /app export/import/oracle contract passed for {candidate_sha[:12]}"
            if result.returncode == 0
            else f"qa/artifact-transfer failed ({result.returncode})"
            + (f": {detail}" if detail else "")
        ),
    )


def run(
    *,
    repo_root: Path,
    task_dir: Path,
    task_name: str,
    measured: str | None = None,
    oracle_cmd: list[str] | None = None,
    require: tuple[str, ...] | None = None,
    hook_specs: list | None = None,
) -> Report:
    task_name = validate_task_path(task_name)
    repo_root = Path(repo_root).resolve()
    task_dir = Path(task_dir).resolve()
    if task_dir != (repo_root / task_name).resolve():
        raise ValueError("task_dir does not match the validated task path")
    report = Report()
    journal = Journal.open(repo_root, task_name)
    check_tags(task_dir, report)
    check_difficulty(task_dir, measured, report)
    check_config_integrity(task_dir, report)
    check_gold_symbols(task_dir, report)
    check_divergent_fixture(task_dir, report)
    check_test_ratchet(task_dir, journal, report)
    check_journal(journal, report)
    check_excursion(journal, report)
    check_budget(journal, report)
    check_scope(repo_root, task_name, report)
    check_single_lever(repo_root, task_name, journal.mode, report)
    check_task_author(repo_root, task_dir, task_name, report)
    check_findings(task_name, journal, report)
    check_controls_roster(repo_root, report)
    check_fixture_corpus(task_dir, report)
    check_contamination(repo_root, report)
    check_untracked_deps(repo_root, report)
    check_control_manifest(repo_root, task_dir, task_name, report)
    check_structural(task_dir, report)
    check_diff(repo_root, report)
    check_formatting(repo_root, report, hook_specs)
    check_hygiene(task_dir, report)
    check_artifact_transfer(task_dir, report)
    check_oracle(oracle_cmd, report)
    h = surface_hashes(task_dir)
    report.add("graded-hash", PASS, f"{h['gradedHash']} ({h['gradedFiles']} files)", blocking=False)
    if require:
        report.require(require)
    return report


# --- receipts ---------------------------------------------------------------
#
# A gate that passed is only evidence if it can be shown to have passed on *this*
# tree. Prefer a receipt the repository's own canonical hook emits; when the repo
# publishes no such contract, write this native fallback rather than inventing a
# schema the repo does not have.


def _git(repo_root: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


def receipt_path(repo_root: Path, task_name: str) -> Path:
    """Receipts live OUTSIDE the worktree.

    Written into the repo they dirty the tree, and a dirty tree invalidates the
    very receipt just written -- the check would fail on its own side effect.
    Keyed by repo path so two checkouts of the same repo cannot share one.
    """
    task_name = validate_task_path(task_name)
    key = hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:12]
    base = Path(os.environ.get("BENCHSMITH_RECEIPT_DIR", Path.home() / ".cache" / "benchsmith" / "receipts"))
    return base / key / f"{task_name}.receipt.json"


def canonical_receipt(repo_root: Path) -> Path | None:
    """A receipt the repository itself declares, if it has one.

    Never fabricate one: a repo with no receipt contract gets the native fallback
    below, and the difference is recorded rather than smoothed over.
    """
    for candidate in (".gate/receipt.json", ".ci/gate-receipt.json", "tools/gate/receipt.json"):
        p = Path(repo_root) / candidate
        if p.is_file():
            return p
    return None


HOOK_PROOF_VERSION = 2
OUTER_HOOK_AUTHORITY = "BENCHSMITH_OUTER_HOOK_AUTHORITY"
OUTER_HOOK_STATE = "BENCHSMITH_OUTER_HOOK_STATE"


def _live_process_ancestor(ancestor_pid: object) -> bool:
    """Return true only when the recorded issuer is a live strict ancestor."""
    try:
        wanted = int(ancestor_pid)
    except (TypeError, ValueError):
        return False
    if wanted <= 1:
        return False
    current = os.getpid()
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            raw = Path(f"/proc/{current}/stat").read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            parent = int(fields[1])
        except (OSError, ValueError, IndexError):
            try:
                probe = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(current)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                parent = int(probe.stdout.strip()) if probe.returncode == 0 else 0
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return False
        if parent == wanted:
            return Path(f"/proc/{wanted}").is_dir()
        current = parent
    return False


def nested_hook_is_authorized(repo_root: Path, task_name: str) -> bool:
    """Accept only a nested verify-receipt arm started by our private hook run."""
    token = os.environ.get(OUTER_HOOK_AUTHORITY, "")
    state_path = Path(os.environ.get(OUTER_HOOK_STATE, ""))
    if not token or not state_path.is_file():
        return False
    try:
        validated_task = validate_task_path(task_name)
        parent_stat = state_path.parent.stat()
        state_stat = state_path.stat()
        parent_mode = stat.S_IMODE(parent_stat.st_mode)
        mode = stat.S_IMODE(state_stat.st_mode)
        document = json.loads(state_path.read_text())
        current = _git(Path(repo_root), "rev-parse", "HEAD")
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False
    return bool(
        parent_mode == 0o700
        and mode == 0o600
        and parent_stat.st_uid == os.getuid()
        and state_stat.st_uid == os.getuid()
        and document.get("token") == token
        and document.get("task") == validated_task
        and document.get("repo") == str(Path(repo_root).resolve())
        and document.get("head") == current
        and _live_process_ancestor(document.get("pid"))
        and str(Path(os.environ.get("GATE_RECEIPT", "")).parent) == str(state_path.parent)
    )


def live_hook_identity(repo_root: Path) -> dict:
    """Identify the exact pre-push hook Git would execute in this checkout."""
    repo_root = Path(repo_root).resolve()
    try:
        raw = _git(repo_root, "rev-parse", "--git-path", "hooks/pre-push")
    except (OSError, subprocess.CalledProcessError) as error:
        return {"version": HOOK_PROOF_VERSION, "error": f"hook path is unreadable: {error}"}
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    path = path.absolute()
    identity = {
        "version": HOOK_PROOF_VERSION,
        "path": str(path),
        "present": path.exists() or path.is_symlink(),
        "executable": bool(path.exists() and os.access(path, os.X_OK)),
        "symlink": path.is_symlink(),
    }
    if path.is_symlink():
        try:
            identity["linkTarget"] = os.readlink(path)
        except OSError as error:
            identity["error"] = f"hook symlink is unreadable: {error}"
            return identity
    if identity["present"]:
        try:
            identity["contentDigest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            identity["error"] = f"hook content is unreadable: {error}"
    return identity


def resolve_publication_target(
    repo_root: Path,
    *,
    remote: str = "origin",
    branch: str = "",
) -> dict:
    """Resolve an omitted branch from the remote's actual default."""
    repo_root = Path(repo_root).resolve()
    remote = str(remote or "origin").strip()
    requested = str(branch or "").strip()
    try:
        remote_url = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "--push", remote],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "ok": False,
            "configured": False,
            "remote": remote,
            "branch": requested,
            "reason": f"publication remote {remote!r} is unreadable: {error}",
        }
    if remote_url.returncode != 0:
        if requested:
            return {
                "ok": False,
                "configured": False,
                "remote": remote,
                "branch": requested,
                "reason": f"publication remote {remote!r} is not configured",
            }
        return {"ok": True, "configured": False, "remote": remote, "branch": ""}
    if requested:
        return {
            "ok": True,
            "configured": True,
            "remote": remote,
            "branch": requested,
            "remoteUrl": remote_url.stdout.strip(),
        }

    try:
        symbolic = subprocess.run(
            ["git", "-C", str(repo_root), "symbolic-ref", "--quiet", "--short",
             f"refs/remotes/{remote}/HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        symbolic = None
    prefix = f"{remote}/"
    resolved = symbolic.stdout.strip() if symbolic is not None else ""
    if symbolic is not None and symbolic.returncode == 0 and resolved.startswith(prefix):
        resolved = resolved[len(prefix):]
    else:
        try:
            advertised = subprocess.run(
                ["git", "-C", str(repo_root), "ls-remote", "--symref", remote, "HEAD"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "ok": False,
                "configured": True,
                "remote": remote,
                "branch": "",
                "reason": f"could not read the default branch for publication remote {remote!r}: {error}",
            }
        resolved = ""
        if advertised.returncode == 0:
            for line in advertised.stdout.splitlines():
                if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                    resolved = line[len("ref: refs/heads/") : -len("\tHEAD")]
                    break
    if not resolved:
        return {
            "ok": False,
            "configured": True,
            "remote": remote,
            "branch": "",
            "reason": f"could not resolve the default branch for publication remote {remote!r}",
        }
    return {
        "ok": True,
        "configured": True,
        "remote": remote,
        "branch": resolved,
        "remoteUrl": remote_url.stdout.strip(),
    }


def _run_live_hook(
    repo_root: Path,
    task_name: str,
    head: str,
    hook_receipt: Path,
    authority_path: Path,
    authority_token: str,
    *,
    remote: str,
    branch: str,
) -> tuple[dict | None, str]:
    """Execute the exact active foreign pre-push hook against the candidate."""
    identity = live_hook_identity(repo_root)
    if identity.get("error"):
        return None, str(identity["error"])
    proof = {
        "version": HOOK_PROOF_VERSION,
        "identity": identity,
        "candidateSha": head,
        "remote": remote,
        "branch": branch,
    }
    try:
        remote_url = _git(repo_root, "remote", "get-url", "--push", remote)
    except (OSError, subprocess.CalledProcessError) as error:
        return None, f"live pre-push hook proof could not resolve remote {remote}: {error}"
    remote_ref = f"refs/heads/{branch}"
    remote_result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-remote", "--heads", remote, remote_ref],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if remote_result.returncode != 0:
        return None, "live pre-push hook proof could not read the target remote"
    fields = remote_result.stdout.split()
    if len(fields) != 2 or fields[1] != remote_ref:
        return None, f"target branch {remote}/{branch} does not exist; refusing hook attestation"
    remote_sha = fields[0]
    proof["remoteSha"] = remote_sha
    if not identity.get("present") or not identity.get("executable"):
        proof["state"] = "not-applicable"
        return proof, ""

    hook_path = Path(str(identity["path"]))
    try:
        hook_path.read_text(errors="replace")
    except OSError as error:
        return None, f"live pre-push hook is unreadable: {error}"

    local_ref = subprocess.run(
        ["git", "-C", str(repo_root), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip() or "HEAD"
    env = os.environ.copy()
    env.update(
        GATE_RECEIPT=str(hook_receipt),
        BENCHSMITH_TASK=task_name,
        BENCHSMITH_CANDIDATE_SHA=head,
        **{
            OUTER_HOOK_AUTHORITY: authority_token,
            OUTER_HOOK_STATE: str(authority_path),
        },
    )
    try:
        result = subprocess.run(
            [str(hook_path), remote, remote_url],
            cwd=repo_root,
            env=env,
            input=f"{local_ref} {head} {remote_ref} {remote_sha}\n",
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        return None, "live pre-push hook timed out"
    except OSError as error:
        return None, f"live pre-push hook could not run: {error}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, "live pre-push hook failed" + (f": {detail[-1][:200]}" if detail else "")
    if live_hook_identity(repo_root) != identity:
        return None, "live pre-push hook changed while it was being verified"
    try:
        after_head = _git(repo_root, "rev-parse", "HEAD")
        dirty = bool(_git(repo_root, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError) as error:
        return None, f"candidate changed during live hook proof: {error}"
    if after_head != head or dirty:
        return None, "live pre-push hook changed the exact candidate or dirtied the worktree"
    proof.update(
        state="passed",
        outputDigest="sha256:" + hashlib.sha256(
            ((result.stdout or "") + "\0" + (result.stderr or "")).encode()
        ).hexdigest(),
    )
    return proof, ""


def authenticate_live_hook(
    repo_root: Path,
    task_name: str,
    receipt: dict,
    *,
    remote: str = "",
    branch: str = "",
) -> dict:
    """Re-run the current outer hook from an already validated gate receipt."""
    repo_root = Path(repo_root)
    task_name = validate_task_path(task_name)
    recorded = receipt.get("repositoryHook") or {}
    bound_remote = str(recorded.get("remote") or "")
    bound_branch = str(recorded.get("branch") or "")
    requested_remote = str(remote or bound_remote)
    requested_branch = str(branch or bound_branch)
    if bound_remote and requested_remote != bound_remote:
        return {"ok": False, "reason": "publication remote differs from the gate receipt"}
    if bound_branch and requested_branch != bound_branch:
        return {"ok": False, "reason": "publication branch differs from the gate receipt"}
    if not bound_branch:
        identity = live_hook_identity(repo_root)
        if (
            recorded.get("state") == "not-applicable"
            and not identity.get("error")
            and not identity.get("executable")
        ):
            return {"ok": True, "proof": recorded}
        return {"ok": False, "reason": "gate receipt has no configured publication target"}
    head = str(receipt.get("head") or "")
    checks = receipt.get("checks") or {}
    required = receipt.get("requiredChecks") or []
    inapplicable = receipt.get("inapplicableChecks") or []
    gates = {
        name: {
            PASS: "pass", FAIL: "fail", NOT_RUN: "not_run", TIMEOUT: "timeout"
        }.get(checks.get(name), str(checks.get(name) or "").lower())
        for name in required
    }
    document = {
        "task": task_name,
        "commit": head,
        "dirty": False,
        "gates": gates,
        "inapplicable": inapplicable,
        "source": "benchsmith-authentication",
    }
    with tempfile.TemporaryDirectory(prefix="benchsmith-hook-") as private_dir:
        private = Path(private_dir)
        private.chmod(0o700)
        hook_path = private / "receipt.json"
        hook_path.write_text(json.dumps(document, indent=1))
        hook_path.chmod(0o600)
        authority_path = private / "authority.json"
        authority_token = secrets.token_hex(32)
        authority_path.write_text(json.dumps({
            "token": authority_token,
            "repo": str(repo_root.resolve()),
            "task": task_name,
            "head": head,
            "pid": os.getpid(),
        }))
        authority_path.chmod(0o600)
        proof, problem = _run_live_hook(
            repo_root, task_name, head, hook_path, authority_path, authority_token,
            remote=requested_remote, branch=requested_branch,
        )
        if proof is None:
            return {"ok": False, "reason": problem}
        return {"ok": True, "proof": proof}


def _push_policy(artifacts: dict) -> list[str]:
    required = list(PUSH_REQUIRED)
    if (artifacts.get("controlManifest") or {}).get("mode") == "enforce":
        required.extend(CONTROL_REQUIRED)
    return required


def _receipt_requirements(report: Report) -> tuple[list[str], list[str]]:
    by_name = {check.name: check for check in report.checks}
    required: list[str] = []
    inapplicable: list[str] = []
    for name in _push_policy(report.artifacts):
        check = by_name.get(name)
        if (
            check is not None
            and check.state == NOT_RUN
            and not check.blocking
            and name in INAPPLICABLE_PUSH_CHECKS
        ):
            inapplicable.append(name)
        else:
            required.append(name)
    return required, inapplicable


def _emit_hook_receipt(
    path: Path,
    task_name: str,
    report: Report,
    head: str,
    dirty: bool,
    required_checks: list[str],
    inapplicable_checks: list[str],
) -> str:
    """Write the private, ephemeral receipt consumed by the live hook."""
    by_name = {check.name: check for check in report.checks}
    gates = {}
    for name in required_checks:
        check = by_name.get(name)
        if check is None:
            continue
        gates[name] = {
            PASS: "pass",
            FAIL: "fail",
            NOT_RUN: "not_run",
            TIMEOUT: "timeout",
        }.get(check.state, check.state.lower())
    try:
        path.write_text(json.dumps({
            "task": task_name, "commit": head, "dirty": dirty, "gates": gates,
            "inapplicable": inapplicable_checks,
            "source": "benchsmith",
            "note": "gate names are benchsmith's checks, not the repo's G1-G5; "
                    "not_run and timeout are not pass",
        }, indent=1))
        path.chmod(0o600)
    except OSError:
        return ""
    return str(path)


def write_receipt(
    repo_root: Path,
    task_name: str,
    report: Report,
    *,
    derived_from: str = "",
    remote: str = "origin",
    branch: str = "",
    defer_hook: bool = False,
    expected_remote_sha: str = "",
) -> dict:
    """Bind a passing gate run and the repository hook to an exact clean HEAD."""
    repo_root = Path(repo_root)
    try:
        task_name = validate_task_path(task_name)
    except ValueError as error:
        return {"state": "not_written", "reason": str(error)}
    try:
        head = _git(repo_root, "rev-parse", "HEAD")
        tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
        dirty = bool(_git(repo_root, "status", "--porcelain"))
    except (subprocess.CalledProcessError, OSError) as e:
        return {"state": "not_run", "reason": f"git unavailable: {e}"}

    # A dirty tree cannot be bound to a receipt: the thing that passed is not the
    # thing that would be pushed. Decline to write rather than emit a receipt that
    # records its own invalidity -- a misleading artifact is worse than none.
    if dirty:
        return {"state": "not_written", "reason": "worktree is dirty; commit before gating"}
    if not report.ok:
        return {"state": "not_written", "reason": "gate did not pass"}
    by_name = {c.name: c for c in report.checks}
    required_checks, inapplicable_checks = _receipt_requirements(report)
    unsafe = [name for name in required_checks
              if name not in by_name or by_name[name].state != PASS]
    if unsafe:
        return {"state": "not_written",
                "reason": "push-required checks are not PASS: " + ", ".join(unsafe)}

    try:
        gate_fingerprint = safety.snapshot("gate")["digest"]
        publish_fingerprint = safety.snapshot("publish_policy")["digest"]
    except safety.SafetyRefused as error:
        return {"state": "not_written", "reason": str(error)}

    identity = live_hook_identity(repo_root)
    if identity.get("error"):
        return {"state": "not_written", "reason": str(identity["error"])}
    target = resolve_publication_target(repo_root, remote=remote, branch=branch)
    if not target.get("ok"):
        return {"state": "not_written", "reason": str(target.get("reason") or "")}
    resolved_remote = str(target.get("remote") or "")
    resolved_branch = str(target.get("branch") or "")

    hook_proof = None
    if not target.get("configured"):
        if identity.get("present") and identity.get("executable"):
            return {
                "state": "not_written",
                "reason": "an executable pre-push hook exists but no publication target is configured",
            }
        hook_proof = {
            "version": HOOK_PROOF_VERSION,
            "identity": identity,
            "candidateSha": head,
            "remote": "",
            "branch": "",
            "remoteSha": "",
            "state": "not-applicable",
        }
    elif defer_hook:
        if not derived_from:
            return {
                "state": "not_written",
                "reason": "live hook execution may be deferred only for a derived carry receipt",
            }
        try:
            remote_sha = validate_sha(expected_remote_sha, "expected remote SHA")
        except ValueError as error:
            return {"state": "not_written", "reason": str(error)}
        hook_proof = {
            "version": HOOK_PROOF_VERSION,
            "identity": identity,
            "candidateSha": head,
            "remote": resolved_remote,
            "branch": resolved_branch,
            "remoteSha": remote_sha,
            "state": "deferred",
        }
    else:
        with tempfile.TemporaryDirectory(prefix="benchsmith-hook-") as private_dir:
            private = Path(private_dir)
            private.chmod(0o700)
            hook_path = private / "receipt.json"
            authority_path = private / "authority.json"
            authority_token = secrets.token_hex(32)
            authority_path.write_text(json.dumps({
                "token": authority_token,
                "repo": str(repo_root.resolve()),
                "task": task_name,
                "head": head,
                "pid": os.getpid(),
            }))
            authority_path.chmod(0o600)
            emitted_hook = _emit_hook_receipt(
                hook_path,
                task_name,
                report,
                head.strip(),
                dirty,
                required_checks,
                inapplicable_checks,
            )
            if not emitted_hook:
                return {
                    "state": "not_written",
                    "reason": "could not write the private receipt consumed by the live pre-push hook",
                }
            hook_proof, hook_problem = _run_live_hook(
                repo_root,
                task_name,
                head.strip(),
                hook_path,
                authority_path,
                authority_token,
                remote=resolved_remote,
                branch=resolved_branch,
            )
            if hook_proof is None:
                return {"state": "not_written", "reason": hook_problem}

    body = {
        "task": task_name,
        "head": head,
        "tree": tree,
        "clean": True,
        "ok": True,
        "gateFingerprint": gate_fingerprint,
        "publishPolicyFingerprint": publish_fingerprint,
        "repositoryHook": hook_proof,
        "checks": {c.name: c.state for c in report.checks},
        "requiredChecks": required_checks,
        "inapplicableChecks": inapplicable_checks,
        "notRun": [c.name for c in report.checks if c.state == NOT_RUN],
        "artifacts": report.artifacts,
        "source": "canonical" if canonical_receipt(repo_root) else "benchsmith-native-fallback",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    resolved = report.artifacts.get("controlManifest") or {}
    if resolved:
        from . import controls

        body["cacheIdentity"] = controls.cache_identity(tree, resolved, report.artifacts)
    if derived_from:
        body["derivedFrom"] = derived_from
    body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    out = receipt_path(repo_root, task_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    return body


def _receipt_digest(body: dict) -> str:
    unsigned = {k: v for k, v in body.items() if k != "digest"}
    return hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()[:16]


def _receipt_body(repo_root: Path, task_name: str) -> tuple[dict | None, str]:
    path = receipt_path(repo_root, task_name)
    if not path.is_file():
        return None, "no receipt; run the gate on this exact tree"
    try:
        body = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return None, f"unreadable receipt: {error}"
    if body.get("digest") != _receipt_digest(body):
        return None, "receipt digest does not match its contents"
    gate_fingerprint = str(body.get("gateFingerprint") or "")
    publish_fingerprint = str(body.get("publishPolicyFingerprint") or "")
    if not gate_fingerprint or not publish_fingerprint:
        return None, "receipt predates safety fingerprints; re-run the gate"
    try:
        current_gate = safety.snapshot("gate")["digest"]
        current_publish = safety.snapshot("publish_policy")["digest"]
    except safety.SafetyRefused as error:
        return None, str(error)
    if gate_fingerprint != current_gate:
        return None, "gate implementation changed; re-run the gate"
    if publish_fingerprint != current_publish:
        return None, "publish policy changed; re-run the gate"
    hook_proof = body.get("repositoryHook")
    if not isinstance(hook_proof, dict):
        return None, "receipt predates live pre-push hook binding; re-run the gate"
    recorded_hook = hook_proof.get("identity")
    if not isinstance(recorded_hook, dict):
        return None, "receipt live pre-push hook identity is malformed"
    current_hook = live_hook_identity(repo_root)
    if current_hook.get("error"):
        return None, str(current_hook["error"])
    if recorded_hook != current_hook:
        return None, "live pre-push hook changed; re-run the gate"
    hook_state = str(hook_proof.get("state") or "")
    if hook_state not in {"passed", "not-applicable", "deferred"}:
        return None, "receipt has no usable live pre-push hook proof"
    if hook_state == "deferred":
        if not body.get("derivedFrom"):
            return None, "only a derived carry receipt may defer live hook execution"
        if not hook_proof.get("remote") or not hook_proof.get("branch"):
            return None, "deferred live hook proof has no bound publication target"
        try:
            validate_sha(str(hook_proof.get("remoteSha") or ""), "deferred hook remote SHA")
        except ValueError as error:
            return None, str(error)
    if str(hook_proof.get("candidateSha") or "") != str(body.get("head") or ""):
        return None, "live pre-push hook proof is for another candidate"
    if not body.get("ok"):
        return None, "receipt records a failing gate"
    policy = set(_push_policy(body.get("artifacts") or {}))
    if "requiredChecks" in body:
        required_checks = body.get("requiredChecks")
        inapplicable_checks = body.get("inapplicableChecks")
        if not isinstance(required_checks, list) or not all(
            isinstance(name, str) for name in required_checks
        ):
            return None, "receipt required-check list is malformed"
        if not isinstance(inapplicable_checks, list) or not all(
            isinstance(name, str) for name in inapplicable_checks
        ):
            return None, "receipt applicability list is malformed"
        required_set = set(required_checks)
        inapplicable_set = set(inapplicable_checks)
        if (
            len(required_set) != len(required_checks)
            or len(inapplicable_set) != len(inapplicable_checks)
            or required_set & inapplicable_set
            or required_set | inapplicable_set != policy
            or not inapplicable_set <= INAPPLICABLE_PUSH_CHECKS
        ):
            return None, "receipt applicability does not cover the current push policy"
        checks = body.get("checks") or {}
        wrongly_exempted = sorted(
            name for name in inapplicable_set if checks.get(name) != NOT_RUN
        )
        if wrongly_exempted:
            return (
                None,
                "receipt marks a completed check inapplicable: "
                + ", ".join(wrongly_exempted),
            )
    else:
        # Receipts written before applicability was recorded remain valid only
        # under the old, stricter rule that every policy check passed.
        required_checks = sorted(policy)
    checks = body.get("checks") or {}
    unsafe = [name for name in required_checks if checks.get(name) != PASS]
    if unsafe:
        return None, "receipt lacks passing push-required checks: " + ", ".join(unsafe)
    try:
        receipt_at = datetime.fromisoformat(str(body.get("at") or ""))
        committed_at = datetime.fromisoformat(_git(repo_root, "show", "-s", "--format=%cI", body["head"]))
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        return None, f"receipt binding is unreadable: {error}"
    if receipt_at < committed_at:
        return None, "receipt predates the commit it claims to certify"
    return body, ""


def verify_receipt(repo_root: Path, task_name: str) -> tuple[bool, str]:
    """A receipt is only valid for the exact clean HEAD it was written against.

    Reused, stale or dirty-tree evidence is not a pass. Nothing may supply
    receipt content except a fresh gate run -- never an environment variable,
    never a copy.
    """
    repo_root = Path(repo_root)
    body, problem = _receipt_body(repo_root, task_name)
    if body is None:
        return False, problem
    try:
        head = _git(repo_root, "rev-parse", "HEAD")
        dirty = bool(_git(repo_root, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError) as e:
        return (False, f"unreadable receipt or git failure: {e}")
    if body.get("head") != head:
        return (False, f"receipt is for {str(body.get('head'))[:8]}, HEAD is {head[:8]}")
    if dirty:
        return (False, "worktree is dirty; the receipt describes a tree that is no longer here")
    return (True, f"{body['source']} receipt {body['digest']} for {head[:8]}")


CARRYABLE_CHECKS = frozenset({"oracle", "artifact-transfer", "config-integrity", "tags"})
RERUN_ON_CARRY = frozenset(PUSH_REQUIRED) - CARRYABLE_CHECKS


def carry_receipt(
    repo_root: Path,
    task_name: str,
    candidate: str,
    *,
    remote: str = "origin",
    branch: str = "",
    defer_hook: bool = False,
    expected_remote_sha: str = "",
    review_requests: dict | None = None,
) -> dict:
    """Issue an exact-candidate receipt from whitelisted tree-invariant evidence."""
    repo_root = Path(repo_root)
    body, problem = _receipt_body(repo_root, task_name)
    if body is None:
        return {"ok": False, "reason": problem}
    original = str(body.get("head") or "")
    try:
        before = _git(repo_root, "rev-parse", f"{original}:{task_name}")
        after = _git(repo_root, "rev-parse", f"{candidate}:{task_name}")
        head = _git(repo_root, "rev-parse", "HEAD")
    except subprocess.CalledProcessError as error:
        return {"ok": False, "reason": f"could not resolve receipt trees: {error}"}
    if head != candidate:
        return {"ok": False, "reason": f"candidate {candidate[:8]} is not HEAD {head[:8]}"}
    if not before or before != after:
        return {"ok": False, "reason": "task tree changed; receipt evidence cannot be carried"}

    prior = body.get("checks") or {}
    prior_artifacts = body.get("artifacts") or {}
    artifact_required = bool((prior_artifacts.get("artifactTransfer") or {}).get("required"))
    carryable = set(CARRYABLE_CHECKS)
    if not artifact_required:
        carryable.discard("artifact-transfer")
    unavailable = sorted(name for name in carryable if prior.get(name) != PASS)
    if unavailable:
        return {"ok": False, "reason": "prior receipt lacks carryable evidence: " +
                ", ".join(unavailable)}

    report = Report()
    for artifact_name in ("artifactTransfer", "changeEvidence"):
        artifact = prior_artifacts.get(artifact_name)
        if isinstance(artifact, dict):
            report.artifacts[artifact_name] = dict(artifact)
    for name in sorted(carryable):
        report.add(name, PASS,
                   f"carried from {original[:12]} over identical task tree {before[:12]}")
    if not artifact_required:
        report.add(
            "artifact-transfer",
            NOT_RUN,
            "no tests/Dockerfile in the byte-identical task tree",
            blocking=False,
        )
    check_scope(repo_root, task_name, report)
    check_diff(repo_root, report)
    check_contamination(repo_root, report)
    check_findings(
        task_name,
        Journal.open(repo_root, task_name),
        report,
        request_snapshot=review_requests,
    )
    check_control_manifest(repo_root, repo_root / task_name, task_name, report)
    report.require(PUSH_REQUIRED)
    if not report.ok:
        return {"ok": False, "reason": "candidate controls did not pass", "report": report.as_dict()}
    receipt = write_receipt(
        repo_root,
        task_name,
        report,
        derived_from=original,
        remote=remote,
        branch=branch,
        defer_hook=defer_hook,
        expected_remote_sha=expected_remote_sha,
    )
    if not receipt.get("ok"):
        return {"ok": False, "reason": receipt.get("reason", "receipt was not written")}
    return {"ok": True, "receipt": receipt, "carried": sorted(CARRYABLE_CHECKS),
            "rerun": sorted(RERUN_ON_CARRY)}


# --- hook installation ------------------------------------------------------

PRE_PUSH = """#!/bin/sh
# benchsmith pre-push gate. Never bypass with --no-verify.
set -eu
exec "$BENCHSMITH_BIN" gate --repo "$(git rev-parse --show-toplevel)" \\
     --task "${BENCHSMITH_TASK:?set BENCHSMITH_TASK to the task directory name}" \\
     --verify-receipt
"""


def install_hooks(repo_root: Path, lib_dir: Path) -> list[str]:
    """Install into the hooks directory the repo ALREADY uses.

    Never repoint `core.hooksPath`: another tool's gate may live there, and
    redirecting it silently disables that gate. An existing hook is chained,
    never overwritten -- reordering someone else's tooling unasked is worse than
    saying so loudly.
    """
    out = []
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        hooks = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
        if hooks is None:
            r = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
                capture_output=True,
                text=True,
                check=True,
            )
            hooks = Path(repo_root) / r.stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        return [f"could not resolve hooks path: {e}"]

    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-push"
    body = PRE_PUSH.replace("$BENCHSMITH_BIN", str(Path(lib_dir).parent.parent / "bin" / "benchsmith"))
    if target.exists():
        existing = target.read_text()
        if "benchsmith pre-push gate" in existing:
            out.append(f"pre-push already ours at {target}")
        else:
            out.append(f"FOREIGN pre-push hook at {target} — left alone; chain it manually")
            return out
    else:
        target.write_text(body)
        target.chmod(0o755)
        out.append(f"installed pre-push at {target}")
    return out
