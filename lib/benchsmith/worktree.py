"""One working tree per task, so parallel workers cannot overwrite each other.

Eight workers in one checkout share one index and one working tree. They stage
over each other, commit each other's half-finished edits, and `git status` means
nothing to any of them. Serialising the *push* does not help: the damage happens
long before anything reaches a remote.

A worktree gives each task its own index and files while sharing the object
store, so a commit made in one is immediately pushable from anywhere — which is
what lets the publish lane stay in the coordinator.

Detached HEAD on purpose. A branch per task would be a second thing to name,
reconcile and clean up, and nothing here needs a branch: the publish lane pushes
a commit SHA, not a ref.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Sibling to the checkout, not inside it: a worktree under the repo would be
# picked up by its own `git status`, its own globs, and its own gate.
DIRNAME = ".benchsmith-worktrees"

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorktreeRefused(Exception):
    """Not safe to create or remove. The message is the reason."""


@dataclass(frozen=True)
class Worktree:
    task: str
    path: str
    base: str
    created: bool

    def as_dict(self) -> dict:
        return {"task": self.task, "path": self.path, "base": self.base, "created": self.created}


def _git(repo: Path, *args, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def root(repo: Path) -> Path:
    """Where worktrees for this checkout live."""
    repo = Path(repo).resolve()
    return repo.parent / f"{DIRNAME}"


def path_for(repo: Path, task: str) -> Path:
    if not SAFE.match(task or ""):
        raise WorktreeRefused(f"unsafe task name for a directory: {task!r}")
    return root(repo) / f"{Path(repo).resolve().name}--{task}"


def existing(repo: Path) -> dict:
    """Worktrees git already knows about, path -> commit."""
    r = _git(Path(repo), "worktree", "list", "--porcelain")
    out, cur = {}, None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            cur = line.split(" ", 1)[1]
        elif line.startswith("HEAD ") and cur:
            out[cur] = line.split(" ", 1)[1]
    return out


def ensure(repo: Path, task: str, *, base: str = "HEAD") -> Worktree:
    """A worktree for this task, created if absent, reused if present.

    Reused rather than recreated: a worker that was interrupted has work in
    there, and silently discarding it to get a clean tree would lose exactly the
    thing worth keeping.
    """
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise WorktreeRefused(f"{repo} is not a git checkout")
    wt = path_for(repo, task)
    if wt.is_dir() and str(wt) in existing(repo):
        return Worktree(task, str(wt), base, created=False)
    if wt.exists():
        # A directory git does not know about. Removing it would be guessing at
        # what is in it.
        raise WorktreeRefused(
            f"{wt} exists but is not a registered worktree. Inspect it, then "
            f"`git worktree prune` or remove it by hand."
        )
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(repo, "worktree", "add", "--detach", str(wt), base)
    if r.returncode != 0:
        raise WorktreeRefused(f"could not create a worktree at {wt}: {r.stderr.strip()[:200]}")
    return Worktree(task, str(wt), base, created=True)


def release(repo: Path, task: str, *, force: bool = False, check_lease: bool = True) -> dict:
    """Remove a task's worktree once its work is published or abandoned.

    Refuses while the tree is dirty unless forced: uncommitted work in there is
    somebody's round, and the whole point of isolating it was not to lose it.
    """
    repo = Path(repo).resolve()
    wt = path_for(repo, task)
    if not wt.is_dir():
        return {"task": task, "removed": False, "reason": "no worktree"}
    if check_lease and not force:
        # A worktree whose task is leased to a live worker is in use. Removing
        # it stops that worker mid-round -- which is exactly what happened.
        try:
            from .remote_lease import RemoteLease

            lease = RemoteLease(task, repo)
            sha = lease.remote_sha()
            if sha:
                who = lease.owner(sha)
                if who.session or not who.holder_is_gone:
                    return {"task": task, "removed": False, "owner": who.as_dict(),
                            "reason": f"leased to worker {who.session or who.pid}; releasing this "
                                      "worktree would stop it mid-round"}
        except Exception:  # noqa: BLE001 - an unreadable lease is not a licence
            return {"task": task, "removed": False,
                    "reason": "could not confirm the task is unleased; refusing to remove it"}
    dirty = _git(wt, "status", "--porcelain").stdout.strip()
    if dirty and not force:
        return {"task": task, "removed": False,
                "reason": f"{len(dirty.splitlines())} uncommitted change(s); "
                          "commit or pass force to discard them"}
    r = _git(repo, "worktree", "remove", *(["--force"] if force else []), str(wt))
    if r.returncode != 0:
        return {"task": task, "removed": False, "reason": r.stderr.strip()[:200]}
    return {"task": task, "removed": True, "path": str(wt)}


def prune(repo: Path) -> dict:
    """Forget worktrees whose directories are gone."""
    r = _git(Path(repo), "worktree", "prune", "-v")
    return {"ok": r.returncode == 0, "detail": (r.stdout or r.stderr).strip()[:300]}
