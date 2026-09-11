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
import subprocess
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

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
PUSH_REQUIRED = ("oracle", "scope", "config-integrity", "tags",
                 "diff-ratchet", "diff-weakening", "contamination",
                 # A repair that addressed nothing is not a repair.
                 "review-findings")


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

    def add(self, *a, **kw) -> None:
        self.checks.append(Check(*a, **kw))

    def require(self, names) -> None:
        """Mark checks whose NOT_RUN must block. Names that ran are unaffected."""
        wanted = set(names or ())
        for c in self.checks:
            if c.name in wanted:
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
    stop = journal.stop_reason()
    if stop:
        report.add("budget", FAIL, stop)
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
    if len(hit) > 1:
        report.add("single-lever", FAIL,
                   "moves " + ", ".join(sorted(hit)) + " in one hardening round; "
                   "split them so the next measurement is attributable")
    elif not hit:
        report.add("single-lever", PASS, "no difficulty lever moved")
    else:
        report.add("single-lever", PASS, f"one lever: {hit.pop()}")


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


def check_findings(task_name: str, journal, report: Report, *, binary: str = "codimango") -> None:
    """A repair round must address what the reviewer asked for.

    The whole point of `needs_revision` is that a person named something wrong.
    Pushing a repair with no finding recorded means the loop cannot have
    verified the change it claims to have made -- it never wrote down what the
    change was, or how it would know it worked.
    """
    from .reviews import requests, unaddressed

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

    failed, exempted = [], []
    for c in checks:
        if str(c.get("status", "")).lower() in ("pass", "ok", "passed"):
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
        failed.append(f"{c.get('name')}: {detail[:100]}")
    note = f"; exempted: {', '.join(exempted)}" if exempted else ""
    report.add("structural", FAIL if failed else PASS,
               ("; ".join(failed[:4]) + note) if failed
               else f"{len(checks)} upstream check(s) pass{note}")


def check_diff(repo_root: Path, report: Report) -> None:
    """Staged-vs-HEAD checks: is this change worse than the last one?

    Distinct from `check_test_ratchet`, which compares against the last round
    benchsmith RECORDED. Anything committed between rounds is invisible to that
    one, and `staged vs HEAD` is cheaply available only here.
    """
    from . import diffcheck

    for name, res in diffcheck.run(Path(repo_root)).items():
        report.add(name, res["state"], res["detail"])


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
    report = Report()
    journal = Journal.open(Path(repo_root), task_name)
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
    check_diff(repo_root, report)
    check_formatting(repo_root, report, hook_specs)
    check_hygiene(task_dir, report)
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


HOOK_RECEIPT = "/tmp/gate-receipt-{task}.json"


def _emit_hook_receipt(repo_root: Path, task_name: str, report: Report,
                       head: str, dirty: bool) -> str:
    """Also write the receipt the repo's own pre-push hook reads."""
    path = Path(os.environ.get("GATE_RECEIPT") or HOOK_RECEIPT.format(task=task_name))
    gates = {}
    for c in report.checks:
        if not c.blocking:
            continue
        gates[c.name] = {PASS: "pass", FAIL: "fail",
                         NOT_RUN: "not_run", TIMEOUT: "timeout"}.get(c.state, c.state.lower())
    try:
        path.write_text(json.dumps({
            "task": task_name, "commit": head, "dirty": dirty, "gates": gates,
            "source": "benchsmith",
            "note": "gate names are benchsmith's checks, not the repo's G1-G5; "
                    "not_run and timeout are not pass",
        }, indent=1))
    except OSError:
        return ""
    return str(path)


def write_receipt(repo_root: Path, task_name: str, report: Report) -> dict:
    """Bind a passing gate run to an exact clean HEAD."""
    repo_root = Path(repo_root)
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

    # The task repos ship their own pre-push hook, and it reads a receipt at
    # $GATE_RECEIPT (default /tmp/gate-receipt-<task>.json) with `commit`,
    # `dirty` and a `gates` map that must be all-pass. Benchsmith wrote its
    # receipt somewhere else in its own shape, so every benchsmith-gated push
    # was rejected by the repo's hook -- two systems enforcing the same rule and
    # refusing to believe each other.
    #
    # The emitted gates are benchsmith's own check names, not the repo's G1-G5.
    # Renaming them to match would claim checks that did not run; a reader of
    # this receipt sees exactly what was verified.
    _emit_hook_receipt(repo_root, task_name, report, head.strip(), dirty)

    body = {
        "task": task_name,
        "head": head,
        "tree": tree,
        "clean": True,
        "ok": True,
        "checks": {c.name: c.state for c in report.checks},
        "notRun": [c.name for c in report.checks if c.state == NOT_RUN],
        "source": "canonical" if canonical_receipt(repo_root) else "benchsmith-native-fallback",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    out = receipt_path(repo_root, task_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n")
    return body


def verify_receipt(repo_root: Path, task_name: str) -> tuple[bool, str]:
    """A receipt is only valid for the exact clean HEAD it was written against.

    Reused, stale or dirty-tree evidence is not a pass. Nothing may supply
    receipt content except a fresh gate run -- never an environment variable,
    never a copy.
    """
    repo_root = Path(repo_root)
    p = receipt_path(repo_root, task_name)
    if not p.is_file():
        return (False, "no receipt; run the gate on this exact tree")
    try:
        body = json.loads(p.read_text())
        head = _git(repo_root, "rev-parse", "HEAD")
        dirty = bool(_git(repo_root, "status", "--porcelain"))
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError) as e:
        return (False, f"unreadable receipt or git failure: {e}")

    if not body.get("ok"):
        return (False, "receipt records a failing gate")
    if body.get("head") != head:
        return (False, f"receipt is for {str(body.get('head'))[:8]}, HEAD is {head[:8]}")
    if dirty:
        return (False, "worktree is dirty; the receipt describes a tree that is no longer here")
    return (True, f"{body['source']} receipt {body['digest']} for {head[:8]}")


# --- hook installation ------------------------------------------------------

PRE_PUSH = """#!/bin/sh
# benchsmith pre-push gate. Never bypass with --no-verify.
set -eu
exec python3 "$BENCHSMITH_BIN" gate --repo "$(git rev-parse --show-toplevel)" \\
     --task "${BENCHSMITH_TASK:?set BENCHSMITH_TASK to the task directory name}" \\
     --require-push-set
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
