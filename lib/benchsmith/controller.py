"""Fenced coordinator ownership and fleet admission checks.

A coordinator epoch is a capability: every mutating fleet operation must prove
that the canonical controller record still names its exact session and epoch.
Expiry alone never authorizes takeover while the prior Agentcloud run is still
open or cannot be inspected.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import hold
from . import safety

SCHEMA_VERSION = 2
STATE_DIR = Path(".benchsmith") / "fleet"
STATE_FILE = "controller.json"
LOCK_FILE = ".controller.lock"
DEFAULT_TTL_SECONDS = 20 * 60
DEFAULT_MIN_FREE_GB = 25.0


class ControllerRefused(ValueError):
    """The caller cannot safely act as the fleet coordinator."""


class ControllerIndeterminate(ControllerRefused):
    """A durable write may have committed even though its sync failed."""


def implementation() -> dict:
    try:
        return safety.snapshot("controller_dispatch")
    except safety.SafetyRefused as error:
        raise ControllerRefused(str(error)) from error


def _root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def paths(status_root: str | Path) -> tuple[Path, Path]:
    directory = _root(status_root) / STATE_DIR
    return directory / STATE_FILE, directory / LOCK_FILE


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ControllerRefused(f"controller record is unreadable: {error}") from error
    if not isinstance(value, dict) or value.get("schemaVersion") not in {1, SCHEMA_VERSION}:
        raise ControllerRefused("controller record has an unsupported schema")
    return value


def _sync_parent(path: Path, operation: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except OSError as error:
        raise ControllerIndeterminate(
            f"{operation} may have committed; reconcile with `controller verify`"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_parent(path, "controller write")
    finally:
        temporary.unlink(missing_ok=True)


def _run_inspect(
    session_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool | None:
    run = runner or subprocess.run
    try:
        result = run(
            ["agentcloudctl", "inspect", "-s", session_id],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    states = {
        line.split(":", 1)[1].strip().lower()
        for line in result.stdout.splitlines()
        if line.startswith("running:")
    }
    if states == {"true"}:
        return True
    if states == {"false"}:
        return False
    return None


def _identity(
    repo: str | Path, status_root: str | Path, session_id: str
) -> tuple[str, str, str]:
    canonical_repo = str(_root(repo))
    canonical_status = str(_root(status_root))
    session = str(session_id or "").strip()
    if not session:
        raise ControllerRefused("controller session id is required")
    return canonical_repo, canonical_status, session


def acquire(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
    inspector: Callable[[str], bool | None] | None = None,
    epoch_factory: Callable[[], str] | None = None,
) -> dict:
    """Acquire one epoch, refusing expiry-only takeover of a live owner."""
    if ttl_seconds < 60:
        raise ControllerRefused("controller TTL must be at least 60 seconds")
    canonical_repo, canonical_status, session = _identity(
        repo, status_root, session_id
    )
    source_identity = implementation()
    state_path, lock_path = paths(canonical_status)
    timestamp = time.time() if now is None else float(now)
    inspect = inspector or (lambda owner: _run_inspect(owner))
    make_epoch = epoch_factory or (lambda: uuid.uuid4().hex)

    replacing_legacy = False
    with _locked(lock_path):
        current = _load(state_path)
        if current:
            if str(current.get("canonicalRepo") or "") != canonical_repo:
                raise ControllerRefused(
                    "controller record belongs to a different canonical repository"
                )
            if str(current.get("statusRoot") or "") != canonical_status:
                raise ControllerRefused(
                    "controller record belongs to a different canonical status root"
                )
            owner = str(current.get("sessionId") or "")
            expires = float(current.get("expiresAt") or 0)
            replacing_legacy = bool(
                owner == session
                and not current.get("controllerDispatchFingerprint")
            )
            if expires > timestamp and not replacing_legacy:
                if owner != session:
                    raise ControllerRefused(
                        f"controller is owned by live epoch session {owner}"
                    )
                raise ControllerRefused(
                    "this session already owns a live controller epoch; use renew with the epoch"
                )
        snapshot = dict(current)

    owner = str(snapshot.get("sessionId") or "")
    if owner and not replacing_legacy:
        running = inspect(owner)
        if running is True:
            raise ControllerRefused(
                f"expired controller owner {owner} is still run-open; explicit handoff required"
            )
        if running is None:
            raise ControllerRefused(
                f"expired controller owner {owner} could not be inspected; refusing takeover"
            )

    with _locked(lock_path):
        if _load(state_path) != snapshot:
            raise ControllerRefused(
                "controller changed while its expired owner was inspected; retry"
            )
        committed_at = time.time() if now is None else float(now)
        epoch = make_epoch()
        if not epoch:
            raise ControllerRefused("controller epoch generator returned an empty value")
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "canonicalRepo": canonical_repo,
            "statusRoot": canonical_status,
            "sessionId": session,
            "epoch": epoch,
            "controllerDispatchFingerprint": source_identity["digest"],
            "controllerDispatchSourceHead": source_identity["sourceHead"],
            "acquiredAt": committed_at,
            "renewedAt": committed_at,
            "expiresAt": committed_at + ttl_seconds,
        }
        _atomic_write(state_path, document)
    return {"acquired": True, "reused": False, **document}


def _verify_locked(
    state_path: Path,
    *,
    canonical_repo: str,
    canonical_status: str,
    session: str,
    epoch: str,
    now: float,
    controller_digest: str,
    allow_expired: bool = False,
) -> dict:
    current = _load(state_path)
    if not current:
        raise ControllerRefused("no controller epoch is recorded")
    expected = {
        "canonicalRepo": canonical_repo,
        "statusRoot": canonical_status,
        "sessionId": session,
        "epoch": str(epoch or ""),
    }
    for field, value in expected.items():
        if not value or str(current.get(field) or "") != value:
            raise ControllerRefused(f"stale or foreign controller {field}")
    recorded_digest = str(current.get("controllerDispatchFingerprint") or "")
    if not recorded_digest:
        raise ControllerRefused("controller record has no safety fingerprint; reacquire controller")
    if recorded_digest != controller_digest:
        raise ControllerRefused(
            "controller/dispatch implementation changed; reacquire controller"
        )
    if not allow_expired and float(current.get("expiresAt") or 0) <= now:
        raise ControllerRefused("controller epoch has expired")
    return current


def verify(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    now: float | None = None,
) -> dict:
    canonical_repo, canonical_status, session = _identity(
        repo, status_root, session_id
    )
    controller_digest = implementation()["digest"]
    state_path, lock_path = paths(canonical_status)
    with _locked(lock_path):
        current = _verify_locked(
            state_path,
            canonical_repo=canonical_repo,
            canonical_status=canonical_status,
            session=session,
            epoch=epoch,
            now=time.time() if now is None else float(now),
            controller_digest=controller_digest,
        )
    return {"verified": True, **current}


def renew(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> dict:
    if ttl_seconds < 60:
        raise ControllerRefused("controller TTL must be at least 60 seconds")
    canonical_repo, canonical_status, session = _identity(
        repo, status_root, session_id
    )
    controller_digest = implementation()["digest"]
    state_path, lock_path = paths(canonical_status)
    timestamp = time.time() if now is None else float(now)
    with _locked(lock_path):
        current = _verify_locked(
            state_path,
            canonical_repo=canonical_repo,
            canonical_status=canonical_status,
            session=session,
            epoch=epoch,
            now=timestamp,
            controller_digest=controller_digest,
            allow_expired=True,
        )
        current["renewedAt"] = timestamp
        current["expiresAt"] = timestamp + ttl_seconds
        _atomic_write(state_path, current)
    return {"renewed": True, **current}


def release(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    now: float | None = None,
) -> dict:
    canonical_repo, canonical_status, session = _identity(
        repo, status_root, session_id
    )
    controller_digest = implementation()["digest"]
    state_path, lock_path = paths(canonical_status)
    with _locked(lock_path):
        current = _verify_locked(
            state_path,
            canonical_repo=canonical_repo,
            canonical_status=canonical_status,
            session=session,
            epoch=epoch,
            now=time.time() if now is None else float(now),
            controller_digest=controller_digest,
            allow_expired=True,
        )
        state_path.unlink()
        _sync_parent(state_path, "controller release")
    return {"released": True, **current}


def _verify_hold(
    repo: Path,
    *,
    hold_reader: Callable[[Path], dict] | None = None,
) -> dict:
    state = (hold_reader or hold.current)(repo)
    if not state.get("readable"):
        raise ControllerRefused(
            str(state.get("reason") or "repository hold is unreadable")
        )
    if not state.get("held"):
        raise ControllerRefused(
            str(state.get("reason") or "repository hold is absent or expired")
        )
    expected_holder = os.environ.get("USER", "")
    if not expected_holder or state.get("holder") != expected_holder:
        raise ControllerRefused(
            f"repository hold belongs to {state.get('holder') or 'unknown'}, not {expected_holder or 'this user'}"
        )
    return state


def verify_write(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    now: float | None = None,
    hold_reader: Callable[[Path], dict] | None = None,
) -> dict:
    controller = verify(repo, status_root, session_id, epoch, now=now)
    hold_state = _verify_hold(_root(repo), hold_reader=hold_reader)
    return {"controller": controller, "hold": hold_state}


@contextmanager
def write_fence(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    now: float | None = None,
    hold_reader: Callable[[Path], dict] | None = None,
):
    """Hold the controller lock across one coordinator mutation window."""
    canonical_repo, canonical_status, session = _identity(
        repo, status_root, session_id
    )
    controller_digest = implementation()["digest"]
    state_path, lock_path = paths(canonical_status)
    with _locked(lock_path):
        current = _verify_locked(
            state_path,
            canonical_repo=canonical_repo,
            canonical_status=canonical_status,
            session=session,
            epoch=epoch,
            now=time.time() if now is None else float(now),
            controller_digest=controller_digest,
        )
        hold_state = _verify_hold(_root(repo), hold_reader=hold_reader)
        yield {"controller": current, "hold": hold_state}


def _verify_disk(
    path: Path,
    minimum_free_gb: float,
    *,
    disk_usage: Callable[[str | Path], object] = shutil.disk_usage,
) -> int:
    usage = disk_usage(path)
    free_bytes = int(getattr(usage, "free"))
    required_bytes = int(float(minimum_free_gb) * 1024**3)
    if free_bytes < required_bytes:
        raise ControllerRefused(
            f"only {free_bytes / 1024**3:.1f} GiB free at {path}; "
            f"controller requires {minimum_free_gb:.1f} GiB"
        )
    return free_bytes


def verify_target(
    repo: str | Path,
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    hold_reader: Callable[[Path], dict] | None = None,
    disk_usage: Callable[[str | Path], object] = shutil.disk_usage,
) -> dict:
    target = _root(repo)
    return {
        "repo": str(target),
        "hold": _verify_hold(target, hold_reader=hold_reader),
        "freeBytes": _verify_disk(target, min_free_gb, disk_usage=disk_usage),
    }


def admit(
    repo: str | Path,
    status_root: str | Path,
    session_id: str,
    epoch: str,
    *,
    canary_image: str,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    now: float | None = None,
    hold_reader: Callable[[Path], dict] | None = None,
    disk_usage: Callable[[str | Path], object] = shutil.disk_usage,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict:
    """Verify the epoch, hold, disk reserve, and a real local container start."""
    checked = verify_write(
        repo,
        status_root,
        session_id,
        epoch,
        now=now,
        hold_reader=hold_reader,
    )
    if min_free_gb < 0:
        raise ControllerRefused("minimum free disk cannot be negative")
    canonical_repo = _root(repo)
    canonical_status = _root(status_root)
    free_by_path = {
        str(canonical_repo): _verify_disk(
            canonical_repo, min_free_gb, disk_usage=disk_usage
        )
    }
    if canonical_status != canonical_repo:
        free_by_path[str(canonical_status)] = _verify_disk(
            canonical_status, min_free_gb, disk_usage=disk_usage
        )
    required_bytes = int(float(min_free_gb) * 1024**3)
    image = str(canary_image or "").strip()
    if not image:
        raise ControllerRefused("a preloaded container canary image is required")
    run = runner or subprocess.run
    for argv, label in (
        (["docker", "image", "inspect", image], "container canary image is unavailable locally"),
        (
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/bin/true",
                image,
            ],
            "container start canary failed",
        ),
    ):
        try:
            result = run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ControllerRefused(f"{label}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[:240]
            raise ControllerRefused(f"{label}: {detail}")
    return {
        "ok": True,
        "canonicalRepo": str(_root(repo)),
        "statusRoot": str(_root(status_root)),
        "sessionId": session_id,
        "epoch": epoch,
        "controllerDispatchFingerprint": checked["controller"]["controllerDispatchFingerprint"],
        "freeBytes": min(free_by_path.values()),
        "freeBytesByPath": free_by_path,
        "minimumFreeBytes": required_bytes,
        "canaryImage": image,
        "hold": checked["hold"],
    }
