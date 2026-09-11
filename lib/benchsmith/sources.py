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


def task_list_argv(binary: str = "codimango") -> tuple[list[str] | None, str]:
    """Ask the installed CLI how to list tasks, never assume.

    `codimango api tasks list` is the legacy spelling; the current CLI exposes
    `codimango task list` and has dropped `api`. Hardcoding either one means the
    backlog silently becomes empty on the machine that has the other -- which is
    what happened, on a fresh runtime, with a real invocation. `adapter.discover`
    already resolves this; the mistake was going around it.
    """
    try:
        from .adapter import Unresolved, discover

        surface = discover(binary)
    except Exception as e:  # noqa: BLE001 - discovery failing is a reportable state
        return None, f"{type(e).__name__}: {e}"
    if not surface.tasks_list:
        return None, "the installed CLI exposes no task-list subcommand"
    return [binary, *surface.site, *surface.tasks_list, "--json"], ""


def fetch_codimango(binary: str = "codimango") -> tuple[list[dict], list[str]]:
    """Every task the platform says is ours."""
    argv, why = task_list_argv(binary)
    if argv is None:
        return [], [f"could not resolve the task-list surface: {why}"]
    code, out, err = _run(argv)
    if code != 0:
        return [], [f"`{' '.join(argv)}` failed: {err.strip()[:200]}"]
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


def fetch_gsd(cfg, limit: int = 200) -> tuple[list[dict], list[str]]:
    """Cards on ONE board.

    `tasks.gsd.task list --project-id` is the board surface; it carries the
    section, which is the column the blueprint's priority tiers are named after.
    The earlier version used `tasks.task list --owner-is-me`, which is not a
    board at all -- it is every open task the user owns, and it filled the queue
    with 94 oncall parents and translation requests.

    Without a project id this returns nothing and says so. Guessing a board is
    worse than having none: an empty queue is visibly empty, a wrong one looks
    like work.
    """
    if not cfg.configured:
        return [], ["no GSD board configured; run `benchsmith config` for how to set one"]
    argv = ["meta", "tasks.gsd.task", "list", f"--project-id={cfg.project_id}",
            f"--limit={limit}", "--output", "json"]
    if cfg.assignee:
        argv.append(f"--assignee={cfg.assignee}")
    code, out, err = _run(argv)
    if code != 0:
        return [], [f"GSD board {cfg.project_id} could not be read: {err.strip()[:200]}"]
    try:
        rows = json.loads(out[out.index("["):]) if "[" in out else []
    except ValueError as e:
        return [], [f"GSD board did not return JSON: {e}"]
    return rows if isinstance(rows, list) else [], []


def normalise_codimango(rows: list[dict], *, require_owner: bool = True) -> tuple[list[dict], list[str]]:
    """Keep the rows that represent work THIS caller owns.

    `currentUserIsTaskOwner` is computed by the server for the calling
    credential, so it is an ownership answer rather than an inference from a
    name. It matters because `tasks list` does not always return your own work:
    `--filter reviewing`, `--pod` and `--tag` all return other people's tasks,
    and hardening somebody else's task is not a thing to do by accident.
    """
    keep, notes = [], []
    for r in rows:
        status = str(r.get("status") or "")
        if require_owner and r.get("currentUserIsTaskOwner") is False:
            notes.append(f"{r.get('name') or r.get('id')}: owned by {r.get('importedBy') or 'someone else'}"
                         f"{' (you are the reviewer)' if r.get('currentUserIsReviewer') else ''}; "
                         "not queued for work")
            continue
        if status in NOT_OUR_WORK:
            # Named, not silent: "my task vanished from the queue" is a worse
            # experience than one line saying a reviewer has it.
            notes.append(f"{r.get('name') or r.get('id')}: {NOT_OUR_WORK[status]}; not queued")
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


def check_assignee_scope(rows: list[dict], assignee: str) -> list[str]:
    """Did the server-side assignee filter actually take?

    `--assignee=kngreen` filters correctly, but the rows come back carrying a
    DISPLAY name ("Kristin Green"). Comparing that to a unixname is a category
    error, not a check -- it would reject every row. What can be checked is
    whether the filter narrowed to one person: if several assignees come back
    from a filtered query, the filter was ignored and the scope is not what was
    asked for.
    """
    if not assignee:
        return []
    who = {str(r.get("assignee") or r.get("owner") or "") for r in rows if r.get("assignee") or r.get("owner")}
    if len(who) > 1:
        return [f"--assignee={assignee} returned {len(who)} distinct assignees "
                f"({', '.join(sorted(who)[:4])}); the filter did not take — treat this scope as wrong"]
    return []


def normalise_gsd(rows: list[dict], columns: dict[str, str] | None = None,
                  known_tasks: list[str] | None = None,
                  assignee: str = "") -> tuple[list[dict], list[str]]:
    """Map GSD cards onto queue kinds, flagging probable duplicates.

    There is no link field between a GSD card and a Codimango task, so a
    duplicate can only be guessed at from the wording. A guess is therefore
    recorded as a flag rather than acted on as a deletion: dropping a card we
    merely suspect is a duplicate loses real work silently, while keeping a
    flagged one costs an idea-tier slot -- the cheapest slot there is.
    """
    from .config import DEFAULT_SECTIONS, SKIP

    columns = columns or DEFAULT_SECTIONS
    lowered = {k.lower(): v for k, v in columns.items()}
    known = {t.lower(): _tokens(t) for t in (known_tasks or [])}
    out, notes = [], check_assignee_scope(rows, assignee)
    for r in rows:
        title = str(r.get("title") or "")
        number = str(r.get("number") or r.get("id") or "")
        section_raw = str(r.get("section") or "").strip()
        section = section_raw.lower()
        kind = lowered.get(section)
        if kind == SKIP:
            continue  # the column exists and is deliberately not work
        if kind is None:
            # Unmapped: queued at the cheapest tier but NOT dispatchable, and
            # named so the map can be corrected. Auto-working an unknown column
            # is how an "Archived" card becomes an idea to go build.
            kind = "idea"
            unmapped = section_raw or "(none)"
            notes.append(f"section {unmapped!r} is not in the section map; "
                         "queued as an idea but held for confirmation")
        item = {"name": number or title[:60], "title": title, "kind": kind}
        if lowered.get(section) is None:
            item["unmappedSection"] = section_raw or "(none)"
        tok = _tokens(title)
        for name, ntok in known.items():
            if name in title.lower() or (tok and ntok and len(tok & ntok) >= 3):
                item["duplicateOf"] = name
                notes.append(f"{number}: probably duplicates {name}; flagged, not dropped")
                break
        out.append(item)
    return out, notes


def discover(*, binary: str = "codimango", cfg=None, with_gsd: bool = True,
             require_owner: bool = True) -> dict:
    """The payload `build_queue` already expects."""
    tasks, notes = fetch_codimango(binary)
    tasks, n2 = normalise_codimango(tasks, require_owner=require_owner)
    notes += n2
    ideas: list[dict] = []
    if with_gsd and cfg is not None:
        rows, n3 = fetch_gsd(cfg)
        notes += n3
        ideas, n4 = normalise_gsd(rows, columns=cfg.sections, assignee=cfg.assignee,
                                  known_tasks=[str(t.get("name") or "") for t in tasks])
        notes += n4
    return {"tasks": tasks, "ideas": ideas, "notes": notes,
            "gsd": cfg.as_dict() if cfg is not None else None}
