"""`assay <subcommand>` — the one entry point.

Subcommands are deliberately thin: they resolve inputs, call a module, and print
JSON or a rendered report. Judgement lives in SKILL.md; arithmetic lives in the
modules; neither lives here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import gate as gate_mod
from .adapter import Identity, Platform, Unresolved, discover
from .bar import evaluate
from .journal import Journal, git_trailers, surface_hashes
from .snapshot import build, evidence, infra_fraction


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _identity(args) -> Identity:
    return Identity(
        task_name=args.task,
        task_id=args.task_id or os.environ.get("ASSAY_TASK_ID", ""),
        task_uuid=args.task_uuid or os.environ.get("ASSAY_TASK_UUID", ""),
        source_repo=args.source_repo or os.environ.get("ASSAY_SOURCE_REPO", ""),
        active_sha=args.sha or os.environ.get("ASSAY_ACTIVE_SHA", ""),
    )


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
    m = build(
        raw.get("task") or {},
        raw.get("jobs") or [],
        raw.get("trials") or {},
        strongest=strongest,
        steps=tuple(raw.get("steps") or ("1",)),
        active_sha=raw.get("activeSha") or args.sha or "",
        categories=tuple(raw.get("categories") or ()),
    )
    all_trials = [t for lst in (raw.get("trials") or {}).values() for t in lst]
    result = evaluate(m, target=args.target)
    result["infra"] = infra_fraction(m.rows)
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
    if args.status:
        j.set_status(args.status, oracle_passing=not args.oracle_failing)
    j.save()
    _out({"round": entry, "stop": j.stop_reason(), "status": j.data["status"], "journal": str(j.path)})
    return 0


def cmd_gate(args) -> int:
    repo = Path(args.repo).resolve()
    report = gate_mod.run(
        repo_root=repo,
        task_dir=repo / args.task,
        task_name=args.task,
        measured=args.measured,
        oracle_cmd=args.oracle.split() if args.oracle else None,
    )
    if args.json:
        _out(report.as_dict())
    else:
        print(report.render())
    return 0 if report.ok else 1


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
    p = argparse.ArgumentParser(prog="assay", description="The hard-task bar for benchmark tasks.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, *, task=True, repo=True):
        if repo:
            sp.add_argument("--repo", default=".", help="repository root")
        if task:
            sp.add_argument("--task", required=True, help="task directory name")
        return sp

    s = sub.add_parser("probe", help="resolve the platform CLI surface")
    s.set_defaults(fn=cmd_probe)

    s = common(sub.add_parser("read", help="fresh, identity-checked platform read"), repo=False)
    for flag in ("--task-id", "--task-uuid", "--source-repo", "--sha"):
        s.add_argument(flag, default="")
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("bar", help="compute the hardness bar from a read payload")
    s.add_argument("input", nargs="?", default="-")
    s.add_argument("--sha", default="")
    s.add_argument("--target", default=os.environ.get("ASSAY_TARGET", "hard-preferred"),
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
    s.set_defaults(fn=cmd_record)

    s = common(sub.add_parser("gate", help="run the pre-push gate"))
    s.add_argument("--measured", default=None)
    s.add_argument("--oracle", default=os.environ.get("ASSAY_ORACLE", ""))
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_gate)

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
        print(f"assay: {e}", file=sys.stderr)
        return 2
