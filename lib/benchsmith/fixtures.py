"""The hand-written fixture corpus: known cheats must fail, correct alternatives must pass.

Two questions no generated mutant answers:

  * **Q1, is the grader too loose?** A negative fixture is a cheat vector someone
    already thought of -- a hardcoded answer, a discarded call, a decorative
    hook. It must not score 1.0.
  * **Q2, is the grader too strict?** A positive fixture is a correct-but-
    different solution. It must score 1.0.

`mutate.py` generates near-misses; this runs the ones a human wrote down. They
are complementary: a generated mutant finds holes nobody anticipated, a corpus
fixture keeps a hole that was already found from reopening.

Ported from the swe-bench repo's G2/G5. Four of its judgements are load-bearing:

  * **Glob, never hardcode.** A fixed name list is one task's cheat vectors
    imposed on every task -- and it does not merely fail the wrong task, it
    SKIPS the fixtures the task does own, so the gate can go green having run
    nothing.
  * **A timeout is a third state.** Not pass, not fail, not not-run. Narrating
    one into "it was expected to score 0.0 anyway" is how a reward-hack fixture
    stops being checked.
  * **Aggregate with MIN.** A trial passes only when every step scores 1.0. This
    has been got wrong twice -- once as a mean against a threshold, once as a
    max that picks the best step.
  * **An empty corpus is NOT_RUN**, and not run is not passed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

PASS, FAIL, NOT_RUN, TIMEOUT = "PASS", "FAIL", "NOT_RUN", "TIMEOUT"

NEGATIVE_DIRS = ("qa/negative",)
POSITIVE_DIRS = ("qa/positive", "qa/variants")

# A `*suffix.sh` negative is gold PLUS a tamper: it appends to the last step so
# the genuine solution still runs in full before the tamper. Any other negative
# is a standalone cheat and must replace EVERY step -- otherwise a later gold
# step silently repairs the cheat and the fixture scores 1.0.
SUFFIX = "suffix"
STANDALONE = "standalone"


@dataclass(frozen=True)
class Outcome:
    name: str
    kind: str  # "negative" | "positive"
    state: str
    score: float | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "state": self.state,
            "score": self.score,
            "detail": self.detail,
        }


def solve_steps(task_dir: Path) -> list[Path]:
    """Every solve script, in execution order."""
    task_dir = Path(task_dir)
    stepped = sorted(task_dir.glob("steps/*/solve.sh"))
    if stepped:
        return stepped
    single = task_dir / "solve.sh"
    return [single] if single.is_file() else []


def discover(task_dir: Path) -> tuple[list[Path], list[Path]]:
    """Glob the corpus. Never a hardcoded name list."""
    task_dir = Path(task_dir)
    neg = sorted(f for d in NEGATIVE_DIRS for f in (task_dir / d).glob("*.sh"))
    pos = sorted(f for d in POSITIVE_DIRS for f in (task_dir / d).glob("*.sh"))
    return neg, pos


def negative_kind(fixture: Path) -> str:
    return SUFFIX if fixture.name.endswith("suffix.sh") else STANDALONE


def verdict(rewards, *, expect_pass: bool) -> tuple[str, float | None, str]:
    """MIN across steps. A trial passes only when every step scores 1.0."""
    vals = []
    for v in rewards or []:
        if v in ("", None, "null"):
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return FAIL, None, "no gradable reward (likely a harness error, not a verdict)"
    low = min(vals)
    scored_full = low >= 1.0
    if expect_pass:
        return (
            (PASS, low, "scored 1.0; a correct alternative still passes")
            if scored_full
            else (
                FAIL,
                low,
                f"scored {low}; a correct-but-different solution was denied — "
                "the grader is too strict on this vector",
            )
        )
    return (
        (
            FAIL,
            low,
            f"scored {low} — the cheat passed; the grader is too loose on this vector",
        )
        if scored_full
        else (PASS, low, f"blocked at {low}")
    )


def run_corpus(task_dir: Path, *, runner, backup_suffix: str = ".gate-backup") -> dict:
    """Run every fixture, restoring the solve scripts after each one.

    `runner(task_dir) -> (returncode, rewards)`; returncode 124 means timeout,
    matching `timeout(1)`.
    """
    task_dir = Path(task_dir)
    neg, pos = discover(task_dir)
    steps = solve_steps(task_dir)
    if not steps:
        return {
            "state": NOT_RUN,
            "outcomes": [],
            "detail": "no solve.sh to overlay; the corpus cannot run",
        }
    if not neg and not pos:
        return {
            "state": NOT_RUN,
            "outcomes": [],
            "detail": "no fixtures under qa/negative, qa/positive or qa/variants — "
            "and not run is not passed",
        }

    outcomes: list[Outcome] = []
    backups = {s: Path(str(s) + backup_suffix) for s in steps}
    for s, b in backups.items():
        shutil.copyfile(s, b)
    try:
        for fixture, kind in [(f, "negative") for f in neg] + [
            (f, "positive") for f in pos
        ]:
            gold_last = backups[steps[-1]].read_text()
            text = fixture.read_text()
            if kind == "positive":
                if len(steps) != 1:
                    outcomes.append(
                        Outcome(
                            fixture.name,
                            kind,
                            NOT_RUN,
                            None,
                            "a single positive script cannot independently replace a multi-step "
                            "solution; provide one complete alternative per step",
                        )
                    )
                    continue
                steps[0].write_text(text)
            elif negative_kind(fixture) == SUFFIX:
                steps[-1].write_text(gold_last + "\n" + text)
            else:
                for s in steps:
                    s.write_text(text)
            for s in steps:
                s.chmod(0o755)

            try:
                rc, rewards = runner(task_dir)
            except Exception as e:  # noqa: BLE001
                outcomes.append(
                    Outcome(
                        fixture.name,
                        kind,
                        NOT_RUN,
                        None,
                        f"runner error: {type(e).__name__}: {e}",
                    )
                )
                continue
            finally:
                for s, b in backups.items():
                    shutil.copyfile(b, s)

            if rc == 124 and not rewards:
                # Never narrated into "it was expected to fail anyway".
                outcomes.append(
                    Outcome(
                        fixture.name,
                        kind,
                        TIMEOUT,
                        None,
                        "timed out with no verdict; re-run with a larger budget — "
                        "never assume the expected score",
                    )
                )
                continue
            state, score, why = verdict(rewards, expect_pass=(kind == "positive"))
            outcomes.append(Outcome(fixture.name, kind, state, score, why))
    finally:
        for s, b in backups.items():
            if b.is_file():
                shutil.copyfile(b, s)
                b.unlink(missing_ok=True)

    failed = [o for o in outcomes if o.state == FAIL]
    timed = [o for o in outcomes if o.state == TIMEOUT]
    notrun = [o for o in outcomes if o.state == NOT_RUN]
    state = FAIL if failed else (TIMEOUT if timed else (NOT_RUN if notrun else PASS))
    return {
        "state": state,
        "outcomes": [o.as_dict() for o in outcomes],
        "negatives": len(neg),
        "positives": len(pos),
        "detail": (
            "; ".join(f"{o.name}: {o.detail}" for o in (failed + timed + notrun)[:4])
            if (failed or timed or notrun)
            else f"{len(neg)} negative(s) blocked, {len(pos)} positive(s) still pass"
        ),
    }
