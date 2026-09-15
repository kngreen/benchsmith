"""Component-scoped identity for Benchsmith safety decisions."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable

DIGEST_SCHEMA = 1

_MANIFESTS: dict[str, tuple[tuple[str, tuple[str, ...] | None], ...]] = {
    "controller_dispatch": (
        ("lib/benchsmith/controller.py", None),
        (
            "lib/benchsmith/resolve.py",
            ("Decision", "WORK_STATUSES", "work_eligibility"),
        ),
        (
            "lib/benchsmith/queue.py",
            (
                "Item",
                "Leases",
                "TERMINAL",
                "NEEDS_HUMAN",
                "TIER_REVISION",
                "TIER_DRAFT_FAILED",
                "TIER_DRAFT_PENDING",
                "TIER_DRAFT_PASSING",
                "PASSING_VALIDATION",
                "FAILING_VALIDATION",
                "build_queue",
                "read_journals",
                "_tier",
            ),
        ),
        ("lib/benchsmith/sources.py", None),
        (
            "lib/benchsmith/dispatch.py",
            (
                "AGENTCLOUD_HARNESSES",
                "DEFAULT_HARNESS",
                "HOST",
                "HANDOFF_FIELDS",
                "HANDOFF_STATES",
                "_FULL_SHA",
                "Plan",
                "benchsmith_root",
                "bootstrap_block",
                "scaffold_prompt",
                "review_prompt",
                "worker_prompt",
                "plan",
                "run",
                "session_id",
                "parse_handoff",
                "write_assignment",
                "finalize_handoff",
            ),
        ),
        (
            "lib/benchsmith/cli.py",
            (
                "_active_table_leases",
                "FleetSnapshot",
                "_discover_fleet",
                "_idle_fleet_payload",
                "cmd_dispatch",
                "cmd_scaffold",
                "cmd_reviewfleet",
                "cmd_fleet",
                "_cmd_fleet",
                "_bind_worker",
            ),
        ),
        ("lib/benchsmith/hold.py", None),
        ("lib/benchsmith/remote_lease.py", None),
        ("lib/benchsmith/worktree.py", None),
        ("lib/benchsmith/watch.py", None),
        (
            "lib/benchsmith/task_status.py",
            (
                "VALIDATING",
                "READY_TO_PUBLISH",
                "READY_GREEN",
                "STATE_DIR",
                "STATE_FILE",
                "status_root",
                "paths",
                "_load",
                "read",
                "peek",
            ),
        ),
    ),
    "gate": (
        ("lib/benchsmith/gate.py", None),
        ("lib/benchsmith/controls.py", None),
        ("lib/benchsmith/diffcheck.py", None),
        ("lib/benchsmith/hooks.py", None),
        ("lib/benchsmith/reviews.py", None),
        ("lib/benchsmith/fixtures.py", None),
        ("lib/benchsmith/journal.py", None),
        ("lib/benchsmith/contamination.py", None),
    ),
    "publish_policy": (
        ("lib/benchsmith/publish.py", None),
        ("lib/benchsmith/candidate.py", None),
        ("lib/benchsmith/hold.py", None),
        ("lib/benchsmith/remote_ref.py", None),
        ("lib/benchsmith/remote_lease.py", None),
        (
            "lib/benchsmith/resolve.py",
            ("FROZEN", "AWAITING_REVIEW", "Decision", "publication_eligibility"),
        ),
        (
            "lib/benchsmith/gate.py",
            (
                "PUSH_REQUIRED",
                "CONTROL_REQUIRED",
                "INAPPLICABLE_PUSH_CHECKS",
                "_push_policy",
                "_receipt_requirements",
                "_receipt_digest",
                "_receipt_body",
                "verify_receipt",
                "carry_receipt",
            ),
        ),
        ("lib/benchsmith/reviews.py", None),
        ("lib/benchsmith/cli.py", ("cmd_publish",)),
    ),
}


class SafetyRefused(ValueError):
    """The running Benchsmith checkout cannot support a safety claim."""


def installation_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _selected_source(path: Path, symbols: tuple[str, ...] | None) -> bytes:
    source = path.read_text(encoding="utf-8")
    if symbols is None:
        return source.encode()

    tree = ast.parse(source, filename=str(path))
    wanted = set(symbols)
    found: dict[str, str] = {}
    for node in tree.body:
        names: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        for name in names & wanted:
            segment = ast.get_source_segment(source, node)
            if segment is None:
                raise SafetyRefused(f"could not isolate {name} in {path}")
            found[name] = segment

    missing = sorted(wanted - found.keys())
    if missing:
        raise SafetyRefused(
            f"safety manifest references missing symbols in {path}: {', '.join(missing)}"
        )
    return "\n\n".join(f"## {name}\n{found[name]}" for name in sorted(found)).encode()


def component_digest(component: str, *, root: str | Path | None = None) -> str:
    manifest = _MANIFESTS.get(component)
    if manifest is None:
        raise SafetyRefused(f"unknown safety component {component!r}")
    base = Path(root).resolve() if root is not None else installation_root()
    digest = hashlib.sha256()
    digest.update(f"benchsmith-safety-v{DIGEST_SCHEMA}\0{component}\0".encode())
    for relative, symbols in manifest:
        path = base / relative
        if not path.is_file():
            raise SafetyRefused(f"safety manifest path is missing: {path}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(json.dumps(symbols, sort_keys=True).encode())
        digest.update(b"\0")
        digest.update(_selected_source(path, symbols))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def snapshot(
    component: str,
    *,
    root: str | Path | None = None,
    require_clean: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict:
    base = Path(root).resolve() if root is not None else installation_root()
    for relative in ("SKILL.md", "bin/benchsmith", "lib/benchsmith"):
        if not (base / relative).exists():
            raise SafetyRefused(f"Benchsmith canonical path is missing: {base / relative}")

    run = runner or subprocess.run
    try:
        top = run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = run(
            ["git", "-C", str(base), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        head = run(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SafetyRefused(f"Benchsmith checkout identity is unreadable: {error}") from error

    if top.returncode or Path(top.stdout.strip()).resolve() != base:
        raise SafetyRefused("Benchsmith is not running from its canonical checkout root")
    if status.returncode or head.returncode:
        detail = status.stderr or head.stderr or "git identity failed"
        raise SafetyRefused(f"Benchsmith checkout identity is unreadable: {detail.strip()[:200]}")
    dirty = bool(status.stdout.strip())
    if require_clean and dirty:
        raise SafetyRefused("Benchsmith checkout is dirty; safety artifacts require a clean tree")

    return {
        "component": component,
        "digest": component_digest(component, root=base),
        "schemaVersion": DIGEST_SCHEMA,
        "sourceHead": head.stdout.strip(),
        "sourceRoot": str(base),
        "clean": not dirty,
    }
