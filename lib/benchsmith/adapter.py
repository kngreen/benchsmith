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
    jobs_list: tuple[str, ...] = ()
    trials_list: tuple[str, ...] = ()
    trials_artifacts: tuple[str, ...] = ()
    supports_no_cache: bool = False
    supports_agentic_review: bool = False
    legacy: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "binary": self.binary,
            "site": list(self.site),
            "legacy": self.legacy,
            "supportsNoCache": self.supports_no_cache,
            "supportsAgenticReview": self.supports_agentic_review,
            "notes": self.notes,
        }


def _run(argv: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _help(binary: str, *words: str) -> str:
    try:
        r = _run([binary, *words, "--help"], timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "") + (r.returncode and r.stderr or "")


def discover(binary: str | None = None, site: str | None = None) -> Surface:
    """Probe the CLI once and resolve every read we need.

    Probing costs a few seconds at session start and removes an entire class of
    silent breakage when the platform ships its replacement CLI.
    """
    binary = binary or os.environ.get("BENCHSMITH_CODIMANGO", "codimango")
    path = shutil.which(binary)
    if not path:
        raise Unresolved(f"{binary} is not on PATH")

    site = site or os.environ.get("BENCHSMITH_SITE", "nest")
    root = _help(binary)
    surface = Surface(binary=binary)
    surface.legacy = "LEGACY" in root.upper()

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
        surface.jobs_list = ("job", "list")
        surface.trials_list = ("trial", "list")
        surface.trials_artifacts = ("trial", "artifacts")

    show_help = _help(binary, *surface.task_show)
    surface.supports_no_cache = "--no-cache" in show_help
    if not surface.supports_no_cache:
        surface.notes.append("no --no-cache flag; reads may be served from cache")
    surface.supports_agentic_review = "--include-agentic-review" in _help(binary, *surface.jobs_list)
    return surface


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

    def jobs(self) -> list:
        extra = ("--include-agentic-review", "latest") if self.surface.supports_agentic_review else ()
        out = self._json(self._argv(self.surface.jobs_list, self.identity.task_name, extra=extra))
        return out if isinstance(out, list) else out.get("jobs", [])

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
