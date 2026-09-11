"""Where the backlog comes from.

`build_queue` was always able to order Codimango tasks and GSD ideas -- it takes
them as arguments and is pure. What was missing is the part that goes and gets
them, so in practice the coordinator could only order work someone had already
handed it in a file.

Fetching is separated from normalising on purpose: `fetch_*` does IO and nothing
else, `normalise_*` is pure and carries every judgement worth testing. A rule
that only runs when the network is up is a rule nobody can check.
"""

from __future__ import annotations

import json
import re
import subprocess

# GSD board columns, matched case-insensitively against a task's progress and
# its tags. Defaults follow the fleet blueprint's column names; a board that
# spells them differently supplies its own mapping rather than being silently
# mismatched into the idea tier.
DEFAULT_GSD_COLUMNS = {
    "task needs review": "gsd_review",
    "task is ready to scaffold": "gsd_scaffold",
    "task ideas (auto-generated)": "idea",
    "task ideas": "idea",
}

# Statuses that are somebody else's move, not ours. Kept explicit so a new
# platform status shows up as unrecognised rather than being quietly worked on.
NOT_OUR_WORK = {
    "accepted": "already accepted",
    "used_in_training": "already in training",
    "being_reviewed": "a reviewer holds it",
    "needs_reviewers_assigned": "waiting on reviewer assignment, not on work",
}


def _run(argv: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", f"{type(e).__name__}: {e}"
    return r.returncode, r.stdout, r.stderr


def fetch_codimango(binary: str = "codimango") -> tuple[list[dict], list[str]]:
    """Every task the platform says is ours."""
    code, out, err = _run([binary, "api", "tasks", "list", "--json"])
    if code != 0:
        return [], [f"codimango task list failed: {err.strip()[:200]}"]
    try:
        doc = json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError as e:
        return [], [f"codimango task list did not return JSON: {e}"]
    rows = doc.get("tasks")
    if rows is None:
        # An empty list and a missing key are different facts. Saying "no tasks"
        # for a response we failed to understand is how a full backlog reads as
        # an empty one.
        return [], ["codimango response had no `tasks` key; treating as unknown, not empty"]
    return rows, []


def fetch_gsd(owner: str = "", tags: str = "", limit: int = 200) -> tuple[list[dict], list[str]]:
    """Open GSD cards for a board."""
    argv = ["meta", "tasks.task", "list", "--status-is=OPEN",
            f"--limit={limit}", "--output", "json"]
    argv.append(f"--owner-is={owner}" if owner else "--owner-is-me")
    if tags:
        argv.append(f"--tags-include-any-of={tags}")
    code, out, err = _run(argv)
    if code != 0:
        return [], [f"GSD task list failed: {err.strip()[:200]}"]
    try:
        rows = json.loads(out[out.index("["):]) if "[" in out else []
    except ValueError as e:
        return [], [f"GSD task list did not return JSON: {e}"]
    return rows if isinstance(rows, list) else [], []


def normalise_codimango(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Keep the rows that represent work, and say why the rest were dropped."""
    keep, notes = [], []
    for r in rows:
        status = str(r.get("status") or "")
        if status in NOT_OUR_WORK:
            continue
        if status not in ("draft", "needs_revision"):
            notes.append(f"{r.get('name') or r.get('id')}: unrecognised status {status!r}; "
                         "not queued — check whether it needs a tier")
            continue
        keep.append(r)
    return keep, notes


_SLUG = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {w for w in _SLUG.sub(" ", (text or "").lower()).split() if len(w) > 3}


def normalise_gsd(rows: list[dict], columns: dict[str, str] | None = None,
                  known_tasks: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Map GSD cards onto queue kinds, flagging probable duplicates.

    There is no link field between a GSD card and a Codimango task, so a
    duplicate can only be guessed at from the wording. A guess is therefore
    recorded as a flag rather than acted on as a deletion: dropping a card we
    merely suspect is a duplicate loses real work silently, while keeping a
    flagged one costs an idea-tier slot -- the cheapest slot there is.
    """
    columns = columns or DEFAULT_GSD_COLUMNS
    known = {t.lower(): _tokens(t) for t in (known_tasks or [])}
    out, notes = [], []
    for r in rows:
        title = str(r.get("title") or "")
        number = str(r.get("number") or r.get("id") or "")
        haystack = " ".join([str(r.get("progress") or ""),
                             " ".join(r.get("tags") or []) if isinstance(r.get("tags"), list) else str(r.get("tags") or "")]).lower()
        kind = None
        for column, mapped in columns.items():
            if column in haystack:
                kind = mapped
                break
        if kind is None:
            # An unmapped card is an idea by default: lowest priority, so a
            # mis-mapping costs the least it can.
            kind = "idea"
        item = {"name": number or title[:60], "title": title, "kind": kind}
        tok = _tokens(title)
        for name, ntok in known.items():
            if name in title.lower() or (tok and ntok and len(tok & ntok) >= 3):
                item["duplicateOf"] = name
                notes.append(f"{number}: probably duplicates {name}; flagged, not dropped")
                break
        out.append(item)
    return out, notes


def discover(*, binary: str = "codimango", gsd_owner: str = "", gsd_tags: str = "",
             with_gsd: bool = True) -> dict:
    """The payload `build_queue` already expects."""
    tasks, notes = fetch_codimango(binary)
    tasks, n2 = normalise_codimango(tasks)
    notes += n2
    ideas: list[dict] = []
    if with_gsd:
        rows, n3 = fetch_gsd(gsd_owner, gsd_tags)
        notes += n3
        ideas, n4 = normalise_gsd(rows, known_tasks=[str(t.get("name") or "") for t in tasks])
        notes += n4
    return {"tasks": tasks, "ideas": ideas, "notes": notes}
