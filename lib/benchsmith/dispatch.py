"""Stage 3: start one worker on one task, and get a small answer back.

Non-publishing by construction. A stage-3 worker prepares a commit and reports;
it never pushes. The single publish lane is stage 4, and keeping them apart is
what stops N workers contending on one shared main -- the serialisation a fleet
audit found to be the actual submission bottleneck.

Dispatch is planned before it is run. `plan()` returns the exact argv and writes
nothing; `run()` executes it only when explicitly applied. Everything here is
therefore inspectable before anything starts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import candidate as candidate_mod

# Probed against the live API, not assumed: `--harness` accepts `codex` and
# `native`; `claude` and `metacode` are rejected by agentcloud\wire\HarnessKind.
# So the 1P delegation hop cannot be an agentcloud session -- it runs locally.
AGENTCLOUD_HARNESSES = frozenset({"codex", "native"})

# Empty means "whatever the tenant defaults to". `--harness codex` passes
# `--dry-run` -- which only validates the enum -- and is then REJECTED with
# HTTP 400 at create time on this tenant. Validating a value is not the same as
# being able to use it, so the default is the one that demonstrably starts.
DEFAULT_HARNESS = ""

HANDOFF_LIMIT = 4096

# The one host benchsmith is installed on. A worker that lands anywhere else
# cannot run it: the repository is private, so the "just clone it" fallback
# returns HTTP 403 from a fresh runtime. Being off-host is a reportable state,
# not something to work around.
HOST = socket.gethostname()

# The lowercase proxy vars on a devserver point at a host that is frequently
# dead; the uppercase ones work. Reaching GitHub needs the working pair, and
# codimango's API needs .internalmeta.com excluded from proxying.
PROXY_PREAMBLE = (
    "export https_proxy=http://fwdproxy:8080 http_proxy=http://fwdproxy:8080\n"
    'export no_proxy="$no_proxy,.internalmeta.com"'
)

# A worker returns this and nothing else. The cap is the point: a supervisor
# holding N full transcripts is the context-exhaustion the fleet audit measured
# at 550-724K input tokens per call, most of it spent waiting.
HANDOFF_FIELDS = (
    "work_item", "state", "base_sha", "commit_sha", "gate_receipt", "next_action", "note",
    "source_base_sha", "carried_from_sha", "lease_token", "lease_task", "session",
    "finalization", "submission_id", "validation", "review", "evidence_url", "status_repo",
)

# Where a worker also writes its answer. Stdout is not durable: a launcher that
# backgrounds the process, or drops its pipe, loses the handoff and the work
# with it -- observed once, where a completed local pass reported nothing.
HANDOFF_DIR = ".benchsmith/handoff"
ASSIGNMENT_DIR = ".benchsmith/assignments"
HANDOFF_STATES = frozenset({
    "ready_to_publish", "blocked", "needs_human", "no_change", "failed", "in_progress",
    # The commit is published and the platform is chewing on it. A wave takes
    # tens of minutes to hours; a worker that sits through one holds a slot,
    # burns context, and learns nothing it could not learn on arrival. It hands
    # the SHA back and the coordinator re-dispatches when the wave lands.
    "awaiting_validation",
})


# Names that are almost certainly an unsubstituted placeholder rather than a
# task. Twelve sessions were once started as `benchsmith: t`, from a `--task <t>`
# in the skill text that an agent copied literally.
# What a mode is called in a session title. `needs_revision` is the platform's
# word for the state, so `revise` is the operator-facing label for the work;
# `repair` stays the internal mode name because the journal records it.
MODE_LABELS = {"harden": "harden", "repair": "revise", "revise": "revise",
               "scaffold": "scaffold", "review": "review"}
MODE_ALIASES = {"revise": "repair"}


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode or "harden", mode or "harden")


PLACEHOLDERS = frozenset({
    "t", "f", "n", "x", "task", "name", "taskname", "task-name", "task_name",
    "repo", "handoff", "session", "id", "foo", "bar", "example", "todo",
})


class DispatchRefused(Exception):
    """The dispatch is not safe or not possible. The message is the reason."""


class CandidateHandoffRefused(DispatchRefused):
    """A ready handoff whose full candidate stack is not publication-safe."""

    def __init__(self, reason: str, handoff: dict | None = None):
        super().__init__(reason)
        self.handoff = dict(handoff or {})


@dataclass
class Plan:
    backend: str
    argv: list[str]
    task: str
    publishing: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def shell(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    def as_dict(self) -> dict:
        return {"backend": self.backend, "task": self.task, "publishing": self.publishing,
                "argv": self.argv, "shell": self.shell, "notes": self.notes}


def resume_block(repo: str, task: str) -> str:
    """Tell a fresh worker what the last one already established.

    AgentCloud has no resume action -- create, describe, list and poll are the
    whole surface, and events are immutable -- so a session cannot be continued
    programmatically. Durability therefore cannot live in the session. It lives
    in the journal, which is better: it survives the session being lost
    entirely, and any worker on any host can pick the task up.
    """
    import json as _json

    path = Path(repo).expanduser() / ".benchsmith" / f"{task}.json"
    try:
        data = _json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    rounds = data.get("rounds") or []
    if not rounds:
        return ""
    last = rounds[-1]
    return (
        f"This task has {len(rounds)} prior rounds. Mode is "
        f"{data.get('mode') or 'unset'}; the last round was classified "
        f"{last.get('class') or last.get('cls') or 'unrecorded'}"
        + (f" ({data['status']})" if data.get("status") else "")
        + ".\nRead `.benchsmith/" + task + ".json` before doing anything. Continue that "
        "history; do not restart the task, and do not re-litigate a round it "
        "already closed.\n\n"
    )


def benchsmith_root() -> str:
    """The absolute path of the running installation.

    Not `~`: an agentcloud session runs on the same devserver but under a
    different HOME (`/var/localhome/devenvuser...`), so a tilde resolves
    somewhere the installation is not and the worker silently re-clones.
    """
    return str(Path(__file__).resolve().parents[2])


def bootstrap_block(root: str | None = None) -> str:
    """Put benchsmith on the worker's disk.

    A remote worker cannot be handed benchsmith through `--skills`. SkillsService
    serves the SKILL.md body only -- nested files are withheld from remote nodes
    unless --skill-materialization is on, and it is off by default and not
    exposed on the session CLI. benchsmith is a package, not prose, so a
    body-only delivery yields a worker that has the judgement and none of the
    commands. It has to arrive as files.
    """
    root = root or benchsmith_root()
    return (
        f"**This work runs only on `{HOST}`.** benchsmith is a package installed there. It is not "
        "delivered with this skill and it cannot be fetched: the repository is private, so a "
        "fresh runtime's clone returns HTTP 403. If you are not on that host, attach it first.\n\n"
        "```bash\n"
        f"{PROXY_PREAMBLE}\n"
        f'for c in "$BENCHSMITH_BIN" "$HOME/.claude/skills/benchsmith/bin/benchsmith" '
        f'"$(command -v benchsmith 2>/dev/null)" "{root}/bin/benchsmith"; do\n'
        '  [ -n "$c" ] && [ -x "$c" ] && export BENCHSMITH_BIN="$c" && break\n'
        'done\n'
        '[ -x "$BENCHSMITH_BIN" ] || { echo "NOT ON THE HOST"; exit 1; }\n'
        '"$BENCHSMITH_BIN" preflight --json\n'
        "```\n\n"
        f"If `$BENCHSMITH_BIN` is missing you are not on `{HOST}`. Attach that devserver and retry "
        "once. If you still cannot reach it, report `state=blocked` saying so and stop -- being "
        "off-host is the finding, not something to work around, and cloning from GitHub will only "
        "produce a misleading 403.\n\n"
        "**Never ask for a credential.** Not a token, cookie, OIDC value, API key, or a file "
        "containing one; not to unblock yourself, not read-only, not once. A credential pasted "
        "into a session is stored in that conversation and its journal. If `codimango` cannot "
        "authenticate you are in a fresh container rather than on the host that holds the "
        "credential -- attach the host, or report `state=blocked` saying you are not on an "
        "authenticated one. Do not open a login page or mint a token.\n\n"
        "Never improvise a substitute for the gate: an ungated push is the failure this exists "
        "to prevent.\n\n"
    )


def scaffold_prompt(info: dict, repo: str, slug: str) -> str:
    """The brief for an IDEA, which is not a task yet.

    A GSD card has no slug, no directory and no oracle. Handing it to the repair
    loop points a worker at a checkout with nothing in it -- observed on
    T288273925, where dispatch bound the card number as though it were a task
    name. An idea goes through intake and scaffolding, and intake can say KILL.
    """
    return (
        f"This is an IDEA CARD, not an existing task. {info.get('gsd', '')} has no task "
        f"directory, no oracle and no measurements. Do NOT run the repair or hardening "
        f"loop on it.\n\n"
        f"Card: {info.get('title', '')}\n"
        f"Track: {info.get('track') or 'unknown — determine it from the card before scaffolding'}\n"
        f"Scaffold into: {repo}\n"
        f"Proposed task name: {slug}\n"
        f"  This name is a PROPOSAL derived from the card title. It is permanent once "
        f"scaffolded, so if it misdescribes the task, choose a better one and say why "
        f"in your handoff.\n\n"
        f"--- card body ---\n{(info.get('description') or '')[:2500]}\n--- end card ---\n\n"
        "Route: §3 intake first — run the kill tests and reach GO / DERISK / KILL. "
        "**A KILL is a successful outcome**; report it and stop rather than scaffolding "
        "something the screen rejected. On GO, scaffold from the official skeleton for the "
        "track (STEP S), then author it (§4).\n\n"
        "**A bare skeleton is not done.** Generating the official tree and stopping leaves an "
        "uncommitted, ungradeable directory that looks like progress and is not: no instruction, "
        "no tests, no reference solution, nothing to measure. Finish §4 — author the task, prove "
        "the oracle passes and the unchanged base fails, commit, and run the gate. If you cannot "
        "get that far, report `state=blocked` and say where you stopped; do not report a skeleton "
        "as a result.\n\n"
        "YOU MAY NOT PUSH. Prepare the commit, run the gate, and stop.\n\n"
        "**Finalize through the CLI; do not write the handoff file directly.** Pipe the JSON "
        f"below to `$BENCHSMITH_BIN handoff --repo {shlex.quote(repo)} --task {shlex.quote(slug)}`. "
        "It durably renames the handoff before releasing this worker's exact lease.\n\n"
        "```json\n"
        '{"work_item":"...","state":"ready_to_publish|awaiting_validation|blocked|needs_human|no_change|failed",'
        '"base_sha":"...","commit_sha":"...","gate_receipt":"...","next_action":"...",'
        '"source_base_sha":"optional old base for a carried rebase",'
        '"carried_from_sha":"optional old candidate for a carried rebase",'
        '"note":"<=200 chars","validation":"...","review":"...","evidence_url":"https://..."}\n'
        "```\n\n"
        "Then say what happened in **one plain sentence**. A person reads this session, and a wall "
        "of JSON in their inbox tells them nothing. Do not print the JSON itself.\n"
    )


# The canonical reviewer per track. iOS has none in `codimango bench ai-review`,
# which is a gap to report rather than a reason to substitute another track's
# rubric.
TRACK_REVIEWERS = {
    "swe-bench-pro": "review-task-swebench-v2",
    "tbench": "review-task-tbench-v2",
    "t-bench": "review-task-tbench-v2",
    "ml-bench": "review-task-mlbench-v1",
    "mlbench": "review-task-mlbench-v1",
}


def review_prompt(task: str, repo: str, *, track: str = "", due: str = "") -> str:
    """The brief for reviewing somebody else's task.

    Reviewing is never automated to a verdict. The worker gathers evidence and
    drafts feedback; submitting it stays a human decision, exactly as publishing
    a task does.
    """
    canonical = TRACK_REVIEWERS.get(track.lower(), "")
    reviewer_line = (
        f"1. Canonical reviewer: `codimango bench ai-review {canonical} -p {repo}/{task}`\n"
        if canonical else
        f"1. **No canonical reviewer exists for track {track!r}** in `codimango bench ai-review`. "
        "Say so in your report. Do NOT substitute another track's rubric — its checks assume a "
        "different task shape and would produce confident findings about the wrong thing.\n"
    )
    return (
        f"Review the task {task} in {repo}. Track: {track or 'unknown'}."
        + (f" SLA: {due[:10]}." if due else "") + "\n\n"
        "**The repository is READ-ONLY.** Do not commit, push, edit, rerun validation, or contact "
        "the author. You are producing a review, not a repair.\n\n"
        "**You may not submit the review.** Draft every field; submitting stays a human decision.\n\n"
        "Run both, in this order:\n"
        + reviewer_line +
        "2. Then the second pass: read `~/.claude/skills/codimango-review-critic/SKILL.md` and "
        "follow it. It reviews the reviewer — it verifies each finding against the exact task "
        "revision, reads prior reviews, and preserves unverified caveats rather than dropping "
        "them.\n\n"
        "Bind every finding to the exact revision you read. A finding cited against a commit the "
        "task has moved past is worse than no finding: the author cannot reproduce it, and "
        "reconciling that costs more than the review saved.\n\n"
        "End the review with one machine-readable line produced from your actual evidence: "
        "`BENCHSMITH_CRITIC_RECEIPT={\"task_id\":\"...\",\"sha\":\"<full SHA>\","
        "\"critic_version\":\"...\",\"session_id\":\"<this session>\","
        "\"decision\":\"Accept|Request changes|Reject\","
        "\"evidence_digest\":\"<64 lowercase hex>\",\"timestamp\":\"<ISO-8601>\"}`. "
        "Benchsmith fetches this line from the terminal session itself; do not write a substitute "
        "receipt into the task.\n\n"
        f"Write your report to `{repo}/{HANDOFF_DIR}/review-{task}.md`. Finalize the JSON handoff "
        f"through `$BENCHSMITH_BIN handoff --repo {shlex.quote(repo)} --task {shlex.quote(task)}` "
        "with `state` one of `ready_to_publish` (feedback drafted and ready for a human to "
        "submit), `blocked`, `needs_human`, or `failed`. Then say in one plain sentence what "
        "you found.\n"
    )


def worker_prompt(task: str, repo: str, *, mode: str = "harden", target: str = "hard-preferred",
                  bootstrap: bool = False, resume: bool = True) -> str:
    """The instruction a stage-3 worker gets. Deliberately narrow."""
    return (
        (bootstrap_block() if bootstrap else "")
        + (resume_block(repo, task) if resume else "")
        +
        # NOT "use the benchsmith skill": agentcloud resolves a skill name
        # through SkillsService, benchsmith is registered with neither namespace
        # it serves, and an agent told to "use the skill" burns a minute looking
        # for an alias and then asks which notebook it is. Name the file.
        f"Read the instructions at {benchsmith_root()}/SKILL.md and follow them. "
        f"There is no `/benchsmith` slash command and no Skillbook alias — the "
        f"skill is a directory on this host, and `$BENCHSMITH_BIN` above is its CLI.\n"
        f"Work on exactly one task: {task}, in {repo}.\n"
        'Run `"$BENCHSMITH_BIN" preflight` first and honour what it says degrades.\n'
        f"BENCHSMITH_MODE={mode}. BENCHSMITH_TARGET={target}.\n"
        "\n"
        'YOU MAY NOT PUSH. Prepare the commit, run `"$BENCHSMITH_BIN" gate`, and stop.\n'
        
        "Pushing is owned by the coordinator's single publish lane; a worker that "
        "pushes creates the contention this design exists to remove.\n"
        "\n"
        "Do not work on any other task, and do not read another task's files.\n\n"
        "**Finalize through the CLI; do not write the handoff file directly.** Pipe the JSON "
        f"below to `$BENCHSMITH_BIN handoff --repo {shlex.quote(repo)} --task {shlex.quote(task)}`. "
        "It durably renames the handoff before releasing this worker's exact lease.\n\n"
        "```json\n"
        '{"work_item":"...","state":"ready_to_publish|awaiting_validation|blocked|needs_human|no_change|failed",'
        '"base_sha":"...","commit_sha":"...","gate_receipt":"...","next_action":"...",'
        '"source_base_sha":"optional old base for a carried rebase",'
        '"carried_from_sha":"optional old candidate for a carried rebase",'
        '"note":"<=200 chars","validation":"...","review":"...","evidence_url":"https://..."}\n'
        "```\n\n"
        "Then say what happened in **one plain sentence**. A person reads this session, and a wall "
        "of JSON in their inbox tells them nothing. Do not print the JSON itself.\n"
    )


def _placeholder(task: str) -> str:
    """Refuse a name that is a placeholder rather than a task."""
    name = (task or "").strip()
    if not name:
        return "no task name given"
    if name.startswith("<") or name.endswith(">") or name.startswith("$"):
        return f"task name {name!r} looks like an unsubstituted placeholder"
    if name.lower() in PLACEHOLDERS:
        return (f"task name {name!r} is a placeholder, not a task. Resolve the reference first: "
                "`benchsmith resolve <what the user gave you>`")
    return ""


def _exists(repo: str, task: str) -> bool:
    return (Path(repo).expanduser() / task / "task.toml").is_file()


def plan(task: str, repo: str, *, backend: str = "agentcloud", harness: str = DEFAULT_HARNESS,
         skills: str | None = None, mode: str = "harden", target: str = "hard-preferred",
         bootstrap: bool | None = None, idea: dict | None = None) -> Plan:
    """Build the exact command. Runs nothing, writes nothing.

    `skills` is off by default and stays that way until benchsmith is actually
    registered with SkillsService. Passing an alias that resolves to nothing is
    worse than passing none: the session starts, the skill is silently absent,
    and the worker improvises.
    """
    if bootstrap is None:
        # A local worker already has the files. A remote one does not.
        bootstrap = backend == "agentcloud"
    mode = MODE_ALIASES.get(mode, mode)
    bad = _placeholder(task)
    if bad:
        raise DispatchRefused(bad)
    if mode not in {"review", "scaffold"} and repo and _exists(repo, task):
        from .passatk import capability

        native = capability(Path(repo) / task)
        if native.get("applicable") and not native.get("ready"):
            raise DispatchRefused(native["reason"])
    if mode == "review":
        prompt = ((bootstrap_block() if bootstrap else "")
                  + review_prompt(task, repo, track=(idea or {}).get("track", ""),
                                  due=(idea or {}).get("due", "")))
    elif mode == "scaffold":
        if not isinstance(idea, dict) or not idea:
            raise DispatchRefused(
                "scaffold mode needs the resolved card; dispatching an idea by name alone "
                "is what points a worker at an empty checkout"
            )
        if not repo:
            raise DispatchRefused(
                f"no checkout for track {idea.get('track') or 'unknown'}; scaffolding into the "
                "wrong repo is not visible until validation"
            )
        if _exists(repo, task):
            raise DispatchRefused(
                f"{repo}/{task} already exists; scaffolding over a real task would overwrite it. "
                "Choose a different name, or dispatch it as a task rather than an idea"
            )
        prompt = ((bootstrap_block() if bootstrap else "")
                  + scaffold_prompt(idea, repo, task))
    elif mode == "review":
        pass  # prompt already built above
    else:
        # A worker sent at a directory that is not there burns a session to
        # discover what one `is_file()` call already knows.
        if repo and not _exists(repo, task):
            raise DispatchRefused(
                f"{repo}/{task}/task.toml does not exist, so {task!r} is not a task in that "
                "checkout. If it is an idea, dispatch it with mode=scaffold; if the name is "
                "wrong, resolve the reference first"
            )
        prompt = worker_prompt(task, repo, mode=mode, target=target, bootstrap=bootstrap)
    notes: list[str] = []
    if bootstrap:
        notes.append("worker clones benchsmith itself; --skills cannot deliver a package")

    if backend == "agentcloud":
        if harness and harness not in AGENTCLOUD_HARNESSES:
            raise DispatchRefused(
                f"agentcloud rejects harness {harness!r}; valid: {sorted(AGENTCLOUD_HARNESSES)}. "
                "metacode and claude are not agentcloud harnesses -- run those locally."
            )
        # Twelve identically-named sessions are unreadable in a fleet view, so
        # the title carries what tells them apart: the task, and what is being
        # done to it.
        argv = ["meta", "agentcloud.session", "create",
                "--title", f"[benchsmith][{mode_label(mode)}]: {task}",
                "--message", prompt, "--output", "json"]
        if harness:
            argv[3:3] = ["--harness", harness]
        if skills:
            argv += ["--skills", skills]
        notes.append("poll with `meta agentcloud.session poll --session-id <id>`")
    elif backend == "codex":
        argv = ["codex", "exec", prompt]
        notes.append("blocks until the worker finishes; run detached for concurrency")
    elif backend == "metacode":
        # The 1P hop only. Never a task worker: it cannot be an agentcloud
        # session, and the guard denies it for the guarded artifact classes.
        argv = ["metacode", "run", "--yolo", "-m", "meta/muse-spark-1.3-internal", prompt]
        notes.append("1P delegation only -- not a task worker")
    else:
        raise DispatchRefused(f"unknown backend {backend!r}")

    return Plan(backend=backend, argv=argv, task=task, publishing=False, notes=notes)


def run(p: Plan, *, apply: bool = False, timeout: int = 900, runner=None) -> dict:
    """Execute a plan. Refuses unless explicitly applied.

    `runner` exists so a test can exercise this without starting anything. It is
    not a nicety: the mutation harness removes each guard in turn and then calls
    this, so with no injection point a suite run spawned real AgentCloud
    sessions -- dozens of them, titled from fixture task names.
    """
    if not apply:
        raise DispatchRefused("dispatch not applied; pass apply=True to actually start a worker")
    if p.publishing:
        raise DispatchRefused("stage 3 workers are non-publishing; the publish lane is stage 4")
    if runner is None and os.environ.get("BENCHSMITH_NO_DISPATCH"):
        # A blunt second line of defence. A guard that can be mutated away is
        # exactly the guard a mutation harness will mutate away, so the harness
        # must not be able to reach a real subprocess at all.
        raise DispatchRefused(
            "BENCHSMITH_NO_DISPATCH is set: refusing to start a real worker. "
            "Pass an explicit runner if you meant to exercise this in a test."
        )
    try:
        if runner is not None:
            return runner(p)
        r = subprocess.run(p.argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "task": p.task, "error": f"{type(e).__name__}: {e}"}
    return {"ok": r.returncode == 0, "task": p.task, "returncode": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}


def session_id(stdout: str) -> str:
    """The id `agentcloud.session create` returned, so a caller can follow it."""
    try:
        doc = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    except ValueError:
        return ""
    return str(doc.get("session_id") or "")


def poll_session(sid: str, *, limit: int = 400, max_pages: int = 20,
                 runner=None) -> tuple[list[dict], str]:
    """Read a session journal to the end.

    Seq numbers are NOT contiguous, so the end cannot be computed -- it has to be
    walked. A single page looks complete and is usually the beginning.
    """
    def _call(argv):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout, r.stderr

    call = runner or _call
    events: list[dict] = []
    cursor, pages = "", 0
    while pages < max_pages:
        argv = ["meta", "agentcloud.session", "poll", "--session-id", sid,
                "--output", "json", "--limit", str(limit)]
        if cursor:
            argv += ["--cursor", cursor]
        code, out, err = call(argv)
        if code != 0:
            return events, f"poll failed: {err.strip()[:160]}"
        docs, i, dec = [], 0, json.JSONDecoder()
        while i < len(out):
            while i < len(out) and out[i] in " \n\r\t":
                i += 1
            if i >= len(out):
                break
            try:
                obj, i = dec.raw_decode(out, i)
            except ValueError:
                break
            docs.append(obj)
        if docs and isinstance(docs[0], list):
            events.extend(docs[0])
        meta = docs[1] if len(docs) > 1 and isinstance(docs[1], dict) else {}
        if str(meta.get("has_more")) != "yes":
            return events, ""
        cursor, pages = str(meta.get("next_cursor") or ""), pages + 1
        if not cursor:
            return events, ""
    return events, f"stopped after {max_pages} pages; the journal may be longer"


ORCHESTRATOR_TITLE = "[benchsmith] orchestrator"


def rename(sid: str, title: str, *, runner=None) -> dict:
    """Retitle a session. Used to mark the coordinator's own."""
    argv = ["meta", "agentcloud.ui", "rename", "--session-id", sid, "--title", title]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=120))
    r = run(argv)
    code = r[0] if isinstance(r, tuple) else r.returncode
    return {"session": sid, "title": title, "renamed": code == 0}


def snooze(sid: str, duration: str = "24h", *, runner=None) -> dict:
    """Hide a worker session from the human inbox.

    A dispatched worker is machine-to-machine traffic. It is polled by id, so
    hiding it costs the coordinator nothing, and an inbox showing twelve of them
    shows the operator nothing either. What belongs there is work that needs a
    person -- so a worker is surfaced again only when it says so.
    """
    argv = ["meta", "agentcloud.ui", "snooze", "--session-id", sid, "--duration", duration]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=120))
    r = run(argv)
    code = r[0] if isinstance(r, tuple) else r.returncode
    return {"session": sid, "snoozed": code == 0}


def surface(sid: str, *, runner=None) -> dict:
    """Put a session back in the inbox, because a human is now the next step."""
    argv = ["meta", "agentcloud.ui", "unsnooze", "--session-id", sid]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=120))
    r = run(argv)
    code = r[0] if isinstance(r, tuple) else r.returncode
    return {"session": sid, "surfaced": code == 0}


# States where a person is the next step, so the session goes back in the inbox.
NEEDS_A_HUMAN = frozenset({"blocked", "needs_human", "failed"})

# A worker with no journal activity for this long is not thinking, it is stuck.
# Generous, because a Codimango validation wave legitimately takes a long time
# and a worker waiting on one is doing exactly the right thing.
STALL_SECONDS = 90 * 60
# Wall clock for one worker. Long, because a task can legitimately need many
# validation waves; past it the answer is a fresh worker resuming from the
# journal, not a longer leash on a session whose context is exhausted.
MAX_WORKER_HOURS = 24

_ERRORY = re.compile(r"\berror\b|\bexception\b|traceback|fatal|failed to", re.I)


def _stamp(text: str) -> float | None:
    """Parse an event timestamp without discarding its UTC offset."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        pass
    parts = raw.split()
    offsets = {"UTC": 0, "GMT": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
               "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
    if len(parts) == 3 and parts[2] in offsets:
        try:
            zone = timezone(timedelta(hours=offsets[parts[2]]))
            return datetime.strptime(" ".join(parts[:2]), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=zone
            ).timestamp()
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(raw.replace("T", " "), fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def health(sid: str, *, runner=None, now: float | None = None) -> dict:
    """Is this worker working, stuck, erroring, or done?

    A supervisor that only asks "did it produce a handoff" cannot tell a worker
    thinking hard from one that died twenty minutes ago, and both look like
    silence.
    """
    events, why = poll_session(sid, runner=runner)
    if why and not events:
        return {"session": sid, "state": "unreadable", "reason": why}
    if not events:
        return {"session": sid, "state": "starting", "events": 0}

    now = time.time() if now is None else now
    stamps = [s for s in (_stamp(e.get("created")) for e in events) if s]
    last = max(stamps) if stamps else None
    first = min(stamps) if stamps else None
    idle = (now - last) if last else None
    ran_for = (last - first) if (last and first) else None

    finished = any(str(e.get("type")) in ("run_finished", "session_archived") for e in events)
    errors = [str(e.get("type")) for e in events
              if _ERRORY.search(json.dumps(e.get("event") or "")[:2000])]

    if finished:
        state = "finished"
    elif idle is None:
        # No parseable timestamps: we cannot tell, and guessing "healthy" is how
        # a dead worker holds a slot all day.
        state = "unknown"
    elif idle < 0:
        state = "unknown"
    elif idle > STALL_SECONDS:
        state = "stalled"
    else:
        state = "working"

    return {
        "session": sid, "state": state, "events": len(events),
        "idleSeconds": None if idle is None else int(idle),
        "ranForSeconds": None if ran_for is None else int(ran_for),
        "overRuntime": bool(ran_for and ran_for > MAX_WORKER_HOURS * 3600),
        "errorEvents": len(errors),
        "reason": {
            "finished": "the run ended",
            "stalled": f"no activity for {int((idle or 0) / 60)} minutes",
            "unknown": (f"latest event is {abs(int(idle))} seconds in the future; clock skew is "
                        "unverified" if idle is not None and idle < 0 else
                        "no parseable timestamps; treat as unverified, not healthy"),
            "working": f"last activity {int((idle or 0) / 60)} minutes ago",
        }.get(state, ""),
    }


def relieve(sid: str, *, runner=None, poller=None, sleeper=time.sleep,
            delays=(0, 1, 2, 4, 8)) -> dict:
    """Terminate a worker and confirm its journal reached a terminal event."""
    argv = ["meta", "dm.session", "archive", f"--session={sid}", "--output=json"]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=120))
    r = run(argv)
    code = r[0] if isinstance(r, tuple) else r.returncode
    if code != 0:
        return {"session": sid, "relieved": False, "terminated": False,
                "reason": "termination command failed"}
    if poller is None and runner is not None:
        return {"session": sid, "relieved": False, "terminated": False,
                "reason": "termination command succeeded but no confirmation reader was supplied"}
    for attempt, delay in enumerate(delays, 1):
        if delay:
            sleeper(delay)
        events, _ = poll_session(sid, runner=poller)
        if any(str(e.get("type")) in ("run_finished", "session_archived") for e in events):
            return {"session": sid, "relieved": True, "terminated": True,
                    "attempts": attempt}
    return {"session": sid, "relieved": False, "terminated": False,
            "attempts": len(delays), "reason": "termination was not confirmed"}


def _blocked_candidate_result(
    sid: str, error: CandidateHandoffRefused, *, source: str
) -> dict:
    reason = f"publication safety rejected the candidate: {error}"
    handoff = {
        key: value for key, value in error.handoff.items() if key in HANDOFF_FIELDS
    }
    handoff.update(state="blocked", note=reason)
    return {
        "session": sid,
        "state": "blocked",
        "reason": reason,
        "handoff": handoff,
        "source": source,
        "candidateRejected": True,
    }


def collect(sid: str, *, repo: str = "", task: str = "", runner=None) -> dict:
    """Everything a supervisor needs from one worker, in one call."""
    # The handoff file first: it is what the worker was told to write, it
    # survives a lost pipe, and reading it costs one stat instead of walking a
    # journal. Session text is the fallback, not the contract.
    if repo and task:
        path = Path(repo).expanduser() / HANDOFF_DIR / f"{task}.json"
        try:
            doc = parse_handoff(path.read_text())
            final = finalize_handoff(repo, task, {**doc, "session": doc.get("session") or sid})
            durable = parse_handoff(path.read_text())
            return {"session": sid, "state": "done", "handoff": durable, "source": "file",
                    "finalization": final}
        except CandidateHandoffRefused as error:
            return _blocked_candidate_result(sid, error, source="file")
        except (OSError, DispatchRefused):
            pass

    events, why = poll_session(sid, runner=runner)
    if not events:
        # An empty journal moments after create is a session that has not
        # started emitting yet. Calling that unreadable makes a supervisor
        # abandon a healthy worker on a race it should simply wait out.
        return {"session": sid,
                "state": "unreadable" if why else "starting",
                "reason": why or "no events yet; the session is still starting — poll again",
                "retryable": not why}

    finished = any(str(e.get("type")) in ("run_finished", "session_archived") for e in events)
    texts: list[str] = []
    for e in events:
        ev = e.get("event")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except ValueError:
                continue
        if isinstance(ev, dict):
            block = ev.get("block") or {}
            txt = block.get("text") or block.get("content") or ""
            if isinstance(txt, str) and txt.strip():
                texts.append(txt)

    # Newest first: a worker may reason about the handoff shape before emitting
    # it, and an earlier mention must not be mistaken for the answer.
    for txt in reversed(texts):
        try:
            doc = parse_handoff(txt)
            if repo and task:
                final = finalize_handoff(repo, task, {**doc, "session": doc.get("session") or sid})
                durable = parse_handoff(
                    (Path(repo) / HANDOFF_DIR / f"{task}.json").read_text()
                )
                return {"session": sid, "state": "done", "handoff": durable,
                        "source": "session", "finalization": final}
            return {"session": sid, "state": "done", "handoff": doc, "source": "session"}
        except CandidateHandoffRefused as error:
            return _blocked_candidate_result(sid, error, source="session")
        except DispatchRefused:
            continue
    return {"session": sid,
            "state": "finished-without-handoff" if finished else "running",
            "reason": ("the worker finished but emitted no valid handoff"
                       if finished else "still working"),
            "events": len(events)}


def parse_handoff(text: str) -> dict:
    """Read a worker's answer, and refuse one that is not an answer."""
    if text is None:
        raise DispatchRefused("no handoff returned")
    if len(text) > HANDOFF_LIMIT * 4:
        raise DispatchRefused(
            f"handoff is {len(text)} bytes; a worker returning a transcript instead of a "
            f"handoff is how a supervisor's context is exhausted"
        )
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise DispatchRefused("handoff contains no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise DispatchRefused(f"handoff is not valid JSON: {e}") from e
    state = str(doc.get("state") or "")
    if state not in HANDOFF_STATES:
        raise DispatchRefused(f"unknown handoff state {state!r}; expected one of {sorted(HANDOFF_STATES)}")
    if state == "ready_to_publish":
        # This is the one claim a coordinator acts on, so neither half of its
        # ancestry range may be guessed.
        if not doc.get("commit_sha"):
            raise CandidateHandoffRefused(
                "state=ready_to_publish with no commit_sha", doc
            )
        if not doc.get("base_sha"):
            raise CandidateHandoffRefused(
                "state=ready_to_publish with no base_sha; unknown ancestry blocks publication",
                doc,
            )
    return {k: doc.get(k) for k in HANDOFF_FIELDS if k in doc}


def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w") as handle:
        json.dump(document, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def assignment_path(repo: str | Path, task: str) -> Path:
    return Path(repo) / ASSIGNMENT_DIR / f"{task}.json"


def write_assignment(
    repo: str | Path,
    task: str,
    session: str,
    lease_token: str,
    lease_task: str = "",
    *,
    status_repo: str = "",
    submission_id: str = "",
) -> Path:
    path = assignment_path(repo, task)
    _atomic_json(
        path,
        {
            "task": task,
            "session": session,
            "lease_token": lease_token,
            "lease_task": lease_task or task,
            "status_repo": status_repo,
            "submission_id": submission_id,
        },
    )
    return path


def finalize_handoff(
    repo: str | Path,
    task: str,
    document: dict,
    *,
    remote: str = "origin",
    branch: str = "main",
    lease_runner=None,
) -> dict:
    """Persist a terminal handoff, then release its exact lease by CAS.

    The two operations cannot be atomic across a filesystem and a Git remote.
    Their order makes every crash safe: a durable handoff with a live lease is
    recoverable, and a missing lease is observed only after the handoff exists.
    """
    parsed = parse_handoff(json.dumps(document))
    state = str(parsed.get("state") or "")
    if state == "in_progress":
        raise DispatchRefused("in_progress is not a terminal handoff")

    assignment = {}
    try:
        assignment = json.loads(assignment_path(repo, task).read_text())
    except (OSError, ValueError):
        pass
    for field in ("session", "lease_token", "lease_task", "status_repo", "submission_id"):
        assigned = str(assignment.get(field) or "")
        supplied = str(parsed.get(field) or "")
        if assigned and supplied and assigned != supplied:
            raise DispatchRefused(f"handoff {field} does not match the dispatched worker")
        if assigned:
            parsed[field] = assigned
        elif field in {"status_repo", "submission_id"}:
            # These choose where shared state is written and which submission is
            # linked. They are coordinator metadata, not worker-authored claims.
            parsed.pop(field, None)

    if state == "ready_to_publish":
        try:
            candidate_mod.verify_handoff(
                Path(repo), task, parsed, remote=remote, branch=branch
            )
        except candidate_mod.CandidateRejected as error:
            raise CandidateHandoffRefused(str(error), parsed) from error

    finalization = dict(parsed.get("finalization") or {})
    finalization["phase"] = "durable"
    parsed["finalization"] = finalization
    path = Path(repo) / HANDOFF_DIR / f"{task}.json"
    _atomic_json(path, parsed)

    token = str(parsed.get("lease_token") or "")
    if not token:
        return {"ok": True, "path": str(path), "phase": "durable", "released": False,
                "reason": "legacy handoff has no lease token; release requires reconciliation"}

    from .remote_lease import RemoteLease

    lease = RemoteLease(str(parsed.get("lease_task") or task), repo, remote=remote,
                        runner=lease_runner)
    released = lease.release(token)
    if not released["released"]:
        return {"ok": False, "path": str(path), "phase": "durable", "released": False,
                "reason": released["reason"]}

    parsed["finalization"] = {"phase": "released"}
    _atomic_json(path, parsed)
    return {"ok": True, "path": str(path), "phase": "released", "released": True,
            "session": parsed.get("session"), "leaseToken": token}
