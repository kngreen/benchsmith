"""What the reviewer actually asked for, and whether it was done.

`needs_revision` means a person named something wrong. The loop had the parts to
track that -- a finding carries an acceptance test at open time, and closing one
requires evidence -- but nothing read the reviews, so every finding had to be
transcribed by hand, and nothing checked that any existed. A repair round could
push having addressed nothing.

Two things are deliberately not automated here.

Splitting a `decisionReason` into separate findings is a reading task: one
paragraph can contain three requests or one request stated three ways, and a
regex that guesses wrong either invents work or hides some. The prose is
surfaced verbatim and the agent opens the findings.

Deciding a finding is fixed is also not automated. `close_finding` already
demands evidence rather than an assertion; what was missing was anything
insisting the findings be there at all.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

REVISION_DECISIONS = {"revision", "needs_revision", "request_changes", "changes_requested"}


@dataclass(frozen=True)
class Request:
    reviewer: str
    at: str
    decision: str
    text: str
    commit: str = ""

    def as_dict(self) -> dict:
        return {"reviewer": self.reviewer, "at": self.at, "decision": self.decision,
                "commit": self.commit, "text": self.text}


def _rows(doc):
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for v in doc.values():
            if isinstance(v, list):
                return v
    return []


def _fetch(binary: str, verb: tuple, task: str) -> list:
    argv = [binary, *verb, task, "--json"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0 or "{" not in r.stdout and "[" not in r.stdout:
        return []
    start = min([i for i in (r.stdout.find("{"), r.stdout.find("[")) if i >= 0] or [0])
    try:
        return _rows(json.loads(r.stdout[start:]))
    except ValueError:
        return []


def requests(task: str, *, binary: str = "codimango") -> dict:
    """Every outstanding change request, newest first."""
    revs = _fetch(binary, ("api", "tasks", "reviews"), task)
    out = []
    for r in revs:
        decision = str(r.get("decision") or "").lower()
        if decision not in REVISION_DECISIONS:
            continue
        text = str(r.get("decisionReason") or "").strip()
        out.append(Request(reviewer=str(r.get("reviewer") or "unknown"),
                           at=str(r.get("reviewedAt") or ""), decision=decision,
                           text=text or "(the reviewer left no written reason)"))

    # Human comments are where a reviewer elaborates. System events are noise,
    # and there are far more of them than of anything worth reading.
    comments = _fetch(binary, ("api", "tasks", "comments"), task)
    human = [c for c in comments
             if not c.get("isSystemEvent") and str(c.get("body") or "").strip()
             and not c.get("deletedAt")]

    out.sort(key=lambda x: x.at, reverse=True)
    return {
        "task": task,
        "requests": [r.as_dict() for r in out],
        "comments": [{"author": str(c.get("authorName") or c.get("authorUnixname") or ""),
                      "at": str(c.get("createdAt") or ""),
                      "commit": str(c.get("commitSha") or ""),
                      "isReviewSubmission": bool(c.get("isReviewSubmission")),
                      "body": str(c.get("body") or "")[:1200]}
                     for c in human[-12:]],
        "systemComments": len(comments) - len(human),
    }


def unaddressed(journal_findings: dict, req: dict) -> tuple[bool, str]:
    """Can this repair round claim to be done?

    A revision request with no finding recorded against it is the failure this
    exists to catch: the loop cannot have verified a change it never wrote down.
    """
    if not req.get("requests"):
        return False, "no outstanding revision request on the platform"
    findings = journal_findings or {}
    if not findings:
        return True, ("the reviewer requested changes and no finding is recorded. Open one per "
                      "requested change, each with the acceptance test that proves it gone, "
                      "before repairing anything")
    still_open = [k for k, v in findings.items() if v.get("state") != "closed"]
    if still_open:
        return True, f"{len(still_open)} finding(s) still open: {', '.join(sorted(still_open)[:5])}"
    return False, f"{len(findings)} finding(s) closed with evidence"
