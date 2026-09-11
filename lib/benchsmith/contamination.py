"""Overlay contamination and transaction artifacts in the git snapshot.

Reads blobs from git, never from the working tree, so unstaged cleanup cannot
hide contaminated content that is about to be committed. That is the whole point:
a `solve.sh` restored on disk after a mutant run still ships the mutant if the
index holds the contaminated blob.

Ported from the t-bench repo's `check-repository-contamination.sh`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Markers a variant/mutant runner leaves behind in a reference solution.
OVERLAY_MARKERS = ("variant overlay", "mutant overlay", "mutant (", "mutant applied")

# Deliberately OVER-selects: any `.solve.sh.*` and any `.run-*.lock`, not an
# enumeration of known suffixes. An enumerated prefilter drifts the moment a new
# artifact shape appears -- the original's first cut omitted `.solve.sh.restore.*`
# and silently stopped rejecting it.
CANDIDATE = re.compile(r"(^|/)(solve\.sh|\.solve\.sh\..*)$|(^|/)\.run-[^/]*\.lock(/|$)")
ARTIFACT = re.compile(r"(^|/)\.solve\.sh\..*$|(^|/)\.run-[^/]*\.lock(/|$)")
SOLVE = re.compile(r"(^|/)solve\.sh$")

# Intentional negative controls live here and are contaminated on purpose.
EXCLUDED = re.compile(r"^gate-fixtures/")


@dataclass(frozen=True)
class Finding:
    path: str
    what: str

    def as_dict(self) -> dict:
        return {"path": self.path, "what": self.what}


def _git(repo: Path, *a, binary: bool = False):
    return subprocess.run(["git", "-C", str(repo), *a],
                          capture_output=True, text=not binary, timeout=120)


def entries(repo: Path, rev: str = "") -> tuple[list[tuple[str, str, str]], int]:
    """(mode, oid, path) for the index or a tree, plus the total entry count."""
    if rev:
        r = _git(repo, "ls-tree", "-r", "-z", rev)
        parse = lambda rec: (rec.split("\t", 1)[0].split()[0], rec.split("\t", 1)[0].split()[2],
                             rec.split("\t", 1)[1])
    else:
        r = _git(repo, "ls-files", "-s", "-z")
        parse = lambda rec: (rec.split("\t", 1)[0].split()[0], rec.split("\t", 1)[0].split()[1],
                             rec.split("\t", 1)[1])
    if r.returncode != 0:
        return [], 0
    recs = [x for x in r.stdout.split("\0") if x.strip()]
    out = []
    for rec in recs:
        try:
            out.append(parse(rec))
        except (IndexError, ValueError):
            continue
    return out, len(recs)


def check(repo: Path, rev: str = "") -> dict:
    """Symlinked or overlay-marked solve.sh, and tracked transaction artifacts."""
    repo = Path(repo)
    all_entries, total = entries(repo, rev)
    if not all_entries:
        return {"state": "NOT_RUN", "examined": 0, "findings": [],
                "detail": "no git snapshot to read"}

    # Only paths that could possibly be a finding are inspected. The original
    # ran two case statements per entry over 2604 entries and took ~9.5s per
    # commit; forking git was only 1.4s of that, so batching alone would not
    # have fixed it. Coverage is unchanged -- anything else cannot be a finding.
    candidates = [(m, oid, p) for (m, oid, p) in all_entries
                  if CANDIDATE.search(p) and not EXCLUDED.match(p)]

    findings: list[Finding] = []
    for mode, oid, path in candidates:
        if set(oid) == {"0"}:
            continue  # intent-to-add: no blob in the proposed snapshot
        if ARTIFACT.search(path):
            findings.append(Finding(path, "transaction artifact is tracked"))
            continue
        if not SOLVE.search(path):
            continue
        if mode == "120000":
            # A symlinked solve.sh points at whatever the last run left behind.
            findings.append(Finding(path, "solve.sh is a symlink"))
            continue
        r = _git(repo, "cat-file", "blob", oid)
        if r.returncode != 0:
            findings.append(Finding(path, "blob unreadable; cannot clear it"))
            continue
        for marker in OVERLAY_MARKERS:
            if marker in r.stdout:
                findings.append(Finding(path, f"overlay marker {marker!r}"))
                break

    return {
        "state": "FAIL" if findings else ("PASS" if candidates else "NOT_RUN"),
        "examined": len(candidates),
        "total": total,
        "findings": [f.as_dict() for f in findings],
        "detail": ("; ".join(f"{f.path}: {f.what}" for f in findings[:4]) if findings
                   else (f"{len(candidates)} candidate(s) clean of {total} entries" if candidates
                         else f"no solve.sh or transaction artifact among {total} entries")),
    }
