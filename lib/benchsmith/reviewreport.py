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

import re
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


def collect(plans: list) -> dict:
    """One row per dispatched review."""
    drafts, absent = [], []
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
    ready = [d for d in drafts if d.submittable]
    return {
        "drafted": len(drafts),
        "submittable": len(ready),
        "reviews": [d.as_dict() for d in drafts],
        "absent": absent,
    }


def render(res: dict) -> str:
    rows = res.get("reviews") or []
    if not rows and not res.get("absent"):
        return "No reviews drafted yet."
    out = [f"**Reviews drafted — {res['drafted']}, of which {res['submittable']} are complete "
           "and ready for you to submit.** Nothing has been submitted.", ""]
    for r in sorted(rows, key=lambda x: (not x["submittable"], x["task"])):
        mark = "✓" if r["submittable"] else "·"
        why = ""
        if r["missingSections"]:
            why = f"  — missing: {', '.join(r['missingSections'][:3])}"
        elif not r["decision"]:
            why = "  — no decision recorded"
        elif r["words"] > WORD_CAP:
            why = f"  — {r['words']} words, over the {WORD_CAP} cap"
        out.append(f"{mark} {r['task']}  **{r['decision'] or 'no decision'}**"
                   f"  ({r['words']}w){why}")
        out.append(f"    {r['path']}")
    for a in res.get("absent") or []:
        out.append(f"· {a['task']}  — {a['reason']}")
    return "\n".join(out)
