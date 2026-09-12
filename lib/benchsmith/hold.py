"""A hold on a whole repository, not one task.

Task leases answer "is anyone else working this task". They cannot answer "is
anyone else about to land a stack of commits on main", which is a different
question and the one that was being handled by announcement:

    "Task 207170 owns aai_labs_ollo main now ... Hold main until 4:15 PM ET."

An announcement is not a mechanism. Every worker that did not read it kept
preparing pushes into a branch somebody else had claimed, and the coordinator
had nothing to check.

Same primitive as the task lease -- a ref the remote arbitrates -- because the
alternative is a second kind of lock with a second set of failure modes.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

from . import remote_ref

REF = "refs/heads/benchsmith-locks/__repo__"
DEFAULT_MINUTES = 45


def _git(repo, *args, timeout: int = 60):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def _parse(msg: str) -> dict:
    import re

    out = {}
    for k in ("holder", "host", "until", "why"):
        m = re.search(rf"\b{k}=([^\s]+)", msg or "")
        if m:
            out[k] = m.group(1)
    return out


def current(repo, *, remote: str = "origin") -> dict:
    """Who holds the branch, if anyone. Unreadable is not free."""
    observed = remote_ref.read(repo, remote, REF)
    if not observed["readable"]:
        return {"readable": False, "held": None,
                "reason": f"could not read the repository hold: {observed['detail'][:140]}"}
    sha = observed["sha"]
    if not sha:
        return {"readable": True, "held": False}
    info = _parse(_git(repo, "show", "-s", "--format=%B", sha).stdout)
    until = int(info.get("until") or 0)
    if until and time.time() > until:
        return {"readable": True, "held": False, "expired": info,
                "reason": "the hold has expired"}
    return {"readable": True, "held": True, "sha": sha, "holder": info.get("holder", "?"),
            "host": info.get("host", "?"), "why": (info.get("why") or "").replace("_", " "),
            "minutesLeft": int((until - time.time()) / 60) if until else None}


def take(repo, *, why: str = "", minutes: int = DEFAULT_MINUTES,
         remote: str = "origin") -> dict:
    """Claim the branch for a bounded window. Never open-ended."""
    now = current(repo, remote=remote)
    if not now.get("readable"):
        return {"taken": False, **now}
    if now.get("held"):
        return {"taken": False, **now, "reason": "somebody else holds it"}
    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    msg = (f"benchsmith repo hold holder={os.environ.get('USER', 'unknown')} "
           f"host={socket.gethostname()} until={int(time.time() + minutes * 60)} "
           f"why={(why or 'unstated').replace(' ', '_')}")
    tok = _git(repo, "commit-tree", tree, "-m", msg).stdout.strip()
    if not tok:
        return {"taken": False, "reason": "could not create the hold token"}
    # A hold that never expires is a hold somebody forgets to release.
    result = remote_ref.update(repo, remote, REF, tok)
    if result["state"] != remote_ref.CONFIRMED:
        return {"taken": False, "reason": result["detail"], "reconciliation": result}
    return {"taken": True, "minutes": minutes, "why": why, "token": tok,
            "reconciliation": result}


def release(repo, *, remote: str = "origin") -> dict:
    now = current(repo, remote=remote)
    if not now.get("held"):
        return {"released": False, "reason": "not held"}
    result = remote_ref.update(repo, remote, REF, "", expected=now["sha"])
    return {"released": result["state"] == remote_ref.CONFIRMED,
            "reason": "" if result["state"] == remote_ref.CONFIRMED else result["detail"],
            "reconciliation": result}
