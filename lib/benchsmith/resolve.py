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
import os
import re
import subprocess
from pathlib import Path

# https://codimango.internalmeta.com/submissions/210976?jobId=...&trialId=...
# Statuses where the task is submitted and a reviewer owns it. Iterating now
# changes the thing they are looking at, and their feedback arrives against a
# revision that no longer exists.
# Frozen: the task is finished. Modifying it corrupts shipped data, and there is
# no override.
FROZEN = {"accepted", "used_in_training"}

AWAITING_REVIEW = {
    "needs_reviewers_assigned": "submitted; waiting for a reviewer to be assigned",
    "being_reviewed": "a reviewer has it now",
    "accepted": "accepted",
    "used_in_training": "accepted and already in training",
}

URL_ID = re.compile(r"/submissions/(\d+)")
BARE_ID = re.compile(r"^\d+$")
# A GSD card. It is an IDEA, not a task: it has no slug, no directory and no
# oracle, so binding it as though it were a task name points a worker at a
# checkout with nothing in it.
GSD_ID = re.compile(r"^T(\d+)$|/tasks/?\?t=(\d+)|internalfb\.com/(T\d+)")

# Track -> the canonical checkout to scaffold into. Read off the card's title
# prefix, because a T-Bench seed scaffolded into the swe-bench repo is a
# mistake nobody notices until validation.
TRACK_REPOS = {
    "t-bench": ("t-bench-aai-labs",),
    "swe-bench": ("swe-bench-aai-labs",),
}
TRACK_HINTS = ((("t-bench", "tbench", "terminal bench"), "t-bench"),
               (("swe-bench", "swebench", "swe bench"), "swe-bench"))

STOPWORDS = {"a", "an", "the", "so", "no", "of", "for", "to", "in", "on", "and", "or",
             "that", "with", "when", "is", "are", "be", "by", "at", "from", "into",
             "without", "not", "its", "it"}


class Unresolved(Exception):
    """The reference names no task we can see. The message says what was tried."""


def _tasks(binary: str = "codimango") -> list[dict]:
    from .sources import task_list_argv

    argv, why = task_list_argv(binary)
    if argv is None:
        raise Unresolved(f"could not resolve the task-list surface: {why}")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or "{" not in r.stdout:
        raise Unresolved(f"`{' '.join(argv)}` failed: {r.stderr.strip()[:160]}")
    return json.loads(r.stdout[r.stdout.index("{"):]).get("tasks") or []


# A task directory exists in every scratch, review and base-tree clone that ever
# touched it. Picking the first match dispatches a worker at a throwaway copy.
def _canonical() -> tuple:
    from .config import load_paths

    return load_paths().canonical


CANONICAL = ("swe-bench-aai-labs", "t-bench-aai-labs", "aai-labs")


def find_repos(task: str, roots: list[str] | None = None) -> list[str]:
    """Every checkout holding this task, canonical-looking ones first."""
    hits = [str(Path(r)) for r in (roots or _default_roots())
            if (Path(r) / task / "task.toml").is_file()]

    markers = _canonical()

    def rank(p: str) -> tuple:
        name = Path(p).name
        for i, marker in enumerate(markers):
            if name.startswith(marker):
                return (0, i, name)
        return (1, 0, name)

    return sorted(hits, key=rank)


def find_repo(task: str, roots: list[str] | None = None) -> str | None:
    hits = find_repos(task, roots)
    return hits[0] if hits else None


def _default_roots() -> list[str]:
    """Every git checkout we can plausibly reach, most specific first.

    Configured roots win. Otherwise: the Meta devserver convention, then a
    couple of ordinary places, then the parent of wherever benchsmith is being
    run from -- because a person who cloned their repos side by side is the
    common case and should not have to configure anything.
    """
    from .config import load_paths

    cfg = load_paths()
    bases = [Path(r) for r in cfg.repo_roots] if cfg.repo_roots else [
        Path("/data/users") / Path.home().name,
        Path.home() / "repos",
        Path.home() / "src",
        Path.home(),
        Path.cwd().parent,
    ]
    def is_checkout(p: Path) -> bool:
        # Scanning a home directory walks into things the user cannot stat --
        # editor servers, other people's mounts. An unreadable directory is not
        # a checkout, and it is certainly not a reason to crash the resolver.
        try:
            return (p / ".git").exists()
        except OSError:
            return False

    out, seen = [], set()
    for base in bases:
        try:
            if not base.is_dir():
                continue
            # A configured root may itself BE a checkout, not a directory of them.
            cands = [base] if is_checkout(base) else sorted(base.glob("*"))
        except OSError:
            continue
        for cand in cands:
            s = str(cand)
            if s not in seen and is_checkout(cand):
                out.append(s)
                seen.add(s)
    return out


def _gsd_card(number: str) -> dict:
    r = subprocess.run(["meta", "tasks.task", "describe", "--task", number, "--output", "json"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise Unresolved(f"could not read GSD card {number}: {r.stderr.strip()[:160]}")
    body = r.stdout[r.stdout.index("{"):] if "{" in r.stdout else ""
    if not body:
        raise Unresolved(f"GSD card {number} returned no JSON")
    doc = json.loads(body)
    return doc[0] if isinstance(doc, list) else doc


def track_of(text: str) -> str:
    low = (text or "").lower()
    for needles, track in TRACK_HINTS:
        if any(n in low for n in needles):
            return track
    return ""


def slugify(title: str, *, words: int = 4) -> str:
    """A candidate task name from a card title. Deterministic, so two runs agree."""
    # Drop a leading bracketed tag: "[T-Bench seed #221] Ordering a schema..."
    body = re.sub(r"^\s*\[[^\]]*\]\s*", "", title or "")
    parts = [w for w in re.split(r"[^a-z0-9]+", body.lower()) if w and w not in STOPWORDS]
    return "-".join(parts[:words])


def resolve_gsd(number: str, *, roots: list[str] | None = None) -> dict:
    """Bind a GSD card as what it is: an idea that has not been scaffolded."""
    card = _gsd_card(number)
    title = str(card.get("title") or "")
    track = track_of(title) or track_of(str(card.get("description") or ""))
    repo = None
    if track:
        from .config import load_paths

        markers = load_paths().track_repos.get(track, TRACK_REPOS.get(track, ()))
        repo = next((r for r in (roots or _default_roots())
                     if any(Path(r).name.startswith(m) for m in markers)), None)
    return {
        "kind": "idea",
        "gsd": number,
        "title": title,
        "section": str(card.get("my_tasks_section") or ""),
        "owner": str(card.get("owner") or ""),
        "owned": str(card.get("owner") or "") in ("", os.environ.get("USER", "")),
        "track": track,
        # A proposal, not a decision that has been made. It is stated so the flow
        # continues; a task name is permanent, so it is stated LOUDLY.
        "suggestedSlug": slugify(title),
        "repo": repo,
        "needsScaffold": True,
        "mode": "scaffold",
        "description": str(card.get("description") or "")[:4000],
    }


def resolve(ref: str, *, binary: str = "codimango", roots: list[str] | None = None) -> dict:
    """`ref` may be a task name, a numeric id, or a submissions URL."""
    ref = (ref or "").strip()
    if not ref:
        raise Unresolved("no task reference given")

    g = GSD_ID.search(ref)
    if g:
        return resolve_gsd(next(x for x in g.groups() if x).lstrip("T").join(("T", "")), roots=roots)

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
            # A task scaffolded a moment ago exists on disk and not on the
            # platform. Refusing it strands the loop exactly where it should be
            # picking up: nothing can be measured until it is pushed, and it
            # cannot be pushed until it has been authored and gated.
            local = find_repos(ref, roots)
            if local:
                return {
                    "kind": "task",
                    "task": ref,
                    "id": "",
                    "status": "unregistered",
                    "registered": False,
                    "validation": "",
                    "sha": "",
                    "repo": local[0],
                    "otherRepos": local[1:],
                    "owned": True,
                    "owner": "",
                    "reviewer": False,
                    "mode": "harden",
                    "note": ("not on the platform yet: it exists only in this checkout. There are "
                             "no measurements to read — author it, gate it, and push it before "
                             "expecting a bar."),
                }
            near = [str(t.get("name")) for t in rows if ref.lower() in str(t.get("name", "")).lower()]
            raise Unresolved(
                f"no task named {ref!r}, and no checkout on this host holds it."
                + (f" Did you mean: {', '.join(near[:3])}?" if near else "")
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
        "mode": ("wait" if str(hit.get("status")) in AWAITING_REVIEW
                 else "repair" if str(hit.get("status")) == "needs_revision" else "harden"),
        "awaitingReview": str(hit.get("status")) in AWAITING_REVIEW,
        "awaitingReason": AWAITING_REVIEW.get(str(hit.get("status")), ""),
        "registered": True,
        "kind": "task",
    }
