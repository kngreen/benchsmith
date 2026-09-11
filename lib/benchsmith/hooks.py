"""Repo pre-commit hooks, absorbed into the gate.

The AAI Labs task repos ship a `.githooks/pre-commit` that runs prettier and
eslint over staged files under a path prefix. Reproducing it here means one
implementation, versioned with the skill, that every worker gets -- rather than
a script each clone may or may not have wired up.

**Speed is the whole design constraint**, because this runs on every gate in a
loop that may be running a dozen workers:

  1. *Fast path.* Nothing runs unless a staged file matches a configured glob.
     A benchsmith round almost always touches `tests/`, `instruction.md` or
     `solution/` -- none of which match a `web/src` glob -- so the common case
     costs one `git diff --cached` and no subprocesses at all.
  2. *Content cache.* A pass is keyed on the tool's argv plus the exact bytes of
     the files it saw. Re-gating an unchanged tree is a cache hit, not a second
     `npx` cold start.
  3. *Concurrency.* Independent hooks run at the same time; the wall clock is
     the slowest hook, not their sum.
  4. *A budget.* Exceeding it reports NOT_RUN with the reason. A formatter that
     hangs must not become a loop that hangs.

Checking and fixing are separate commands on purpose. `benchsmith fmt` mutates
and is run before committing; the gate only ever *checks*. A gate that rewrote
files would change the graded surface after its own hashes were computed, and
the receipt would attest to a tree that no longer exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "benchsmith" / "hooks"

DEFAULT_BUDGET = 120

PASS, FAIL, NOT_RUN, SKIPPED = "PASS", "FAIL", "NOT_RUN", "SKIPPED"

# Mirrors the `.githooks/pre-commit` shipped in the AAI Labs task repos.
DEFAULT_HOOKS = [
    {
        "name": "prettier",
        "globs": ["web/src/**/*.ts", "web/src/**/*.tsx", "web/src/**/*.css"],
        "cwd": "web",
        "strip": "web/",
        "check": ["npx", "prettier", "--check", "--log-level", "warn"],
        "fix": ["npx", "prettier", "--write", "--log-level", "warn"],
    },
    {
        "name": "eslint",
        "globs": ["web/src/**/*.ts", "web/src/**/*.tsx"],
        "cwd": "web",
        "strip": "web/",
        "check": ["npx", "eslint", "--max-warnings", "0", "--no-warn-ignored"],
        "fix": ["npx", "eslint", "--fix", "--max-warnings", "0", "--no-warn-ignored"],
    },
]


def glob_to_re(pat: str) -> re.Pattern:
    """`**/` spans directories, `*` does not. fnmatch conflates them."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matching(paths: list[str], globs: list[str]) -> list[str]:
    pats = [glob_to_re(g) for g in globs]
    return [p for p in paths if any(r.match(p) for r in pats)]


def staged(repo_root: Path) -> tuple[list[str], str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"git unavailable: {e}"
    if r.returncode != 0:
        return [], f"git failed: {r.stderr.strip()[:120]}"
    return [p for p in r.stdout.splitlines() if p.strip()], ""


@dataclass
class Result:
    name: str
    state: str
    detail: str = ""
    files: int = 0
    seconds: float = 0.0
    cached: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "detail": self.detail,
                "files": self.files, "seconds": round(self.seconds, 2), "cached": self.cached}


def _key(argv: list[str], repo_root: Path, files: list[str]) -> str:
    h = hashlib.sha256()
    h.update("\x00".join(argv).encode())
    for rel in sorted(files):
        h.update(rel.encode())
        try:
            h.update((repo_root / rel).read_bytes())
        except OSError:
            # Unreadable content cannot be cached against -- a key that ignores
            # the bytes would hit on a file that changed underneath it.
            h.update(b"\xff<unreadable>")
    return h.hexdigest()


def _cached(key: str) -> bool:
    return (CACHE_DIR / key).is_file()


def _remember(key: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / key).write_text(str(time.time()))
    except OSError:
        pass  # a cache that cannot be written is slow, not wrong


def run_one(repo_root: Path, spec: dict, paths: list[str], *, fix: bool = False,
            budget: int = DEFAULT_BUDGET, use_cache: bool = True, runner=None) -> Result:
    name = str(spec.get("name") or "hook")
    files = matching(paths, list(spec.get("globs") or []))
    if not files:
        return Result(name, SKIPPED, "no staged file matches", 0, 0.0)

    argv = list(spec.get("fix" if fix else "check") or [])
    if not argv:
        return Result(name, NOT_RUN, f"no {'fix' if fix else 'check'} command configured", len(files))

    strip = str(spec.get("strip") or "")
    rel = [p[len(strip):] if strip and p.startswith(strip) else p for p in files]
    cwd = repo_root / str(spec.get("cwd") or ".")
    if not cwd.is_dir():
        return Result(name, NOT_RUN, f"cwd {spec.get('cwd')!r} does not exist", len(files))

    key = _key(argv, repo_root, files)
    # Only a *check* may be served from cache. A fix is expected to mutate the
    # tree, and skipping it because an identical tree once passed would leave
    # the files unwritten while reporting success.
    if use_cache and not fix and _cached(key):
        return Result(name, PASS, "cached", len(files), 0.0, cached=True)

    started = time.time()
    try:
        run = runner or (lambda a, c: subprocess.run(a, cwd=str(c), capture_output=True,
                                                     text=True, timeout=budget))
        r = run(argv + rel, cwd)
    except subprocess.TimeoutExpired:
        return Result(name, NOT_RUN, f"exceeded the {budget}s budget", len(files), time.time() - started)
    except OSError as e:
        # A missing toolchain is NOT_RUN, never a pass: "node is not installed"
        # and "the code is formatted" are not the same finding.
        return Result(name, NOT_RUN, f"could not run: {e}", len(files), time.time() - started)

    took = time.time() - started
    if r.returncode == 0:
        if not fix:
            _remember(key)
        return Result(name, PASS, "clean" if not fix else "applied", len(files), took)
    return Result(name, FAIL, ((r.stdout or "") + (r.stderr or "")).strip()[:400], len(files), took)


def run_all(repo_root: Path, specs: list[dict] | None = None, *, fix: bool = False,
            budget: int = DEFAULT_BUDGET, use_cache: bool = True, paths: list[str] | None = None,
            runner=None) -> dict:
    specs = specs if specs is not None else DEFAULT_HOOKS
    if paths is None:
        paths, why = staged(Path(repo_root))
        if why:
            return {"ok": False, "state": NOT_RUN, "reason": why, "results": [], "seconds": 0.0}

    # The fast path: decided from one `git diff --cached`, no subprocess at all.
    wanted = [s for s in specs if matching(paths, list(s.get("globs") or []))]
    if not wanted:
        return {"ok": True, "state": SKIPPED, "reason": "no staged file matches any hook",
                "results": [], "seconds": 0.0}

    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        results = list(pool.map(
            lambda s: run_one(Path(repo_root), s, paths, fix=fix, budget=budget,
                              use_cache=use_cache, runner=runner),
            wanted,
        ))
    failed = [r for r in results if r.state == FAIL]
    notrun = [r for r in results if r.state == NOT_RUN]
    return {
        "ok": not failed,
        "state": FAIL if failed else (NOT_RUN if notrun else PASS),
        "reason": ("; ".join(f"{r.name}: {r.detail[:120]}" for r in failed) if failed
                   else "; ".join(f"{r.name}: {r.detail[:80]}" for r in notrun) or "clean"),
        "results": [r.as_dict() for r in results],
        "seconds": round(time.time() - started, 2),
    }


def detect(repo_root: Path) -> dict:
    """What the repo ships, and whether git would actually run it.

    A hook file is not a hook. `.githooks/pre-commit` only runs if it is in the
    directory git uses, and in every AAI Labs clone checked, `core.hooksPath`
    was unset and `.git/hooks/` was empty -- so the shipped hook had never run.
    Sunsetting one is a different decision depending on which of those is true.
    """
    repo_root = Path(repo_root)
    found = []
    for d in (".githooks", ".git/hooks"):
        p = repo_root / d
        if p.is_dir():
            found += [str(f.relative_to(repo_root)) for f in sorted(p.iterdir())
                      if f.is_file() and not f.name.endswith(".sample")]
    r = subprocess.run(["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
                       capture_output=True, text=True)
    hooks_path = r.stdout.strip() if r.returncode == 0 else ""
    active_dir = hooks_path or ".git/hooks"
    active = [f for f in found if f.startswith(active_dir.rstrip("/"))]
    return {
        "files": found,
        "coreHooksPath": hooks_path or None,
        "activeDir": active_dir,
        "active": active,
        "verdict": ("git runs these" if active else
                    "no hook git would run; the shipped files are inert here"),
    }
