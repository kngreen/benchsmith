"""Is this change worse than the last one?

Every other control here asks "is the current tree bad?". None asked whether the
change makes it worse, which is why coverage shrinking, fixture-arm removal and
assertion weakening were invisible to them.

These checks are **diff-shaped**: they compare the staged index against HEAD. That
comparison is exactly and cheaply available only at commit time, which is why the
task repos put them in a pre-commit hook rather than in the pre-push gate. The
journal-based ratchet is not a substitute -- it compares against the last round
benchsmith *recorded*, so anything committed between rounds is invisible to it.

Ported from the t-bench repo's `scripts/check_staged_diff.py`, which has been
catching real defects. Two of its judgements are worth keeping deliberately:

  * A file that will not parse is a **finding**, not a skip. A line-based check
    happily "examines" a broken file and reports clean -- the silent-inert shape
    these checks exist to catch.
  * Nothing examined is **NOT_RUN**, reported and never printed as a pass.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

GRADED = re.compile(r"(^|/)(steps/[^/]+/)?tests?/.*\.(py|go|swift)$")
PATCH_CONFIG = re.compile(r"(^|/)(steps/[^/]+/)?tests?/config\.json$")
GO_TEST = re.compile(r"(?m)^\s*func\s+(Test[A-Za-z0-9_]+)\s*\(")
GO_ASSERT = re.compile(r"\b(?:t|tb)\.(?:Errorf?|Fatalf?|FailNow)\s*\(|\b(?:assert|require)\.[A-Za-z0-9_]+\s*\(")
SWIFT_TEST = re.compile(r"(?m)^\s*func\s+(test[A-Za-z0-9_]+)\s*\(")
SWIFT_ASSERT = re.compile(r"\bXCTAssert[A-Za-z0-9_]*\s*\(")

# Tokens that turn a failing assertion into a passing one.
WEAKEN_TOKENS = [
    (re.compile(r"\|\|\s*true\b"), "|| true added"),
    (re.compile(r"\bor\s+True\b"), "or True added"),
    (re.compile(r"@?pytest\.(mark\.)?skip"), "skip added"),
    (re.compile(r"^\s*#\s*noqa"), "noqa added"),
]
ASSERT_OR = re.compile(r"^\s*assert\s+.+\s+or\s+")
TOLERANCE = re.compile(
    r"(?:abs\s*\([^)]*\)\s*<=?\s*|rel\s*=\s*|abs\s*=\s*|delta\s*=\s*|atol\s*=\s*|rtol\s*=\s*)"
    r"([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)")

# A removal is allowed when the SAME commit records why. Only proofs added or
# changed in the staged commit count: a standing allowlist would let one old
# entry authorise every future removal of that name.
REMOVAL_LEDGERS = (".benchsmith/removals.jsonl",)


@dataclass(frozen=True)
class Finding:
    path: str
    what: str
    line: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "what": self.what, "line": self.line[:160]}


def _git(repo: Path, *a) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=120)


def blob(repo: Path, rev: str, path: str) -> str | None:
    """Contents at a revision. `rev=""` means the staged index."""
    r = _git(repo, "show", f"{rev}:{path}")
    return r.stdout if r.returncode == 0 else None


@dataclass(frozen=True)
class ChangeSet:
    """The change under review, wherever it currently lives.

    A gate that only reads the index cannot evaluate a committed tree, and
    `commit, then gate, then push` is the natural order -- so every push-required
    diff check reported NOT_RUN at exactly the moment it mattered. Observed on a
    real run: "the push-strict gate cannot evaluate a committed tree because its
    scope checks only staged changes."

    `new`/`old` are the revisions to read blobs from: the index against HEAD when
    something is staged, HEAD against its parent once it is committed.
    """

    paths: list
    new: str
    old: str
    source: str

    @property
    def empty(self) -> bool:
        return not self.paths


def changeset(repo: Path, filt: str = "ACMRD") -> ChangeSet:
    r = _git(repo, "diff", "--cached", "--name-only", f"--diff-filter={filt}")
    staged = [p for p in r.stdout.splitlines() if p.strip()] if r.returncode == 0 else []
    if staged:
        return ChangeSet(staged, "", "HEAD", "index")
    # Nothing staged: review the commit itself. A root commit has no parent, so
    # everything in it is new and there is nothing to ratchet against.
    has_parent = _git(repo, "rev-parse", "--verify", "HEAD~1").returncode == 0
    if not has_parent:
        return ChangeSet([], "HEAD", "", "root-commit")
    c = _git(repo, "diff", "--name-only", f"--diff-filter={filt}", "HEAD~1", "HEAD")
    paths = [p for p in c.stdout.splitlines() if p.strip()] if c.returncode == 0 else []
    return ChangeSet(paths, "HEAD", "HEAD~1", "commit")


def staged_paths(repo: Path) -> list[str]:
    return changeset(repo).paths


def counts(text: str) -> tuple[int, int]:
    t = ast.parse(text)
    tests = sum(1 for n in ast.walk(t)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))
    return tests, sum(1 for n in ast.walk(t) if isinstance(n, ast.Assert))


def test_names(text: str) -> set[str]:
    t = ast.parse(text)
    return {n.name for n in ast.walk(t)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")}


def _patch_sources(text: str) -> dict[str, str]:
    """Extract candidate test source carried inside a config's test_patch."""
    try:
        patch = json.loads(text).get("test_patch") or ""
    except (ValueError, TypeError) as error:
        raise SyntaxError(f"config JSON does not parse: {error}") from error
    sources: dict[str, list[str]] = {}
    current = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            if current == "/dev/null":
                current = ""
            else:
                sources.setdefault(current, [])
        elif current and line.startswith("+") and not line.startswith("+++"):
            sources[current].append(line[1:])
        elif current and line.startswith(" "):
            sources[current].append(line[1:])
    return {path: "\n".join(lines) + "\n" for path, lines in sources.items()
            if re.search(r"(?:_test\.go$|Tests?\.swift$|(^|/)tests?/.*\.py$)", path)}


def _sources(path: str, text: str) -> dict[str, str]:
    if PATCH_CONFIG.search(path):
        return _patch_sources(text)
    return {path: text} if GRADED.search(path) else {}


def _metrics(path: str, text: str) -> tuple[set[str], int]:
    suffix = Path(path).suffix
    if suffix == ".py":
        return test_names(text), counts(text)[1]
    if suffix == ".go":
        try:
            parsed = subprocess.run(["gofmt"], input=text, capture_output=True, text=True,
                                    timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SyntaxError(f"Go parser unavailable: {error}") from error
        if parsed.returncode:
            raise SyntaxError("Go source does not parse: " + parsed.stderr.strip()[:120])
        return set(GO_TEST.findall(text)), len(GO_ASSERT.findall(text))
    if suffix == ".swift":
        if text.count("{") != text.count("}"):
            raise SyntaxError("unbalanced Swift braces in extracted patch source")
        return set(SWIFT_TEST.findall(text)), len(SWIFT_ASSERT.findall(text))
    raise SyntaxError(f"unsupported graded syntax: {path}")


def _graded(path: str) -> bool:
    return bool(GRADED.search(path) or PATCH_CONFIG.search(path))


def _records(text: str | None) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        reason = str(rec.get("reason") or "").strip()
        if not reason:
            continue  # a record with no reason proves nothing
        for field in ("test", "item"):
            name = rec.get(field)
            if isinstance(name, str) and name:
                out[(field, name)] = reason
    return out


def fresh_proofs(repo: Path, task: str, cs: "ChangeSet | None" = None) -> dict:
    """Removal justifications added or changed by THIS change."""
    cs = cs or changeset(repo)
    out: dict[str, str] = {}
    for cand in ([f"{task}/{REMOVAL_LEDGERS[0]}"] if task else []) + list(REMOVAL_LEDGERS):
        old, new = _records(blob(repo, cs.old, cand)), _records(blob(repo, cs.new, cand))
        for (field, name), reason in new.items():
            if old.get((field, name)) != reason:
                out[name] = reason
    return out


def check_ratchet(repo: Path, paths: list[str], cs: ChangeSet | None = None) -> tuple[list[Finding], int, list[Finding]]:
    """Graded coverage may not shrink without a recorded reason."""
    cs = cs or changeset(repo)
    findings: list[Finding] = []
    unparsed: list[Finding] = []
    examined = 0
    for path in paths:
        if not _graded(path):
            continue
        new, old = blob(repo, cs.new, path), blob(repo, cs.old, path)
        if old is None:
            if new is None:
                continue
            try:
                new_sources = _sources(path, new)
                if not new_sources:
                    raise SyntaxError("test patch contains no supported graded source")
                for source_path, source in new_sources.items():
                    names, assertions = _metrics(source_path, source)
                    if names and assertions == 0:
                        raise SyntaxError(f"{source_path} contains tests but zero examined assertions")
            except SyntaxError as e:
                unparsed.append(Finding(path, f"does not parse: {str(e).splitlines()[0]}"))
                continue
            examined += len(new_sources)
            continue
        if new is None:
            findings.append(Finding(path, "graded test file deleted outright"))
            continue
        try:
            old_sources, new_sources = _sources(path, old), _sources(path, new)
            if not new_sources:
                raise SyntaxError("test patch contains no supported graded source")
            old_metrics = [_metrics(p, source) for p, source in old_sources.items()]
            new_metrics = [_metrics(p, source) for p, source in new_sources.items()]
            old_names = set().union(*(m[0] for m in old_metrics)) if old_metrics else set()
            new_names = set().union(*(m[0] for m in new_metrics)) if new_metrics else set()
            old_a = sum(m[1] for m in old_metrics)
            new_a = sum(m[1] for m in new_metrics)
            if new_names and new_a == 0:
                raise SyntaxError("graded tests contain zero examined assertions")
            gone = old_names - new_names
        except SyntaxError as e:
            unparsed.append(Finding(path, f"does not parse: {str(e).splitlines()[0]}"))
            continue
        examined += len(new_sources)
        proven = fresh_proofs(repo, path.split("/tests/")[0].split("/steps/")[0], cs=cs)
        for name in sorted(gone):
            if name not in proven:
                findings.append(Finding(path, f"test removed with no recorded reason: {name}"))
        if new_a < old_a and not gone:
            findings.append(Finding(path, f"assertion count fell {old_a} -> {new_a} with no test removed"))
    return findings, examined, unparsed


def check_weakening(repo: Path, paths: list[str], cs: ChangeSet | None = None) -> tuple[list[Finding], int, list[Finding]]:
    """Assertions may not get weaker."""
    cs = cs or changeset(repo)
    findings: list[Finding] = []
    unparsed: list[Finding] = []
    examined = 0
    for path in paths:
        if not _graded(path):
            continue
        new = blob(repo, cs.new, path)
        old = blob(repo, cs.old, path)
        try:
            new_sources = _sources(path, new or "")
            if not new_sources:
                raise SyntaxError("test patch contains no supported graded source")
            for source_path, source in new_sources.items():
                names, assertions = _metrics(source_path, source)
                if names and assertions == 0:
                    raise SyntaxError(f"{source_path} contains tests but zero examined assertions")
        except SyntaxError as e:
            unparsed.append(Finding(path, f"does not parse: {str(e).splitlines()[0]}"))
            continue
        examined += len(new_sources)
        old_lines = (old or "").splitlines()
        new_lines = (new or "").splitlines()
        delta = list(difflib.unified_diff(old_lines, new_lines, n=0))
        added = [l[1:] for l in delta if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:] for l in delta if l.startswith("-") and not l.startswith("---")]
        for line in added:
            for pat, label in WEAKEN_TOKENS:
                if pat.search(line):
                    findings.append(Finding(path, label, line.strip()))
            # Only a NEW `or` broadens: rewriting a line that already had one is
            # not a weakening, and flagging it trains people to ignore the check.
            if ASSERT_OR.search(line) and not any(ASSERT_OR.search(o) for o in removed):
                findings.append(Finding(path, "assertion broadened with `or`", line.strip()))
        old_t = [float(m) for l in removed for m in TOLERANCE.findall(l)]
        new_t = [float(m) for l in added for m in TOLERANCE.findall(l)]
        if old_t and new_t and max(new_t) > max(old_t):
            findings.append(Finding(path, f"tolerance widened {max(old_t)} -> {max(new_t)}"))
    return findings, examined, unparsed


def run(repo: Path, paths: list[str] | None = None) -> dict:
    repo = Path(repo)
    cs = changeset(repo)
    paths = cs.paths if paths is None else paths
    out = {}
    for name, fn in (("diff-ratchet", check_ratchet), ("diff-weakening", check_weakening)):
        findings, examined, unparsed = fn(repo, paths, cs)
        # Unparsed files are findings in their own right, never a quiet skip.
        all_f = findings + unparsed
        if all_f:
            state = "FAIL"
        elif examined == 0:
            state = "NOT_RUN"  # nothing examined is not a pass
        else:
            state = "PASS"
        out[name] = {
            "state": state,
            "examined": examined,
            "findings": [f.as_dict() for f in all_f],
            "source": cs.source,
            "detail": ("; ".join(f"{f.path}: {f.what}" for f in all_f[:4]) if all_f
                       else (f"{examined} graded file(s) clean ({cs.source})" if examined
                             else f"no supported graded source in the {cs.source} diff")),
        }
    return out
