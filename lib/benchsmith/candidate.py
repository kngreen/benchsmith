"""Publication-time proof that one candidate stack belongs to one task.

The ordinary gate examines the commit in front of it.  Publication needs a
stronger question: what does the whole unpublished stack contain relative to a
base that is actually on the remote?  This module owns that proof so handoff,
collection, and the final push cannot disagree about the answer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .identifiers import FULL_SHA as _FULL_SHA
from .identifiers import validate_task_path

_ABSENT = "<absent>"


class CandidateRejected(Exception):
    """The candidate cannot be shown to contain only the named task."""


class CandidateTreeChanged(CandidateRejected):
    """A rebase was scoped correctly, but changed the candidate task bytes."""


@dataclass(frozen=True)
class StackProof:
    task: str
    candidate_sha: str
    declared_base_sha: str
    remote_base_sha: str
    mode: str
    changed_paths: tuple[str, ...]
    task_tree: str
    needs_rebase: bool = False
    source_base_sha: str = ""
    carried_from_sha: str = ""
    base_task_tree: str = ""

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "candidateSha": self.candidate_sha,
            "declaredBaseSha": self.declared_base_sha,
            "remoteBaseSha": self.remote_base_sha,
            "mode": self.mode,
            "changedPaths": list(self.changed_paths),
            "taskTree": self.task_tree,
            "needsRebase": self.needs_rebase,
            "sourceBaseSha": self.source_base_sha or None,
            "carriedFromSha": self.carried_from_sha or None,
            "baseTaskTree": self.base_task_tree or None,
        }


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run(repo: Path, git, *args: str) -> subprocess.CompletedProcess:
    try:
        return git(repo, *args)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateRejected(
            f"git could not establish candidate ancestry ({type(error).__name__})"
        ) from error


def _task_name(task: str) -> str:
    try:
        return validate_task_path(task)
    except ValueError as error:
        raise CandidateRejected(str(error)) from error


def _commit(repo: Path, value: str, label: str, *, git) -> str:
    value = str(value or "").strip()
    if not _FULL_SHA.fullmatch(value):
        raise CandidateRejected(
            f"{label} must be one full commit SHA; abbreviated, missing, or ambiguous bases block"
        )
    result = _run(repo, git, "rev-parse", "--verify", f"{value}^{{commit}}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) != 1 or not _FULL_SHA.fullmatch(lines[0]):
        detail = result.stderr.strip()[:160]
        raise CandidateRejected(
            f"{label} {value[:12]} is not a resolvable local commit"
            + (f": {detail}" if detail else "")
        )
    resolved = lines[0].lower()
    if resolved != value.lower():
        raise CandidateRejected(f"{label} did not resolve to the exact declared commit")
    return resolved


def _remote_base(repo: Path, remote: str, branch: str, *, git) -> str:
    ref = f"refs/heads/{branch}"
    result = _run(repo, git, "ls-remote", "--heads", remote, ref)
    if result.returncode != 0:
        raise CandidateRejected(
            "current remote base is unreadable: "
            + (result.stderr.strip()[:160] or "git ls-remote failed")
        )
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            rows.append(fields[0])
    if len(rows) != 1:
        raise CandidateRejected(
            f"current remote base is {'missing' if not rows else 'ambiguous'} for {remote}/{branch}"
        )
    return _commit(repo, rows[0], "current remote base", git=git)


def _ancestor(repo: Path, older: str, newer: str, *, git, relation: str) -> bool:
    result = _run(repo, git, "merge-base", "--is-ancestor", older, newer)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise CandidateRejected(
        f"git could not decide {relation}; unknown ancestry blocks publication"
    )


def _stack_paths(repo: Path, base: str, candidate: str, *, git) -> tuple[str, ...]:
    commits = _run(repo, git, "rev-list", "--topo-order", f"{base}..{candidate}")
    if commits.returncode != 0:
        raise CandidateRejected(
            "git could not enumerate the candidate stack; unknown ancestry blocks publication"
        )
    shas = [line.strip() for line in commits.stdout.splitlines() if line.strip()]
    if any(not _FULL_SHA.fullmatch(sha) for sha in shas):
        raise CandidateRejected("git returned an ambiguous commit while enumerating the stack")

    paths: set[str] = set()
    for sha in shas:
        changed = _run(
            repo,
            git,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-m",
            "-z",
            sha,
        )
        if changed.returncode != 0:
            raise CandidateRejected(
                f"git could not inspect stack commit {sha[:12]}; publication scope is unknown"
            )
        paths.update(path for path in changed.stdout.split("\0") if path)
    return tuple(sorted(paths))


def _scoped_paths(repo: Path, task: str, base: str, candidate: str, *, git) -> tuple[str, ...]:
    paths = _stack_paths(repo, base, candidate, git=git)
    prefix = f"{task}/"
    stray = [path for path in paths if path != task and not path.startswith(prefix)]
    if stray:
        shown = ", ".join(stray[:5])
        suffix = f" (+{len(stray) - 5} more)" if len(stray) > 5 else ""
        raise CandidateRejected(
            f"candidate stack {base[:12]}..{candidate[:12]} changes outside {task}: "
            f"{shown}{suffix}"
        )
    return paths


def _tree(repo: Path, commit: str, task: str, *, git, required: bool) -> str:
    result = _run(repo, git, "ls-tree", "-z", commit, "--", task)
    if result.returncode != 0:
        raise CandidateRejected(
            f"task tree at {commit[:12]} is unreadable; byte identity is unknown"
        )
    entries = [entry for entry in result.stdout.split("\0") if entry]
    if not entries:
        if required:
            raise CandidateRejected(f"candidate {commit[:12]} has no {task} task tree")
        return _ABSENT
    if len(entries) != 1 or "\t" not in entries[0]:
        raise CandidateRejected(f"task tree at {commit[:12]} is ambiguous")
    metadata, path = entries[0].split("\t", 1)
    fields = metadata.split()
    if (
        path != task
        or len(fields) != 3
        or fields[1] != "tree"
        or not _FULL_SHA.fullmatch(fields[2])
    ):
        raise CandidateRejected(f"{task} at {commit[:12]} is not one task directory tree")
    return fields[2].lower()


def _receipt_derivation(repo: Path, task: str, handoff: dict, candidate: str) -> str:
    """Return the source candidate only from a valid, exact-candidate carry receipt."""
    marker = str(handoff.get("gate_receipt") or "")
    if not marker:
        return ""
    try:
        from . import gate as gate_mod

        body, _ = gate_mod._receipt_body(repo, task)
    except Exception:  # noqa: BLE001 - absence merely means this is not a carry
        return ""
    if body is None or str(body.get("head") or "").lower() != candidate:
        return ""
    accepted = {
        str(body.get("digest") or ""),
        f"sha256:{body.get('digest') or ''}",
        str(gate_mod.receipt_path(repo, task)),
    }
    if marker not in accepted:
        return ""
    return str(body.get("derivedFrom") or "").lower()


def prove_carry(
    repo: Path,
    task: str,
    *,
    source_base: str,
    source_candidate: str,
    target_base: str,
    target_candidate: str,
    git=_git,
) -> StackProof:
    """Prove a rebased candidate carries only identical bytes for one task."""
    repo = Path(repo)
    task = _task_name(task)
    source_base = _commit(repo, source_base, "old base", git=git)
    source_candidate = _commit(repo, source_candidate, "carried candidate", git=git)
    target_base = _commit(repo, target_base, "current remote base", git=git)
    target_candidate = _commit(repo, target_candidate, "rebased candidate", git=git)

    if not _ancestor(
        repo,
        source_base,
        source_candidate,
        git=git,
        relation="whether the old base is an ancestor of the carried candidate",
    ):
        raise CandidateRejected("old base is not an ancestor of the carried candidate")
    if not _ancestor(
        repo,
        target_base,
        target_candidate,
        git=git,
        relation="whether the current remote is an ancestor of the rebased candidate",
    ):
        raise CandidateRejected("current remote base is not an ancestor of the rebased candidate")

    _scoped_paths(repo, task, source_base, source_candidate, git=git)
    target_paths = _scoped_paths(repo, task, target_base, target_candidate, git=git)
    old_base_tree = _tree(repo, source_base, task, git=git, required=False)
    remote_tree = _tree(repo, target_base, task, git=git, required=False)
    if old_base_tree != remote_tree:
        raise CandidateRejected(
            "old base task tree differs from the current remote task tree; "
            "the rebase base is unsafe"
        )
    source_tree = _tree(repo, source_candidate, task, git=git, required=True)
    candidate_tree = _tree(repo, target_candidate, task, git=git, required=True)
    if source_tree != candidate_tree:
        raise CandidateTreeChanged(
            "rebased candidate task tree differs from the carried candidate task tree"
        )
    return StackProof(
        task=task,
        candidate_sha=target_candidate,
        declared_base_sha=target_base,
        remote_base_sha=target_base,
        mode="carry",
        changed_paths=target_paths,
        task_tree=candidate_tree,
        source_base_sha=source_base,
        carried_from_sha=source_candidate,
        base_task_tree=old_base_tree,
    )


def verify_handoff(
    repo: Path,
    task: str,
    handoff: dict,
    *,
    remote: str = "origin",
    branch: str = "main",
    remote_base: str = "",
    git=_git,
) -> StackProof:
    """Prove the ready candidate is scoped to ``task`` from a trusted base."""
    repo = Path(repo)
    task = _task_name(task)
    candidate = _commit(repo, str(handoff.get("commit_sha") or ""), "candidate", git=git)
    declared = _commit(repo, str(handoff.get("base_sha") or ""), "declared base", git=git)
    current = (
        _commit(repo, remote_base, "current remote base", git=git)
        if remote_base
        else _remote_base(repo, remote, branch, git=git)
    )
    if declared == candidate:
        raise CandidateRejected(
            "declared base equals the candidate; publication scope would be an empty self-range"
        )

    explicit_source = str(handoff.get("source_base_sha") or "").lower()
    explicit_candidate = str(handoff.get("carried_from_sha") or "").lower()
    if bool(explicit_source) != bool(explicit_candidate):
        raise CandidateRejected(
            "a carried rebase must name both source_base_sha and carried_from_sha"
        )
    receipt_source = _receipt_derivation(repo, task, handoff, candidate)
    if explicit_candidate and receipt_source != explicit_candidate:
        raise CandidateRejected(
            "carried_from_sha is not the source candidate named by the exact-candidate gate receipt"
        )
    if receipt_source:
        source_candidate = explicit_candidate or receipt_source
        source_base = explicit_source or (declared if declared != current else "")
        if not source_base:
            raise CandidateRejected(
                "the carried receipt names an old candidate but not its old base; "
                "source_base_sha is required"
            )
        if declared not in {source_base, current}:
            raise CandidateRejected(
                "declared base is neither the proved old base nor the current remote base"
            )
        return prove_carry(
            repo,
            task,
            source_base=source_base,
            source_candidate=source_candidate,
            target_base=current,
            target_candidate=candidate,
            git=git,
        )
    if explicit_source:
        raise CandidateRejected(
            "manual carry fields require a gate receipt derived from carried_from_sha"
        )

    if not _ancestor(
        repo,
        declared,
        candidate,
        git=git,
        relation="whether the declared base is an ancestor of the candidate",
    ):
        raise CandidateRejected(
            "declared base is not an ancestor of the candidate and no verified carry proof exists"
        )

    remote_is_ancestor = _ancestor(
        repo,
        current,
        candidate,
        git=git,
        relation="whether the current remote base is an ancestor of the candidate",
    )
    if remote_is_ancestor:
        declared_before_remote = _ancestor(
            repo,
            declared,
            current,
            git=git,
            relation="whether the declared base precedes the current remote base",
        )
        remote_before_declared = _ancestor(
            repo,
            current,
            declared,
            git=git,
            relation="whether the current remote base precedes the declared base",
        )
        if not declared_before_remote and not remote_before_declared:
            raise CandidateRejected(
                "declared base and current remote base are on incomparable histories; "
                "base is ambiguous"
            )
        scope_base = declared if current == candidate else current
        paths = _scoped_paths(repo, task, scope_base, candidate, git=git)
        return StackProof(
            task=task,
            candidate_sha=candidate,
            declared_base_sha=declared,
            remote_base_sha=current,
            mode="direct",
            changed_paths=paths,
            task_tree=_tree(repo, candidate, task, git=git, required=True),
        )

    if not _ancestor(
        repo,
        declared,
        current,
        git=git,
        relation="whether the declared old base is an ancestor of the current remote base",
    ):
        raise CandidateRejected(
            "candidate does not descend from the current remote and its declared base is not "
            "a known old remote ancestor; base is ambiguous"
        )
    paths = _scoped_paths(repo, task, declared, candidate, git=git)
    old_tree = _tree(repo, declared, task, git=git, required=False)
    remote_tree = _tree(repo, current, task, git=git, required=False)
    if old_tree != remote_tree:
        raise CandidateRejected(
            "declared old base task tree differs from the current remote task tree; "
            "automatic rebase is unsafe"
        )
    return StackProof(
        task=task,
        candidate_sha=candidate,
        declared_base_sha=declared,
        remote_base_sha=current,
        mode="stale-base",
        changed_paths=paths,
        task_tree=_tree(repo, candidate, task, git=git, required=True),
        needs_rebase=True,
        base_task_tree=old_tree,
    )
