"""`benchsmith <subcommand>` — the one entry point.

Subcommands are deliberately thin: they resolve inputs, call a module, and print
JSON or a rendered report. Judgement lives in SKILL.md; arithmetic lives in the
modules; neither lives here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
import sys
from pathlib import Path

from . import fixtures as fixtures_mod
from . import fetchrepo as fetch_mod
from . import gate as gate_mod
from . import ideas as ideas_mod
from . import preflight as preflight_mod
from . import backoff as backoff_mod
from . import config as config_mod
from . import controller as controller_mod
from . import coverage
from . import critic_receipt as critic_mod
from . import controls as controls_mod
from . import hold as hold_mod
from . import hooks as hooks_mod
from . import dispatch as dispatch_mod
from . import mutate as mutate_mod
from . import passatk as passatk_mod
from . import causal as causal_mod
from . import remote_lease as rlease_mod
from . import reviewqueue as rq_mod
from . import reviewreport as rr_mod
from . import reviews as reviews_mod
from . import rerun as rerun_mod
from . import resolve as resolve_mod
from . import watch as watch_mod
from . import worktree as wt_mod
from . import publish as publish_mod
from . import sources
from . import stats as stats_mod
from . import task_status as task_status_mod
from .queue import (DEFAULT_WORKERS, MAX_WORKERS, Leases, build_queue, changes,
                    fingerprint, read_journals, render, render_changes)
from .adapter import Identity, Platform, Unresolved, discover
from .bar import evaluate
from .journal import Journal, git_trailers, surface_hashes
from .snapshot import build, evidence, infra_fraction


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _status_transition(repo: str | Path, event: str, task: str, **fields) -> dict:
    """Status reporting must never hide the command's primary result."""
    status_repo = fields.pop("status_repo", None)
    announce = fields.pop("announce", True)
    try:
        return task_status_mod.transition(
            repo,
            event,
            task,
            status_repo=status_repo,
            announce=announce,
            **fields,
        )
    except Exception as error:  # noqa: BLE001 - surfaced alongside the real result
        return {
            "changed": False,
            "markdown": None,
            "error": f"{type(error).__name__}: {error}",
        }


def _status_updates(
    repo: str | Path,
    patches: list[dict],
    *,
    status_repo=None,
    announce: bool = True,
    guard=None,
) -> dict:
    try:
        return task_status_mod.update_many(
            repo,
            patches,
            status_repo=status_repo,
            announce=announce,
            guard=guard,
        )
    except Exception as error:  # noqa: BLE001 - surfaced alongside the real result
        return {
            "changed": False,
            "markdown": None,
            "error": f"{type(error).__name__}: {error}",
        }


def _identity(args) -> Identity:
    return Identity(
        task_name=args.task,
        task_id=args.task_id or os.environ.get("BENCHSMITH_TASK_ID", ""),
        task_uuid=args.task_uuid or os.environ.get("BENCHSMITH_TASK_UUID", ""),
        source_repo=args.source_repo or os.environ.get("BENCHSMITH_SOURCE_REPO", ""),
        active_sha=args.sha or os.environ.get("BENCHSMITH_ACTIVE_SHA", ""),
    )


def cmd_preflight(args) -> int:
    """What is missing, and what degrades because of it."""
    result = preflight_mod.run(Path(args.repo).resolve() if args.repo else None, args.task or None)
    if args.json:
        _out(result)
    else:
        print(preflight_mod.render(result))
    return 0 if result["ok"] else 1


def cmd_controller(args) -> int:
    """Acquire and verify the fenced fleet-controller epoch."""
    repo = Path(args.repo).resolve()
    status_repo = Path(args.status_repo).resolve()
    if args.action == "acquire":
        result = controller_mod.acquire(
            repo,
            status_repo,
            args.session_id,
            ttl_seconds=args.ttl_sec,
        )
    elif args.action == "renew":
        result = controller_mod.renew(
            repo,
            status_repo,
            args.session_id,
            args.epoch,
            ttl_seconds=args.ttl_sec,
        )
    elif args.action == "verify":
        result = controller_mod.verify(
            repo, status_repo, args.session_id, args.epoch
        )
    else:
        result = controller_mod.release(
            repo, status_repo, args.session_id, args.epoch
        )
    _out(result)
    return 0


def cmd_probe(args) -> int:
    """Resolve the CLI surface once, so nothing downstream hardcodes it."""
    try:
        _out(discover().as_dict())
        return 0
    except Unresolved as e:
        _out({"error": str(e), "resolved": False})
        return 3


def cmd_read(args) -> int:
    """Fetch task, jobs and trials fresh, identity-checked."""
    try:
        platform = Platform(discover(), _identity(args))
        task = platform.task()
        jobs = platform.jobs()
        trials = {str(j.get("id")): platform.trials(str(j.get("id"))) for j in jobs}
    except Unresolved as e:
        _out({"error": str(e), "state": "not_run"})
        return 3
    _out({"task": task, "jobs": jobs, "trials": trials})
    return 0


def _load(args) -> dict:
    if args.input == "-":
        return json.load(sys.stdin)
    return json.loads(Path(args.input).read_text())


def _bind_worker(
    lease,
    sid: str,
    worktree: str,
    task: str,
    *,
    status_repo: str = "",
    submission_id: str = "",
) -> dict:
    """Bind a created worker or terminate it before reporting dispatch success."""
    if lease is None:
        return {"ok": True, "bound": False, "noRemoteLease": True}
    bound = lease.bind(sid)
    if bound.get("bound"):
        try:
            dispatch_mod.write_assignment(
                worktree,
                task,
                sid,
                lease.sha,
                lease.task,
                status_repo=status_repo,
                submission_id=submission_id,
            )
            return {"ok": True, **bound}
        except OSError as error:
            bound = {"bound": False, "reason": f"could not persist worker assignment: {error}"}
    stopped = dispatch_mod.relieve(sid)
    released = lease.release() if stopped.get("terminated") else {
        "released": False,
        "reason": "session termination was not confirmed; retaining the lease",
    }
    return {"ok": False, **bound, "termination": stopped, "release": released}


def _release_session_lease(repo: Path, task: str, sid: str) -> dict:
    """Release only the lease whose token names the terminated session."""
    lease = rlease_mod.RemoteLease(task, repo)
    sha = lease.remote_sha()
    if not sha:
        return {"released": True, "reason": "already absent"}
    owner = lease.owner(sha)
    if owner.session != sid:
        return {"released": False, "reason": f"lease belongs to {owner.session or 'no session'}",
                "owner": owner.as_dict()}
    lease.sha = sha
    return lease.release()


def cmd_bar(args) -> int:
    """Compute the §5 bar from a snapshot payload."""
    raw = _load(args)
    strongest = [tuple(x) for x in (raw.get("strongest") or [])]
    # Attribution needs a checkout to resolve ancestry and hash surfaces. With
    # no repo, the rule falls back to exact SHA equality -- narrower, never
    # wider, so a payload-only run cannot admit a row a repo-backed run would
    # have rejected.
    covers = None
    if args.repo and args.task:
        covers = coverage.covers_factory(Path(args.repo).resolve(), args.task)
    critic = None
    if args.repo and args.task:
        critic, _ = critic_mod.load(Path(args.repo).resolve(), args.task,
                                    raw.get("activeSha") or args.sha or "")
    m = build(
        raw.get("task") or {},
        raw.get("jobs") or [],
        raw.get("trials") or {},
        strongest=strongest,
        steps=tuple(raw.get("steps") or ("1",)),
        active_sha=raw.get("activeSha") or args.sha or "",
        categories=tuple(raw.get("categories") or ()),
        covers=covers,
        critic_receipt=critic,
    )
    all_trials = [t for lst in (raw.get("trials") or {}).values() for t in lst]
    result = evaluate(m, target=args.target)
    result["infra"] = infra_fraction(m.rows, raw.get("jobs") or [])
    result["evidence"] = evidence(all_trials)
    result["reviews"] = [r.__dict__ for r in m.reviews]
    _out(result)
    return 0 if result["verdict"] in {"HARD", "MEDIUM"} else 1


def cmd_record(args) -> int:
    repo = Path(args.repo).resolve()
    task_dir = repo / args.task
    j = Journal.open(repo, args.task)
    payload = _load(args) if args.input else {}
    entry = j.record(
        task_dir=task_dir,
        sha=args.sha or "",
        cls=args.cls,
        fix=args.fix,
        signals=payload.get("signals") or payload.get("task") or {},
        measurement=payload.get("measurement") or payload,
        evidence=payload.get("evidence") or {},
        explain=args.explain or "",
        hardening=args.hardening,
    )
    if args.mode:
        j.set_mode(args.mode)
    if args.open_finding:
        fid, _, rest = args.open_finding.partition("=")
        symptom, _, acceptance = rest.partition("::")
        j.open_finding(fid.strip(), symptom.strip(), acceptance.strip())
    if args.close_finding:
        fid, _, evidence = args.close_finding.partition("=")
        j.close_finding(fid.strip(), args.sha or "", evidence.strip())
    if args.status:
        j.set_status(args.status, oracle_passing=not args.oracle_failing)
    j.save()
    result = {
        "round": entry,
        "stop": j.stop_reason(),
        "status": j.data["status"],
        "mode": j.mode,
        "closure": j.closure_summary(),
        "openFindings": j.open_findings(),
        "journal": str(j.path),
    }
    signals = payload.get("signals") or payload.get("task") or {}
    result["taskStatus"] = _status_transition(
        repo,
        "record",
        args.task,
        mode=j.mode,
        journal_status=str(j.data.get("status") or ""),
        class_name=str(entry.get("class") or ""),
        detail=" ".join(x for x in (args.fix, args.explain) if x),
        sha=(args.sha or str(entry.get("sha") or "")) or None,
        validation=(str(signals.get("validationStatus") or entry.get("cloudStatus") or "")
                    or None),
        review=task_status_mod.review_summary(payload.get("reviews") or []) or None,
        evidence=task_status_mod.evidence_url(payload),
        announce=False,
    )
    _out(result)
    return 0


def cmd_gate(args) -> int:
    repo = Path(args.repo).resolve()
    if args.verify_receipt:
        ok, why = gate_mod.verify_receipt(repo, args.task)
        _out({"ok": ok, "reason": why})
        return 0 if ok else 1
    report = gate_mod.run(
        repo_root=repo,
        task_dir=repo / args.task,
        task_name=args.task,
        measured=args.measured,
        oracle_cmd=args.oracle.split() if args.oracle else None,
        hook_specs=config_mod.load_hooks(repo),
        require=(gate_mod.PUSH_REQUIRED if args.require_push_set
                 else tuple(x for x in (args.require or "").split(",") if x)),
    )
    receipt = gate_mod.write_receipt(repo, args.task, report) if report.ok else None
    if args.json:
        _out({**report.as_dict(), "receipt": receipt})
    else:
        print(report.render())
        if receipt and receipt.get("digest"):
            print(f"  receipt {receipt['digest']} ({receipt['source']}) for {receipt['head'][:8]}")
    return 0 if report.ok else 1


def cmd_queue(args) -> int:
    """Read-only prioritised backlog. Mutates nothing."""
    repo = Path(args.repo).resolve()
    if args.fetch:
        cfg = config_mod.load(repo, project_id=args.gsd_project, assignee=args.gsd_assignee)
        raw = sources.discover(cfg=cfg, with_gsd=not args.no_gsd,
                               require_owner=not args.include_others)
    else:
        raw = json.loads(Path(args.input).read_text()) if args.input else {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    items = build_queue(
        tasks or [],
        journals=read_journals(repo),
        leases=Leases(repo).active(),
        ideas=raw.get("ideas") if isinstance(raw, dict) else None,
    )
    ready = [i for i in items if i.dispatchable]

    # Only post when something moved. An identical queue reposted every poll is
    # noise, and noise is how a real change gets missed.
    snap = repo / ".benchsmith" / "queue.json"
    try:
        before = json.loads(snap.read_text())
    except (OSError, ValueError):
        before = {}
    after = fingerprint(items)
    ch = changes(before, after)
    if args.remember:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps(after, indent=1))

    held = [n.split(":")[0] for n in (raw.get("notes") or [])
            if "reviewer" in n or "already accepted" in n]
    payload = {
        "total": len(items),
        "brief": render(items, held=held),
        "changed": ch["changed"],
        "changes": ch if ch["changed"] else None,
        "changeBrief": render_changes(ch) or None,
        "dispatchable": len(ready),
        "gsd": raw.get("gsd") if isinstance(raw, dict) else None,
        "notes": raw.get("notes") if isinstance(raw, dict) else [],
        "next": [i.as_dict() for i in ready[: args.workers]],
        "queue": [i.as_dict() for i in items],
    }
    if args.json:
        _out(payload)
    else:
        for i in items:
            mark = "  " if i.dispatchable else "· "
            note = i.skip or (f"claimed by {i.claimed_by}" if i.claimed_by else i.reason)
            print(f"{mark}{i.tier:>3} {i.tierName if hasattr(i,'tierName') else '':<0}{i.task:<52} {note}")
        print(f"\n  {len(ready)} dispatchable of {len(items)}; next {min(args.workers, len(ready))}")
    return 0


def cmd_ideas_init(args) -> int:
    _out(ideas_mod.init_board(
        Path(args.repo), name=args.name, owner=args.owner,
        project=args.project, apply=args.apply,
    ))
    return 0


def cmd_ideas_harvest(args) -> int:
    _out(ideas_mod.harvest(
        Path(args.repo), project=args.project, owner=args.owner,
        limit=args.limit, max_cards=args.max_cards, idea_ids=args.idea_id,
        include_unassessed=args.include_unassessed, apply=args.apply,
    ))
    return 0


def cmd_ideas_mark(args) -> int:
    _out(ideas_mod.mark(
        Path(args.repo), args.gsd_task, args.verdict, evidence=args.evidence,
        core_one=args.core_one, core_two=args.core_two,
        project=args.project, apply=args.apply,
    ))
    return 0


def cmd_ideas_references(args) -> int:
    _out(ideas_mod.inspect_reference(args.task_id, binary=args.codimango))
    return 0


def cmd_ideas_landscape(args) -> int:
    _out(ideas_mod.landscape(
        tags=args.tag, seed=args.seed, task_ids=args.task_id,
        limit=args.limit, idea_limit=args.idea_limit, binary=args.codimango,
    ))
    return 0


def cmd_claim(args) -> int:
    r = Leases(Path(args.repo).resolve()).claim(args.task)
    _out(r)
    return 0 if r["ok"] else 1


def cmd_release(args) -> int:
    r = Leases(Path(args.repo).resolve()).release(args.task)
    _out(r)
    return 0 if r["ok"] else 1


def cmd_dispatch(args) -> int:
    """Plan (default) or start one non-publishing worker."""
    idea = None
    if args.mode == "scaffold":
        if not args.card:
            _out({"ok": False, "reason": "--mode scaffold needs --card <GSD ref>; an idea cannot "
                                         "be dispatched by slug alone"})
            return 2
        try:
            idea = resolve_mod.resolve(args.card)
        except resolve_mod.Unresolved as e:
            _out({"ok": False, "reason": str(e)})
            return 2
    try:
        p = dispatch_mod.plan(args.task, args.repo, backend=args.backend, harness=args.harness,
                              skills=args.skills, mode=args.mode, target=args.target,
                              bootstrap=args.bootstrap, idea=idea)
    except dispatch_mod.DispatchRefused as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    if not args.apply:
        _out({"planned": p.as_dict(), "applied": False,
              "hint": "re-run with --apply to actually start it"})
        return 0
    result = dispatch_mod.run(p, apply=True)
    sid = dispatch_mod.session_id(result.get("stdout") or "")
    started = bool(result.get("ok") and sid)
    if sid:
        result["session"] = sid
    elif result.get("ok"):
        result["warning"] = "worker start returned no session id"
    result["taskStatus"] = _status_transition(
        Path(args.repo).resolve(),
        "worker-start" if started else "collect",
        args.task,
        mode=args.mode,
        state="working" if started else "failed",
        detail=result.get("error") or result.get("stderr") or "",
        session=sid or None,
    )
    _out(result)
    return 0


def cmd_mutate(args) -> int:
    """Would the suite catch a near-miss? Survivors are grader holes."""
    task_dir = Path(args.repo) / args.task
    res = mutate_mod.probe(task_dir, args.target or [], shlex.split(args.test_cmd or ""))
    _out(res)
    # NOT_RUN is not a pass. It exits non-zero so a caller cannot read an
    # unrun probe as a clean one.
    return 0 if res["status"] == "PASS" else 1


def cmd_resolve(args) -> int:
    """Turn a name, id, or submissions URL into a bound task."""
    try:
        _out(resolve_mod.resolve(args.ref))
    except resolve_mod.Unresolved as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    return 0


def _active_table_leases(repo: Path, status_repo: Path) -> dict[str, str]:
    """Treat canonical active-owner rows as claims, even after a lease was lost."""
    document = task_status_mod.read(repo, status_repo=status_repo)
    claims: dict[str, str] = {}
    for task, row in (document.get("rows") or {}).items():
        status = str(row.get("status") or "")
        session = str(row.get("workerSession") or "")
        if session and (
            status.startswith("revision:")
            or status in {
                task_status_mod.VALIDATING,
                task_status_mod.READY_TO_PUBLISH,
                task_status_mod.READY_GREEN,
            }
        ):
            claims[str(task)] = f"session:{session}"
    return claims


def cmd_fleet(args) -> int:
    """Plan freely; apply only inside one verified controller epoch."""
    repo = Path(args.repo).resolve() if args.repo else Path.cwd().resolve()
    status_repo_value = str(
        getattr(args, "status_repo", "")
        or os.environ.get("BENCHSMITH_STATUS_ROOT", "")
    ).strip()
    status_repo = (
        Path(status_repo_value).expanduser().resolve()
        if status_repo_value
        else repo
    )
    if not args.apply:
        return _cmd_fleet(args, repo, status_repo, None, 0.0)
    if not status_repo_value:
        raise controller_mod.ControllerRefused(
            "fleet --apply requires --status-repo or BENCHSMITH_STATUS_ROOT"
        )
    session_id = str(
        getattr(args, "session_id", "")
        or os.environ.get("BENCHSMITH_CONTROLLER_SESSION", "")
    ).strip()
    controller_epoch = str(
        getattr(args, "controller_epoch", "")
        or os.environ.get("BENCHSMITH_CONTROLLER_EPOCH", "")
    ).strip()
    canary_image = str(
        getattr(args, "container_canary_image", "")
        or os.environ.get("BENCHSMITH_CANARY_IMAGE", "")
    ).strip()
    min_free_gb = float(
        getattr(args, "min_free_gb", controller_mod.DEFAULT_MIN_FREE_GB)
    )
    admission = controller_mod.admit(
        repo,
        status_repo,
        session_id,
        controller_epoch,
        canary_image=canary_image,
        min_free_gb=min_free_gb,
    )
    with controller_mod.write_fence(
        repo, status_repo, session_id, controller_epoch
    ):
        return _cmd_fleet(args, repo, status_repo, admission, min_free_gb)


def _cmd_fleet(
    args,
    repo: Path,
    status_repo: Path,
    admission: dict | None,
    min_free_gb: float,
) -> int:
    """Discover, dispatch, bind, and persist while the caller holds the fence."""
    cfg = config_mod.load(repo, project_id=args.gsd_project)
    journals = read_journals(repo)
    leases = Leases(repo).active()
    for task, owner in _active_table_leases(repo, status_repo).items():
        leases.setdefault(task, owner)

    # The board is the THIRD source, not a co-equal one. Platform tasks are work
    # that demonstrably exists; a board card is a claim that some does. So the
    # board is not even fetched until the platform cannot fill the slots -- which
    # also means an unconfigured board is invisible on a normal day instead of
    # being a standing complaint.
    raw = sources.discover(cfg=cfg, with_gsd=False)
    items = build_queue(raw.get("tasks") or [], journals=journals, leases=leases)
    ready = [i for i in items if i.dispatchable]
    needs_board = None

    if len(ready) < args.workers and not args.no_gsd:
        if cfg.configured:
            raw = sources.discover(cfg=cfg, with_gsd=True)
            items = build_queue(raw.get("tasks") or [], journals=journals, leases=leases,
                                ideas=raw.get("ideas") or [])
            ready = [i for i in items if i.dispatchable]
        else:
            # Asked for only when it would actually change what happens next,
            # and asked for concretely -- "configure GSD" is not a question
            # anyone can answer without going and finding the number.
            needs_board = {
                "why": (f"only {len(ready)} platform task(s) are dispatchable and "
                        f"{args.workers} workers were asked for; the GSD board is the next source "
                        "and it is not configured"),
                "ask": ("Which GSD board holds your task cards? Paste the URL or the project id — "
                        "it is the number in https://www.internalfb.com/tasks/project/<ID>/list"),
                "thenRun": "benchsmith fleet --gsd-project <id> --workers "
                           f"{args.workers} --apply",
            }

    workers, clamp_note = args.workers, None
    if workers > MAX_WORKERS:
        clamp_note = (f"asked for {workers}; clamped to {MAX_WORKERS}. Past that the limit is the "
                      "devserver and the platform's validation capacity, not benchsmith")
        workers = MAX_WORKERS
    all_ready = ready
    ready = all_ready[:workers]
    waiting = all_ready[workers:]

    # One ls-remote per repository, not one per task. Tasks can resolve to
    # different checkouts, so the state is keyed by the repo it came from.
    _lease_states: dict = {}

    def lease_state_for(target_repo: str) -> dict:
        if target_repo not in _lease_states:
            try:
                _lease_states[target_repo] = rlease_mod.states(target_repo)
            except Exception as e:  # noqa: BLE001 - reported per task, not fatal here
                _lease_states[target_repo] = {"__error__": str(e)}
        return _lease_states[target_repo]

    plans, started = [], []
    run_dir = status_repo / ".benchsmith" / "fleet"
    claimed: list = []
    table_update = None
    announce_status = False
    raw_by_name = {
        str(row.get("name") or row.get("id") or ""): row
        for row in (raw.get("tasks") or [])
    }

    def _status_metadata(task_name: str, plan: dict | None = None) -> dict:
        plan = plan or {}
        source = raw_by_name.get(task_name) or {}
        return {
            "submission_id": str(plan.get("taskId") or source.get("id") or "") or None,
            "sha": str(
                plan.get("sha")
                or source.get("validationCommitSha")
                or source.get("commitSha")
                or ""
            ) or None,
            "validation": (
                str(plan.get("validation") or source.get("validationStatus") or "") or None
            ),
        }

    def _fleet_status_patches() -> list[dict]:
        patches = []
        for item in waiting:
            patches.append(
                task_status_mod.transition_patch(
                    "queued", item.task, **_status_metadata(item.task)
                )
            )
        for plan in plans:
            metadata = _status_metadata(str(plan.get("task") or ""), plan)
            status_task = str(plan.get("workItem") or plan.get("task") or "")
            if plan.get("session") and plan.get("ok"):
                patches.append(
                    task_status_mod.transition_patch(
                        "worker-start",
                        status_task,
                        mode=str(plan.get("mode") or ""),
                        session=str(plan.get("session") or ""),
                        **metadata,
                    )
                )
            elif plan.get("skipped") or plan.get("ok") is False or "session" in plan:
                patches.append(
                    task_status_mod.transition_patch(
                        "collect",
                        str(plan.get("workItem") or plan.get("task") or ""),
                        mode=str(plan.get("mode") or ""),
                        state=("blocked" if plan.get("indeterminate") else
                               "failed" if plan.get("ok") is False or not plan.get("session")
                               else "blocked"),
                        detail=str(
                            plan.get("skipped")
                            or plan.get("error")
                            or plan.get("warning")
                            or ""
                        ),
                        status_source="routing",
                        **metadata,
                    )
                )
        return patches

    def _persist() -> None:
        """Write what has happened so far, after every worker.

        Writing only at the end meant an interrupted run recorded nothing: its
        leases and worktrees leaked, and the supervisor could not name a single
        worker it had started.
        """
        if not args.apply:
            return
        nonlocal table_update
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "current.json").write_text(json.dumps(
                {"started": started, "at": time.time(),
                 "deadline": time.time() + args.max_runtime * 3600,
                 "maxRuntimeHours": args.max_runtime,
                 "plans": [{k: v for k, v in pl.items()
                            if k in ("task", "taskId", "repo", "worktree", "mode", "session",
                                     "workItem", "sha", "validation", "statusRepo", "remoteLease",
                                     "remoteLeaseToken")}
                           for pl in plans if "session" in pl]}, indent=1))
        except OSError:
            pass
        update = _status_updates(
            repo,
            _fleet_status_patches(),
            status_repo=status_repo,
            announce=announce_status,
        )
        if table_update is None or update.get("changed") or update.get("error"):
            table_update = update

    for n, item in enumerate(ready, 1):
        if args.apply:
            print(f"[{n}/{len(ready)}] {item.task}: claiming and preparing…",
                  file=sys.stderr, flush=True)
        # Each worker is dispatched against the checkout that actually holds its
        # task, not against one repo assumed to hold them all.
        info = {}
        try:
            info = resolve_mod.resolve(item.task, rows=raw.get("tasks") or None)
            target, mode = info.get("repo"), info.get("mode", "harden")
        except resolve_mod.Unresolved as e:
            plans.append({"task": item.task, "skipped": f"unresolved: {e}"})
            continue
        if not target:
            plans.append({"task": item.task,
                          "skipped": ("no checkout for track "
                                      f"{info.get('track') or 'unknown'}" if mode == "scaffold"
                                      else "no checkout on this host holds it")})
            continue
        # An idea is dispatched under its PROPOSED SLUG, not the card number: the
        # worker is creating that directory, and a card number is not a name.
        name = (info.get("suggestedSlug") or item.task) if mode == "scaffold" else item.task
        if mode != "scaffold":
            native = passatk_mod.capability(Path(target) / name)
            if native.get("applicable") and not native.get("ready"):
                plans.append({"task": item.task, "skipped": native["reason"],
                              "state": native["state"], "capability": native})
                continue
        if args.apply:
            controller_mod.verify_target(target, min_free_gb=min_free_gb)
        # Its own working tree, or eight workers share one index and stage over
        # each other long before anything reaches a remote.
        # Claim the task across hosts before spending a worker on it. A local
        # lease says nothing about a second machine, and two hosts repairing one
        # task interleave their commits on one branch.
        lease = None
        if args.apply and not args.no_remote_lease:
            try:
                st = lease_state_for(target)
                if "__error__" in st:
                    raise rlease_mod.LeaseLost(st["__error__"])
                lease = rlease_mod.RemoteLease(item.task, target,
                                               known=st.get(item.task, ""))
                got = lease.acquire()
            except rlease_mod.LeaseLost as e:
                # Not knowing who holds it is not the same as nobody holding it.
                plans.append({"task": item.task,
                              "skipped": f"remote lease unreadable: {e}. Pass --no-remote-lease "
                                         "if this repository has no shared remote"})
                continue
            if got.get("held"):
                claimed.append((lease, item.task, target))
            if not got.get("held"):
                plans.append({"task": item.task,
                              "skipped": f"claimed elsewhere: {got.get('reason', '')}",
                              "owner": got.get("owner")})
                continue

        work_in = target
        if args.apply and not args.shared_tree:
            try:
                w = wt_mod.ensure(Path(target), name)
                work_in = w.path
            except wt_mod.WorktreeRefused as e:
                plans.append({"task": item.task, "skipped": f"worktree: {e}"})
                continue
        try:
            p = dispatch_mod.plan(name, work_in, mode=mode, target=args.target, idea=info)
        except dispatch_mod.DispatchRefused as e:
            plans.append({"task": item.task, "skipped": str(e)})
            continue
        entry = {"task": item.task, "workItem": name, "tier": item.tier,
                 "tierName": item.as_dict()["tierName"],
                 "taskId": str(info.get("id") or ""),
                 "repo": target, "worktree": work_in if work_in != target else None,
                 "mode": mode, "sha": str(info.get("sha") or ""),
                 "validation": str(info.get("validation") or ""), "statusRepo": str(status_repo)}
        if lease is not None:
            entry["remoteLease"] = lease.ref
        if mode == "scaffold":
            entry["card"] = info.get("gsd")
            entry["proposedName"] = name
            entry["title"] = info.get("title", "")[:90]
        if args.apply:
            try:
                res = dispatch_mod.run(p, apply=True)
            except Exception:
                if Path(work_in).resolve() != Path(target).resolve():
                    try:
                        wt_mod.release(Path(target), name)
                    except Exception:  # noqa: BLE001 - preserve the dispatch failure
                        pass
                if lease is not None:
                    try:
                        lease.release()
                    except Exception:  # noqa: BLE001 - preserve the dispatch failure
                        pass
                    claimed = [c for c in claimed if c[1] != item.task]
                raise
            # The session id is the only thing that makes a dispatch followable.
            # Returning the raw stdout and leaving the caller to dig it out is
            # how a supervisor loses track of a worker it started.
            sid = dispatch_mod.session_id(res.get("stdout") or "")
            entry["ok"] = res.get("ok")
            entry["session"] = sid
            # Out of the human inbox the moment it starts. It is polled by id,
            # so this costs the coordinator nothing, and twelve rows of
            # machine-to-machine traffic tell the operator nothing.
            if sid and not args.no_snooze:
                entry["snoozed"] = dispatch_mod.snooze(sid)["snoozed"]
            if not sid:
                if res.get("ok"):
                    entry["indeterminate"] = True
                    entry["warning"] = (
                        "worker creation reported success without a session id; "
                        "lease and worktree retained for reconciliation"
                    )
                else:
                    entry["warning"] = (
                        "worker creation failed without a session id"
                    )
            entry["error"] = res.get("error") or (res.get("stderr") or "")[:200] or None
            print(f"[{n}/{len(ready)}] {item.task}: started {sid or '(no session id)'}",
                  file=sys.stderr, flush=True)
            if sid:
                if lease is not None:
                    lifecycle = _bind_worker(
                        lease,
                        sid,
                        work_in,
                        name,
                        status_repo=str(status_repo),
                        submission_id=str(info.get("id") or ""),
                    )
                    entry["leaseBinding"] = lifecycle
                    if not lifecycle["ok"]:
                        entry["ok"] = False
                        entry["skipped"] = "worker terminated because lease binding was not confirmed"
                        plans.append(entry)
                        _persist()
                        continue
                    entry["remoteLeaseToken"] = lease.sha
                else:
                    try:
                        dispatch_mod.write_assignment(
                            work_in,
                            name,
                            sid,
                            "",
                            status_repo=str(status_repo),
                            submission_id=str(info.get("id") or ""),
                        )
                    except OSError as error:
                        entry["warning"] = (
                            "worker started but status assignment was not persisted: "
                            f"{error}"
                        )
                if Path(work_in).resolve() != Path(target).resolve():
                    try:
                        dispatch_mod.write_assignment(
                            target,
                            name,
                            sid,
                            lease.sha if lease is not None else "",
                            lease.task if lease is not None else name,
                            status_repo=str(status_repo),
                            submission_id=str(info.get("id") or ""),
                        )
                    except OSError as error:
                        entry["warning"] = (
                            "worker started but the canonical status assignment was not persisted: "
                            f"{error}"
                        )
                claimed = [c for c in claimed if c[1] != item.task]
                started.append({"task": item.task, "session": sid})
            elif lease is not None:
                if entry.get("indeterminate"):
                    claimed = [c for c in claimed if c[1] != item.task]
                else:
                    lease.release()
        else:
            entry["shell"] = p.shell
        plans.append(entry)
        if args.apply:
            _persist()

    by_repo: dict[str, int] = {}
    for pl in plans:
        if "repo" in pl:
            by_repo[pl["repo"]] = by_repo.get(pl["repo"], 0) + 1
    payload = {"discovered": len(items), "dispatchable": sum(1 for i in items if i.dispatchable),
               "workers": workers, "applied": bool(args.apply),
               # Named because it is the only thing that serialises: one publish
               # lane per repository. It is held for a push and released before
               # validation, so N tasks in one repo cost N pushes, not N cycles.
               "workersPerRepo": by_repo,
               "selected": [p["task"] for p in plans if "skipped" not in p],
               "started": started, "plans": plans, "notes": raw.get("notes") or []}
    if admission is not None:
        payload["admission"] = admission
    if needs_board:
        payload["needsGsdBoard"] = needs_board
    if clamp_note:
        payload["clamped"] = clamp_note
    if "payload_released" in dir():
        payload["releasedUnused"] = payload_released
    announce_status = True
    _persist()
    if args.apply:
        payload["taskStatus"] = table_update or _status_updates(
            repo,
            _fleet_status_patches(),
            status_repo=status_repo,
            announce=True,
        )
    # Anything claimed but never dispatched is released. A lease and a worktree
    # held for a task nobody is working on strands that task from every other
    # host until the TTL expires.
    released = []
    for lease, task_name, target in claimed:
        try:
            wt_mod.release(Path(target), task_name)
            lease.release()
            released.append(task_name)
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            pass
    if released:
        payload_released = released
    if not args.apply:
        payload["hint"] = "re-run with --apply to start these"
    _out(payload)
    return 2 if any(plan.get("indeterminate") for plan in plans) else 0


def cmd_scaffold(args) -> int:
    """Idea -> task, in one call. Resolves the card and dispatches a scaffold worker."""
    try:
        idea = resolve_mod.resolve(args.ref)
    except resolve_mod.Unresolved as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    if idea.get("kind") != "idea":
        _out({"ok": False, "reason": f"{args.ref} resolves to an existing task "
                                     f"({idea.get('task')}), not an idea. Run the round on it "
                                     "instead of scaffolding."})
        return 2
    name = args.name or idea.get("suggestedSlug") or ""
    repo = args.repo or idea.get("repo") or ""

    # Already scaffolded is not an error: the desired end state -- a task the
    # loop can run -- is satisfied. Refusing here strands the flow one step
    # short of the thing it exists to reach, which is what happened when a
    # retry hit the overwrite guard and stopped.
    if repo and name and (Path(repo) / name / "task.toml").is_file():
        _out({"state": "already-scaffolded", "card": idea.get("gsd"), "task": name,
              "repo": repo, "registered": False,
              "next": "run the round on it; it is unregistered, so there are no measurements "
                      "to read until it is authored, gated and pushed",
              "resolve": f"benchsmith resolve {name}"})
        return 0
    try:
        p = dispatch_mod.plan(name, repo, backend=args.backend, mode="scaffold", idea=idea)
    except dispatch_mod.DispatchRefused as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    out = {"card": idea.get("gsd"), "title": idea.get("title"), "track": idea.get("track"),
           "repo": repo, "proposedName": name, "applied": bool(args.apply)}
    if args.apply:
        res = dispatch_mod.run(p, apply=True)
        out["session"] = dispatch_mod.session_id(res.get("stdout") or "")
        out["ok"] = res.get("ok")
        out["taskStatus"] = _status_transition(
            Path(repo).resolve(),
            "worker-start" if res.get("ok") else "collect",
            name,
            mode="scaffold",
            state="working" if res.get("ok") else "failed",
            detail=res.get("error") or res.get("stderr") or "",
            session=out["session"] or None,
        )
        # The point of scaffolding is to get a task the loop can run. Say so, so
        # nobody treats a fresh skeleton as the end of the job.
        out["thenRun"] = f"benchsmith resolve {name}   # then run the round; it will be unregistered"
    else:
        out["shell"] = p.shell
        out["hint"] = "re-run with --apply to start it"
    _out(out)
    return 0


def cmd_relieve(args) -> int:
    """End a worker and start a successor that resumes from the journal.

    A worker that has stalled, errored out, or exhausted its wall clock is
    holding a slot and producing nothing. Its work is not lost: the journal is
    the durable record, so a fresh worker picks up where it left off with a
    clean context rather than inheriting an exhausted one.
    """
    repo = Path(args.repo).resolve()
    out = {"task": args.task, "oldSession": args.session_id}
    try:
        info = resolve_mod.resolve(args.task)
    except resolve_mod.Unresolved as e:
        _out({**out, "successor": None, "reason": str(e)})
        return 2
    lease_repo = Path(info.get("repo") or repo)
    if args.session_id:
        termination = dispatch_mod.relieve(args.session_id)
        out["termination"] = termination
        if not termination.get("terminated"):
            _out({**out, "successor": None,
                  "reason": "old session termination was not confirmed; retaining its lease"})
            return 2
    if args.session_id and not args.no_remote_lease:
        try:
            old_release = _release_session_lease(lease_repo, args.task, args.session_id)
        except rlease_mod.LeaseLost as e:
            old_release = {"released": False, "reason": str(e)}
        out["oldLease"] = old_release
        if not old_release.get("released"):
            _out({**out, "successor": None,
                  "reason": "terminated worker's lease could not be released safely"})
            return 2
    if args.no_successor:
        _out({**out, "successor": None})
        return 0
    work_in = info.get("repo") or str(repo)
    if not args.shared_tree:
        try:
            work_in = wt_mod.ensure(Path(info.get("repo") or repo), args.task).path
        except wt_mod.WorktreeRefused as e:
            _out({**out, "successor": None, "reason": f"worktree: {e}"})
            return 2
    # The replacement needs the claim too. Starting one without a lease is how
    # a "replacement" becomes a second worker on a task somebody still owns.
    lease = None
    if not args.no_remote_lease:
        try:
            lease = rlease_mod.RemoteLease(args.task, lease_repo)
            got = lease.acquire()
        except rlease_mod.LeaseLost as e:
            _out({**out, "successor": None, "reason": f"remote lease unreadable: {e}"})
            return 2
        if not got.get("held"):
            _out({**out, "successor": None,
                  "reason": f"not claiming a successor: {got.get('reason', 'lease held')}",
                  "owner": got.get("owner")})
            return 2
    try:
        p = dispatch_mod.plan(args.task, work_in, mode=info.get("mode", "harden"))
    except dispatch_mod.DispatchRefused as e:
        if lease is not None:
            lease.release()
        _out({**out, "successor": None, "reason": str(e)})
        return 2
    if not args.apply:
        _out({**out, "successorPlanned": p.shell, "applied": False})
        return 0
    res = dispatch_mod.run(p, apply=True)
    sid = dispatch_mod.session_id(res.get("stdout") or "")
    if sid and not args.no_snooze:
        dispatch_mod.snooze(sid)
    if lease is not None:
        if sid:
            lifecycle = _bind_worker(
                lease,
                sid,
                work_in,
                args.task,
                status_repo=str(repo),
                submission_id=str(info.get("id") or ""),
            )
            out["leaseBinding"] = lifecycle
            if not lifecycle["ok"]:
                _out({**out, "successor": None,
                      "reason": "successor lease binding was not confirmed"})
                return 2
            if Path(work_in).resolve() != Path(info.get("repo") or repo).resolve():
                try:
                    dispatch_mod.write_assignment(
                        Path(info.get("repo") or repo),
                        args.task,
                        sid,
                        lease.sha,
                        lease.task,
                        status_repo=str(repo),
                        submission_id=str(info.get("id") or ""),
                    )
                except OSError as error:
                    out["assignmentWarning"] = str(error)
        else:
            # No session means no worker; holding the claim would strand the task.
            lease.release()
    elif sid:
        try:
            dispatch_mod.write_assignment(
                work_in,
                args.task,
                sid,
                "",
                status_repo=str(repo),
                submission_id=str(info.get("id") or ""),
            )
            if Path(work_in).resolve() != Path(info.get("repo") or repo).resolve():
                dispatch_mod.write_assignment(
                    Path(info.get("repo") or repo),
                    args.task,
                    sid,
                    "",
                    status_repo=str(repo),
                    submission_id=str(info.get("id") or ""),
                )
        except OSError as error:
            out["assignmentWarning"] = str(error)
    result = {**out, "successor": sid, "applied": True,
              "note": "the successor resumes from the journal, not from the old session"}
    result["taskStatus"] = _status_transition(
        repo,
        "worker-start" if sid else "collect",
        args.task,
        mode=str(info.get("mode") or ""),
        state="working" if sid else "failed",
        detail=str(res.get("error") or res.get("stderr") or ""),
        submission_id=str(info.get("id") or "") or None,
        session=sid or None,
        sha=str(info.get("sha") or "") or None,
        validation=str(info.get("validation") or "") or None,
    )
    _out(result)
    return 0


def cmd_hold(args) -> int:
    """A bounded claim on the whole branch, for a landing window."""
    repo = Path(args.repo).resolve()
    if args.action == "show":
        _out(hold_mod.current(repo))
    elif args.action == "take":
        _out(hold_mod.take(repo, why=args.why, minutes=args.minutes))
    else:
        _out(hold_mod.release(repo))
    return 0


def cmd_lease(args) -> int:
    """The cross-host claim on a task: show, take, or drop it."""
    repo = Path(args.repo).resolve()
    try:
        lease = rlease_mod.RemoteLease(args.task, repo)
        if args.action == "show":
            sha = lease.remote_sha()
            _out({"ref": lease.ref, "held": bool(sha),
                  "owner": lease.owner(sha).as_dict() if sha else None})
        elif args.action == "take":
            _out({"ref": lease.ref, **lease.acquire()})
        else:
            sha = lease.remote_sha()
            if not sha:
                _out({"ref": lease.ref, "released": False, "reason": "not held"})
                return 0
            who = lease.owner(sha)
            # A lease taken by an earlier command has a dead PID by now. Only
            # a live foreign holder is a reason to refuse.
            if not (who.mine or who.holder_is_gone) and not args.force:
                _out({"ref": lease.ref, "released": False, "owner": who.as_dict(),
                      "reason": (f"held by {who.host} pid {who.pid}, which is still running; "
                                 "pass --force only if you know it is gone")})
                return 2
            lease.sha = sha
            _out({"ref": lease.ref, **lease.release()})
    except rlease_mod.LeaseLost as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    return 0


def cmd_status(args) -> int:
    """What every dispatched worker is doing right now, in one line each."""
    repo = Path(args.repo).resolve()
    try:
        run = json.loads((repo / ".benchsmith" / "fleet" / "current.json").read_text())
    except (OSError, ValueError):
        table_error = ""
        try:
            table = task_status_mod.read(repo)
            table_path = task_status_mod.paths(repo)[1]
            table_snapshot = task_status_mod.snapshot_path(
                repo, int(table.get("revision") or 0)
            )
        except Exception as error:  # noqa: BLE001
            table, table_path, table_snapshot = None, "", None
            table_error = f"{type(error).__name__}: {error}"
        payload = {"workers": [], "reason": "no fleet run recorded in this checkout"}
        if table is not None:
            payload["taskStatus"] = {
                "changed": False,
                "markdown": None,
                "revision": int(table.get("revision") or 0),
                "markdownPath": str(table_path),
                "snapshotPath": (
                    str(table_snapshot)
                    if table_snapshot is not None and table_snapshot.is_file()
                    else None
                ),
                "rows": len(table.get("rows") or {}),
            }
        elif table_error:
            payload["taskStatus"] = {"changed": False, "markdown": None, "error": table_error}
        _out(payload)
        return 0
    rows = []
    patches = []
    try:
        existing_rows = (task_status_mod.read(repo).get("rows") or {})
    except task_status_mod.StatusTableError:
        existing_rows = {}
    plans = run.get("plans") or []
    needs_metadata = any(
        not plan.get("taskId")
        and not (existing_rows.get(str(plan.get("task") or "")) or {}).get("submissionId")
        for plan in plans
    )
    platform_by_name = {}
    if needs_metadata:
        platform_rows, _ = sources.fetch_codimango()
        platform_by_name = {
            str(item.get("name") or item.get("id") or ""): item for item in platform_rows
        }
    for pl in plans:
        sid = pl.get("session") or ""
        task_name = str(pl.get("workItem") or pl.get("task") or "")
        # The worker wrote its handoff inside its own worktree. Reading the
        # canonical checkout finds an older one from a previous round, which is
        # worse than finding none.
        res = dispatch_mod.collect(sid, repo=pl.get("worktree") or pl.get("repo", ""),
                                   task=task_name)
        hand = res.get("handoff") or {}
        platform = platform_by_name.get(str(pl.get("task") or "")) or {}
        stored = existing_rows.get(task_name) or {}
        observed_state = str(hand.get("state") or res.get("state") or "")
        detail = str(hand.get("note") or res.get("reason") or "")
        row = {"task": pl.get("task"), "mode": pl.get("mode"), "session": sid,
               "state": observed_state, "note": detail[:110]}
        # A worker with no handoff yet may be thinking or may have died twenty
        # minutes ago. Both look like silence, and only one deserves the slot.
        if not hand and sid:
            h = dispatch_mod.health(sid)
            row["health"] = h["state"]
            row["idleSeconds"] = h.get("idleSeconds")
            row["errorEvents"] = h.get("errorEvents")
            if h["state"] == "stalled" or h.get("overRuntime"):
                row["relieve"] = (f"benchsmith relieve --repo {args.repo} "
                                  f"--task {pl.get('task')} --session-id {sid} --apply")
                row["why"] = ("over its wall clock" if h.get("overRuntime") else h["reason"])
                observed_state = "needs_human" if h.get("overRuntime") else "blocked"
                detail = row["why"]
            elif h["state"] == "unreadable":
                observed_state = "unreadable"
                detail = h.get("reason") or detail
            else:
                observed_state = h["state"]
                detail = h.get("reason") or detail
        patches.append(
            task_status_mod.transition_patch(
                "collect",
                task_name,
                mode=str(pl.get("mode") or ""),
                state=observed_state,
                detail=detail,
                submission_id=str(
                    pl.get("taskId")
                    or hand.get("submission_id")
                    or platform.get("id")
                    or stored.get("submissionId")
                    or ""
                )
                or None,
                session=str(sid) or None,
                sha=str(
                    hand.get("commit_sha")
                    or pl.get("sha")
                    or platform.get("validationCommitSha")
                    or platform.get("commitSha")
                    or stored.get("sha")
                    or ""
                )
                or None,
                validation=str(
                    hand.get("validation")
                    or pl.get("validation")
                    or platform.get("validationStatus")
                    or stored.get("validation")
                    or ""
                )
                or None,
                review=str(
                    hand.get("review")
                    or platform.get("agenticReviewStatus")
                    or stored.get("review")
                    or ""
                )
                or None,
                evidence=task_status_mod.evidence_url(hand) or None,
            )
        )
        rows.append(row)
    done = [r for r in rows if r["state"] not in ("running", "starting")]
    stuck = [r for r in rows if r.get("relieve")]
    table = _status_updates(repo, patches, status_repo=repo)
    _out({"workers": rows, "running": len(rows) - len(done), "finished": len(done),
          "needRelief": len(stuck), "taskStatus": table})
    return 0


def cmd_task_status(args) -> int:
    """Read the durable fleet table without changing any row."""
    repo = Path(args.repo).resolve()
    try:
        # An explicit render is itself the publication of this revision; consume
        # any pending automatic announcement so the next poll does not repeat it.
        task_status_mod.update_many(repo, [], announce=True)
        document = task_status_mod.read(repo)
    except task_status_mod.StatusTableError as error:
        _out({"ok": False, "reason": str(error)})
        return 2
    if args.json:
        _out(document)
    else:
        print(task_status_mod.render(document), end="")
    return 0


def cmd_worktree(args) -> int:
    """Per-task working trees: list, create, release."""
    repo = Path(args.repo).resolve()
    if args.action == "list":
        _out({"root": str(wt_mod.root(repo)), "worktrees": wt_mod.existing(repo)})
        return 0
    if not args.task:
        _out({"ok": False, "reason": f"--task is required for {args.action}"})
        return 2
    try:
        if args.action == "add":
            _out(wt_mod.ensure(repo, args.task).as_dict())
        else:
            _out(wt_mod.release(repo, args.task, force=args.force))
    except wt_mod.WorktreeRefused as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    return 0


def cmd_rerun(args) -> int:
    """Re-measure the same commit, when the platform failed rather than the task."""
    j = Journal.open(Path(args.repo), args.task)
    rounds = j.data.get("rounds") or []
    last = str((rounds[-1].get("class") or rounds[-1].get("cls") or "")) if rounds else ""
    ok, why = rerun_mod.authorised(last, budget_left=1 if not args.force else 99)
    if not ok:
        _out({"ok": False, "reason": why, "lastClass": last})
        return 2
    _out({"authorised": why, **rerun_mod.trigger(args.task, apply=args.apply)})
    return 0


def cmd_reviewfleet(args) -> int:
    """The other backlog: tasks waiting on you to review them."""
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    rows, notes = sources.fetch_codimango_reviewing()
    items, why_not = rq_mod.build(rows)
    ready = [i for i in items if i.dispatchable][: args.workers]

    _rev_lease_states: dict = {}

    def lease_states_for_review(target_repo: str) -> dict:
        if target_repo not in _rev_lease_states:
            try:
                _rev_lease_states[target_repo] = rlease_mod.states(target_repo)
            except Exception as e:  # noqa: BLE001
                _rev_lease_states[target_repo] = {"__error__": str(e)}
        return _rev_lease_states[target_repo]

    plans, started = [], []
    for n, item in enumerate(ready, 1):
        try:
            info = resolve_mod.resolve(item.task, rows=rows)
        except resolve_mod.Unresolved as e:
            plans.append({"task": item.task, "skipped": f"unresolved: {e}"})
            continue
        target = info.get("repo")
        if not target:
            # A review queue is mostly other people's repositories -- ten
            # assigned reviews here span three, none of them checked out.
            # Skipping is the wrong answer when the tree is a clone away.
            src = str(next((r.get("sourceRepo") for r in rows
                            if str(r.get("name")) == item.task), "") or "")
            got = fetch_mod.ensure(src, sha=str(next(
                (r.get("validationCommitSha") or r.get("commitSha") for r in rows
                 if str(r.get("name")) == item.task), "") or "")) if src else {
                "ok": False, "reason": "the task record names no source repository"}
            if not got.get("ok"):
                plans.append({"task": item.task,
                              "skipped": f"no checkout and could not fetch one: {got.get('reason')}"})
                continue
            target = got["path"]
            entry_fetched = got.get("reused") is False
        else:
            entry_fetched = False
        try:
            p = dispatch_mod.plan(item.task, target, mode="review",
                                  idea={"track": item.track, "due": item.due})
        except dispatch_mod.DispatchRefused as e:
            plans.append({"task": item.task, "skipped": str(e)})
            continue
        # Reviews do not write, but two reviewers on one task is duplicated
        # effort and two drafts that may disagree -- and the second one to
        # finish silently overwrites the first's report file.
        lease = None
        if args.apply and not args.no_remote_lease:
            try:
                st = lease_states_for_review(target)
                if "__error__" in st:
                    raise rlease_mod.LeaseLost(st["__error__"])
                lease = rlease_mod.RemoteLease(item.task, target, known=st.get(item.task, ""))
                got = lease.acquire()
            except rlease_mod.LeaseLost as e:
                plans.append({"task": item.task, "skipped": f"remote lease unreadable: {e}"})
                continue
            if not got.get("held"):
                plans.append({"task": item.task,
                              "skipped": f"already being reviewed: {got.get('reason', '')}"})
                continue

        # A worktree so each reviewer reads one task's tree and nothing else.
        work_in = target
        if args.apply and not args.shared_tree:
            try:
                work_in = wt_mod.ensure(Path(target), item.task).path
            except wt_mod.WorktreeRefused as e:
                plans.append({"task": item.task, "skipped": f"worktree: {e}"})
                continue
        entry = {"task": item.task, "tier": item.tier, "tierName": item.as_dict()["tierName"],
                 "repo": target, "worktree": work_in if work_in != target else None,
                 "track": item.track, "due": item.due, "clonedForReview": entry_fetched}
        if args.apply:
            print(f"[{n}/{len(ready)}] review {item.task}…", file=sys.stderr, flush=True)
            res = dispatch_mod.run(p, apply=True)
            sid = dispatch_mod.session_id(res.get("stdout") or "")
            entry["session"] = sid
            if sid and not args.no_snooze:
                dispatch_mod.snooze(sid)
            if lease is not None:
                if sid:
                    lifecycle = _bind_worker(lease, sid, work_in, item.task)
                    entry["leaseBinding"] = lifecycle
                    if not lifecycle["ok"]:
                        entry["skipped"] = "reviewer terminated because lease binding was not confirmed"
                        plans.append(entry)
                        continue
                    entry["remoteLease"] = lease.ref
                    entry["remoteLeaseToken"] = lease.sha
                else:
                    lease.release()
            if sid:
                started.append({"task": item.task, "session": sid})
        else:
            entry["shell"] = p.shell
        plans.append(entry)

    if args.apply and started:
        rdir = repo / ".benchsmith" / "fleet"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "reviews.json").write_text(json.dumps(
            {"started": started, "at": time.time(),
             "plans": [{k: v for k, v in pl.items()
                        if k in ("task", "repo", "worktree", "track", "session",
                                 "remoteLease", "remoteLeaseToken")}
                       for pl in plans if "session" in pl]}, indent=1))

    _out({"queue": len(items), "dispatchable": sum(1 for i in items if i.dispatchable),
          "brief": rq_mod.render(items, why_not), "notMine": why_not[:8],
          "applied": bool(args.apply), "started": started, "plans": plans, "notes": notes,
          "hint": None if args.apply else "re-run with --apply to start these"})
    return 0


def cmd_reviewstatus(args) -> int:
    """Every drafted review, with its decision and whether the form is complete."""
    repo = Path(args.repo).resolve()
    try:
        run = json.loads((repo / ".benchsmith" / "fleet" / "reviews.json").read_text())
    except (OSError, ValueError):
        _out({"drafted": 0, "reviews": [],
              "reason": "no review run recorded in this checkout"})
        return 0
    res = rr_mod.collect(run.get("plans") or [], links=not args.no_links)
    res["brief"] = rr_mod.render(res)
    # Nothing here submits. That is the one step in the review loop that should
    # stay a person's, and the whole point of gathering them is to make it easy.
    res["note"] = "nothing has been submitted; these are drafts for you to review and send"
    _out(res)
    return 0


def cmd_review(args) -> int:
    """What the reviewer asked for, and whether it has been written down."""
    j = Journal.open(Path(args.repo), args.task)
    req = reviews_mod.requests(args.task)
    blocked, why = reviews_mod.unaddressed(j.data.get("findings") or {}, req)
    _out({**req, "findings": j.data.get("findings") or {},
          "closure": j.closure_summary(), "blocked": blocked, "verdict": why})
    return 1 if blocked else 0


def cmd_causal(args) -> int:
    """Did the last hardening change move the rate, or did the sample?"""
    j = Journal.open(Path(args.repo), args.task)
    _out(causal_mod.assess(j.data.get("rounds") or []).as_dict())
    return 0


def cmd_watch(args) -> int:
    """Has the wave for this exact SHA landed? One cheap read, not a held slot."""
    res = watch_mod.state(args.task, args.sha, pushed_at=args.pushed_at or None)
    res["taskStatus"] = _status_transition(
        Path(args.repo).resolve(),
        "watch",
        args.task,
        status_repo=getattr(args, "status_repo", "") or None,
        state=str(res.get("state") or ""),
        detail=str(res.get("reason") or ""),
        sha=args.sha,
        submission_id=str(res.get("submissionId") or "") or None,
        validation=str(res.get("validation") or "") or None,
        review=str(res.get("review") or "") or None,
        orphaned=bool(res.get("orphaned")),
    )
    _out(res)
    # Only `terminal` means there is something new to act on.
    return 0 if res["state"] == watch_mod.TERMINAL else 1


def cmd_collect(args) -> int:
    """Read one worker's handoff — from disk first, then the session journal."""
    res = dispatch_mod.collect(args.session_id, repo=args.repo, task=args.task)
    handoff = res.get("handoff") or {}
    state = str(handoff.get("state") or "")
    # Back into the inbox exactly when a person is the next step, and not before.
    if state in dispatch_mod.NEEDS_A_HUMAN or res.get("state") == "finished-without-handoff":
        res["surfaced"] = dispatch_mod.surface(args.session_id)["surfaced"]
        res["why_surfaced"] = f"state={state or res.get('state')} needs a person"
    task = str(args.task or handoff.get("work_item") or "")
    if task:
        res["taskStatus"] = _status_transition(
            Path(args.repo or ".").resolve(),
            "collect",
            task,
            status_repo=(getattr(args, "status_repo", "")
                         or handoff.get("status_repo") or None),
            state=state or str(res.get("state") or ""),
            detail=str(handoff.get("note") or res.get("reason") or ""),
            submission_id=str(handoff.get("submission_id") or "") or None,
            session=args.session_id,
            sha=str(handoff.get("commit_sha") or "") or None,
            validation=str(handoff.get("validation") or "") or None,
            review=str(handoff.get("review") or "") or None,
            evidence=task_status_mod.evidence_url(handoff) or None,
        )
    _out(res)
    return 0 if res.get("state") == "done" else 1


def cmd_config(args) -> int:
    """Show the resolved configuration, and how to set what is missing."""
    cfg = config_mod.load(Path(args.repo).resolve())
    payload = {"gsd": cfg.as_dict(),
               "sources": {"user": str(config_mod.USER_CONFIG),
                           "repo": str(Path(args.repo).resolve() / config_mod.REPO_CONFIG),
                           "env": "BENCHSMITH_GSD_PROJECT / BENCHSMITH_GSD_ASSIGNEE"}}
    if args.json:
        _out(payload)
    else:
        print(f"GSD board:  {cfg.project_id or '(not configured)'}")
        print(f"assignee:   {cfg.assignee or '(none)'}")
        print(f"sections:   {json.dumps(cfg.sections)}")
        print(f"user cfg:   {config_mod.USER_CONFIG}")
        print(f"repo cfg:   {Path(args.repo).resolve() / config_mod.REPO_CONFIG}")
        if not cfg.configured:
            print()
            print(config_mod.HOWTO)
    return 0


def cmd_handoff(args) -> int:
    """Durably record a terminal handoff, then release its exact task lease."""
    repo = Path(args.repo).resolve()
    document: dict = {}
    try:
        document = _load(args)
        result = dispatch_mod.finalize_handoff(
            repo,
            args.task,
            document,
            remote=args.remote,
            branch=getattr(args, "branch", "main"),
        )
    except dispatch_mod.CandidateHandoffRefused as error:
        handoff = error.handoff or document
        result = {
            "ok": False,
            "phase": "not-written",
            "reason": str(error),
            "candidateRejected": True,
        }
        result["taskStatus"] = _status_transition(
            repo,
            "handoff",
            args.task,
            status_repo=handoff.get("status_repo") or None,
            state="blocked",
            detail=f"publication safety rejected the candidate: {error}",
            submission_id=str(handoff.get("submission_id") or "") or None,
            session=str(handoff.get("session") or "") or None,
            sha=str(handoff.get("commit_sha") or "") or None,
            announce=False,
        )
        _out(result)
        return 2
    except (OSError, ValueError, dispatch_mod.DispatchRefused) as error:
        _out({"ok": False, "phase": "not-written", "reason": str(error)})
        return 2
    try:
        handoff = dispatch_mod.parse_handoff(Path(result["path"]).read_text())
    except (OSError, dispatch_mod.DispatchRefused):
        handoff = document
    result["taskStatus"] = _status_transition(
        repo,
        "handoff",
        args.task,
        status_repo=handoff.get("status_repo") or None,
        state=str(handoff.get("state") or ""),
        detail=" ".join(
            str(handoff.get(key) or "") for key in ("note", "next_action")
        ).strip(),
        submission_id=str(handoff.get("submission_id") or "") or None,
        session=str(handoff.get("session") or "") or None,
        sha=str(handoff.get("commit_sha") or "") or None,
        validation=str(handoff.get("validation") or "") or None,
        review=str(handoff.get("review") or "") or None,
        evidence=task_status_mod.evidence_url(handoff) or None,
        announce=False,
    )
    _out(result)
    return 0 if result["ok"] else 2


def cmd_fmt(args) -> int:
    """Apply the repo's formatters to staged files. Mutating; run before commit."""
    repo = Path(args.repo).resolve()
    res = hooks_mod.run_all(repo, config_mod.load_hooks(repo), fix=True, budget=args.budget)
    # Re-stage what the formatters rewrote -- the repo hook's last step, and the
    # reason it was worth having.
    if res["ok"] and res["results"] and args.restage:
        paths, _ = hooks_mod.staged(repo)
        if paths:
            subprocess.run(["git", "-C", str(repo), "add", "--"] + paths,
                           capture_output=True, text=True)
            res["restaged"] = len(paths)
    _out(res)
    return 0 if res["ok"] else 1


def cmd_hooks(args) -> int:
    """What the repo ships, what benchsmith would run, and how long it takes."""
    repo = Path(args.repo).resolve()
    specs = config_mod.load_hooks(repo)
    payload = {"repo": hooks_mod.detect(repo),
               "benchsmith": [{"name": s.get("name"), "globs": s.get("globs")} for s in specs]}
    if args.time:
        payload["check"] = hooks_mod.run_all(repo, specs, use_cache=False)
        payload["checkCached"] = hooks_mod.run_all(repo, specs)
    _out(payload)
    return 0


def cmd_corpus(args) -> int:
    """Run the hand-written fixture corpus. Minutes per fixture."""
    from . import fixtures as fx

    task_dir = Path(args.repo).resolve() / args.task

    def runner(td):
        r = subprocess.run([args.bench, "bench", "run", "-p", str(td), "--n-attempts", "1"],
                           capture_output=True, text=True, timeout=args.timeout)
        rewards = re.findall(r'"reward"\s*:\s*([0-9.]+|null)', r.stdout)
        return r.returncode, rewards

    res = fx.run_corpus(task_dir, runner=runner)
    _out(res)
    return 0 if res["state"] == fx.PASS else 1


def cmd_stats(args) -> int:
    _out(stats_mod.collect(Path(args.root)))
    return 0


def cmd_backoff(args) -> int:
    """How long to wait before reading the platform again."""
    j = Journal.open(Path(args.repo), args.task)
    _out(backoff_mod.advise(j.data.get("rounds") or []).as_dict())
    return 0


def cmd_passatk(args) -> int:
    """Difficulty for the local iOS / macOS-VM track."""
    raw = _load(args)
    runs = [passatk_mod.Run(**r) for r in (raw.get("runs") or [])]
    res = passatk_mod.measure(runs, raw.get("build") or "local", k=args.k)
    res["rows"] = [
        {"slot": str(r.slot), "kind": r.kind.value, "note": r.note} for r in res["rows"]
    ]
    _out(res)
    return 0 if res["ok"] else 1


def cmd_publish(args) -> int:
    """The single publication lane. Plans unless --apply."""
    repo = Path(args.repo).resolve()
    handoff = json.loads(Path(args.handoff).read_text()) if args.handoff else json.load(sys.stdin)
    lane = publish_mod.Lane(repo, run_id=args.run_id)
    lease = None
    try:
        if args.apply and not args.no_remote_lease:
            lease = rlease_mod.RemoteLease(args.task, repo, remote=args.remote)
            claim = lease.acquire()
            if not claim.get("held"):
                raise publish_mod.PublishRefused(
                    "publication could not claim the task lease: "
                    + str(claim.get("reason") or claim.get("owner") or "unknown lease state")
                )
        result = publish_mod.publish(repo, args.task, handoff, remote=args.remote,
                                     branch=args.branch, lane=lane, apply=args.apply,
                                     rebase=args.rebase,
                                     allow_review_status=args.allow_review_status,
                                     remote_lease=lease)
        if args.apply or result.get("state") == "rebased":
            result["taskStatus"] = _status_transition(
                repo,
                "publish",
                args.task,
                status_repo=handoff.get("status_repo") or None,
                state=str(result.get("state") or ""),
                detail=str(result.get("detail") or result.get("error") or ""),
                submission_id=str(handoff.get("submission_id") or "") or None,
                session=str(handoff.get("session") or "") or None,
                sha=str(result.get("commit") or result.get("newSha")
                        or handoff.get("commit_sha") or "") or None,
                validation="pending" if result.get("ok") else None,
                evidence=task_status_mod.evidence_url(handoff) or None,
                ok=bool(result.get("ok")),
                needs_regate=bool(result.get("needsRegate")),
            )
        _out(result)
        if args.apply and result.get("ok") is False:
            return 2
    except publish_mod.PublishRefused as e:
        result = {"ok": False, "reason": str(e)}
        if args.apply:
            result["taskStatus"] = _status_transition(
                repo,
                "publish",
                args.task,
                status_repo=handoff.get("status_repo") or None,
                detail=str(e),
                submission_id=str(handoff.get("submission_id") or "") or None,
                session=str(handoff.get("session") or "") or None,
                sha=str(handoff.get("commit_sha") or "") or None,
                ok=False,
            )
        _out(result)
        return 2
    finally:
        if lease is not None and lease.sha and lane.pending() is None:
            lease.release()
    return 0


def cmd_critic_receipt(args) -> int:
    result = critic_mod.ingest(Path(args.repo).resolve(), args.task, args.sha, args.session_id)
    _out(result)
    return 0 if result.get("ok") else 1


def cmd_controls(args) -> int:
    repo = Path(args.repo).resolve()
    result = controls_mod.resolve(repo, args.task, repo / args.task)
    _out(result)
    return 0 if result.get("ok") else 1


def cmd_reconcile(args) -> int:
    """After a crash: did the pending push land?"""
    repo = Path(args.repo).resolve()
    lane = publish_mod.Lane(repo, run_id=args.run_id)
    pending = lane.pending()
    res = lane.reconcile(repo)
    if res["state"] == publish_mod.LANDED and pending and pending.lease_sha:
        lease = rlease_mod.RemoteLease(pending.task, repo, remote=pending.remote)
        released = lease.release(pending.lease_sha)
        res["leaseRelease"] = released
        if released.get("released"):
            lane.clear_intent()
    _out(res)
    # `unknown` must not read as success -- a caller that retries on 0 would
    # double-push exactly when it is least able to tell.
    lease_ok = not res.get("leaseRelease") or res["leaseRelease"].get("released")
    return 0 if res["state"] in ("clean", publish_mod.NOT_LANDED, publish_mod.LANDED) and lease_ok else 1


def cmd_hash(args) -> int:
    _out(surface_hashes(Path(args.repo) / args.task))
    return 0


def cmd_install(args) -> int:
    _out({"hooks": gate_mod.install_hooks(Path(args.repo).resolve(), Path(__file__).resolve().parent)})
    return 0


def cmd_trailers(args) -> int:
    print("\n".join(git_trailers(args.run_id, args.workflow)))
    return 0


def _friendly(e: Exception) -> str | None:
    """A message for the errors a caller can actually act on.

    A traceback tells an agent the tool is broken. Most of these mean it passed
    the wrong thing, which is a different problem with a different fix -- and
    guessing at the difference is how one ends up trying a second argument shape
    instead of a correct id.
    """
    from .adapter import IdentityMismatch, Unresolved

    if isinstance(e, IdentityMismatch):
        return f"{e}. The name and the id disagree: check which task you meant."
    if isinstance(e, Unresolved):
        return f"{e}"
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="benchsmith", description="The hard-task bar for benchmark tasks.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, *, task=True, repo=True):
        if repo:
            sp.add_argument("--repo", default=".", help="repository root")
        if task:
            sp.add_argument("--task", required=True, help="task directory name")
        return sp

    s = sub.add_parser("preflight", help="check composed skills, CLI and env before a round")
    s.add_argument("--repo", default="")
    s.add_argument("--task", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_preflight)

    s = sub.add_parser("controller", help="acquire or verify the fenced fleet controller")
    s.add_argument("action", choices=("acquire", "renew", "verify", "release"))
    s.add_argument("--repo", required=True, help="canonical task repository")
    s.add_argument("--status-repo", required=True, help="canonical shared status root")
    s.add_argument(
        "--session-id",
        default=os.environ.get("BENCHSMITH_CONTROLLER_SESSION", ""),
    )
    s.add_argument(
        "--epoch", default=os.environ.get("BENCHSMITH_CONTROLLER_EPOCH", "")
    )
    s.add_argument(
        "--ttl-sec", type=int, default=controller_mod.DEFAULT_TTL_SECONDS
    )
    s.set_defaults(fn=cmd_controller)

    s = sub.add_parser("probe", help="resolve the platform CLI surface")
    s.set_defaults(fn=cmd_probe)

    s = common(sub.add_parser("read", help="fresh, identity-checked platform read"))
    for flag in ("--task-id", "--task-uuid", "--source-repo", "--sha"):
        s.add_argument(flag, default="")
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("bar", help="compute the hardness bar from a read payload")
    s.add_argument("input", nargs="?", default="-")
    s.add_argument("--sha", default="")
    # Optional: with a checkout, a measurement taken at a descendant commit
    # whose graded and visible surfaces are unchanged still counts. Without
    # one, the rule is exact SHA equality.
    s.add_argument("--repo", default=None)
    s.add_argument("--task", default=None)
    s.add_argument("--target", default=os.environ.get("BENCHSMITH_TARGET", "hard-preferred"),
                   choices=("hard-only", "hard-preferred"))
    s.set_defaults(fn=cmd_bar)

    s = common(sub.add_parser("record", help="append a round to the journal"))
    s.add_argument("--class", dest="cls", required=True)
    s.add_argument("--fix", required=True)
    s.add_argument("--sha", default="")
    s.add_argument("--input", default="")
    s.add_argument("--explain", default="")
    s.add_argument("--hardening", action="store_true")
    s.add_argument("--status", default="")
    s.add_argument("--oracle-failing", action="store_true")
    s.add_argument("--mode", default="", choices=("", "repair", "harden", "scaffold"))
    s.add_argument("--open-finding", default="",
                   help="ID=symptom::acceptance-test — acceptance is mandatory")
    s.add_argument("--close-finding", default="", help="ID=evidence")
    s.set_defaults(fn=cmd_record)

    s = common(sub.add_parser("gate", help="run the pre-push gate"))
    s.add_argument("--measured", default=None)
    s.add_argument("--oracle", default=os.environ.get("BENCHSMITH_ORACLE", ""))
    s.add_argument("--json", action="store_true")
    s.add_argument("--verify-receipt", action="store_true",
                   help="check an existing receipt against the exact clean HEAD")
    s.add_argument("--require", default="",
                   help="comma-separated checks whose NOT_RUN must block")
    s.add_argument("--require-push-set", action="store_true",
                   help=f"require the push-critical set: {','.join(gate_mod.PUSH_REQUIRED)}")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("queue", help="read-only prioritised backlog")
    s.add_argument("--repo", default=".")
    s.add_argument("--input", default="", help="tasks payload JSON (from `read`/`tasks list`)")
    s.add_argument("--fetch", action="store_true",
                   help="discover work from codimango and GSD instead of --input")
    s.add_argument("--gsd-project", default="", help="GSD project id (overrides config/env)")
    s.add_argument("--gsd-assignee", default="", help="GSD assignee (default: you)")
    s.add_argument("--no-gsd", action="store_true", help="codimango only")
    s.add_argument("--include-others", action="store_true",
                   help="queue tasks you do not own (default: refuse)")
    s.add_argument("--workers", type=int, default=3)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-remember", dest="remember", action="store_false", default=True,
                   help="do not update the change-detection snapshot")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("ideas", help="manage a personal GSD board of human T-Bench seeds")
    idea_sub = s.add_subparsers(dest="ideas_cmd", required=True)

    i = idea_sub.add_parser("init", help="plan or create the standalone GSD board")
    i.add_argument("--repo", default=".")
    i.add_argument("--name", default=ideas_mod.DEFAULT_BOARD_NAME)
    i.add_argument("--owner", default="", help="owner unixname (default: authenticated user)")
    i.add_argument("--project", default="", help="connect an existing GSD project")
    i.add_argument("--apply", action="store_true", help="create remote objects and save config")
    i.set_defaults(fn=cmd_ideas_init)

    i = idea_sub.add_parser("harvest", help="copy screen-ready human seeds from Idea Exchange")
    i.add_argument("--repo", default=".")
    i.add_argument("--project", default="", help="override the configured GSD project")
    i.add_argument("--owner", default="", help="owner for created cards")
    i.add_argument("--limit", type=int, default=25, help="bounded Idea Exchange read (1-100)")
    i.add_argument("--max-cards", type=int, default=10)
    i.add_argument("--idea-id", action="append", default=[],
                   help="harvest this exact Idea ID; repeatable")
    i.add_argument("--include-unassessed", action="store_true",
                   help="include seeds without a MEDIUM/HIGH predicted novelty signal")
    i.add_argument("--apply", action="store_true", help="create the planned GSD cards")
    i.set_defaults(fn=cmd_ideas_harvest)

    i = idea_sub.add_parser("mark", help="record a completed pre-build hardness screen")
    i.add_argument("--repo", default=".")
    i.add_argument("--project", default="", help="override the configured GSD project")
    i.add_argument("--gsd-task", required=True)
    i.add_argument("--verdict", required=True, choices=("GO", "DERISK", "KILL"))
    i.add_argument("--evidence", required=True)
    i.add_argument("--core-one", default="")
    i.add_argument("--core-two", default="")
    i.add_argument("--apply", action="store_true", help="update the GSD card")
    i.set_defaults(fn=cmd_ideas_mark)

    i = idea_sub.add_parser(
        "references", help="fail-closed calibration precheck for Codimango examples")
    i.add_argument("task_id", nargs="+")
    i.add_argument("--codimango", default="/usr/local/bin/codimango")
    i.set_defaults(fn=cmd_ideas_references)

    i = idea_sub.add_parser(
        "landscape", help="map submitted-task saturation and compare a human seed")
    i.add_argument("--tag", action="append", default=[],
                   help="sample submitted T-Bench tasks with this tag; repeatable (default: ripen-v1)")
    i.add_argument("--task-id", action="append", default=[],
                   help="include an exact Codimango reference task; repeatable")
    i.add_argument("--seed", default="",
                   help="human-authored seed to compare; omission produces coverage only")
    i.add_argument("--limit", type=int, default=20,
                   help="maximum tasks per tag (1-20)")
    i.add_argument("--idea-limit", type=int, default=10,
                   help="maximum Idea Exchange collision candidates (1-100)")
    i.add_argument("--codimango", default="/usr/local/bin/codimango")
    i.set_defaults(fn=cmd_ideas_landscape)

    s = common(sub.add_parser("claim", help="take an exclusive lease on a task"))
    s.set_defaults(fn=cmd_claim)

    s = common(sub.add_parser("release", help="release a lease"))
    s.set_defaults(fn=cmd_release)

    s = common(sub.add_parser("dispatch", help="plan or start one non-publishing worker"))
    s.add_argument("--backend", default="agentcloud", choices=("agentcloud", "codex", "metacode"))
    s.add_argument("--harness", default=dispatch_mod.DEFAULT_HARNESS)
    s.add_argument("--skills", default=None,
                   help="Skillbook aliases; off by default -- benchsmith is not registered")
    s.add_argument("--bootstrap", dest="bootstrap", action="store_true", default=None,
                   help="force the clone preamble (default: on for agentcloud)")
    s.add_argument("--no-bootstrap", dest="bootstrap", action="store_false")
    s.add_argument("--mode", default="harden", choices=("harden", "repair", "scaffold"))
    s.add_argument("--card", default="",
                   help="GSD reference for --mode scaffold (T123, or a task URL)")
    s.add_argument("--target", default=os.environ.get("BENCHSMITH_TARGET", "hard-preferred"))
    s.add_argument("--apply", action="store_true", help="actually start the worker")
    s.set_defaults(fn=cmd_dispatch)

    s = common(sub.add_parser("mutate", help="probe whether the suite catches near-misses"))
    s.add_argument("--target", action="append", help="file to mutate; repeatable")
    s.add_argument("--test-cmd", default="", help="command that runs the suite")
    s.set_defaults(fn=cmd_mutate)

    s = sub.add_parser("fmt", help="apply the repo's formatters to staged files")
    s.add_argument("--repo", default=".")
    s.add_argument("--budget", type=int, default=hooks_mod.DEFAULT_BUDGET)
    s.add_argument("--no-restage", dest="restage", action="store_false", default=True)
    s.set_defaults(fn=cmd_fmt)

    s = sub.add_parser("hooks", help="repo hooks vs benchsmith's, and their cost")
    s.add_argument("--repo", default=".")
    s.add_argument("--time", action="store_true", help="measure a real run")
    s.set_defaults(fn=cmd_hooks)

    s = common(sub.add_parser("corpus", help="run the hand-written fixture corpus"))
    s.add_argument("--bench", default="codimango")
    s.add_argument("--timeout", type=int, default=3600)
    s.set_defaults(fn=cmd_corpus)

    s = sub.add_parser("scaffold", help="turn an idea card into a task, then loop on it")
    s.add_argument("ref", help="GSD card reference, e.g. T288273925")
    s.add_argument("--name", default="", help="task slug (default: derived from the card title)")
    s.add_argument("--repo", default="", help="checkout (default: canonical one for the track)")
    s.add_argument("--backend", default="codex",
                   choices=("agentcloud", "codex", "metacode"),
                   help="codex by default: it runs on this host, where benchsmith is installed")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_scaffold)

    s = sub.add_parser("hold", help="claim the whole branch for a landing window")
    s.add_argument("action", choices=("show", "take", "release"))
    s.add_argument("--repo", default=".")
    s.add_argument("--why", default="", help="what the window is for")
    s.add_argument("--minutes", type=int, default=hold_mod.DEFAULT_MINUTES)
    s.set_defaults(fn=cmd_hold)

    s = sub.add_parser("lease", help="the cross-host claim on a task")
    s.add_argument("action", choices=("show", "take", "release"))
    s.add_argument("--repo", default=".")
    s.add_argument("--task", required=True)
    s.add_argument("--force", action="store_true", help="release a lease you do not hold")
    s.set_defaults(fn=cmd_lease)

    s = sub.add_parser("worktree", help="per-task working trees")
    s.add_argument("action", choices=("list", "add", "release"))
    s.add_argument("--repo", default=".")
    s.add_argument("--task", default="")
    s.add_argument("--force", action="store_true", help="release even with uncommitted work")
    s.set_defaults(fn=cmd_worktree)

    s = common(sub.add_parser("rerun", help="re-measure the same commit after an infra failure"))
    s.add_argument("--apply", action="store_true")
    s.add_argument("--force", action="store_true", help="ignore the per-commit rerun budget")
    s.set_defaults(fn=cmd_rerun)

    s = sub.add_parser("review-status", help="every drafted review, ready or not")
    s.add_argument("--repo", default=".")
    s.add_argument("--no-links", action="store_true",
                   help="skip creating a paste per review (offline, or a dry look)")
    s.set_defaults(fn=cmd_reviewstatus)

    s = sub.add_parser("review-fleet", help="work the queue of tasks assigned to you to review")
    s.add_argument("--repo", default="")
    s.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--no-snooze", action="store_true")
    s.add_argument("--shared-tree", action="store_true")
    s.add_argument("--no-remote-lease", action="store_true")
    s.set_defaults(fn=cmd_reviewfleet)

    s = common(sub.add_parser("review", help="what the reviewer asked for, and its closure state"))
    s.set_defaults(fn=cmd_review)

    s = common(sub.add_parser("causal", help="did the last hardening change move the rate?"))
    s.set_defaults(fn=cmd_causal)

    s = sub.add_parser("relieve", help="end a stuck worker and start a successor")
    s.add_argument("--repo", default=".")
    s.add_argument("--task", required=True)
    s.add_argument("--session-id", default="")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--no-successor", action="store_true", help="end it without replacing it")
    s.add_argument("--no-remote-lease", action="store_true")
    s.add_argument("--no-snooze", action="store_true")
    s.add_argument("--shared-tree", action="store_true")
    s.set_defaults(fn=cmd_relieve)

    s = sub.add_parser("status", help="what every dispatched worker is doing")
    s.add_argument("--repo", default=".")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("task-status", help="render the durable Markdown task-status table")
    s.add_argument("--repo", default=".")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_task_status)

    s = common(sub.add_parser("watch", help="has the wave for this SHA landed?"))
    s.add_argument("--sha", required=True)
    s.add_argument("--pushed-at", type=float, default=0.0,
                   help="unix time of the push, so a stalled import can be called orphaned")
    s.add_argument("--status-repo", default="",
                   help="coordinator checkout holding the shared task-status table")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("collect", help="read a worker session and return its handoff")
    s.add_argument("--session-id", required=True)
    s.add_argument("--repo", default="", help="read the handoff file from here first")
    s.add_argument("--task", default="")
    s.add_argument("--status-repo", default="",
                   help="coordinator checkout holding the shared task-status table")
    s.set_defaults(fn=cmd_collect)

    s = common(sub.add_parser("handoff", help="persist a terminal handoff and release its lease"))
    s.add_argument("input", nargs="?", default="-")
    s.add_argument("--remote", default="origin")
    s.add_argument("--branch", default="main")
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("resolve", help="task name, id, or submissions URL -> a bound task")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("fleet", help="discover, order, and dispatch down the priority list")
    s.add_argument("--repo", default="", help="where journals and leases live (default: cwd)")
    s.add_argument("--status-repo", default="", help="canonical shared task-status root; required with --apply")
    s.add_argument(
        "--session-id",
        default=os.environ.get("BENCHSMITH_CONTROLLER_SESSION", ""),
        help="exact fenced coordinator session; required with --apply",
    )
    s.add_argument(
        "--controller-epoch",
        default=os.environ.get("BENCHSMITH_CONTROLLER_EPOCH", ""),
        help="controller epoch returned by `benchsmith controller acquire`",
    )
    s.add_argument(
        "--container-canary-image",
        default=os.environ.get("BENCHSMITH_CANARY_IMAGE", ""),
        help="preloaded local image used for the real container-start admission canary",
    )
    s.add_argument(
        "--min-free-gb",
        type=float,
        default=controller_mod.DEFAULT_MIN_FREE_GB,
        help="minimum free disk before any fleet side effect",
    )
    s.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"concurrent workers (default {DEFAULT_WORKERS}, clamped at {MAX_WORKERS})")
    s.add_argument("--gsd-project", default="")
    s.add_argument("--no-gsd", action="store_true")
    s.add_argument("--target", default=os.environ.get("BENCHSMITH_TARGET", "hard-preferred"))
    s.add_argument("--apply", action="store_true", help="actually start the workers")
    s.add_argument("--no-snooze", action="store_true",
                   help="leave worker sessions in the AgentCloud inbox")
    s.add_argument("--shared-tree", action="store_true",
                   help="run workers in the checkout itself instead of per-task worktrees")
    s.add_argument("--no-remote-lease", action="store_true",
                   help="skip the cross-host claim (single host, or no shared remote)")
    s.add_argument("--max-runtime", type=float, default=24.0,
                   help="hours before the run should wind down (recorded, not enforced)")
    s.set_defaults(fn=cmd_fleet)

    s = sub.add_parser("config", help="show the resolved configuration")
    s.add_argument("--repo", default=".")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("stats", help="what this loop has actually done")
    s.add_argument("--root", default=".")
    s.set_defaults(fn=cmd_stats)

    s = common(sub.add_parser("backoff", help="how long to wait after a platform round"))
    s.set_defaults(fn=cmd_backoff)

    s = sub.add_parser("passatk", help="difficulty for the local iOS / macOS-VM track")
    s.add_argument("input", nargs="?", default="-")
    s.add_argument("--k", type=int, default=1)
    s.set_defaults(fn=cmd_passatk)

    s = common(sub.add_parser("publish", help="the single publication lane for this repository"))
    s.add_argument("--handoff", default="", help="worker handoff JSON (default: stdin)")
    s.add_argument("--remote", default="origin")
    s.add_argument("--branch", default="main")
    s.add_argument("--run-id", default="")
    s.add_argument("--apply", action="store_true", help="actually push")
    s.add_argument("--allow-review-status", default="",
                   help="override a review hold; must name the CURRENT status exactly, "
                        "and never permits a frozen (accepted / training) task")
    s.add_argument("--rebase", action="store_true",
                   help="if the remote moved but this task is untouched, rebase onto it")
    s.add_argument("--no-remote-lease", action="store_true",
                   help="publish without a shared task lease (repositories with no shared remote only)")
    s.set_defaults(fn=cmd_publish)

    s = common(sub.add_parser("critic-receipt", help="ingest an exact-SHA critic session receipt"))
    s.add_argument("--session-id", required=True)
    s.add_argument("--sha", required=True)
    s.set_defaults(fn=cmd_critic_receipt)

    s = common(sub.add_parser("controls", help="resolve declared, detected, and required controls"))
    s.set_defaults(fn=cmd_controls)

    s = sub.add_parser("reconcile", help="after a crash: did the pending push land?")
    s.add_argument("--repo", default=".")
    s.add_argument("--run-id", default="")
    s.set_defaults(fn=cmd_reconcile)

    s = common(sub.add_parser("hash", help="graded and visible surface hashes"))
    s.set_defaults(fn=cmd_hash)

    s = common(sub.add_parser("install-hooks", help="install the pre-push gate"), task=False)
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("trailers", help="print the provenance commit trailers")
    s.add_argument("--run-id", required=True)
    s.add_argument("--workflow", required=True)
    s.set_defaults(fn=cmd_trailers)

    args = p.parse_args(argv)
    try:
        try:
            return args.fn(args)
        except Exception as e:  # noqa: BLE001 - a traceback reads as a broken tool
            friendly = _friendly(e)
            if friendly is None:
                raise
            _out({"ok": False, "reason": friendly})
            return 2
    except (ValueError, FileNotFoundError) as e:
        # A refused operation is a result, not a crash. The message is the point.
        print(f"benchsmith: {e}", file=sys.stderr)
        return 2
