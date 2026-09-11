"""A committed file may not depend on a path that is not committed.

The defect this ends: a tracked gate script sourced an UNTRACKED helper, so the
gate shipped and was dead on every machine but the author's. `git commit -a`
reproduces it trivially -- it stages the tracked edit and ignores the new file.

Anchored on **invocation**, not on path-shaped text. Matching any path-like
substring false-positives on docstrings and message strings, and misses the real
defect, whose line is `source "$GATE_SCRIPT_DIR/gate-status.sh"` -- a
variable-built path that never matches a literal. So: match the verb, then
resolve by basename.

Ported from the t-bench repo's `check_untracked_deps.py`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Backtick is deliberately absent: as command substitution it is legacy and rare,
# as markdown it is everywhere, and including it flagged a script named inside
# another script's own docstring.
INVOKE = re.compile(
    r"""(?:^|\||&|;|\$\(|\bexec\s|\bsource\s|\.\s|\bbash\s|\bsh\s|\bpython3?\s)\s*"""
    r"""["']?(?P<path>(?:[\w./$\{\}-]*/)?(?P<base>[\w-]+(?:\.[\w-]+)*\.(?:sh|py)))(?![\w.])""",
    re.M,
)
ASSIGN_PATH = re.compile(
    r'''^\s*(?:export\s+|readonly\s+|local\s+)?'''
    r'''(?P<var>[A-Za-z_][A-Za-z0-9_]*)=["']?'''
    r'''(?P<path>(?:[\w./$\{\}-]*/)?[\w-]+(?:\.[\w-]+)*\.(?:sh|py))["']?\s*$'''
)
INVOKE_BOUND = re.compile(
    r'''(?:^|\||&|;|\bexec\s|\bsource\s|\.\s|\bbash\s|\bsh\s|\bpython3?\s)\s*'''
    r'''["']?\$\{?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}?["']?(?![\w/])'''
)
SOURCE_FILE = re.compile(r"\.(sh|py|bash)$")
IGNORE_BASENAMES = {"setup.py", "conftest.py", "__init__.py"}
# Repo tooling plus task-owned host runners. Task `solve.sh`/`test.sh` reference
# CONTAINER paths by design and are excluded; mutant/variant and corpus runners
# execute on the host and must be dependency-closed in a fresh clone.
TOOLING_DIR = re.compile(
    r"^(?:(?:scripts|\.githooks|\.benchsmith|gate-fixtures)/|"
    r"[^/]+/(?:run-(?:mutants|variants)\.sh|corpus/.*\.(?:sh|py))$)"
)
# A variable-built path is in scope only when the variable points INSIDE the repo.
# A var resolving to an external plugin cache is correctly absent and none of
# this check's business.
REPO_ROOT_VARS = {"REPO", "REPO_DIR", "REPO_ROOT", "GATE_SCRIPT_DIR", "SELF",
                  "SKILL_BIN", "DIR", "TASK_DIR", "BENCHSMITH_BIN"}
VAR_PREFIX = re.compile(r"^\$\{?(\w+)\}?/")


def _git(repo: Path, *a) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=120)


def check(repo: Path, rev: str = "") -> dict:
    repo = Path(repo)
    if rev:
        universe = set(_git(repo, "ls-tree", "-r", "--name-only", rev).stdout.splitlines())
        candidates = sorted(universe)
        read = lambda p: _git(repo, "show", f"{rev}:{p}").stdout
    else:
        tracked = set(_git(repo, "ls-files").stdout.splitlines())
        staged = set(_git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR").stdout.splitlines())
        deleted = set(_git(repo, "diff", "--cached", "--name-only", "--diff-filter=D").stdout.splitlines())
        universe = (tracked | staged) - deleted
        candidates = sorted(staged)
        read = lambda p: _git(repo, "show", f":{p}").stdout

    sources = [p for p in candidates if SOURCE_FILE.search(p) and TOOLING_DIR.match(p)]
    if not sources:
        return {"state": "NOT_RUN", "examined": 0, "findings": [],
                "detail": "no repo-tooling shell or python file in the snapshot"}

    by_base: dict[str, list[str]] = {}
    for p in universe:
        by_base.setdefault(p.rsplit("/", 1)[-1], []).append(p)

    findings, refs = [], 0
    for path in sources:
        text = read(path)
        if not text:
            continue
        bindings = {}
        for line in text.splitlines():
            m = ASSIGN_PATH.match(line)
            if m:
                bindings[m.group("var")] = m.group("path")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            refs_here = [(m.group("base"), m.group("path")) for m in INVOKE.finditer(line)]
            for m in INVOKE_BOUND.finditer(line):
                target = bindings.get(m.group("var"))
                if target:
                    refs_here.append((target.rsplit("/", 1)[-1], target))
            for base, target in refs_here:
                if target.startswith("/") or base in IGNORE_BASENAMES:
                    continue
                var = VAR_PREFIX.match(target)
                if var and var.group(1) not in REPO_ROOT_VARS:
                    continue
                refs += 1
                if base not in by_base:
                    findings.append({"path": path, "line": lineno, "target": target,
                                     "source": stripped[:110]})

    return {
        "state": "FAIL" if findings else "PASS",
        "examined": len(sources),
        "references": refs,
        "findings": findings,
        "detail": ("; ".join(f"{f['path']}:{f['line']} -> {f['target']} (not committed)"
                             for f in findings[:3]) + ". A fresh clone will not have these"
                   if findings else
                   f"{len(sources)} tooling file(s), {refs} reference(s), all committed"),
    }
