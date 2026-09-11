"""Does this measurement describe the commit we pushed?

The platform validates the branch tip, not your SHA. With several loops pushing
to one repository your commit is buried before the import sweep runs, so
demanding an exact SHA match does not merely lose measurements -- it deadlocks:
the loop waits forever for a verdict that will never be addressed to it.

The fix is not to relax the question but to answer it properly. A later commit's
measurement describes our commit when ours is an ancestor of it AND the graded
and agent-visible surfaces are unchanged between the two. Then the run measured
the same task, whatever else moved in the repository.

Surfaces, not whole directories: hashing exactly what is graded and exactly what
the model can see is a tighter equivalence than byte-comparing a tree that also
holds READMEs, journals and scratch files. A README edit between two commits does
not change what was measured, and must not discard a good measurement.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .journal import surface_hashes

EXACT = "exact"
ANCESTOR_IDENTICAL = "ancestor-identical"
DIVERGENT = "divergent"
STALE = "stale"
UNKNOWN = "unknown"

# Only these two mean "this measurement is about our commit". UNKNOWN is not on
# the list and never joins it: an attribution we could not compute is an open
# question, and a loop that scores open questions as coverage is the fail-open
# behaviour every other gate here exists to prevent.
COVERING = frozenset({EXACT, ANCESTOR_IDENTICAL})


@dataclass(frozen=True)
class Attribution:
    job_sha: str
    verdict: str
    reason: str

    @property
    def covers(self) -> bool:
        return self.verdict in COVERING

    def as_dict(self) -> dict:
        return {"jobSha": self.job_sha, "verdict": self.verdict,
                "reason": self.reason, "covers": self.covers}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120)


def is_ancestor(repo: Path, ancestor: str, descendant: str, *, git=_git) -> bool | None:
    """True/False, or None when git cannot answer -- which is not False."""
    r = git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None  # unknown object, corrupt repo, git missing


def surfaces_at(repo: Path, sha: str, task: str, *, git=_git) -> dict | None:
    """Hash the graded and visible surfaces of `task` as of `sha`.

    Materialises the tree rather than re-deriving hashes from git objects, so
    this shares one implementation with `benchsmith hash`. Two hashers would
    drift, and the drift would silently change which measurements count.
    """
    r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha, "--", task],
                       capture_output=True, timeout=180)
    if r.returncode != 0 or not r.stdout:
        return None
    with tempfile.TemporaryDirectory() as td:
        try:
            with tarfile.open(fileobj=BytesIO(r.stdout)) as tf:
                tf.extractall(td, filter="data")
        except (tarfile.TarError, OSError, TypeError):
            return None
        root = Path(td) / task
        if not root.is_dir():
            return None
        return surface_hashes(root)


def attribute(repo: Path, task: str, ours: str, theirs: str, *, git=_git,
              surfaces=surfaces_at) -> Attribution:
    """Decide whether a measurement taken at `theirs` describes `ours`."""
    if not ours or not theirs:
        return Attribution(theirs or "", UNKNOWN, "missing commit sha on our side or the job's")
    if ours == theirs:
        return Attribution(theirs, EXACT, "job ran on our commit")

    anc = is_ancestor(Path(repo), ours, theirs, git=git)
    if anc is None:
        return Attribution(theirs, UNKNOWN, "git could not decide ancestry; treat as uncovered")
    if not anc:
        # Either the job predates our push or it is on a fork. Both are stale
        # with respect to us, and neither is evidence about our commit.
        return Attribution(theirs, STALE, f"our commit is not an ancestor of {theirs[:8]}")

    a, b = surfaces(Path(repo), ours, task), surfaces(Path(repo), theirs, task)
    if a is None or b is None:
        return Attribution(theirs, UNKNOWN, "could not read the task surfaces at both commits")
    if a["gradedHash"] != b["gradedHash"]:
        return Attribution(theirs, DIVERGENT, "graded surface changed between the two commits")
    if a["visibleHash"] != b["visibleHash"]:
        return Attribution(theirs, DIVERGENT, "agent-visible surface changed between the two commits")
    return Attribution(theirs, ANCESTOR_IDENTICAL,
                       f"{theirs[:8]} is a descendant with identical graded and visible surfaces")


def covers_factory(repo: Path, task: str):
    """A predicate `select_jobs` can use without importing git plumbing."""
    def covers(ours: str, theirs: str) -> Attribution:
        return attribute(repo, task, ours, theirs)
    return covers
