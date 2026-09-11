"""The pre-push gate.

Every check reports PASS, FAIL or NOT_RUN, and **NOT_RUN is never a pass**. A
check that could not run must not read like one that ran and was satisfied --
that distinction is the whole point of the tri-state.

Installed as a `commit-msg` + `pre-push` pair so it cannot be quietly skipped: a
rule the loop can break by forgetting is not a gate.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .journal import Journal, surface_hashes

REQUIRED_TAGS = ("assay-v1", "aai-labs", "semi-synthetic", "private_repos_1p")
TEAM_TAG_PREFIX = "aai-labs-"

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    blocking: bool = True

    @property
    def blocks(self) -> bool:
        # NOT_RUN does not block on its own, but it never clears a box either:
        # it is reported, and a terminal claim that depends on it is not available.
        return self.blocking and self.state == FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *a, **kw) -> None:
        self.checks.append(Check(*a, **kw))

    @property
    def ok(self) -> bool:
        return not any(c.blocks for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.__dict__ for c in self.checks],
            "notRun": [c.name for c in self.checks if c.state == NOT_RUN],
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
        report.add("config-integrity", NOT_RUN, "no tests/config.json (not applicable to this track)")
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
        proof = (Path(task_dir) / ".assay" / "removals.md").is_file()
        report.add(
            "test-ratchet",
            PASS if proof else FAIL,
            f"{prior[-1]} -> {count}" + ("" if proof else "; no per-case invalidity proof"),
        )
    else:
        report.add("test-ratchet", PASS, f"{prior[-1]} -> {count}")


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
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        report.add("scope", NOT_RUN, f"git unavailable: {e}")
        return
    staged = [p for p in r.stdout.split() if p]
    if not staged:
        report.add("scope", NOT_RUN, "nothing staged")
        return
    stray = [p for p in staged if not (p.startswith(f"{task_name}/") or p.startswith(".assay/"))]
    report.add(
        "scope",
        FAIL if stray else PASS,
        ("reaches outside the task: " + ", ".join(stray[:5])) if stray else f"{len(staged)} paths",
    )


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
) -> Report:
    report = Report()
    journal = Journal.open(Path(repo_root), task_name)
    check_tags(task_dir, report)
    check_difficulty(task_dir, measured, report)
    check_config_integrity(task_dir, report)
    check_test_ratchet(task_dir, journal, report)
    check_journal(journal, report)
    check_excursion(journal, report)
    check_budget(journal, report)
    check_scope(repo_root, task_name, report)
    check_hygiene(task_dir, report)
    check_oracle(oracle_cmd, report)
    h = surface_hashes(task_dir)
    report.add("graded-hash", PASS, f"{h['gradedHash']} ({h['gradedFiles']} files)", blocking=False)
    return report


# --- hook installation ------------------------------------------------------

PRE_PUSH = """#!/bin/sh
# assay pre-push gate. Never bypass with --no-verify.
set -eu
exec python3 "$ASSAY_LIB/../bin/assay" gate --repo "$(git rev-parse --show-toplevel)" \\
     --task "${ASSAY_TASK:?set ASSAY_TASK to the task directory name}"
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
    body = PRE_PUSH.replace("$ASSAY_LIB", str(lib_dir))
    if target.exists():
        existing = target.read_text()
        if "assay pre-push gate" in existing:
            out.append(f"pre-push already ours at {target}")
        else:
            out.append(f"FOREIGN pre-push hook at {target} — left alone; chain it manually")
            return out
    else:
        target.write_text(body)
        target.chmod(0o755)
        out.append(f"installed pre-push at {target}")
    return out
