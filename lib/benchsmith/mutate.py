"""Would these tests catch a near-miss?

A suite that passes the reference and fails the unchanged base has been shown to
detect one difference: the whole solution. It has not been shown to discriminate
between the solution and a plausible wrong version of it. Those are different
claims, and only the second one says the grader is any good.

So: build a battery of small, plausible wrong answers against the reference, run
the suite on each, and report the ones it fails to catch. A survivor is a hole
in the grader, stated as a location and an edit, which is the only form of that
finding anyone can act on.

Two distinctions this keeps that a naive mutation run collapses:

  * A mutant that will not build or import is **not viable**. It says nothing
    about the tests -- the compiler rejected it -- so it leaves the denominator
    rather than counting as a catch.
  * An unsupported language is **NOT_RUN**, never a clean bill. Reporting "no
    survivors" for a Swift task with no probe is the exact fail-open this file
    exists to prevent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_BATTERY = 12

CAUGHT, SURVIVED, NOT_VIABLE = "caught", "survived", "not-viable"

# (pattern, replacement, operator name). Order is the priority order when the
# battery is capped, so the operators most likely to expose a missing assertion
# come first.
_COMMON = [
    (r"==", "!=", "equality-flip"),
    (r"!=", "==", "inequality-flip"),
    (r"<=", "<", "boundary-tighten"),
    (r">=", ">", "boundary-tighten"),
    (r"(?<![<>!=])<(?![=])", "<=", "boundary-loosen"),
    (r"(?<![<>!=])>(?![=])", ">=", "boundary-loosen"),
]

RULES: dict[str, list[tuple[str, str, str]]] = {
    ".py": _COMMON + [
        (r"\bTrue\b", "False", "truth-flip"),
        (r"\bFalse\b", "True", "truth-flip"),
        (r"\band\b", "or", "connective-swap"),
        (r"\bor\b", "and", "connective-swap"),
        (r"\bnot\s+", "", "negation-drop"),
        (r"\breturn\s+(?!None\b)\S.*", "return None", "return-none"),
    ],
    ".go": _COMMON + [
        (r"\btrue\b", "false", "truth-flip"),
        (r"\bfalse\b", "true", "truth-flip"),
        (r"&&", "||", "connective-swap"),
        (r"\|\|", "&&", "connective-swap"),
        (r"\bnil\b", "nil /*x*/", "nil-noop"),
    ],
}

SUPPORTED = tuple(RULES)


@dataclass(frozen=True)
class Mutant:
    path: str
    line: int
    operator: str
    before: str
    after: str
    outcome: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "operator": self.operator,
                "before": self.before.strip()[:120], "after": self.after.strip()[:120],
                "outcome": self.outcome}


def code_mask(src: str, suffix: str) -> list:
    """True at every index that is real code, not a string or a comment.

    A line-level comment skip is not enough. `assert msg == "use == here"`
    mutates inside the literal, producing a mutant that changes a message rather
    than a behaviour: it compiles, the tests pass, and it is reported as a
    survivor -- a hole in the grader that is not one.

    Taken from ripen's mutation probe, which walks the source character by
    character rather than trusting line shape.
    """
    n = len(src)
    mask = [True] * n
    line_comment = "#" if suffix == ".py" else "//"
    tri = ('"' * 3, "'" * 3)
    i = 0
    while i < n:
        c = src[i]
        if src.startswith(line_comment, i):
            j = src.find("\n", i)
            j = n if j < 0 else j
        elif suffix != ".py" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif suffix == ".py" and any(src.startswith(q, i) for q in tri):
            q = next(q for q in tri if src.startswith(q, i))
            j = src.find(q, i + 3)
            j = n if j < 0 else j + 3
        elif c == "`":
            j = src.find("`", i + 1)
            j = n if j < 0 else j + 1
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            mask[k] = False
        i = j
    return mask


def _skip(line: str, suffix: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if suffix == ".py":
        return s.startswith("#")
    return s.startswith("//")


def build_battery(files: dict[str, str], *, cap: int = MAX_BATTERY) -> tuple[list[Mutant], list[str]]:
    """Generate mutants deterministically. `files` maps path -> source text.

    Deterministic because a battery that varies run to run cannot be compared
    across rounds, and 'the probe found nothing this time' would be unreadable.
    """
    notes: list[str] = []
    out: list[Mutant] = []
    for path in sorted(files):
        suffix = Path(path).suffix
        rules = RULES.get(suffix)
        if rules is None:
            notes.append(f"{path}: {suffix or 'no extension'} unsupported; not probed")
            continue
        src = files[path]
        mask = code_mask(src, suffix)
        offset = 0
        for lineno, raw in enumerate(src.splitlines(keepends=True), 1):
            line = raw.rstrip("\n")
            if not _skip(line, suffix):
                placed = False
                for pattern, repl, op in rules:
                    pos = 0
                    while not placed:
                        m = re.search(pattern, line[pos:])
                        if not m:
                            break
                        s, e = pos + m.start(), pos + m.end()
                        # Only mutate where the whole match is real code. A
                        # match inside a string changes a message, not a
                        # behaviour: it survives every honest test and is
                        # reported as a hole in the grader that is not one.
                        if all(mask[offset + k] for k in range(s, min(e, len(line)))):
                            out.append(Mutant(path=path, line=lineno, operator=op,
                                              before=line, after=line[:s] + repl + line[e:]))
                            placed = True
                        pos = e
                    if placed:
                        break  # one mutant per line keeps each survivor a single edit
            offset += len(raw)
    if len(out) > cap:
        notes.append(f"battery capped at {cap} of {len(out)} candidate mutants")
        # Spread across files rather than exhausting the first one, so a
        # survivor in a later file is not hidden by an alphabetical accident.
        by_file: dict[str, list[Mutant]] = {}
        for m in out:
            by_file.setdefault(m.path, []).append(m)
        spread: list[Mutant] = []
        i = 0
        while len(spread) < cap:
            added = False
            for path in sorted(by_file):
                if i < len(by_file[path]) and len(spread) < cap:
                    spread.append(by_file[path][i])
                    added = True
            if not added:
                break
            i += 1
        out = spread
    return out, notes


def run_battery(task_dir: Path, mutants: list[Mutant], test_cmd: list[str], *,
                runner=None, timeout: int = 600) -> dict:
    """Apply each mutant to a throwaway copy and run the suite.

    A suite that passes on the mutated tree did not notice the wrong answer.
    """
    if not test_cmd:
        return {"status": "NOT_RUN", "reason": "no test command; the probe cannot be run",
                "mutants": [], "survivors": [], "viable": 0}
    if not mutants:
        return {"status": "NOT_RUN", "reason": "no mutants generated for these files",
                "mutants": [], "survivors": [], "viable": 0}

    def _default(cwd: Path) -> tuple[int, str]:
        r = subprocess.run(test_cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-2000:]

    run = runner or _default

    # Prove the suite passes UNMUTATED before believing anything it says about a
    # mutant. Without this the probe is worthless in the one way that matters:
    # a harness that cannot run at all fails on every mutant, every mutant reads
    # as caught, and the probe reports a clean bill for a suite that never
    # executed. Found exactly that way -- pytest was not installed.
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / task_dir.name
        shutil.copytree(task_dir, base, symlinks=True)
        try:
            base_code, base_log = run(base)
        except Exception as e:  # noqa: BLE001
            return {"status": "NOT_RUN", "reason": f"baseline run failed: {type(e).__name__}: {e}",
                    "mutants": [], "survivors": [], "viable": 0}
    if base_code != 0:
        return {"status": "NOT_RUN",
                "reason": ("the suite does not pass on the unmutated tree, so a 'caught' mutant "
                           f"would be indistinguishable from a broken harness: {base_log.strip()[-300:]}"),
                "mutants": [], "survivors": [], "viable": 0}

    done: list[Mutant] = []
    for mut in mutants:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / task_dir.name
            shutil.copytree(task_dir, work, symlinks=True)
            target = work / mut.path
            if not target.is_file():
                done.append(Mutant(**{**mut.__dict__, "outcome": NOT_VIABLE}))
                continue
            lines = target.read_text().splitlines(keepends=True)
            if mut.line > len(lines):
                done.append(Mutant(**{**mut.__dict__, "outcome": NOT_VIABLE}))
                continue
            eol = "\n" if lines[mut.line - 1].endswith("\n") else ""
            lines[mut.line - 1] = mut.after + eol
            target.write_text("".join(lines))
            try:
                code, log = run(work)
            except Exception as e:  # noqa: BLE001 - a broken mutant is not a caught one
                done.append(Mutant(**{**mut.__dict__, "outcome": NOT_VIABLE}))
                continue
            if _unbuildable(log):
                # The compiler rejected it. That is not the tests discriminating.
                done.append(Mutant(**{**mut.__dict__, "outcome": NOT_VIABLE}))
            else:
                done.append(Mutant(**{**mut.__dict__,
                                      "outcome": SURVIVED if code == 0 else CAUGHT}))

    survivors = [m for m in done if m.outcome == SURVIVED]
    viable = [m for m in done if m.outcome != NOT_VIABLE]
    return {
        "status": "FAIL" if survivors else ("NOT_RUN" if not viable else "PASS"),
        "reason": (f"{len(survivors)} of {len(viable)} viable mutants survived — the suite does "
                   "not discriminate here")
        if survivors
        else ("every mutant was rejected before it could be tested; the probe proved nothing"
              if not viable else f"all {len(viable)} viable mutants were caught"),
        "mutants": [m.as_dict() for m in done],
        "survivors": [m.as_dict() for m in survivors],
        "viable": len(viable),
    }


_UNBUILDABLE = re.compile(
    r"SyntaxError|IndentationError|ImportError|ModuleNotFoundError|"
    r"cannot find package|undefined:|syntax error|does not compile|build failed",
    re.I,
)


def _unbuildable(log: str) -> bool:
    return bool(_UNBUILDABLE.search(log or ""))


def probe(task_dir: Path, targets: list[str], test_cmd: list[str], **kw) -> dict:
    """Full probe: read the targets, build the battery, run it."""
    files: dict[str, str] = {}
    unsupported: list[str] = []
    for rel in targets:
        p = Path(task_dir) / rel
        if not p.is_file():
            continue
        if p.suffix not in SUPPORTED:
            unsupported.append(rel)
            continue
        files[rel] = p.read_text(errors="replace")
    if not files:
        langs = ", ".join(SUPPORTED)
        return {"status": "NOT_RUN",
                "reason": (f"no probeable target ({langs} only); "
                           f"unsupported: {', '.join(unsupported) or 'none found'}. "
                           "Uncovered, not clean."),
                "mutants": [], "survivors": [], "viable": 0}
    mutants, notes = build_battery(files)
    result = run_battery(Path(task_dir), mutants, test_cmd, **kw)
    result["notes"] = notes + ([f"unsupported targets: {', '.join(unsupported)}"] if unsupported else [])
    return result
