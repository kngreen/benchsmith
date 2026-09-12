"""Get a read-only checkout of somebody else's task repository.

A review queue is mostly other people's work: ten assigned reviews here span
three repositories, none of them checked out locally. Skipping a review because
its tree is absent is the wrong answer -- the tree is a `git clone` away, and a
reviewer that cannot read the task cannot review it.

Read-only and shallow on purpose. A reviewer never writes, so there is no reason
to fetch history it will not read, and a full clone of a task repo is minutes
that add nothing.

Kept apart from the author-side checkout search: those are repositories the
person works in, and this is one they are only looking at. Mixing them would put
someone else's repo into the pool a worker might be dispatched to harden in.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Sibling of the author's checkouts, clearly marked. A reviewer's copy is not a
# working tree anyone should commit in.
DIRNAME = ".benchsmith-review-checkouts"

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# SSH form: HTTPS to github.com requires a credential this host does not carry
# for these repos, while the org's SSH cert works.
SSH_HOST = "org-272075201@github.com"


def ssh_url(source_repo: str) -> str:
    """github.com/org/name -> the cert-backed SSH form."""
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+?)(?:\.git)?/?$", (source_repo or "").strip())
    if not m:
        return ""
    return f"{SSH_HOST}:{m.group(1)}/{m.group(2)}.git"


def root(base: Path | None = None) -> Path:
    return (Path(base) if base else Path.home()) / DIRNAME


def path_for(source_repo: str, base: Path | None = None) -> Path | None:
    m = re.search(r"github\.com[/:][^/]+/([^/#?]+?)(?:\.git)?/?$", (source_repo or "").strip())
    if not m or not SAFE.match(m.group(1)):
        return None
    return root(base) / m.group(1)


def ensure(source_repo: str, *, sha: str = "", base: Path | None = None,
           runner=None, timeout: int = 900) -> dict:
    """A checkout of `source_repo`, cloned shallowly if absent.

    An existing checkout is fetched rather than recloned: it may hold another
    reviewer's in-flight read, and re-cloning would be minutes for nothing.
    """
    dest = path_for(source_repo, base)
    if dest is None:
        return {"ok": False, "reason": f"cannot derive a directory from {source_repo!r}"}
    url = ssh_url(source_repo)
    if not url:
        return {"ok": False, "reason": f"not a github URL: {source_repo!r}"}

    run = runner or (lambda argv, cwd=None: subprocess.run(
        argv, cwd=cwd and str(cwd), capture_output=True, text=True, timeout=timeout))

    if (dest / ".git").exists():
        if sha:
            r = run(["git", "-C", str(dest), "cat-file", "-e", sha])
            if getattr(r, "returncode", 1) == 0:
                return {"ok": True, "path": str(dest), "reused": True}
            # Deepen only when the commit under review is genuinely absent.
            run(["git", "-C", str(dest), "fetch", "--depth", "50", "origin", sha])
        return {"ok": True, "path": str(dest), "reused": True}

    dest.parent.mkdir(parents=True, exist_ok=True)
    r = run(["git", "clone", "--depth", "50", "--no-single-branch", url, str(dest)])
    if getattr(r, "returncode", 1) != 0:
        return {"ok": False, "reason": (getattr(r, "stderr", "") or "")[:200] or "clone failed"}
    if sha:
        c = run(["git", "-C", str(dest), "cat-file", "-e", sha])
        if getattr(c, "returncode", 1) != 0:
            run(["git", "-C", str(dest), "fetch", "--depth", "50", "origin", sha])
    return {"ok": True, "path": str(dest), "reused": False}
