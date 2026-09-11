"""`benchsmith <subcommand>` — the one entry point.

Subcommands are deliberately thin: they resolve inputs, call a module, and print
JSON or a rendered report. Judgement lives in SKILL.md; arithmetic lives in the
modules; neither lives here.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from . import gate as gate_mod
from . import preflight as preflight_mod
from . import backoff as backoff_mod
from . import coverage
from . import dispatch as dispatch_mod
from . import mutate as mutate_mod
from . import passatk as passatk_mod
from . import stats as stats_mod
from .queue import Leases, build_queue, read_journals
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
    raw = json.loads(Path(args.input).read_text()) if args.input else {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    items = build_queue(
        tasks or [],
        journals=read_journals(repo),
        leases=Leases(repo).active(),
        ideas=raw.get("ideas") if isinstance(raw, dict) else None,
    )
    ready = [i for i in items if i.dispatchable]
    payload = {
        "total": len(items),
        "dispatchable": len(ready),
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
    try:
        p = dispatch_mod.plan(args.task, args.repo, backend=args.backend, harness=args.harness,
                              skills=args.skills, mode=args.mode, target=args.target,
                              bootstrap=args.bootstrap)
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
    s.add_argument("--mode", default="", choices=("", "repair", "harden"))
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
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("queue", help="read-only prioritised backlog")
    s.add_argument("--repo", default=".")
    s.add_argument("--input", default="", help="tasks payload JSON (from `read`/`tasks list`)")
    s.add_argument("--workers", type=int, default=3)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_queue)

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
    s.add_argument("--mode", default="harden", choices=("harden", "repair"))
    s.add_argument("--target", default=os.environ.get("BENCHSMITH_TARGET", "hard-preferred"))
    s.add_argument("--apply", action="store_true", help="actually start the worker")
    s.set_defaults(fn=cmd_dispatch)

    s = common(sub.add_parser("mutate", help="probe whether the suite catches near-misses"))
    s.add_argument("--target", action="append", help="file to mutate; repeatable")
    s.add_argument("--test-cmd", default="", help="command that runs the suite")
    s.set_defaults(fn=cmd_mutate)

    s = sub.add_parser("stats", help="what this loop has actually done")
    s.add_argument("--root", default=".")
    s.set_defaults(fn=cmd_stats)

    s = common(sub.add_parser("backoff", help="how long to wait after a platform round"))
    s.set_defaults(fn=cmd_backoff)

    s = sub.add_parser("passatk", help="difficulty for the local iOS / macOS-VM track")
    s.add_argument("input", nargs="?", default="-")
    s.add_argument("--k", type=int, default=1)
    s.set_defaults(fn=cmd_passatk)

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
