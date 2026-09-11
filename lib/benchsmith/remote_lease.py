"""An atomic remote claim, so two hosts cannot repair the same task.

The local lease in `queue.py` stops two workers on one machine. It says nothing
about a second machine -- a laptop and a devserver both running the fleet will
each believe they own every task, and their commits will interleave on one
branch.

The claim is a git ref, because git already gives us the only primitive that
matters: a push that fails when the ref moved. `--force-with-lease` is a
compare-and-swap, and the remote is the one thing both hosts can see.

The token is a commit object whose message names the owner, so a stale lease can
be diagnosed rather than merely stepped over.

Adapted from Green's `RemoteTaskLease`, which renews from a heartbeat thread.
benchsmith runs as commands rather than a daemon, so the lease carries a TTL and
is reaped on age instead -- a worker that dies cannot renew, and a lease nobody
renews must eventually be claimable or the task is stranded forever.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

REF = "refs/heads/benchsmith-locks/{task}"
SAFE = re.compile(r"[A-Za-z0-9._-]+")

# Long enough that a slow validation wave never loses the lease, short enough
# that a dead host does not strand a task for a day.
TTL_SECONDS = 4 * 3600


class LeaseLost(Exception):
    """The claim is not held. The message says what happened to it."""


@dataclass
class Owner:
    uuid: str = ""
    host: str = ""
    pid: int = 0
    task: str = ""
    acquired: int = 0

    @property
    def age(self) -> float:
        return time.time() - self.acquired if self.acquired else float("inf")

    @property
    def mine(self) -> bool:
        """This exact process holds it."""
        return self.same_host and self.pid == os.getpid()

    @property
    def same_host(self) -> bool:
        return self.host == socket.gethostname()

    @property
    def holder_is_gone(self) -> bool:
        """The recorded process is dead, on this host.

        benchsmith runs as commands, so the process that took a lease has
        usually exited by the time another command wants it. Treating a lease as
        foreign because the PID differs would make one takeable by nobody --
        including the person who took it.
        """
        if not self.same_host or not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return False

    def as_dict(self) -> dict:
        return {"uuid": self.uuid, "host": self.host, "pid": self.pid,
                "task": self.task, "acquiredAgo": int(self.age) if self.acquired else None}


def parse_owner(message: str) -> Owner:
    def grab(key, cast=str, default=None):
        m = re.search(rf"\b{key}=([^\s]+)", message or "")
        if not m:
            return default
        try:
            return cast(m.group(1))
        except (TypeError, ValueError):
            return default

    return Owner(uuid=grab("uuid", str, "") or "", host=grab("host", str, "") or "",
                 pid=grab("pid", int, 0) or 0, task=grab("task", str, "") or "",
                 acquired=grab("acquired", int, 0) or 0)


class RemoteLease:
    def __init__(self, task: str, repo, *, remote: str = "origin", ttl: int = TTL_SECONDS,
                 runner=None):
        if not SAFE.fullmatch(task or ""):
            raise LeaseLost(f"task name is not safe for a remote ref: {task!r}")
        self.task, self.repo, self.remote, self.ttl = task, str(repo), remote, ttl
        self.ref = REF.format(task=task)
        self.sha = ""
        self._run = runner

    # A lease ref is not a task publication, and the repos' pre-push hook does
    # not distinguish: it computes the touched tasks from local HEAD's range
    # regardless of which ref is being pushed, so a lock push is judged as
    # though it were a code change and demands gate receipts for whatever
    # happens to be in the range.
    #
    # This is the one place benchsmith bypasses a hook, and it is narrow: only
    # pushes to `refs/heads/benchsmith-locks/*`, which contain no task content
    # and can never reach a branch anyone validates. Task publication still goes
    # through the hook, unbypassed.
    _NOVERIFY = ("--no-verify",)

    def _git(self, *args, check: bool = False) -> subprocess.CompletedProcess:
        if self._run is not None:
            return self._run(list(args))
        p = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True, timeout=180)
        if check and p.returncode:
            raise LeaseLost((p.stderr or p.stdout).strip()[:200])
        return p

    def remote_sha(self) -> str:
        p = self._git("ls-remote", "--heads", self.remote, self.ref)
        if p.returncode:
            # Not knowing who holds it is not the same as nobody holding it.
            raise LeaseLost(f"could not read the remote lease: {p.stderr.strip()[:160]}")
        return (p.stdout.split() or [""])[0]

    def owner(self, sha: str = "") -> Owner:
        sha = sha or self.remote_sha()
        if not sha:
            return Owner()
        return parse_owner(self._git("show", "-s", "--format=%B", sha).stdout)

    def _token(self) -> str:
        tree = self._git("rev-parse", "HEAD^{tree}", check=True).stdout.strip()
        msg = (f"benchsmith lease uuid={uuid.uuid4()} host={socket.gethostname()} "
               f"pid={os.getpid()} task={self.task} acquired={int(time.time())}")
        return self._git("commit-tree", tree, "-m", msg, check=True).stdout.strip()

    def acquire(self) -> dict:
        """Claim the task, or say who holds it."""
        held = self.remote_sha()
        if held:
            who = self.owner(held)
            if who.mine or who.holder_is_gone:
                self.sha = held
                return {"held": True, "reused": True, "owner": who.as_dict(),
                        "note": "" if who.mine else "the recorded process is gone; reclaimed"}
            if who.age <= self.ttl:
                return {"held": False, "owner": who.as_dict(),
                        "reason": f"held by {who.host} pid {who.pid} "
                                  f"for {int(who.age / 60)}m; TTL is {int(self.ttl / 60)}m"}
            # Expired. Steal it with a compare-and-swap so two hosts reaping the
            # same stale lease cannot both win.
            token = self._token()
            p = self._git("push", "-q", *self._NOVERIFY,
                          f"--force-with-lease={self.ref}:{held}",
                          self.remote, f"{token}:{self.ref}")
            if p.returncode:
                return {"held": False, "owner": who.as_dict(),
                        "reason": f"lost the race to reap a stale lease: {p.stderr.strip()[:120]}"}
            self.sha = token
            return {"held": True, "reaped": who.as_dict()}

        token = self._token()
        # Creating a ref that already exists is rejected, which is the claim.
        p = self._git("push", "-q", *self._NOVERIFY, self.remote, f"{token}:{self.ref}")
        if p.returncode:
            return {"held": False, "reason": f"another host claimed it first: "
                                             f"{p.stderr.strip()[:120]}"}
        self.sha = token
        return {"held": True, "owner": self.owner(token).as_dict()}

    def assert_owned(self) -> None:
        """Fail closed immediately before a publication mutation."""
        if not self.sha:
            raise LeaseLost("the remote lease is not held")
        remote = self.remote_sha()
        if remote != self.sha:
            raise LeaseLost(
                f"the remote lease moved from {self.sha[:12]} to {remote[:12] or 'missing'}; "
                "another host owns this task now"
            )

    def release(self) -> dict:
        if not self.sha:
            return {"released": False, "reason": "not held"}
        p = self._git("push", "-q", *self._NOVERIFY,
                      f"--force-with-lease={self.ref}:{self.sha}",
                      self.remote, f":{self.ref}")
        ok = p.returncode == 0
        if ok:
            self.sha = ""
        return {"released": ok, "reason": "" if ok else p.stderr.strip()[:160]}
