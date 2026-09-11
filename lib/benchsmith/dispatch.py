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
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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

# Where a worker gets benchsmith from when it is not already on disk.
BENCHSMITH_ORIGIN = "https://github.com/kngreen/benchsmith.git"

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
HANDOFF_FIELDS = ("work_item", "state", "base_sha", "commit_sha", "gate_receipt", "next_action", "note")
HANDOFF_STATES = frozenset({
    "ready_to_publish", "blocked", "needs_human", "no_change", "failed", "in_progress",
})


class DispatchRefused(Exception):
    """The dispatch is not safe or not possible. The message is the reason."""


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
        "First, make benchsmith available:\n"
        "```bash\n"
        f"{PROXY_PREAMBLE}\n"
        f"export BENCHSMITH_BIN={root}/bin/benchsmith\n"
        f'test -x "$BENCHSMITH_BIN" || {{ git clone {BENCHSMITH_ORIGIN} /tmp/benchsmith '
        f'&& export BENCHSMITH_BIN=/tmp/benchsmith/bin/benchsmith; }}\n'
        '"$BENCHSMITH_BIN" preflight --json\n'
        "```\n"
        "If the clone fails, stop and report state=blocked. Do not improvise a "
        "substitute for the gate: an ungated push is the failure this exists to "
        "prevent.\n\n"
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
        "Do not work on any other task, and do not read another task's files.\n"
        "Finish by emitting ONLY this JSON, under 4096 bytes:\n"
        '{"work_item":"...","state":"ready_to_publish|blocked|needs_human|no_change|failed",'
        '"base_sha":"...","commit_sha":"...","gate_receipt":"...","next_action":"...","note":"<=200 chars"}'
    )


def plan(task: str, repo: str, *, backend: str = "agentcloud", harness: str = DEFAULT_HARNESS,
         skills: str | None = None, mode: str = "harden", target: str = "hard-preferred",
         bootstrap: bool | None = None) -> Plan:
    """Build the exact command. Runs nothing, writes nothing.

    `skills` is off by default and stays that way until benchsmith is actually
    registered with SkillsService. Passing an alias that resolves to nothing is
    worse than passing none: the session starts, the skill is silently absent,
    and the worker improvises.
    """
    if bootstrap is None:
        # A local worker already has the files. A remote one does not.
        bootstrap = backend == "agentcloud"
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
        argv = ["meta", "agentcloud.session", "create",
                "--title", f"benchsmith: {task}", "--message", prompt, "--output", "json"]
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


def run(p: Plan, *, apply: bool = False, timeout: int = 900) -> dict:
    """Execute a plan. Refuses unless explicitly applied."""
    if not apply:
        raise DispatchRefused("dispatch not applied; pass apply=True to actually start a worker")
    if p.publishing:
        raise DispatchRefused("stage 3 workers are non-publishing; the publish lane is stage 4")
    try:
        r = subprocess.run(p.argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "task": p.task, "error": f"{type(e).__name__}: {e}"}
    return {"ok": r.returncode == 0, "task": p.task, "returncode": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}


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
    if state == "ready_to_publish" and not doc.get("commit_sha"):
        # The one claim a coordinator acts on, so it is the one that must not be
        # takeable on trust.
        raise DispatchRefused("state=ready_to_publish with no commit_sha")
    return {k: doc.get(k) for k in HANDOFF_FIELDS if k in doc}
