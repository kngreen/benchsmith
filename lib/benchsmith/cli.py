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
from . import gate as gate_mod
from . import ideas as ideas_mod
from . import preflight as preflight_mod
from . import backoff as backoff_mod
from . import config as config_mod
from . import coverage
from . import hooks as hooks_mod
from . import dispatch as dispatch_mod
from . import mutate as mutate_mod
from . import passatk as passatk_mod
from . import resolve as resolve_mod
from . import publish as publish_mod
from . import sources
from . import stats as stats_mod
from .queue import (DEFAULT_WORKERS, MAX_WORKERS, Leases, build_queue, changes,
                    fingerprint, read_journals, render, render_changes)
from .adapter import Identity, Platform, Unresolved, discover
from .bar import evaluate
from .journal import Journal, git_trailers, surface_hashes
from .snapshot import build, evidence, infra_fraction


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


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
    m = build(
        raw.get("task") or {},
        raw.get("jobs") or [],
        raw.get("trials") or {},
        strongest=strongest,
        steps=tuple(raw.get("steps") or ("1",)),
        active_sha=raw.get("activeSha") or args.sha or "",
        categories=tuple(raw.get("categories") or ()),
        covers=covers,
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
    _out({"round": entry, "stop": j.stop_reason(), "status": j.data["status"],
          "mode": j.mode, "closure": j.closure_summary(), "openFindings": j.open_findings(),
          "journal": str(j.path)})
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
    _out(dispatch_mod.run(p, apply=True))
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


def cmd_fleet(args) -> int:
    """The whole coordinator in one call: discover, order, dispatch.

    Plans by default. `--apply` is the only thing that starts a worker, so the
    same command can always be run first to see what it would do.
    """
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    cfg = config_mod.load(repo, project_id=args.gsd_project)
    journals, leases = read_journals(repo), Leases(repo).active()

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
    ready = ready[:workers]

    plans, started = [], []
    for item in ready:
        # Each worker is dispatched against the checkout that actually holds its
        # task, not against one repo assumed to hold them all.
        info = {}
        try:
            info = resolve_mod.resolve(item.task)
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
        try:
            p = dispatch_mod.plan(name, target, mode=mode, target=args.target, idea=info)
        except dispatch_mod.DispatchRefused as e:
            plans.append({"task": item.task, "skipped": str(e)})
            continue
        entry = {"task": item.task, "tier": item.tier, "tierName": item.as_dict()["tierName"],
                 "repo": target, "mode": mode}
        if mode == "scaffold":
            entry["card"] = info.get("gsd")
            entry["proposedName"] = name
            entry["title"] = info.get("title", "")[:90]
        if args.apply:
            res = dispatch_mod.run(p, apply=True)
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
                entry["warning"] = "started but returned no session id; it cannot be followed"
            entry["error"] = res.get("error") or (res.get("stderr") or "")[:200] or None
            started.append({"task": item.task, "session": sid})
        else:
            entry["shell"] = p.shell
        plans.append(entry)

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
    if needs_board:
        payload["needsGsdBoard"] = needs_board
    if clamp_note:
        payload["clamped"] = clamp_note
    if args.apply and started:
        # Written down because a coordinator that only remembers its workers
        # in-context forgets them on compaction, and an unremembered worker is
        # one nobody collects.
        run_dir = repo / ".benchsmith" / "fleet"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "current.json").write_text(json.dumps(
            {"started": started, "at": time.time(),
             "plans": [{k: v for k, v in pl.items() if k in ("task", "repo", "mode", "session")}
                       for pl in plans if "session" in pl]}, indent=1))
    if not args.apply:
        payload["hint"] = "re-run with --apply to start these"
    _out(payload)
    return 0


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
        # The point of scaffolding is to get a task the loop can run. Say so, so
        # nobody treats a fresh skeleton as the end of the job.
        out["thenRun"] = f"benchsmith resolve {name}   # then run the round; it will be unregistered"
    else:
        out["shell"] = p.shell
        out["hint"] = "re-run with --apply to start it"
    _out(out)
    return 0


def cmd_status(args) -> int:
    """What every dispatched worker is doing right now, in one line each."""
    repo = Path(args.repo).resolve()
    try:
        run = json.loads((repo / ".benchsmith" / "fleet" / "current.json").read_text())
    except (OSError, ValueError):
        _out({"workers": [], "reason": "no fleet run recorded in this checkout"})
        return 0
    rows = []
    for pl in run.get("plans") or []:
        sid = pl.get("session") or ""
        res = dispatch_mod.collect(sid, repo=pl.get("repo", ""), task=pl.get("task", ""))
        hand = res.get("handoff") or {}
        rows.append({"task": pl.get("task"), "mode": pl.get("mode"), "session": sid,
                     "state": hand.get("state") or res.get("state"),
                     "note": (hand.get("note") or res.get("reason") or "")[:110]})
    done = [r for r in rows if r["state"] not in ("running", "starting")]
    _out({"workers": rows, "running": len(rows) - len(done), "finished": len(done)})
    return 0


def cmd_collect(args) -> int:
    """Read one worker's handoff — from disk first, then the session journal."""
    res = dispatch_mod.collect(args.session_id, repo=args.repo, task=args.task)
    state = str((res.get("handoff") or {}).get("state") or "")
    # Back into the inbox exactly when a person is the next step, and not before.
    if state in dispatch_mod.NEEDS_A_HUMAN or res.get("state") == "finished-without-handoff":
        res["surfaced"] = dispatch_mod.surface(args.session_id)["surfaced"]
        res["why_surfaced"] = f"state={state or res.get('state')} needs a person"
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
    try:
        _out(publish_mod.publish(repo, args.task, handoff, remote=args.remote,
                                 branch=args.branch, lane=lane, apply=args.apply))
    except publish_mod.PublishRefused as e:
        _out({"ok": False, "reason": str(e)})
        return 2
    return 0


def cmd_reconcile(args) -> int:
    """After a crash: did the pending push land?"""
    repo = Path(args.repo).resolve()
    res = publish_mod.Lane(repo, run_id=args.run_id).reconcile(repo)
    _out(res)
    # `unknown` must not read as success -- a caller that retries on 0 would
    # double-push exactly when it is least able to tell.
    return 0 if res["state"] in ("clean", publish_mod.NOT_LANDED, publish_mod.LANDED) else 1


def cmd_hash(args) -> int:
    _out(surface_hashes(Path(args.repo) / args.task))
    return 0


def cmd_install(args) -> int:
    _out({"hooks": gate_mod.install_hooks(Path(args.repo).resolve(), Path(__file__).resolve().parent)})
    return 0


def cmd_trailers(args) -> int:
    print("\n".join(git_trailers(args.run_id, args.workflow)))
    return 0


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

    s = sub.add_parser("probe", help="resolve the platform CLI surface")
    s.set_defaults(fn=cmd_probe)

    s = common(sub.add_parser("read", help="fresh, identity-checked platform read"), repo=False)
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

    s = sub.add_parser("status", help="what every dispatched worker is doing")
    s.add_argument("--repo", default=".")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("collect", help="read a worker session and return its handoff")
    s.add_argument("--session-id", required=True)
    s.add_argument("--repo", default="", help="read the handoff file from here first")
    s.add_argument("--task", default="")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("resolve", help="task name, id, or submissions URL -> a bound task")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("fleet", help="discover, order, and dispatch down the priority list")
    s.add_argument("--repo", default="", help="where journals and leases live (default: cwd)")
    s.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"concurrent workers (default {DEFAULT_WORKERS}, clamped at {MAX_WORKERS})")
    s.add_argument("--gsd-project", default="")
    s.add_argument("--no-gsd", action="store_true")
    s.add_argument("--target", default=os.environ.get("BENCHSMITH_TARGET", "hard-preferred"))
    s.add_argument("--apply", action="store_true", help="actually start the workers")
    s.add_argument("--no-snooze", action="store_true",
                   help="leave worker sessions in the AgentCloud inbox")
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
    s.set_defaults(fn=cmd_publish)

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
        return args.fn(args)
    except (ValueError, FileNotFoundError) as e:
        # A refused operation is a result, not a crash. The message is the point.
        print(f"benchsmith: {e}", file=sys.stderr)
        return 2
