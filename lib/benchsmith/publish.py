"""Stage 4: one push at a time, per repository, crash-safely.

Workers prepare in parallel; exactly one may publish. That is not throughput
squeamishness -- the platform validates the branch tip, so two loops pushing to
one repository invalidate each other's evidence, and the second push silently
turns the first one's measurement into somebody else's.

The hard part is not the lock, it is crashing while holding it. So the intent to
push is written down BEFORE the push happens. On restart, a recorded intent plus
the actual remote head answers the only question that matters -- did it land? --
and answering it is what makes a retry safe rather than a second push.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import candidate as candidate_mod
from . import safety

LANE_TTL = 3600

LANDED = "landed"
NOT_LANDED = "not-landed"
DIVERGED = "diverged"
UNKNOWN = "unknown"


class PublishRefused(Exception):
    """Not safe to push. The message says why."""


def _policy_identity() -> dict:
    try:
        return safety.snapshot("publish_policy")
    except safety.SafetyRefused as error:
        raise PublishRefused(str(error)) from error


def _git(repo: Path, *args, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )


@dataclass
class Intent:
    task: str
    base_sha: str
    commit_sha: str
    remote: str
    branch: str
    key: str
    at: float
    lease_sha: str = ""
    publish_policy_fingerprint: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Lane:
    """One publication lane per repository, not per task."""

    def __init__(self, repo_root: Path, run_id: str = "", ttl: int = LANE_TTL):
        self.root = Path(repo_root) / ".benchsmith"
        self.lock = self.root / "publish.lane"
        self.intent_path = self.root / "publish.intent.json"
        self.run_id = run_id or "local"
        self.ttl = ttl

    def _owner(self, task: str) -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{task}"

    def holder(self) -> dict | None:
        try:
            doc = json.loads(self.lock.read_text())
        except (OSError, ValueError):
            return None
        if time.time() - float(doc.get("at") or 0) > self.ttl:
            return None  # expired; reapable
        return doc

    def acquire(self, task: str) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        held = self.holder()
        if held:
            return held.get("task") == task  # re-entrant for the same task only
        # An expired lock file still exists on disk; clear it before claiming so
        # O_EXCL means what it says.
        if self.lock.exists():
            try:
                self.lock.unlink()
            except OSError:
                return False
        try:
            fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            json.dump({"task": task, "owner": self._owner(task), "at": time.time()}, f)
        return True

    def release(self, task: str) -> None:
        held = self.holder()
        if held and held.get("task") != task:
            return  # never release somebody else's lane
        self.lock.unlink(missing_ok=True)

    # --- crash safety ---

    def key(self, task: str, commit_sha: str) -> str:
        """Stable across restarts, so a retry is recognisably the same attempt."""
        return f"benchsmith:{self.run_id}:publish:{task}:{commit_sha}"

    def record_intent(self, intent: Intent) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.intent_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(intent.as_dict(), indent=1))
        os.replace(tmp, self.intent_path)  # atomic: a half-written intent is unreadable

    def pending(self) -> Intent | None:
        try:
            return Intent(**json.loads(self.intent_path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def clear_intent(self) -> None:
        self.intent_path.unlink(missing_ok=True)

    def reconcile(self, repo_root: Path, *, git=_git) -> dict:
        """Did the pending push land? The only safe question after a crash."""
        intent = self.pending()
        if intent is None:
            return {"state": "clean", "detail": "no pending push"}
        try:
            current_policy = _policy_identity()["digest"]
        except PublishRefused as error:
            return {"state": UNKNOWN, "intent": intent.as_dict(), "detail": str(error)}
        if not intent.publish_policy_fingerprint:
            return {
                "state": UNKNOWN,
                "intent": intent.as_dict(),
                "detail": "pending intent has no publish-policy fingerprint",
            }
        if intent.publish_policy_fingerprint != current_policy:
            return {
                "state": UNKNOWN,
                "intent": intent.as_dict(),
                "detail": "publish policy changed after the intent was recorded",
            }
        r = git(Path(repo_root), "ls-remote", intent.remote, intent.branch)
        if r.returncode != 0:
            # Not knowing is not the same as not landed. Pushing again here is
            # how a crash becomes a double push.
            return {
                "state": UNKNOWN,
                "intent": intent.as_dict(),
                "detail": f"cannot read the remote: {r.stderr.strip()[:160]}",
            }
        head = (r.stdout.split() or [""])[0]
        if head == intent.commit_sha:
            return {
                "state": LANDED,
                "intent": intent.as_dict(),
                "detail": "the push landed; clear the intent and record the round",
            }
        if head == intent.base_sha:
            return {
                "state": NOT_LANDED,
                "intent": intent.as_dict(),
                "detail": "the remote is still at our base; the push can be retried",
            }
        return {
            "state": DIVERGED,
            "intent": intent.as_dict(),
            "detail": f"the remote moved to {head[:8]}; someone else published. Rebase and "
            "re-gate before retrying — the prepared commit's evidence is stale",
        }


PUBLICATION_EVIDENCE_VERSION = 1


def _compose_publication_evidence(
    repo_root: Path,
    task: str,
    candidate_sha: str,
    *,
    authenticate_review: bool,
    authenticate_hook: bool,
    remote: str,
    branch: str,
) -> dict:
    """Compose current exact-candidate evidence, optionally authenticating its transcript."""
    from . import critic_receipt as critic_mod
    from . import gate as gate_mod

    ok, problem = gate_mod.verify_receipt(Path(repo_root), task)
    if not ok:
        raise PublishRefused(f"gate receipt is not valid for the candidate: {problem}")
    gate_body, problem = gate_mod._receipt_body(Path(repo_root), task)
    if gate_body is None:
        raise PublishRefused(problem)
    if str(gate_body.get("head") or "") != candidate_sha:
        raise PublishRefused("gate receipt is for another candidate SHA")
    hook_proof = gate_body.get("repositoryHook") or {}
    if authenticate_hook:
        hook = gate_mod.authenticate_live_hook(
            Path(repo_root), task, gate_body, remote=remote, branch=branch
        )
        if not hook.get("ok"):
            raise PublishRefused(
                "live pre-push hook authentication failed: "
                + str(hook.get("reason") or "unknown")
            )
        hook_proof = hook["proof"]

    review_body, problem = critic_mod.load(Path(repo_root), task, candidate_sha)
    if review_body is None:
        raise PublishRefused(problem)
    if authenticate_review:
        review_body, problem = critic_mod.verify_live(
            Path(repo_root), task, candidate_sha, review_body
        )
        if review_body is None:
            raise PublishRefused(f"critic receipt is not authenticated: {problem}")
    canonical = review_body.get("canonical_review") or {}
    if canonical.get("decision") != "Accept":
        raise PublishRefused("canonical review did not accept the exact candidate")
    if review_body.get("decision") != "Accept":
        raise PublishRefused("critic review did not accept the exact candidate")

    change = (gate_body.get("artifacts") or {}).get("changeEvidence")
    if not isinstance(change, dict):
        raise PublishRefused(
            "gate receipt predates structured finding/lever evidence; re-run the gate"
        )
    levers = change.get("levers")
    findings = change.get("findings")
    if not isinstance(levers, list) or not all(isinstance(value, str) for value in levers):
        raise PublishRefused("gate receipt lever evidence is malformed")
    if not isinstance(findings, list) or not all(isinstance(value, dict) for value in findings):
        raise PublishRefused("gate receipt finding evidence is malformed")

    return {
        "schema_version": PUBLICATION_EVIDENCE_VERSION,
        "candidate_sha": candidate_sha,
        "gate": {
            "digest": str(gate_body["digest"]),
            "hook_identity": hook_proof.get("identity"),
        },
        "reviews": {
            "receipt_digest": str(review_body["digest"]),
            "task_id": str(review_body.get("task_id") or ""),
            "canonical": {
                "name": str(canonical.get("name") or ""),
                "decision": str(canonical.get("decision") or ""),
            },
            "critic": {
                "version": str(review_body.get("critic_version") or ""),
                "decision": str(review_body.get("decision") or ""),
            },
        },
        "change": change,
    }


def publication_evidence(
    repo_root: Path,
    task: str,
    candidate_sha: str,
    *,
    remote: str = "origin",
    branch: str = "",
) -> dict:
    """Compose evidence after authenticating the terminal transcript and live hook."""
    return _compose_publication_evidence(
        repo_root,
        task,
        candidate_sha,
        authenticate_review=True,
        authenticate_hook=True,
        remote=remote,
        branch=branch,
    )


def verify_publication_evidence(
    repo_root: Path,
    task: str,
    candidate_sha: str,
    supplied: object,
    *,
    authenticate_review: bool = True,
    authenticate_hook: bool = True,
    remote: str = "origin",
    branch: str = "",
) -> dict:
    """Require the typed handoff evidence to equal the current local receipts."""
    if not isinstance(supplied, dict):
        raise PublishRefused(
            "legacy ready_to_publish handoff has no publication_evidence schema v1; "
            "re-run the exact-SHA gate and reviews, then create a new handoff"
        )
    if supplied.get("schema_version") != PUBLICATION_EVIDENCE_VERSION:
        raise PublishRefused(
            "unsupported publication_evidence schema; regenerate it from current receipts"
        )
    if str(supplied.get("candidate_sha") or "") != candidate_sha:
        raise PublishRefused("publication_evidence is for another candidate SHA")
    expected = _compose_publication_evidence(
        Path(repo_root),
        task,
        candidate_sha,
        authenticate_review=authenticate_review,
        authenticate_hook=authenticate_hook,
        remote=remote,
        branch=branch,
    )
    if supplied != expected:
        raise PublishRefused(
            "publication_evidence does not match the current exact-candidate gate/review receipts"
        )
    return expected


def verify_handoff_evidence(
    repo_root: Path,
    task: str,
    candidate_sha: str,
    handoff: dict,
    *,
    authenticate_review: bool = True,
    authenticate_hook: bool = True,
    remote: str = "origin",
    branch: str = "",
) -> dict:
    evidence = verify_publication_evidence(
        repo_root,
        task,
        candidate_sha,
        handoff.get("publication_evidence"),
        authenticate_review=authenticate_review,
        authenticate_hook=authenticate_hook,
        remote=remote,
        branch=branch,
    )
    marker = str(handoff.get("gate_receipt") or "")
    gate_digest = str((evidence.get("gate") or {}).get("digest") or "")
    if not marker:
        raise PublishRefused("ready_to_publish handoff has no gate_receipt")
    if marker not in {gate_digest, f"sha256:{gate_digest}"}:
        raise PublishRefused("handoff gate_receipt does not match publication_evidence")
    if handoff.get("change_evidence") != evidence.get("change"):
        raise PublishRefused(
            "handoff change_evidence does not match the exact gate receipt"
        )
    submission_id = str(handoff.get("submission_id") or "")
    reviewed_task_id = str((evidence.get("reviews") or {}).get("task_id") or "")
    if submission_id and submission_id != reviewed_task_id:
        raise PublishRefused(
            "review receipt task_id does not match the handoff submission_id"
        )
    return evidence


def _resolved_publication_target(
    repo_root: Path, remote: str, branch: str
) -> tuple[str, str]:
    from . import gate as gate_mod

    target = gate_mod.resolve_publication_target(
        Path(repo_root), remote=remote, branch=branch
    )
    if not target.get("ok"):
        raise PublishRefused(str(target.get("reason") or "publication target is unreadable"))
    if not target.get("configured"):
        raise PublishRefused("no publication remote or target branch is configured")
    return str(target["remote"]), str(target["branch"])


def inspect_candidate(
    repo_root: Path,
    task: str,
    handoff: dict,
    *,
    remote: str = "origin",
    branch: str = "",
    git=_git,
) -> dict:
    """Prove candidate scope and classify exact-tip publication without mutation."""
    remote, branch = _resolved_publication_target(repo_root, remote, branch)
    if str(handoff.get("state")) != "ready_to_publish":
        raise PublishRefused(
            f"handoff state is {handoff.get('state')!r}, not ready_to_publish"
        )
    commit_sha = str(handoff.get("commit_sha") or "")
    if not commit_sha:
        raise PublishRefused("no commit_sha; nothing to publish")
    try:
        proof = candidate_mod.verify_handoff(
            Path(repo_root), task, handoff, remote=remote, branch=branch, git=git
        )
    except candidate_mod.CandidateRejected as error:
        raise PublishRefused(f"candidate stack is not publication-safe: {error}") from error
    return {
        "proof": proof,
        "alreadyPublished": proof.remote_base_sha == proof.candidate_sha,
        "commit": proof.candidate_sha,
    }


def _prepare_rebase(
    repo_root: Path,
    task: str,
    proof,
    *,
    commit_sha: str,
    remote: str,
    branch: str,
    review_requests: dict,
    git,
) -> dict:
    """Rebase and carry local receipts while the caller holds the repository lane."""
    head = proof.remote_base_sha
    result = git(
        repo_root,
        "rebase",
        "--onto",
        head,
        proof.declared_base_sha,
        proof.candidate_sha,
        timeout=600,
    )
    if result.returncode != 0:
        git(repo_root, "rebase", "--abort")
        raise PublishRefused(f"rebase onto {head[:8]} failed: {result.stderr.strip()[:200]}")
    moved = (git(repo_root, "rev-parse", "HEAD").stdout.split() or [""])[0]
    git(repo_root, "checkout", "--detach", moved)
    try:
        carry_proof = candidate_mod.prove_carry(
            repo_root,
            task,
            source_base=proof.declared_base_sha,
            source_candidate=proof.candidate_sha,
            target_base=head,
            target_candidate=moved,
            git=git,
        )
    except candidate_mod.CandidateTreeChanged:
        return {
            "state": "rebased", "task": task, "from": commit_sha, "newSha": moved,
            "onto": head, "needsRegate": True,
            "detail": "rebased onto the new head and the task tree changed; re-gate this SHA",
        }
    except candidate_mod.CandidateRejected as error:
        raise PublishRefused(f"rebased candidate stack is not publication-safe: {error}") from error

    from . import critic_receipt as critic_mod
    from . import gate as gate_mod

    carried = gate_mod.carry_receipt(
        repo_root,
        task,
        moved,
        remote=remote,
        branch=branch,
        defer_hook=True,
        expected_remote_sha=head,
        review_requests=review_requests,
    )
    if not carried.get("ok"):
        return {
            "state": "rebased", "task": task, "from": commit_sha, "newSha": moved,
            "onto": head, "needsRegate": True,
            "detail": "task tree is unchanged, but candidate receipt failed: "
            f"{carried.get('reason', 'unknown')}",
        }
    carried_review = critic_mod.carry(repo_root, task, proof.candidate_sha, moved)
    if not carried_review.get("ok"):
        return {
            "state": "rebased", "task": task, "from": commit_sha, "newSha": moved,
            "onto": head, "needsRegate": False, "needsReview": True,
            "gateReceipt": carried["receipt"]["digest"],
            "detail": "task tree is unchanged and gate evidence carried, but exact-SHA "
            f"review evidence did not: {carried_review.get('reason', 'unknown')}",
        }
    receipt = carried["receipt"]["digest"]
    return {
        "state": "rebased", "task": task, "from": commit_sha, "newSha": moved,
        "onto": head, "needsRegate": False, "taskTree": carry_proof.task_tree,
        "gateReceipt": receipt,
        "stackProof": carry_proof.as_dict(),
        "handoffPatch": {
            "base_sha": head, "commit_sha": moved,
            "source_base_sha": proof.declared_base_sha,
            "carried_from_sha": proof.candidate_sha,
            "gate_receipt": receipt,
        },
        "detail": (
            f"rebased onto {head[:8]}; old and current base task trees match, and the carried "
            f"task tree is byte-identical ({carry_proof.task_tree[:12]}). Tree-invariant "
            "evidence was carried and commit-relative controls reran."
        ),
    }


def confirm_already_published(
    repo_root: Path,
    task: str,
    handoff: dict,
    *,
    remote: str = "origin",
    branch: str = "",
    lane: Lane | None = None,
    git=_git,
) -> dict:
    """Re-prove an exact-tip no-op and never fall through to a push."""
    repo_root = Path(repo_root)
    remote, branch = _resolved_publication_target(repo_root, remote, branch)
    policy = _policy_identity()
    inspection = inspect_candidate(
        repo_root, task, handoff, remote=remote, branch=branch, git=git
    )
    if not inspection["alreadyPublished"]:
        raise PublishRefused(
            "remote tip changed after exact-tip classification; reclassify before publication"
        )
    active_lane = lane or Lane(repo_root)
    pending = active_lane.pending()
    if pending and pending.task == task and pending.commit_sha == inspection["commit"]:
        active_lane.clear_intent()
    return {
        "ok": True,
        "state": "already-published",
        "task": task,
        "commit": inspection["commit"],
        "stackProof": inspection["proof"].as_dict(),
        "publishPolicyFingerprint": policy["digest"],
        "nextAction": "watch-exact-sha",
    }


def publish(
    repo_root: Path,
    task: str,
    handoff: dict,
    *,
    remote: str = "origin",
    branch: str = "",
    lane: Lane | None = None,
    apply: bool = False,
    git=_git,
    check_review: bool = True,
    rebase: bool = False,
    allow_review_status: str = "",
    remote_lease=None,
    check_hold: bool = True,
) -> dict:
    """Verify, claim the lane, record the intent, then push exactly once."""
    repo_root = Path(repo_root)
    remote, branch = _resolved_publication_target(repo_root, remote, branch)
    lane = lane or Lane(repo_root)

    policy = _policy_identity()
    inspection = inspect_candidate(
        repo_root, task, handoff, remote=remote, branch=branch, git=git
    )
    commit_sha = inspection["commit"]
    if inspection["alreadyPublished"]:
        return confirm_already_published(
            repo_root,
            task,
            handoff,
            remote=remote,
            branch=branch,
            lane=lane,
            git=git,
        )
    def _cheap_revalidate_candidate_receipt() -> None:
        verify_handoff_evidence(
            repo_root,
            task,
            commit_sha,
            handoff,
            authenticate_review=False,
            authenticate_hook=False,
            remote=remote,
            branch=branch,
        )
        from . import gate as gate_mod

        body, problem = gate_mod._receipt_body(repo_root, task)
        if body is None:
            raise PublishRefused(problem)
        carried_from = str(handoff.get("carried_from_sha") or "")
        if carried_from and str(body.get("derivedFrom") or "") != carried_from:
            raise PublishRefused(
                "gate receipt was not derived from the handoff's carried candidate"
            )
        current = (git(repo_root, "rev-parse", "HEAD").stdout.split() or [""])[0]
        if current != commit_sha:
            raise PublishRefused(
                f"handoff commit is {commit_sha[:8]}, but the candidate HEAD is {current[:8]}"
            )

    def _freeze_check(when: str) -> None:
        """Fail closed when the task cannot be shown publication-eligible."""
        if not check_review:
            return
        from .resolve import publication_eligibility, resolve as _resolve

        try:
            info = _resolve(task, roots=[str(repo_root)])
        except Exception as e:  # noqa: BLE001
            raise PublishRefused(
                f"could not read {task}'s status {when} ({type(e).__name__}); refusing to "
                "publish. An unreadable status is not permission — a blocked push costs a "
                "minute, a push onto an accepted task cannot be undone."
            ) from e
        status = str(info.get("status") or "")
        decision = publication_eligibility(
            status, allow_review_status=allow_review_status
        )
        if not decision.eligible:
            raise PublishRefused(f"{task} is not publication-eligible: {decision.reason}")

    def _reconcile_or_refuse() -> None:
        pending = lane.reconcile(repo_root, git=git)
        if pending["state"] in (LANDED, DIVERGED, UNKNOWN):
            raise PublishRefused(
                f"resolve the pending push first ({pending['state']}): {pending['detail']}"
            )

    def _acquire_or_refuse() -> None:
        if lane.acquire(task):
            return
        held = lane.holder() or {}
        raise PublishRefused(
            f"the publication lane for this repository is held by "
            f"{held.get('task', 'another task')}; one publisher per repository is what "
            "keeps exact-head evidence valid"
        )

    review_note = ""
    proof = inspection["proof"]
    rebase_result = None
    if proof.needs_rebase:
        if not rebase:
            raise PublishRefused(
                f"the remote moved to {proof.remote_base_sha[:8]}, but the candidate stack is "
                "task-only and the old and current remote task trees are byte-identical. "
                "Re-run with rebase=True to move the commit onto it and carry only proven evidence."
            )

        from .reviews import requests as _review_requests

        review_requests = _review_requests(task)
        # Phase one fences every checkout and receipt mutation. Network-backed
        # review state is snapshotted before the lane and compared after it.
        _acquire_or_refuse()
        try:
            _reconcile_or_refuse()
            fenced = inspect_candidate(
                repo_root, task, handoff, remote=remote, branch=branch, git=git
            )
            fenced_proof = fenced["proof"]
            if fenced["alreadyPublished"]:
                return {
                    "ok": True,
                    "state": "already-published",
                    "task": task,
                    "commit": fenced_proof.candidate_sha,
                    "stackProof": fenced_proof.as_dict(),
                    "publishPolicyFingerprint": policy["digest"],
                    "nextAction": "watch-exact-sha",
                }
            if (
                not fenced_proof.needs_rebase
                or fenced_proof.remote_base_sha != proof.remote_base_sha
            ):
                raise PublishRefused(
                    "the remote moved while acquiring the rebase lane; release and retry"
                )
            _freeze_check("immediately before rebasing")
            rebase_result = _prepare_rebase(
                repo_root,
                task,
                fenced_proof,
                commit_sha=commit_sha,
                remote=remote,
                branch=branch,
                review_requests=review_requests,
                git=git,
            )
        finally:
            lane.release(task)

        if rebase_result.get("needsRegate") or rebase_result.get("needsReview"):
            return rebase_result
        if _review_requests(task) != review_requests:
            raise PublishRefused(
                "review findings changed during the rebase phase; retry before publication"
            )
        commit_sha = str(rebase_result["newSha"])
        handoff = {**handoff, **rebase_result["handoffPatch"]}

        # Hook execution and transcript re-fetch happen with no repository lane.
        evidence = publication_evidence(
            repo_root, task, commit_sha, remote=remote, branch=branch
        )
        rebase_result["publicationEvidence"] = evidence
        rebase_result["handoffPatch"].update(
            publication_evidence=evidence,
            change_evidence=evidence["change"],
        )
        handoff.update(rebase_result["handoffPatch"])
    else:
        verify_handoff_evidence(
            repo_root, task, commit_sha, handoff, remote=remote, branch=branch
        )

    # Phase two is short: reacquire, reconcile, re-read the remote and exact local
    # evidence, then push. A move after phase one is a retry, never another rebase.
    _acquire_or_refuse()

    try:
        _reconcile_or_refuse()
        _freeze_check("inside the final publish lane")
        try:
            inspection = inspect_candidate(
                repo_root, task, handoff, remote=remote, branch=branch, git=git
            )
        except PublishRefused as error:
            if rebase_result is not None:
                raise PublishRefused(
                    "the remote moved between the rebase and publish phases; retry without pushing"
                ) from error
            raise
        proof = inspection["proof"]
        if inspection["alreadyPublished"]:
            return {
                "ok": True,
                "state": "already-published",
                "task": task,
                "commit": proof.candidate_sha,
                "stackProof": proof.as_dict(),
                "publishPolicyFingerprint": policy["digest"],
                "nextAction": "watch-exact-sha",
            }
        head = proof.remote_base_sha
        if proof.needs_rebase:
            if rebase_result is not None:
                raise PublishRefused(
                    "the remote moved between the rebase and publish phases; retry without pushing"
                )
            raise PublishRefused(
                "the remote moved while the short publish lane was being acquired; "
                "release and retry so rebase, hook execution, and review authentication "
                "remain outside the lane"
            )
        if rebase_result is not None and not apply:
            _cheap_revalidate_candidate_receipt()
            return rebase_result

        intent = Intent(
            task=task,
            base_sha=head,
            commit_sha=commit_sha,
            remote=remote,
            branch=branch,
            key=lane.key(task, commit_sha),
            at=time.time(),
            lease_sha=str(getattr(remote_lease, "sha", "")),
            publish_policy_fingerprint=policy["digest"],
        )
        if not apply:
            return {
                "planned": intent.as_dict(),
                "applied": False,
                "reviewNote": review_note or None,
                "hint": "re-run with apply=True to actually push",
            }

        current_policy = _policy_identity()["digest"]
        if current_policy != policy["digest"]:
            raise PublishRefused(
                "publish policy changed during this attempt; reclassify the candidate"
            )
        _cheap_revalidate_candidate_receipt()

        # Written before the push, so a crash between here and the next line is
        # recoverable rather than ambiguous. Expensive transcript/hook work ran
        # before the lane; inside it we only recheck local digests and remote
        # coordination state.
        if check_hold:
            from .hold import current as _hold

            h = _hold(repo_root, remote=remote)
            if not h.get("readable"):
                raise PublishRefused(
                    f"{h.get('reason')}; an unreadable hold is not permission"
                )
            if h.get("held"):
                raise PublishRefused(
                    f"{h['holder']} holds {remote}/{branch}"
                    + (
                        f" for another {h['minutesLeft']}m"
                        if h.get("minutesLeft")
                        else ""
                    )
                    + (f": {h['why']}" if h.get("why") else "")
                    + ". Wait for the window to close; the commit is gated and keeps."
                )
        if remote_lease is not None:
            # Same rule as the freeze check, for the same reason: the window
            # between acquiring the lane and pushing is exactly when another
            # host can take the task.
            try:
                remote_lease.assert_owned()
            except Exception as error:  # the lease module supplies the precise cause
                raise PublishRefused(f"remote lease is not owned: {error}") from error
        # The live hook and both review receipts are mutable local evidence.
        # Re-read them after every other pre-push check so a changed hook or
        # overwritten receipt cannot race the actual push.
        _cheap_revalidate_candidate_receipt()
        lane.record_intent(intent)
        try:
            pr = git(
                repo_root,
                "push",
                remote,
                f"{commit_sha}:refs/heads/{branch}",
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "state": UNKNOWN,
                "task": task,
                "key": intent.key,
                "error": "push timed out; it may have landed",
                "hint": "the intent and lease are retained; run reconcile before retrying",
            }
        if pr.returncode != 0:
            return {
                "ok": False,
                "state": UNKNOWN,
                "task": task,
                "key": intent.key,
                "error": pr.stderr.strip()[:400],
                "hint": "the intent and lease are retained; run reconcile before retrying",
            }
        lane.clear_intent()
        return {
            "ok": True,
            "task": task,
            "key": intent.key,
            "commit": commit_sha,
            "remote": remote,
            "branch": branch,
        }
    finally:
        lane.release(task)
