"""Platform access: discover the CLI, read uncached, verify identity.

Two rules drive every design choice here.

**Never hardcode a subcommand.** The installed binary announces itself as legacy
and points at a replacement that keeps the `codimango` command and changes the
shape. A hardcoded call is a time bomb, so the surface is probed once and cached
for the process.

**Never trust a name lookup.** The legacy read surface is name-only
(`tasks show [OPTIONS] NAME`), so identity cannot be bound at the call. It is
verified on the way back instead: a record whose id/uuid disagrees with the one
recorded at intake is another task, and its numbers are not yours.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 180


class Unresolved(Exception):
    """A required capability could not be resolved.

    Raised rather than papered over: a slot we cannot run is declared and
    degraded, never substituted with a command that was never executed.
    """


class IdentityMismatch(Exception):
    """A lookup returned a record for a different task."""


@dataclass
class Surface:
    """The resolved shape of the installed CLI."""

    binary: str
    site: tuple[str, ...] = ()  # e.g. ("--site", "nest")
    task_show: tuple[str, ...] = ()
    tasks_list: tuple[str, ...] = ()
    jobs_list: tuple[str, ...] = ()
    trials_list: tuple[str, ...] = ()
    trials_artifacts: tuple[str, ...] = ()
    supports_no_cache: bool = False
    supports_agentic_review: bool = False
    supports_offset: bool = False
    legacy: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "binary": self.binary,
            "site": list(self.site),
            "tasks_list": list(self.tasks_list),
            "legacy": self.legacy,
            "supportsNoCache": self.supports_no_cache,
            "supportsAgenticReview": self.supports_agentic_review,
            "supportsOffset": self.supports_offset,
            "notes": self.notes,
        }


def _run(argv: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _commands(help_text: str) -> set[str]:
    """First token of each line under a `Commands:` block.

    Matched against the parsed list, never a substring of the whole help text:
    "task" appears in the *description* of unrelated commands, so a substring
    test resolves a command that does not exist and fails only at call time.
    """
    out: set[str] = set()
    in_block = False
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped.lower().rstrip(":") in {"commands", "available commands"}:
            in_block = True
            continue
        if in_block:
            if not line.startswith((" ", "\t")):
                if stripped:
                    in_block = False
                continue
            if stripped and not stripped.startswith("-"):
                out.add(stripped.split()[0])
    return out


def _help(binary: str, *words: str) -> str:
    try:
        r = _run([binary, *words, "--help"], timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    # Deprecation and migration notices may be printed to stderr even when
    # `--help` exits successfully. They are part of capability discovery.
    return (r.stdout or "") + (r.stderr or "")


def discover(binary: str | None = None, site: str | None = None) -> Surface:
    """Probe the CLI once and resolve every read we need.

    Probing costs a few seconds at session start and removes an entire class of
    silent breakage when the platform ships its replacement CLI.
    """
    configured_binary = binary or os.environ.get("BENCHSMITH_CODIMANGO")
    binary = configured_binary or "codimango"
    path = shutil.which(binary)
    if not path:
        raise Unresolved(f"{binary} is not on PATH")

    site = site or os.environ.get("BENCHSMITH_SITE", "nest")
    root = _help(binary)
    replacement_note = ""
    if configured_binary is None and "THIS IS THE LEGACY CODIMANGO CLI" in root.upper():
        replacement = "/usr/local/bin/codimango"
        replacement_path = shutil.which(replacement)
        replacement_root = _help(replacement) if replacement_path and replacement_path != path else ""
        replacement_commands = _commands(replacement_root)
        if {"task", "job", "trial"} <= replacement_commands:
            binary = replacement
            path = replacement_path
            root = replacement_root
            replacement_note = (
                f"PATH resolved the retired CLI at {shutil.which('codimango')}; "
                f"using {replacement}"
            )
    surface = Surface(binary=binary)
    surface.legacy = "LEGACY" in root.upper()
    if replacement_note:
        surface.notes.append(replacement_note)

    # Site selection is global on the legacy CLI and may vanish on the new one.
    if "--site" in root:
        surface.site = ("--site", site)
    else:
        surface.notes.append("no --site flag; using the CLI's configured default")

    # Data surface: `api tasks|jobs|trials` on the legacy CLI, bare
    # `task|job|trial` on the replacement. Probe both; assume neither.
    #
    # Match against the parsed command list, never a substring of the whole help
    # text: "task" appears in the *description* of unrelated commands ("the large
    # task assets a trial downloads"), so `"task" in root` is satisfied by a CLI
    # that has no `task` command at all, and the mismatch only surfaces at call
    # time. That is exactly the substitution this module exists to refuse.
    commands = _commands(root)

    if "api" in commands:
        api = _commands(_help(binary, "api"))
        if {"tasks", "jobs", "trials"} <= api:
            surface.task_show = ("api", "tasks", "show")
            surface.tasks_list = ("api", "tasks", "list")
            surface.jobs_list = ("api", "jobs", "list")
            surface.trials_list = ("api", "trials", "list")
            surface.trials_artifacts = ("api", "trials", "artifacts")
        elif "tasks" in api:
            surface.notes.append(f"`api` present but incomplete: {sorted(api & {'tasks', 'jobs', 'trials'})}")

    if not surface.task_show:
        missing = {"task", "job", "trial"} - commands
        if missing:
            raise Unresolved(
                f"no usable read surface: `api` absent or incomplete, and the bare form is "
                f"missing {sorted(missing)}. Commands seen: {sorted(commands)}"
            )
        surface.task_show = ("task", "show")
        surface.tasks_list = ("task", "list")
        surface.jobs_list = ("job", "list")
        surface.trials_list = ("trial", "list")
        surface.trials_artifacts = ("trial", "artifacts")

    show_help = _help(binary, *surface.task_show)
    surface.supports_no_cache = "--no-cache" in show_help
    if not surface.supports_no_cache:
        surface.notes.append("no --no-cache flag; reads may be served from cache")
    jobs_help = _help(binary, *surface.jobs_list)
    surface.supports_agentic_review = "--include-agentic-review" in jobs_help
    surface.supports_offset = "--offset" in jobs_help
    return surface


# What the offline smoke fixtures assume about the CLI. Smoke stubs `_help`, so
# it verifies PARSING and never REALITY: rename a subcommand upstream and smoke
# stays green while the first failure is at call time in a live round. This is
# the contract the stubs encode, checked against the live binary by
# `verify_surface()` so the drift is detectable rather than silent.
SMOKE_CONTRACT = {
    "legacy": {"task_show": ("api", "tasks", "show"), "jobs_list": ("api", "jobs", "list")},
    "bare": {"task_show": ("task", "show"), "jobs_list": ("job", "list")},
}


def verify_surface(surface: Surface | None = None) -> dict:
    """Does the live CLI still match a shape the offline fixtures know?

    Network-dependent, so it is deliberately NOT in the offline suite -- it runs
    from `preflight`, where a degraded answer is already the expected output.
    """
    try:
        surface = surface or discover()
    except Unresolved as e:
        return {"ok": False, "verdict": "UNRESOLVED", "detail": str(e), "matched": None}
    matched = next(
        (name for name, c in SMOKE_CONTRACT.items() if surface.task_show == c["task_show"]), None
    )
    drift = []
    if matched is None:
        drift.append(f"task_show={surface.task_show} matches no shape the fixtures cover")
    elif surface.jobs_list != SMOKE_CONTRACT[matched]["jobs_list"]:
        drift.append(f"jobs_list={surface.jobs_list} != {SMOKE_CONTRACT[matched]['jobs_list']}")
    return {
        "ok": not drift,
        "verdict": "MATCHES-FIXTURES" if not drift else "DRIFTED",
        "matched": matched,
        "detail": "; ".join(drift) or f"live surface matches the {matched} fixture shape",
        "surface": surface.as_dict(),
    }


@dataclass
class Identity:
    """What was recorded at intake. The basename is not part of it."""

    task_name: str
    task_id: str = ""
    task_uuid: str = ""
    source_repo: str = ""
    active_sha: str = ""


class Platform:
    """Uncached, identity-checked reads."""

    def __init__(self, surface: Surface, identity: Identity):
        self.surface = surface
        self.identity = identity

    def _argv(self, verb: tuple[str, ...], *args: str, extra: tuple[str, ...] = ()) -> list[str]:
        argv = [self.surface.binary, *self.surface.site, *verb, *args, "--json"]
        if self.surface.supports_no_cache:
            argv.append("--no-cache")
        argv.extend(extra)
        return argv

    def _json(self, argv: list[str]) -> dict | list:
        r = _run(argv)
        if r.returncode != 0:
            raise Unresolved(f"{' '.join(argv[:4])} exited {r.returncode}: {r.stderr.strip()[:300]}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise Unresolved(f"{' '.join(argv[:4])} returned unparseable JSON: {e}") from e

    def task(self) -> dict:
        """Fresh task record, identity-verified before it is returned."""
        rec = self._json(self._argv(self.surface.task_show, self.identity.task_name))
        if isinstance(rec, list):
            rec = rec[0] if rec else {}
        self.check_identity(rec)
        return rec

    def jobs(self, page_size: int = 200) -> list:
        """Every job for the task, not the first page.

        The envelope is paginated with a default limit well below what a mature
        task carries -- 87 jobs on one real task, 52 on another. Taking the first
        page silently drops the oldest rounds, and since the drop is silent the
        measurement just looks smaller. `hasMore`/`readTruncated` are checked so
        a surface that ignores `--limit` cannot fail quietly either.
        """
        extra = ("--include-agentic-review", "latest") if self.surface.supports_agentic_review else ()
        collected: list = []
        offset = 0
        while True:
            page_flags = ("--limit", str(page_size))
            if offset and self.surface.supports_offset:
                page_flags += ("--offset", str(offset))
            elif offset:
                # Cannot page without an offset flag; refuse rather than refetch
                # page one forever and silently return duplicates.
                raise Unresolved(
                    f"job list has more pages but this CLI exposes no --offset; "
                    f"got {len(collected)} of {total} jobs"
                )
            argv = self._argv(self.surface.jobs_list, self.identity.task_name,
                              extra=(*page_flags, *extra))
            out = self._json(argv)
            if isinstance(out, list):
                return out
            page = out.get("jobs") or []
            collected.extend(page)
            total = out.get("total")
            more = out.get("hasMore")
            if out.get("readTruncated"):
                raise Unresolved(
                    f"job list reports readTruncated at offset {offset}; the job set is "
                    "incomplete and any rate computed from it would be wrong"
                )
            if not more or not page:
                if isinstance(total, int) and total > len(collected):
                    raise Unresolved(
                        f"job list returned {len(collected)} of {total} jobs with hasMore={more}; "
                        "refusing a partial job set"
                    )
                return collected
            offset += len(page)
            if offset > 5000:
                raise Unresolved("job list pagination exceeded 5000 rows; refusing to loop")

    def trials(self, job_id: str) -> list:
        out = self._json(self._argv(self.surface.trials_list, job_id))
        return out if isinstance(out, list) else out.get("trials", [])

    def check_identity(self, record: dict) -> None:
        """A name lookup that returned someone else's task is not a soft error.

        Basenames collide across repos and survive renames. Where the read
        surface offers no id addressing, this is the only thing standing between
        a measurement and another task's numbers.
        """
        want_id = self.identity.task_id
        want_uuid = self.identity.task_uuid
        got_id = str(record.get("id") or record.get("taskId") or "")
        got_uuid = str(record.get("uuid") or record.get("taskUuid") or "")
        if want_id and got_id and want_id != got_id:
            raise IdentityMismatch(f"expected task id {want_id}, record says {got_id}")
        if want_uuid and got_uuid and want_uuid != got_uuid:
            raise IdentityMismatch(f"expected uuid {want_uuid}, record says {got_uuid}")
        if not (want_id or want_uuid):
            raise IdentityMismatch(
                "no TASK_ID/TASK_UUID recorded at intake; a name-only lookup cannot be trusted"
            )
