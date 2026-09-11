"""Say what is missing and what degrades, before a round rather than during one.

A composed skill that is absent must fail loudly. Field report, nine rounds:
every skill the loop composes with was missing, nothing said so, and the operator
improvised the mechanics by hand for the whole run.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Where a skill can legitimately live, in resolution order.
SKILL_ROOTS = (
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "agent-market" / "plugins" / "team-aai" / "skills",
    Path.home() / ".claude" / "agent-market" / "plugins" / "aai-long-horizon" / "skills",
    Path.home() / ".codex" / "skills",
)

# name -> (what it is used for, what happens without it)
COMPOSED = {
    "task-hardness-screen": (
        "§3 intake kill-tests, GO/DERISK/KILL",
        "intake runs on §3's three additions only; record `screen: not_run` and carry "
        "`unscreened` into the terminal report",
    ),
    "mt-calibrate": (
        "§5 per-step calibration at k>=10",
        "per-step pass/failure comes from a 5-trial cascade, which cannot tell an "
        "unreached step from a failed one; treat per-step rows as unverified",
    ),
    "task-fairness-signal": (
        "§7 genuine failure vs grader false negative",
        "cause attribution is unavailable; every semantic non-pass is unverified, so "
        "`attribution-unavailable` blocks HARD and the bar can reach MEDIUM at best",
    ),
    "codimango-review-critic": (
        "§10a required second-pass review",
        "the `review-critic` row is absent, which is an absent verdict; no terminal "
        "GREEN is available",
    ),
}


def _find(name: str) -> Path | None:
    for root in SKILL_ROOTS:
        p = root / name
        if p.is_dir() and (p / "SKILL.md").is_file():
            return p
    return None


def run(repo_root: Path | None = None, task: str | None = None) -> dict:
    rows, degraded, blocking = [], [], []

    for name, (used_for, without) in COMPOSED.items():
        found = _find(name)
        rows.append(
            {
                "kind": "skill",
                "name": name,
                "state": "present" if found else "MISSING",
                "path": str(found) if found else None,
                "usedFor": used_for,
                "degradesTo": None if found else without,
            }
        )
        if not found:
            (blocking if name == "codimango-review-critic" else degraded).append(name)

    binary = os.environ.get("BENCHSMITH_CODIMANGO", "codimango")
    where = shutil.which(binary)
    rows.append(
        {
            "kind": "cli",
            "name": binary,
            "state": "present" if where else "MISSING",
            "path": where,
            "usedFor": "every platform read",
            "degradesTo": None if where else "no reads are possible; the loop is blind",
        }
    )
    if not where:
        blocking.append(binary)

    # The proxy trap: an internal host absent from no_proxy is routed to whatever
    # http_proxy points at, and fails with a bare connection-refused that reads
    # like the platform being down.
    no_proxy = os.environ.get("no_proxy", "") + "," + os.environ.get("NO_PROXY", "")
    proxied = bool(os.environ.get("http_proxy") or os.environ.get("https_proxy"))
    ok_proxy = (not proxied) or ".internalmeta.com" in no_proxy
    rows.append(
        {
            "kind": "env",
            "name": "no_proxy covers .internalmeta.com",
            "state": "present" if ok_proxy else "MISSING",
            "path": None,
            "usedFor": "reaching codimango.internalmeta.com",
            "degradesTo": None
            if ok_proxy
            else "reads fail with 'Connection refused' that looks like an outage; "
            'export no_proxy="$no_proxy,.internalmeta.com"',
        }
    )
    if not ok_proxy:
        blocking.append("no_proxy")

    # Smoke verifies parsing against stubs; this is the only thing that notices
    # the real CLI moving out from under those stubs.
    try:
        from .adapter import verify_surface

        vs = verify_surface()
        rows.append(
            {
                "kind": "cli",
                "name": "live surface matches offline fixtures",
                "state": "present" if vs["ok"] else "MISSING",
                "path": vs.get("matched"),
                "usedFor": "confidence that the offline suite reflects reality",
                "degradesTo": None if vs["ok"] else f"{vs['verdict']}: {vs['detail']}",
            }
        )
        if not vs["ok"]:
            degraded.append("cli-surface-drift")
        # The legacy CLI still works and still answers, so nothing fails today.
        # It is also the surface every offline fixture is pinned to, which means
        # the day it goes away the whole read path breaks at once with no
        # warning. Say so while there is still time to migrate.
        if vs.get("matched") == "legacy":
            rows.append(
                {
                    "kind": "cli",
                    "name": "codimango CLI is the deprecated build",
                    "state": "degraded",
                    "path": "legacy",
                    "usedFor": "every platform read, and every offline fixture",
                    "degradesTo": ("the legacy CLI is announced as deprecated; the fixtures are "
                                   "pinned to it and will break when it is withdrawn. Migrate: "
                                   "`devfeature install codimango --persist`, then re-run "
                                   "`benchsmith probe` and check the fixtures still match"),
                }
            )
            degraded.append("codimango-cli-deprecated")
    except Exception as e:  # noqa: BLE001 - preflight must never crash the round
        rows.append(
            {
                "kind": "cli",
                "name": "live surface matches offline fixtures",
                "state": "MISSING",
                "path": None,
                "usedFor": "confidence that the offline suite reflects reality",
                "degradesTo": f"could not check: {e}",
            }
        )
        degraded.append("cli-surface-drift")

    if repo_root and task:
        legacy = Path(repo_root) / ".ripen" / f"{task}.json"
        current = Path(repo_root) / ".benchsmith" / f"{task}.json"
        if legacy.is_file() and not current.is_file():
            rows.append(
                {
                    "kind": "journal",
                    "name": ".ripen journal present, .benchsmith absent",
                    "state": "MISSING",
                    "path": str(legacy),
                    "usedFor": "stall counters, hardening budget, excursion detection",
                    "degradesTo": "starting fresh silently resets every counter that can "
                    "escalate; migrate or explicitly start over",
                }
            )
            blocking.append("journal")

    return {
        "ok": not blocking,
        "blocking": blocking,
        "degraded": degraded,
        "checks": rows,
    }


def render(result: dict) -> str:
    width = max(len(r["name"]) for r in result["checks"])
    out = []
    for r in result["checks"]:
        out.append(f"  {r['state']:<8} {r['name']:<{width}}  {r['usedFor']}")
        if r["degradesTo"]:
            out.append(f"           {'':<{width}}  -> {r['degradesTo']}")
    out.append("")
    if result["blocking"]:
        out.append(f"  BLOCKED — missing: {', '.join(result['blocking'])}")
    elif result["degraded"]:
        out.append(f"  DEGRADED — running without: {', '.join(result['degraded'])}")
    else:
        out.append("  PREFLIGHT PASS — every composed dependency resolved")
    return "\n".join(out)
