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

from . import remote_ref

REF = "refs/heads/benchsmith-locks/{task}"
SAFE = re.compile(r"[A-Za-z0-9._-]+")

# Long enough that a slow validation wave never loses the lease, short enough
# that a dead host does not strand a task for a day.
TTL_SECONDS = 4 * 3600


# One round trip answers for every task. Asking per task turned an already slow
# dispatch loop into one that timed out before a single worker started.
def states(repo, *, remote: str = "origin", timeout: int = 60) -> dict:
    """Every held lease in this repository, task -> sha, in one ls-remote."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", remote,
             "refs/heads/benchsmith-locks/*"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LeaseLost(f"could not read the lease refs: {type(error).__name__}: {error}") from error
    if p.returncode:
        raise LeaseLost(f"could not read the lease refs: {p.stderr.strip()[:160]}")
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/benchsmith-locks/"):
            out[parts[1].rsplit("/", 1)[1]] = parts[0]
    return out


class LeaseLost(Exception):
    """The claim is not held. The message says what happened to it."""


@dataclass
class Owner:
    uuid: str = ""
    host: str = ""
    pid: int = 0
    task: str = ""
    acquired: int = 0
    # The worker this lease is actually FOR. The process that takes a lease is
    # the dispatcher, and it exits seconds later; judging liveness by its pid
    # made every lease reclaimable the moment dispatch finished, which is worse
    # than having no lease at all. Once a session is recorded, that session's
    # state is the only thing that says whether the work is still happening.
    session: str = ""

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
        """The recorded holder is definitely gone.

        A lease with a SESSION is owned by that worker, not by whatever process
        created it. The dispatcher exits within seconds of dispatching, so a
        dead dispatcher pid says nothing about whether the worker is still
        running -- and treating it as free released worktrees out from under
        live workers.

        With no session recorded, the lease belongs to a command that never got
        as far as starting one, and a dead pid on this host does mean gone.
        """
        if self.session:
            return False
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
        return {"uuid": self.uuid, "host": self.host, "pid": self.pid, "session": self.session,
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
                 acquired=grab("acquired", int, 0) or 0,
                 session=grab("session", str, "") or "")


class RemoteLease:
    def __init__(self, task: str, repo, *, remote: str = "origin", ttl: int = TTL_SECONDS,
                 runner=None, timeout: int = 60, known: str | None = None,
                 reconcile_delays=remote_ref.DEFAULT_DELAYS, sleeper=time.sleep):
        if not SAFE.fullmatch(task or ""):
            raise LeaseLost(f"task name is not safe for a remote ref: {task!r}")
        self.task, self.repo, self.remote, self.ttl = task, str(repo), remote, ttl
        self.ref = REF.format(task=task)
        self.sha = ""
        self.timeout = timeout
        # A sha the caller already fetched in a batch, so acquire() need not
        # make its own round trip just to discover the ref is free.
        self._known = known
        # Set once the worker exists, then written into the lease by `bind`.
        self.session_id = ""
        self._run = runner
        self.reconcile_delays = reconcile_delays
        self.sleeper = sleeper

    def _git(self, *args, check: bool = False) -> subprocess.CompletedProcess:
        if self._run is not None:
            try:
                return self._run(list(args))
            except subprocess.TimeoutExpired as e:
                raise LeaseLost(f"git {args[0]} timed out") from e
        try:
            p = subprocess.run(["git", "-C", self.repo, *args],
                               capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise LeaseLost(
                f"git {args[0]} against {self.remote} exceeded {self.timeout}s. The remote lease "
                "cannot be taken, so no worker starts. Check the remote, or pass "
                "--no-remote-lease if this repository has no shared one."
            ) from e
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
        # A lease carries metadata only; embedding HEAD's tree makes lock pushes repository-sized.
        tree = self._git("hash-object", "-t", "tree", "-w", "/dev/null", check=True).stdout.strip()
        msg = (f"benchsmith lease uuid={uuid.uuid4()} host={socket.gethostname()} "
               f"pid={os.getpid()} task={self.task} acquired={int(time.time())}"
               + (f" session={self.session_id}" if self.session_id else ""))
        return self._git("commit-tree", tree, "-m", msg, check=True).stdout.strip()

    def _update(self, token: str, *, expected: str | None = None) -> dict:
        run = self._run
        return remote_ref.update(
            self.repo,
            self.remote,
            self.ref,
            token,
            expected=expected,
            timeout=self.timeout,
            runner=run,
            delays=self.reconcile_delays,
            sleeper=self.sleeper,
        )

    def acquire(self) -> dict:
        """Claim the task, or say who holds it."""
        held = self._known if self._known is not None else self.remote_sha()
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
            result = self._update(token, expected=held)
            if result["state"] != remote_ref.CONFIRMED:
                return {"held": False, "owner": who.as_dict(),
                        "reason": f"could not reap stale lease: {result['detail']}",
                        "reconciliation": result}
            self.sha = token
            return {"held": True, "reaped": who.as_dict(), "reconciliation": result,
                    "note": result["detail"] if result.get("attempts") else ""}

        token = self._token()
        # Creating a ref that already exists is rejected, which is the claim.
        result = self._update(token)
        if result["state"] != remote_ref.CONFIRMED:
            return {"held": False, "reason": result["detail"], "reconciliation": result}
        self.sha = token
        return {"held": True, "owner": self.owner(token).as_dict(), "reconciliation": result,
                "note": result["detail"] if result.get("attempts") else ""}

    def bind(self, session_id: str) -> dict:
        """Record which worker this lease is for, once one exists.

        Until this is called the lease names only the dispatcher, and the
        dispatcher is about to exit.
        """
        if not self.sha:
            raise LeaseLost("cannot bind a lease that is not held")
        self.session_id = session_id
        token = self._token()
        result = self._update(token, expected=self.sha)
        if result["state"] != remote_ref.CONFIRMED:
            return {"bound": False, "reason": "lease remains unbound: " + result["detail"],
                    "reconciliation": result}
        self.sha = token
        return {"bound": True, "session": session_id, "token": token,
                "reconciliation": result}

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

    def release(self, token: str = "") -> dict:
        expected = token or self.sha
        if not expected:
            return {"released": False, "reason": "not held"}
        result = self._update("", expected=expected)
        ok = result["state"] == remote_ref.CONFIRMED
        if ok:
            self.sha = ""
        return {"released": ok, "reason": "" if ok else result["detail"],
                "reconciliation": result}
