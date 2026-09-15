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
  * Nothing examined is **NOT_RUN**, reported and never printed as a pass. When a
    graded path did change but extraction examines zero files, that is a finding;
    only a diff with no graded path is inapplicable.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GRADED = re.compile(r"(^|/)(steps/[^/]+/)?tests?/.*$")
PATCH_CONFIG = re.compile(r"(^|/)(steps/[^/]+/)?tests?/config\.json$")
PATCH_CONTAINER = re.compile(
    r"(^|/)(steps/[^/]+/)?tests?/(?:config\.json|test\.patch)$"
)
GO_TEST = re.compile(r"(?m)^\s*func\s+(Test[A-Za-z0-9_]+)\s*\(")
GO_ASSERT = re.compile(r"\b(?:t|tb)\.(?:Errorf?|Fatalf?|FailNow)\s*\(|\b(?:assert|require)\.[A-Za-z0-9_]+\s*\(")
SWIFT_TEST = re.compile(r"(?m)^\s*func\s+(test[A-Za-z0-9_]+)\s*\(")
SWIFT_ASSERT = re.compile(r"\bXCTAssert[A-Za-z0-9_]*\s*\(")
SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"})
TYPESCRIPT_SUFFIXES = frozenset({".ts", ".mts", ".cts", ".tsx"})
CODE_SUFFIXES = SCRIPT_SUFFIXES | frozenset({
    ".c", ".cc", ".cpp", ".cs", ".ex", ".exs", ".go", ".java", ".kt", ".kts",
    ".lua", ".m", ".mm", ".php", ".pl", ".pm", ".py", ".rb", ".rs", ".scala",
    ".sh", ".swift",
})
PATCH_DATA_SUFFIXES = frozenset({
    ".b64", ".bin", ".css", ".csv", ".diff", ".golden", ".gz", ".html",
    ".json", ".md", ".out", ".patch", ".snap", ".tar", ".tgz", ".toml",
    ".txt", ".xml", ".yaml", ".yml", ".zip",
})
DIRECT_TEST_ASSET_NAMES = frozenset({"dockerfile"})

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


@dataclass(frozen=True)
class Language:
    name: str
    metrics: Callable[[str], tuple[set[str], int]]


@dataclass(frozen=True)
class _ScriptToken:
    kind: str
    value: str
    line: int


_NODE_SCRIPT_PARSE = r"""
const fs = require("node:fs");
const moduleApi = require("node:module");
const path = require("node:path");
const vm = require("node:vm");

function loadTypeScript() {
  const roots = new Set(moduleApi.globalPaths || []);
  roots.add(path.resolve(path.dirname(process.execPath), "../lib/node_modules"));
  roots.add("/usr/local/lib/node_modules");
  roots.add("/usr/lib/node_modules");
  const candidates = [];
  for (const root of roots) {
    candidates.push(path.join(root, "typescript"));
    try {
      for (const entry of fs.readdirSync(root, {withFileTypes: true})) {
        if (entry.isDirectory() && !entry.name.startsWith(".")) {
          candidates.push(path.join(root, entry.name, "node_modules", "typescript"));
        }
      }
    } catch (_) {}
  }
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (_) {}
  }
  return null;
}

try {
  let source = fs.readFileSync(0, "utf8");
  const suffix = process.argv[1];
  const typescript = [".ts", ".mts", ".cts", ".tsx"].includes(suffix);
  const jsx = [".jsx", ".tsx"].includes(suffix);
  if (jsx || (typescript && typeof moduleApi.stripTypeScriptTypes !== "function")) {
    const ts = loadTypeScript();
    if (!ts) {
      throw new Error("TypeScript compiler parser is unavailable");
    }
    const kinds = {
      ".js": ts.ScriptKind.JS,
      ".jsx": ts.ScriptKind.JSX,
      ".ts": ts.ScriptKind.TS,
      ".mts": ts.ScriptKind.TS,
      ".cts": ts.ScriptKind.TS,
      ".tsx": ts.ScriptKind.TSX,
    };
    const tree = ts.createSourceFile(`input${suffix}`, source, ts.ScriptTarget.Latest, true, kinds[suffix]);
    const diagnostic = (tree.parseDiagnostics || [])[0];
    if (diagnostic) {
      throw new SyntaxError(ts.flattenDiagnosticMessageText(diagnostic.messageText, " "));
    }
  } else {
    if (typescript) {
      source = moduleApi.stripTypeScriptTypes(source, {
        mode: "transform",
        sourceMap: false,
      });
    }
    if (typeof vm.SourceTextModule !== "function") {
      throw new Error("Node JavaScript module parser is unavailable");
    }
    new vm.SourceTextModule(source);
  }
} catch (error) {
  process.stderr.write(`${error.name}: ${error.message}\n`);
  process.exit(1);
}
"""


@lru_cache(maxsize=128)
def _validate_script(text: str, suffix: str) -> None:
    language = "TypeScript" if suffix in TYPESCRIPT_SUFFIXES else "JavaScript"
    try:
        parsed = subprocess.run(
            [
                "node",
                "--experimental-vm-modules",
                "--no-warnings",
                "-e",
                _NODE_SCRIPT_PARSE,
                suffix,
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyntaxError(f"{language} parser unavailable: {error}") from error
    if parsed.returncode:
        detail = (parsed.stderr or parsed.stdout).strip().splitlines()
        raise SyntaxError(
            f"{language} source does not parse: "
            f"{detail[0][:160] if detail else 'unknown parser error'}"
        )


def _regex_can_start(tokens: list[_ScriptToken]) -> bool:
    if not tokens:
        return True
    previous = tokens[-1]
    if previous.kind in {"identifier", "number", "string", "template", "regex"}:
        return previous.value in {"return", "throw", "case", "delete", "typeof", "void", "yield"}
    return previous.value not in {")", "]", "++", "--"}


def _script_tokens(text: str) -> list[_ScriptToken]:
    tokens: list[_ScriptToken] = []
    line = 1
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            line += char == "\n"
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise SyntaxError("unterminated block comment")
            line += text[i:end + 2].count("\n")
            i = end + 2
            continue
        if char in "'\"`":
            quote = char
            start_line = line
            end = i + 1
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                if quote != "`" and text[end] == "\n":
                    raise SyntaxError("unterminated string literal")
                end += 1
            else:
                raise SyntaxError("unterminated string literal")
            value = text[i:end]
            tokens.append(_ScriptToken("template" if quote == "`" else "string", value, start_line))
            line += value.count("\n")
            i = end
            continue
        if char == "/" and not text.startswith("/=", i) and _regex_can_start(tokens):
            end = i + 1
            escaped = False
            in_class = False
            while end < len(text):
                current = text[end]
                if current == "\n":
                    break
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == "[":
                    in_class = True
                elif current == "]":
                    in_class = False
                elif current == "/" and not in_class:
                    end += 1
                    while end < len(text) and text[end].isalpha():
                        end += 1
                    tokens.append(_ScriptToken("regex", text[i:end], line))
                    i = end
                    break
                end += 1
            if i == end:
                continue
        if char.isalpha() or char in "_$":
            end = i + 1
            while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
                end += 1
            tokens.append(_ScriptToken("identifier", text[i:end], line))
            i = end
            continue
        if char.isdigit():
            end = i + 1
            while end < len(text) and (text[end].isalnum() or text[end] in "._"):
                end += 1
            tokens.append(_ScriptToken("number", text[i:end], line))
            i = end
            continue
        operator = next(
            (
                candidate
                for candidate in (
                    "===", "!==", "=>", "||", "&&", "?.", "==", "!=", "<=", ">=",
                    "++", "--", "/=",
                )
                if text.startswith(candidate, i)
            ),
            char,
        )
        tokens.append(_ScriptToken("punctuation", operator, line))
        i += len(operator)
    return tokens


def _delimiter_pairs(tokens: list[_ScriptToken]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if token.value in {"(", "[", "{"}:
            stack.append((token.value, index))
        elif token.value in closing and stack and stack[-1][0] == closing[token.value]:
            _, opening = stack.pop()
            pairs[opening] = index
    return pairs


def _member_call(
    tokens: list[_ScriptToken], index: int, pairs: dict[int, int]
) -> tuple[list[str], int] | None:
    token = tokens[index]
    if token.kind != "identifier" or (index and tokens[index - 1].value in {".", "?."}):
        return None
    parts = [token.value]
    cursor = index + 1
    while cursor + 1 < len(tokens) and tokens[cursor].value in {".", "?."}:
        if tokens[cursor + 1].kind != "identifier":
            return None
        parts.append(tokens[cursor + 1].value)
        cursor += 2
    if {"each", "skipIf", "runIf"} & set(parts):
        if cursor < len(tokens) and tokens[cursor].value == "(":
            close = pairs.get(cursor)
            if close is None:
                return None
            cursor = close + 1
        elif cursor < len(tokens) and tokens[cursor].kind == "template":
            cursor += 1
        while cursor + 1 < len(tokens) and tokens[cursor].value in {".", "?."}:
            if tokens[cursor + 1].kind != "identifier":
                return None
            parts.append(tokens[cursor + 1].value)
            cursor += 2
    if cursor < len(tokens) and tokens[cursor].value == "(":
        return parts, cursor
    return None


def _is_test_call(parts: list[str]) -> bool:
    if parts[0] in {"xtest", "xit"}:
        return True
    if parts[0] in {"test", "it", "specify"}:
        suite_methods = {
            "describe", "beforeAll", "beforeEach", "afterAll", "afterEach", "use", "step",
            "extend",
        }
        return not (suite_methods & set(parts[1:]))
    return parts[0] == "Deno" and parts[-1] == "test"


def _is_suite_call(parts: list[str]) -> bool:
    return parts[0] in {"describe", "xdescribe"} or (
        parts[0] == "test" and "describe" in parts[1:]
    )


def _literal_value(token: _ScriptToken) -> str:
    return token.value[1:-1] if token.kind in {"string", "template"} else ""


def _object_property(
    tokens: list[_ScriptToken], opening: int, pairs: dict[int, int], names: set[str]
) -> tuple[str, _ScriptToken] | None:
    closing = pairs.get(opening)
    if closing is None:
        return None
    cursor = opening + 1
    while cursor < closing:
        token = tokens[cursor]
        if token.value in {"(", "[", "{"}:
            nested = pairs.get(cursor)
            if nested is None:
                return None
            cursor = nested + 1
            continue
        key = _literal_value(token) or token.value
        if key in names:
            if (
                cursor + 2 < closing
                and tokens[cursor + 1].value == ":"
            ):
                return key, tokens[cursor + 2]
            if cursor + 1 == closing or tokens[cursor + 1].value == ",":
                return key, token
        cursor += 1
    return None


def _test_name(
    tokens: list[_ScriptToken], opening: int, pairs: dict[int, int], fallback_line: int
) -> str:
    first = tokens[opening + 1] if opening + 1 < len(tokens) else None
    if first is not None:
        literal = _literal_value(first)
        if literal:
            return literal
        if first.value == "{":
            named = _object_property(tokens, opening + 1, pairs, {"name"})
            if named is not None:
                literal = _literal_value(named[1])
                if literal:
                    return literal
    return f"test@line-{fallback_line}"


def _option_objects(
    tokens: list[_ScriptToken], opening: int, pairs: dict[int, int]
) -> list[tuple[int, int]]:
    closing = pairs.get(opening)
    if closing is None:
        return []
    objects: list[tuple[int, int]] = []
    cursor = opening + 1
    at_argument_start = True
    while cursor < closing:
        token = tokens[cursor]
        if token.value == ",":
            at_argument_start = True
            cursor += 1
            continue
        if token.value in {"(", "[", "{"}:
            nested = pairs.get(cursor)
            if nested is None:
                return objects
            if token.value == "{" and at_argument_start:
                objects.append((cursor, nested))
            cursor = nested + 1
            at_argument_start = False
            continue
        at_argument_start = False
        cursor += 1
    return objects


def _script_calls(
    tokens: list[_ScriptToken], pairs: dict[int, int]
) -> list[tuple[int, list[str], int, int]]:
    calls = []
    for index in range(len(tokens)):
        call = _member_call(tokens, index, pairs)
        if call is None:
            continue
        parts, opening = call
        closing = pairs.get(opening)
        if closing is not None:
            calls.append((index, parts, opening, closing))
    return calls


def _qualified_test_name(
    leaf: str, opening: int, suites: list[tuple[int, int, str]]
) -> str:
    parents = [
        (suite_open, name)
        for suite_open, suite_close, name in suites
        if suite_open < opening < suite_close
    ]
    return " > ".join([name for _, name in sorted(parents)] + [leaf])


def _test_records(
    tokens: list[_ScriptToken],
    pairs: dict[int, int],
    calls: list[tuple[int, list[str], int, int]],
) -> list[tuple[int, list[str], int, int, str, str, str]]:
    suites = [
        (
            opening,
            closing,
            _test_name(tokens, opening, pairs, tokens[index].line),
        )
        for index, parts, opening, closing in calls
        if _is_suite_call(parts)
    ]
    raw = [
        (
            index,
            parts,
            opening,
            closing,
            _qualified_test_name(
                _test_name(tokens, opening, pairs, tokens[index].line),
                opening,
                suites,
            ),
        )
        for index, parts, opening, closing in calls
        if _is_test_call(parts)
    ]
    totals = Counter(record[4] for record in raw)
    seen: Counter[str] = Counter()
    records = []
    for index, parts, opening, closing, name in raw:
        seen[name] += 1
        metric_name = name if totals[name] == 1 else f"{name}#{seen[name]}"
        event_identity = name
        if totals[name] > 1:
            assertion_tokens = []
            for call_index, call_parts, call_opening, call_closing in calls:
                if not (opening < call_opening < closing):
                    continue
                if call_parts[0] not in {"expect", "assert", "assertThat"}:
                    continue
                assertion_tokens.extend(
                    token.value for token in tokens[call_index:call_closing + 1]
                )
            implementation = " ".join(assertion_tokens)
            fingerprint = hashlib.sha256(implementation.encode()).hexdigest()[:12]
            event_identity = f"{name}@{fingerprint}"
        records.append(
            (index, parts, opening, closing, name, metric_name, event_identity)
        )
    return records


def _script_metrics(text: str, suffix: str) -> tuple[set[str], int]:
    _validate_script(text, suffix)
    tokens = _script_tokens(text)
    pairs = _delimiter_pairs(tokens)
    calls = _script_calls(tokens, pairs)
    tests = {record[5] for record in _test_records(tokens, pairs, calls)}
    assertions = 0
    for _, parts, _, _ in calls:
        if parts[0] == "expect" and not (
            {"assertions", "hasAssertions", "extend"} & set(parts[1:])
        ):
            assertions += 1
        elif parts[0] in {"assert", "assertThat"}:
            assertions += 1
    return tests, assertions


def _python_metrics(text: str) -> tuple[set[str], int]:
    return test_names(text), counts(text)[1]


def _go_metrics(text: str) -> tuple[set[str], int]:
    try:
        parsed = subprocess.run(
            ["gofmt"], input=text, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyntaxError(f"Go parser unavailable: {error}") from error
    if parsed.returncode:
        raise SyntaxError("Go source does not parse: " + parsed.stderr.strip()[:120])
    return set(GO_TEST.findall(text)), len(GO_ASSERT.findall(text))


def _swift_metrics(text: str) -> tuple[set[str], int]:
    if text.count("{") != text.count("}"):
        raise SyntaxError("unbalanced Swift braces in extracted patch source")
    return set(SWIFT_TEST.findall(text)), len(SWIFT_ASSERT.findall(text))


def _shell_metrics(text: str) -> tuple[set[str], int]:
    try:
        parsed = subprocess.run(
            ["bash", "--noprofile", "--norc", "-n"],
            input=text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyntaxError(f"Shell parser unavailable: {error}") from error
    if parsed.returncode:
        detail = (parsed.stderr or parsed.stdout).strip().splitlines()
        raise SyntaxError(
            "Shell source does not parse: "
            + (detail[0][:160] if detail else "unknown parser error")
        )
    executable = any(
        line.strip() and not line.lstrip().startswith("#")
        for line in text.splitlines()
    )
    return ({"<shell-script>"} if executable else set(), int(executable))


LANGUAGES = {
    ".py": Language("Python", _python_metrics),
    ".go": Language("Go", _go_metrics),
    ".sh": Language("Shell", _shell_metrics),
    ".swift": Language("Swift", _swift_metrics),
    **{
        suffix: Language(
            "TypeScript" if suffix in TYPESCRIPT_SUFFIXES else "JavaScript",
            lambda text, suffix=suffix: _script_metrics(text, suffix),
        )
        for suffix in SCRIPT_SUFFIXES
    },
}


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


def _test_named_source(path: str) -> bool:
    stem = Path(path).stem.lower()
    return stem.startswith("test") or stem.endswith(
        ("_test", "-test", ".test", "tests", "spec")
    )


def _looks_like_test_source(path: str) -> bool:
    candidate = Path(path)
    if (
        candidate.name.lower() in DIRECT_TEST_ASSET_NAMES
        or candidate.suffix.lower() in PATCH_DATA_SUFFIXES
    ):
        return False
    return bool(
        GRADED.search(path)
        or "__tests__" in candidate.parts
        or _test_named_source(path)
    )


def _looks_like_patch_test_source(path: str) -> bool:
    candidate = Path(path)
    suffix = candidate.suffix.lower()
    if suffix in PATCH_DATA_SUFFIXES:
        return False
    in_test_tree = any(part in {"test", "tests", "__tests__"} for part in candidate.parts)
    return _test_named_source(path) or (suffix in CODE_SUFFIXES and in_test_tree)


def _unified_patch_sources(patch: str) -> dict[str, str]:
    sources: dict[str, list[str]] = {}
    current = ""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = ""
        elif line.startswith("+++ "):
            current = line[4:].split("\t", 1)[0].removeprefix("b/")
            if current == "/dev/null":
                current = ""
            else:
                sources.setdefault(current, [])
        elif current and line.startswith("+") and not line.startswith("+++"):
            sources[current].append(line[1:])
        elif current and line.startswith(" "):
            sources[current].append(line[1:])
    return {
        path: "\n".join(lines) + "\n"
        for path, lines in sources.items()
        if _looks_like_patch_test_source(path)
    }


def _patch_sources(path: str, text: str) -> dict[str, str]:
    """Extract candidate graded sources from either supported patch container."""
    patch = text
    if PATCH_CONFIG.search(path):
        try:
            document = json.loads(text)
        except (ValueError, TypeError) as error:
            raise SyntaxError(f"config JSON does not parse: {error}") from error
        if not isinstance(document, dict):
            raise SyntaxError("config JSON is not an object")
        patch = document.get("test_patch") or ""
        if not isinstance(patch, str):
            raise SyntaxError("config test_patch is not a string")
    return _unified_patch_sources(patch)


def _sources(path: str, text: str) -> dict[str, str]:
    if PATCH_CONTAINER.search(path):
        return _patch_sources(path, text)
    return {path: text} if GRADED.search(path) else {}


def _metrics(path: str, text: str) -> tuple[set[str], int]:
    language = LANGUAGES.get(Path(path).suffix)
    if language is None:
        raise SyntaxError(f"unsupported graded syntax: {path}")
    try:
        return language.metrics(text)
    except SyntaxError as error:
        raise SyntaxError(f"{language.name} parser rejected {path}: {error}") from error


def _graded(path: str) -> bool:
    return bool(PATCH_CONTAINER.search(path) or _looks_like_test_source(path))


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


def _source_label(container: str, source: str) -> str:
    return container if container == source else f"{container} ({source})"


def _script_weakening_events(
    text: str, *, implementation_identity: bool = False
) -> list[tuple[str, str, str, str]]:
    tokens = _script_tokens(text)
    pairs = _delimiter_pairs(tokens)
    lines = text.splitlines()
    calls = _script_calls(tokens, pairs)
    test_records = _test_records(tokens, pairs, calls)
    tests = [
        (
            opening,
            closing,
            event_identity if implementation_identity else metric_identity,
        )
        for _, _, opening, closing, _, metric_identity, event_identity in test_records
    ]
    test_identities = {
        opening: event_identity if implementation_identity else metric_identity
        for _, _, opening, _, _, metric_identity, event_identity in test_records
    }
    test_groups = {
        opening: name
        for _, _, opening, _, name, _, _ in test_records
    }
    suites = [
        (
            opening,
            closing,
            _test_name(tokens, opening, pairs, tokens[index].line),
        )
        for index, parts, opening, closing in calls
        if _is_suite_call(parts)
    ]
    assertions = [
        (opening, closing)
        for _, parts, opening, closing in calls
        if parts[0] in {"expect", "assert", "assertThat"}
    ]
    events: list[tuple[str, str, str, str]] = []

    def enclosing_test(position: int) -> str:
        containing = [
            (closing - opening, name)
            for opening, closing, name in tests
            if opening < position < closing
        ]
        return min(containing)[1] if containing else "<module>"

    def assertion_identity(position: int) -> str:
        test_name = enclosing_test(position)
        within_test = [
            opening
            for opening, closing in assertions
            if opening < position < closing and enclosing_test(opening) == test_name
        ]
        try:
            ordinal = sorted(within_test).index(
                next(
                    opening
                    for opening, closing in assertions
                    if opening < position < closing
                )
            )
        except (StopIteration, ValueError):
            ordinal = 0
        return f"{test_name}#assertion-{ordinal}"

    def add(label: str, token: _ScriptToken, group: str, identity: str) -> None:
        line = lines[token.line - 1].strip() if token.line <= len(lines) else ""
        events.append((label, group, identity, line))

    for index, token in enumerate(tokens):
        if token.value == "||" and index + 1 < len(tokens) and tokens[index + 1].value == "true":
            identity = assertion_identity(index)
            add("|| true added", token, identity, identity)
    for index, parts, opening, closing in calls:
        is_test = _is_test_call(parts)
        is_suite = parts[0] in {"describe", "xdescribe"} or (
            parts[0] == "test" and "describe" in parts[1:]
        )
        if is_test or is_suite:
            leaf = _test_name(tokens, opening, pairs, tokens[index].line)
            qualified = _qualified_test_name(leaf, opening, suites)
            identity = test_identities[opening] if is_test else qualified
            group = test_groups[opening] if is_test else f"suite:{qualified}"
            if parts[0] in {"xtest", "xit", "xdescribe"}:
                add("disabled test added", tokens[index], group, identity)
            disabled = next(
                (
                    part
                    for part in parts[1:]
                    if part in {"skip", "skipIf", "todo", "only", "fixme"}
                ),
                "",
            )
            if disabled:
                add(f"{disabled} test added", tokens[index], group, identity)
            if is_test:
                for object_open, _ in _option_objects(tokens, opening, pairs):
                    option = _object_property(
                        tokens, object_open, pairs, {"skip", "ignore"}
                    )
                    if option is not None and option[1].value != "false":
                        add(
                            f"{option[0]} option added",
                            option[1],
                            group,
                            identity,
                        )
        if parts[0] not in {"expect", "assert", "assertThat"}:
            continue
        for position in range(opening + 1, closing):
            if tokens[position].value != "||":
                continue
            if position + 1 < closing and tokens[position + 1].value == "true":
                continue
            identity = assertion_identity(position)
            add(
                "assertion broadened with `||`",
                tokens[position],
                identity,
                identity,
            )
    return events


def _script_implementation_ids(text: str) -> set[tuple[str, str]]:
    tokens = _script_tokens(text)
    pairs = _delimiter_pairs(tokens)
    return {
        (record[4], record[6])
        for record in _test_records(tokens, pairs, _script_calls(tokens, pairs))
    }


def _is_disable_event(label: str) -> bool:
    return label == "disabled test added" or label.endswith(
        (" test added", " option added")
    )


def _weakening_findings(
    container: str, source_path: str, old: str, new: str
) -> list[Finding]:
    display_path = _source_label(container, source_path)
    if Path(source_path).suffix == ".sh":
        findings: list[Finding] = []
        old_counts = Counter(
            label
            for line in old.splitlines()
            for pattern, label in WEAKEN_TOKENS
            if pattern.search(line)
        )
        new_lines = [
            (label, line.strip())
            for line in new.splitlines()
            for pattern, label in WEAKEN_TOKENS
            if pattern.search(line)
        ]
        seen: Counter[str] = Counter()
        for label, line in new_lines:
            seen[label] += 1
            if seen[label] > old_counts[label]:
                findings.append(Finding(display_path, label, line))
        return findings
    if Path(source_path).suffix in SCRIPT_SUFFIXES:
        old_events = _script_weakening_events(old)
        new_events = _script_weakening_events(new)
        findings: list[Finding] = []
        emitted: set[tuple[str, str]] = set()

        def emit(label: str, line: str) -> None:
            key = (label, line)
            if key not in emitted:
                findings.append(Finding(display_path, label, line))
                emitted.add(key)

        prior = Counter(
            (label, identity)
            for label, _, identity, _ in old_events
            if not _is_disable_event(label)
        )
        for label, _, identity, line in new_events:
            if _is_disable_event(label):
                continue
            key = (label, identity)
            if prior[key]:
                prior[key] -= 1
            else:
                emit(label, line)

        prior_disabled = Counter(
            (label, group)
            for label, group, _, _ in old_events
            if _is_disable_event(label)
        )
        for label, group, _, line in new_events:
            if not _is_disable_event(label):
                continue
            key = (label, group)
            if prior_disabled[key]:
                prior_disabled[key] -= 1
            else:
                emit(label, line)

        common_implementations = (
            _script_implementation_ids(old) & _script_implementation_ids(new)
        )
        old_implementation_events = _script_weakening_events(
            old, implementation_identity=True
        )
        new_implementation_events = _script_weakening_events(
            new, implementation_identity=True
        )
        prior_by_implementation = Counter(
            (label, group, identity)
            for label, group, identity, _ in old_implementation_events
            if _is_disable_event(label)
            and (group, identity) in common_implementations
        )
        for label, group, identity, line in new_implementation_events:
            if (
                not _is_disable_event(label)
                or (group, identity) not in common_implementations
            ):
                continue
            key = (label, group, identity)
            if prior_by_implementation[key]:
                prior_by_implementation[key] -= 1
            else:
                emit(label, line)
        return findings

    delta = list(difflib.unified_diff(old.splitlines(), new.splitlines(), n=0))
    added = [line[1:] for line in delta if line.startswith("+") and not line.startswith("+++")]
    removed = [line[1:] for line in delta if line.startswith("-") and not line.startswith("---")]
    findings = []
    for line in added:
        for pattern, label in WEAKEN_TOKENS:
            if pattern.search(line):
                findings.append(Finding(display_path, label, line.strip()))
        if ASSERT_OR.search(line) and not any(ASSERT_OR.search(old_line) for old_line in removed):
            findings.append(Finding(display_path, "assertion broadened with `or`", line.strip()))
    old_tolerances = [float(match) for line in removed for match in TOLERANCE.findall(line)]
    new_tolerances = [float(match) for line in added for match in TOLERANCE.findall(line)]
    if old_tolerances and new_tolerances and max(new_tolerances) > max(old_tolerances):
        findings.append(
            Finding(
                display_path,
                f"tolerance widened {max(old_tolerances)} -> {max(new_tolerances)}",
            )
        )
    return findings


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
            old_sources = _sources(path, old or "") if old is not None else {}
            for source_path, source in new_sources.items():
                names, assertions = _metrics(source_path, source)
                if names and assertions == 0:
                    raise SyntaxError(f"{source_path} contains tests but zero examined assertions")
                findings.extend(
                    _weakening_findings(
                        path,
                        source_path,
                        old_sources.get(source_path, ""),
                        source,
                    )
                )
        except SyntaxError as e:
            unparsed.append(Finding(path, f"does not parse: {str(e).splitlines()[0]}"))
            continue
        examined += len(new_sources)
    return findings, examined, unparsed


def run(repo: Path, paths: list[str] | None = None) -> dict:
    repo = Path(repo)
    cs = changeset(repo)
    paths = cs.paths if paths is None else paths
    applicable = any(_graded(path) for path in paths)
    out = {}
    for name, fn in (("diff-ratchet", check_ratchet), ("diff-weakening", check_weakening)):
        findings, examined, unparsed = fn(repo, paths, cs)
        # Unparsed files are findings in their own right, never a quiet skip.
        all_f = findings + unparsed
        if applicable and examined == 0 and not all_f:
            all_f.append(Finding("<diff>", "graded change produced zero examined files"))
        if all_f:
            state = "FAIL"
        elif examined == 0:
            state = "NOT_RUN"
        else:
            state = "PASS"
        out[name] = {
            "state": state,
            "examined": examined,
            "applicable": applicable,
            "findings": [f.as_dict() for f in all_f],
            "source": cs.source,
            "detail": (
                "; ".join(f"{f.path}: {f.what}" for f in all_f[:4])
                if all_f
                else (
                    f"{examined} graded file(s) clean ({cs.source})"
                    if examined
                    else f"not applicable: no graded source changed in the {cs.source} diff"
                )
            ),
        }
    return out
