"""The iOS / macOS-VM track, where difficulty is measured locally.

This track does not produce platform jobs. `codimango bench ios run` executes a
task through `run.sh` against `oracle`, `claude-code` or `metacode`, so the
measurement is a set of local runs rather than a validation sweep, and the
cohorts are not the ones the hosted tracks use.

Everything downstream is deliberately unchanged: this normalises local runs into
the same `Row` objects `bar.evaluate` already consumes. A second difficulty
implementation for a second track is how two tracks come to disagree about what
"hard" means.

Three things are genuinely different here, and each is enforced rather than
noted:

  * **oracle is not a cohort.** It is the reference. It must pass, and it is
    never difficulty evidence -- pooling it would dilute every rate with a run
    that is supposed to succeed.
  * **Two cohorts exist at all.** `claude-code` and `metacode` are the whole
    roster, so the two-family requirement is satisfiable only exactly. Losing
    one cohort does not weaken the evidence, it ends it.
  * **metacode is the model under test**, so a metacode sweep is subject to the
    same saturation block as avocado on the hosted tracks.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass
from math import comb
from pathlib import Path

from .model import Kind, Row, SlotKey

ORACLE = "oracle"
# family_of() maps these onto the shared family names, so a cohort here pools
# with the same family on a hosted track rather than forming a parallel one.
COHORTS = {"claude-code": "opus", "metacode": "avocado"}
MODEL_UNDER_TEST = frozenset({"avocado"})


def capability(task_dir: Path, *, system: str | None = None, backend: str | None = None) -> dict:
    """Return whether this host has a proven execution route for an iOS task."""
    task_dir = Path(task_dir)
    is_ios = (task_dir / "environment" / "vm.conf").is_file()
    if not is_ios:
        return {"applicable": False, "ready": True, "examined": 1}
    host_system = system or platform.system()
    configured = backend if backend is not None else os.environ.get("BENCHSMITH_IOS_BACKEND", "")
    if host_system == "Darwin":
        return {"applicable": True, "ready": True, "backend": "local-darwin", "examined": 2}
    resolved = shutil.which(configured) if configured else None
    if resolved:
        return {"applicable": True, "ready": True, "backend": resolved, "examined": 3}
    return {
        "applicable": True,
        "ready": False,
        "state": "unavailable",
        "examined": 3,
        "reason": (f"iOS task requires Darwin or an executable BENCHSMITH_IOS_BACKEND; "
                   f"this host is {host_system}"),
    }


@dataclass(frozen=True)
class Run:
    """One local execution of run.sh."""

    agent: str
    passed: bool
    step: str = "1"
    ordinal: int = 0
    note: str = ""


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass@k: the chance at least one of k sampled runs passes.

    None when it is not defined -- fewer runs than k, or no runs. A caller that
    wants a number for an undefined quantity is asking the wrong question, and
    returning 0.0 would read as "never passes".
    """
    if n <= 0 or k <= 0 or k > n or c < 0 or c > n:
        return None
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def check_oracle(runs: list[Run]) -> tuple[bool, str]:
    """The reference must pass every time before any rate means anything."""
    oracle = [r for r in runs if r.agent == ORACLE]
    if not oracle:
        return False, "no oracle run; the task is unproven, not hard"
    failed = [r for r in oracle if not r.passed]
    if failed:
        return False, (f"oracle failed {len(failed)} of {len(oracle)} runs; this is a task defect, "
                       "not difficulty -- fix it before reading any cohort rate")
    return True, f"oracle passed {len(oracle)}/{len(oracle)}"


def to_rows(runs: list[Run], build: str) -> tuple[list[Row], list[str]]:
    """Normalise local runs into the shared Row shape."""
    rows: list[Row] = []
    notes: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for r in runs:
        if r.agent == ORACLE:
            continue  # the reference is not a participant
        family = COHORTS.get(r.agent)
        if family is None:
            notes.append(f"unknown agent {r.agent!r}; excluded — not silently pooled")
            continue
        key = (family, r.step)
        ordinal = r.ordinal or seen.get(key, 0)
        seen[key] = ordinal + 1
        rows.append(
            Row(
                slot=SlotKey(stage="passatk", family=family, build=build, step=r.step, ordinal=ordinal),
                # A local run reports pass or fail and nothing about why. That is
                # G, not A: attributing a semantic cause we did not observe is
                # how a harness failure becomes fake hardness evidence.
                kind=Kind.PASS if r.passed else Kind.G,
                note=r.note,
            )
        )
    return rows, notes


def measure(runs: list[Run], build: str, *, k: int = 1) -> dict:
    """Oracle check, pass@k per cohort, and rows for the shared bar."""
    ok, why = check_oracle(runs)
    rows, notes = to_rows(runs, build)

    per: dict[str, dict] = {}
    for agent, family in COHORTS.items():
        mine = [r for r in runs if r.agent == agent]
        n, c = len(mine), sum(1 for r in mine if r.passed)
        per[family] = {"agent": agent, "runs": n, "passes": c, "passAtK": pass_at_k(n, c, k)}

    present = [f for f, v in per.items() if v["runs"] > 0]
    saturated = [f for f in present if per[f]["runs"] and per[f]["passes"] == per[f]["runs"]]
    blocking: list[str] = []
    if not ok:
        blocking.append(why)
    if len(present) < 2:
        blocking.append(
            f"only {len(present)} cohort ran ({', '.join(present) or 'none'}); this track has "
            "exactly two, so the two-family requirement cannot be met by any other cohort"
        )
    for f in saturated:
        if f in MODEL_UNDER_TEST:
            blocking.append(f"{f} solved every run; the model under test is saturated")

    return {
        "oracle": {"ok": ok, "detail": why},
        "cohorts": per,
        "k": k,
        "rows": rows,
        "notes": notes,
        "blocking": blocking,
        "ok": not blocking,
    }
