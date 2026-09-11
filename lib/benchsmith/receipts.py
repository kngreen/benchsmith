"""Reading someone else's receipt.

benchsmith enforces "NOT_RUN is never a pass" on gates it runs itself, and
enforced nothing at all on evidence it reads -- CI job status, deploy logs,
verification-script output. That asymmetry is where overstated evidence lives.

Three ways a green signal fails to mean what it says, all observed:

* **skipped-but-green** -- a deploy log reading `no ALB found - skipping health
  verification` reported as health-verified; a CI e2e job green via its skip
  path with every actual step skipped.
* **permissive predicate** -- `assert_ok` accepting every response except 403,
  so 401, 500 and a `000` network failure all counted as success. A positive
  control that cannot fail is not a control.
* **stale binding** -- evidence timestamped before the commit it certifies.
"""

from __future__ import annotations

import re

# Phrases that mean a step did not execute. Case-insensitive, deliberately broad:
# a false positive costs a manual look, a false negative ships a fiction.
SKIP_MARKERS = (
    r"\bskipp?(?:ed|ing)\b",
    r"\bnot (?:run|executed|performed|applicable)\b",
    r"\bno (?:\w+ )?found\b.*\bskip",
    r"\bnothing to (?:do|run|verify)\b",
    r"\bcondition (?:not met|unmet|false)\b",
    r"\bno tests? (?:ran|executed|found)\b",
    r"\bdry[- ]run\b",
)
_SKIP = re.compile("|".join(SKIP_MARKERS), re.I)

PASSING_STATES = frozenset({"success", "passed", "pass", "ok", "completed", "green"})


def scan_log(text: str) -> dict:
    """Does this log prove the thing its status claims?"""
    hits = [m.group(0).strip() for m in _SKIP.finditer(text or "")]
    return {
        "skipMarkers": sorted(set(hits))[:8],
        "skipped": bool(hits),
        # A log that says it skipped is not evidence the check ran, whatever the
        # surrounding job status says.
        "verdict": "UNPROVEN" if hits else "no-skip-markers",
    }


def read_job(job: dict, *, log: str = "") -> dict:
    """A CI/deploy job's status is a claim; its steps are the evidence.

    A job whose status is `success` while every step is skipped proves the job
    was reachable, not that the work happened.
    """
    status = str(job.get("conclusion") or job.get("status") or job.get("result") or "").lower()
    steps = job.get("steps") or []
    step_states = [str(s.get("conclusion") or s.get("status") or "").lower() for s in steps]
    ran = [s for s in step_states if s and s not in {"skipped", "neutral", "cancelled"}]
    log_scan = scan_log(log)

    if status not in PASSING_STATES:
        verdict, why = "FAIL", f"status {status or '<none>'}"
    elif steps and not ran:
        verdict, why = "UNPROVEN", f"status {status} but all {len(steps)} steps skipped"
    elif log_scan["skipped"]:
        verdict, why = "UNPROVEN", f"log says: {', '.join(log_scan['skipMarkers'][:2])}"
    elif not steps and not log:
        verdict, why = "UNPROVEN", "status only, no steps and no log to corroborate it"
    else:
        verdict, why = "PASS", f"{len(ran)} of {len(steps) or len(ran)} steps executed"

    return {
        "verdict": verdict,
        "reason": why,
        "status": status,
        "stepsTotal": len(steps),
        "stepsRan": len(ran),
        "skipMarkers": log_scan["skipMarkers"],
    }


def predicate_is_permissive(source: str, *, ok_name: str = "assert_ok") -> dict:
    """Is a positive control written as a denylist?

    `assert_ok() { [ "$code" != 403 ]; }` passes on 401, 500 and the `000` curl
    emits on a network failure -- so it cannot distinguish "authorized" from
    "the service is down". A positive control states what it ACCEPTS.
    """
    findings = []
    for m in re.finditer(
        rf"{re.escape(ok_name)}[^\n]*?(?:!=|-ne|not in|!==)\s*[\"']?(\d{{3}})", source or ""
    ):
        findings.append(f"{ok_name} defined by exclusion of {m.group(1)} — accepts every other code")
    if re.search(rf"{re.escape(ok_name)}[^\n]*?(?:==|-eq|in)\s*[\"']?[12]\d\d", source or ""):
        return {"permissive": False, "findings": [], "verdict": "allowlist"}
    return {
        "permissive": bool(findings),
        "findings": findings,
        "verdict": "DENYLIST — cannot fail on 000/401/500" if findings else "undetermined",
    }
