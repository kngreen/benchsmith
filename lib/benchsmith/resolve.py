"""Turn whatever the user typed into a task the loop can bind to.

A skill that answers "send me the task" has already failed the invocation. The
user typed something -- a name, an id, a submissions URL pasted from the browser
-- and working out which is not their job.

Resolution is by IDENTITY, never by the string alone: a numeric id and a name
live in different spaces, and a name that matches nothing must be reported as
unresolved rather than passed through and later mistaken for a directory that
happens not to exist yet.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# https://codimango.internalmeta.com/submissions/210976?jobId=...&trialId=...
URL_ID = re.compile(r"/submissions/(\d+)")
BARE_ID = re.compile(r"^\d+$")


class Unresolved(Exception):
    """The reference names no task we can see. The message says what was tried."""


def _tasks(binary: str = "codimango") -> list[dict]:
    r = subprocess.run([binary, "api", "tasks", "list", "--json"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or "{" not in r.stdout:
        raise Unresolved(f"could not list tasks: {r.stderr.strip()[:160]}")
    return json.loads(r.stdout[r.stdout.index("{"):]).get("tasks") or []


# A task directory exists in every scratch, review and base-tree clone that ever
# touched it. Picking the first match dispatches a worker at a throwaway copy.
CANONICAL = ("swe-bench-aai-labs", "t-bench-aai-labs", "aai-labs")


def find_repos(task: str, roots: list[str] | None = None) -> list[str]:
    """Every checkout holding this task, canonical-looking ones first."""
    hits = [str(Path(r)) for r in (roots or _default_roots())
            if (Path(r) / task / "task.toml").is_file()]

    def rank(p: str) -> tuple:
        name = Path(p).name
        for i, marker in enumerate(CANONICAL):
            if name.startswith(marker):
                return (0, i, name)
        return (1, 0, name)

    return sorted(hits, key=rank)


def find_repo(task: str, roots: list[str] | None = None) -> str | None:
    hits = find_repos(task, roots)
    return hits[0] if hits else None


def _default_roots() -> list[str]:
    base = Path("/data/users") / (Path.home().name)
    return [str(p) for p in sorted(base.glob("*")) if (p / ".git").exists()] if base.is_dir() else []


def resolve(ref: str, *, binary: str = "codimango", roots: list[str] | None = None) -> dict:
    """`ref` may be a task name, a numeric id, or a submissions URL."""
    ref = (ref or "").strip()
    if not ref:
        raise Unresolved("no task reference given")

    m = URL_ID.search(ref)
    wanted_id = m.group(1) if m else (ref if BARE_ID.match(ref) else "")

    rows = _tasks(binary)
    if wanted_id:
        hit = next((t for t in rows if str(t.get("id")) == wanted_id), None)
        if hit is None:
            raise Unresolved(
                f"task id {wanted_id} is not in your task list. It may belong to someone "
                "else, or be archived — check before working on it."
            )
    else:
        hit = next((t for t in rows if str(t.get("name")) == ref), None)
        if hit is None:
            near = [str(t.get("name")) for t in rows if ref.lower() in str(t.get("name", "")).lower()]
            raise Unresolved(
                f"no task named {ref!r}." + (f" Did you mean: {', '.join(near[:3])}?" if near else "")
            )

    name = str(hit.get("name") or "")
    repos = find_repos(name, roots)
    # Ownership is answered here too, so a single-task invocation cannot quietly
    # start work on somebody else's task.
    owned = hit.get("currentUserIsTaskOwner")
    return {
        "task": name,
        "id": str(hit.get("id") or ""),
        "status": str(hit.get("status") or ""),
        "validation": str(hit.get("validationStatus") or ""),
        "sha": str(hit.get("validationCommitSha") or hit.get("commitSha") or ""),
        "repo": repos[0] if repos else None,
        # Named, not hidden: choosing among clones is a decision, and a silent
        # choice among five is how a worker ends up hardening a throwaway copy.
        "otherRepos": repos[1:],
        "owned": owned is not False,
        "owner": str(hit.get("importedBy") or ""),
        "reviewer": bool(hit.get("currentUserIsReviewer")),
        "mode": "repair" if str(hit.get("status")) == "needs_revision" else "harden",
    }
