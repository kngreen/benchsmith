"""Gather every drafted review into one list you can act on.

Workers draft reviews in isolation, which is what keeps one task's findings out
of another's context. The cost is that the drafts end up scattered across as
many worktrees as there were reviewers, and "eleven reviews were written
somewhere" is not a thing anyone can act on.

This reads them back and reports what a person needs in order to decide:
the decision each reviewer reached, whether the canonical form is actually
complete, and where the text is. It submits nothing -- that stays a human
action, and it is the only step in the review loop that should be.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The eight base sections the canonical form requires, in order. A review
# missing one is not submittable, and saying so here is cheaper than finding out
# when the form is rejected.
REQUIRED_SECTIONS = (
    "Quality Review Agent",
    "Contamination Review Agent",
    "Novelty Review Agent",
    "TBR Review Agreement",
    "Human Checks",
    "Decision",
    "Other Notes",
    "Reviewer Confidence",
)
CONDITIONAL = "Agentic Full-Task Review (MM)"

WORD_CAP = 700

DECISION = re.compile(r"^\s*[-*]?\s*\*\*Decision:\*\*\s*(.+?)\s*$", re.M | re.I)
CONFIDENCE = re.compile(r"^\s*[-*]?\s*\*\*Confidence:\*\*\s*(.+?)\s*$", re.M | re.I)
HEADING = re.compile(r"^#{2,4}\s+(.+?)\s*$", re.M)


@dataclass
class Draft:
    task: str
    path: str
    decision: str
    confidence: str
    words: int
    missing: tuple
    has_agentic: bool

    @property
    def submittable(self) -> bool:
        """Complete form, a decision, and inside the word cap."""
        return not self.missing and bool(self.decision) and self.words <= WORD_CAP

    def as_dict(self) -> dict:
        return {"task": self.task, "path": self.path, "decision": self.decision or None,
                "confidence": self.confidence or None, "words": self.words,
                "missingSections": list(self.missing), "agenticSection": self.has_agentic,
                "submittable": self.submittable}


def parse(task: str, path: Path, text: str) -> Draft:
    headings = {h.strip() for h in HEADING.findall(text)}
    missing = tuple(s for s in REQUIRED_SECTIONS if s not in headings)
    d = DECISION.search(text)
    c = CONFIDENCE.search(text)
    return Draft(
        task=task, path=str(path),
        # A decision that is still the template's menu is not a decision.
        decision=("" if d and "/" in d.group(1) else (d.group(1) if d else "")),
        confidence=(c.group(1) if c else ""),
        words=len(text.split()),
        missing=missing,
        has_agentic=CONDITIONAL in headings,
    )


def find(task: str, *roots) -> Path | None:
    """The drafted review, wherever the worker wrote it."""
    for root in roots:
        if not root:
            continue
        p = Path(root) / ".benchsmith" / "handoff" / f"review-{task}.md"
        if p.is_file():
            return p
    return None


def collect(plans: list, *, links: bool = False, runner=None) -> dict:
    """One row per dispatched review."""
    drafts, absent, rows = [], [], []
    by_task = {str(pl.get("task") or ""): pl for pl in plans or []}
    for pl in plans or []:
        task = str(pl.get("task") or "")
        path = find(task, pl.get("worktree"), pl.get("repo"))
        if path is None:
            absent.append({"task": task, "reason": "no drafted review found"})
            continue
        try:
            drafts.append(parse(task, path, path.read_text(errors="replace")))
        except OSError as e:
            absent.append({"task": task, "reason": f"unreadable: {e}"})
    for d in drafts:
        row = d.as_dict()
        pl = by_task.get(d.task, {})
        row["reviewUrl"] = link(d, runner=runner) if links else None
        row["sessionUrl"] = SESSION_URL.format(sid=pl["session"]) if pl.get("session") else None
        row["taskUrl"] = TASK_URL.format(id=pl["taskId"]) if pl.get("taskId") else None
        rows.append(row)
    return {
        "drafted": len(drafts),
        "submittable": sum(1 for d in drafts if d.submittable),
        "reviews": rows,
        "absent": absent,
    }


TASK_URL = "https://codimango.internalmeta.com/submissions/{id}"
SESSION_URL = "https://agentcloud.internalmeta.com/{sid}"


def link(draft: Draft, *, runner=None) -> str:
    """A URL for the full review, because a devserver path is not one.

    The orchestrator thread renders a one-line verdict and the reader has no way
    to reach the reasoning behind it -- the draft is a file on a host they are
    not on. A private paste is durable, clickable, and costs one call.

    The URL is cached beside the draft: re-pasting on every status call would
    make a new link each time, and a verdict whose link changes is worse than
    one with none.
    """
    cached = Path(draft.path).with_suffix(".paste")
    try:
        existing = cached.read_text().strip()
        if existing.startswith("http"):
            return existing
    except OSError:
        pass

    run = runner or (lambda argv, text: subprocess.run(
        argv, input=text, capture_output=True, text=True, timeout=180))
    r = run(["meta", "phabricator.paste", "create", "--stdin", "--private",
             "--title", f"review: {draft.task}", "--language", "markdown",
             "--output", "json"], Path(draft.path).read_text(errors="replace"))
    out = getattr(r, "stdout", "") or ""
    if getattr(r, "returncode", 1) != 0 or "{" not in out:
        return ""
    try:
        url = str(json.loads(out[out.index("{"):]).get("url") or "")
    except ValueError:
        return ""
    if url:
        try:
            cached.write_text(url)
        except OSError:
            pass
    return url


def render(res: dict) -> str:
    rows = res.get("reviews") or []
    if not rows and not res.get("absent"):
        return "No reviews drafted yet."
    out = [f"**Reviews drafted — {res['drafted']}, of which {res['submittable']} are complete "
           "and ready for you to submit.** Nothing has been submitted.", "",
           "| | Task | Verdict | Review | Session |",
           "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (not x["submittable"], x["task"])):
        mark = "✓" if r["submittable"] else "·"
        # A path on a devserver is not a link. Fall back to naming it, but say
        # so, rather than rendering it as though it were reachable.
        review = (f"[full review]({r['reviewUrl']})" if r.get("reviewUrl")
                  else f"`{r['path']}` (on host)")
        session = f"[worker]({r['sessionUrl']})" if r.get("sessionUrl") else "—"
        verdict = r["decision"] or "**no decision**"
        note = ""
        if r["missingSections"]:
            note = f" <br>missing: {', '.join(r['missingSections'][:3])}"
        elif r["words"] > WORD_CAP:
            note = f" <br>{r['words']}w, over the {WORD_CAP} cap"
        out.append(f"| {mark} | {r['task']} | {verdict}{note} | {review} | {session} |")
    for a in res.get("absent") or []:
        out.append(f"| · | {a['task']} | — | {a['reason']} | — |")
    return "\n".join(out)
