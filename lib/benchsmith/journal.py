"""The round journal: `.benchsmith/<task>.json`, machine-written, never hand-edited.

A hand-written journal produces none of the fields the loop reads back, so stall
detection, the hardening budget, regression comparison and excursion detection
all go blind at once -- and the loop then cycles for days with every counter at
zero. If you find yourself designing a state format, you are writing the wrong
thing.

A missing journal and a converged one must not look alike.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CLASSES = (
    "infra",
    "not-measured",
    "contract",
    "suspect-golden",
    "dominant-blocker",
    "contract-disagreement",
    "grader-false-negative",
    "platform-stale",
    "in-band",
    "too-easy",
    "inert",
    "revert",
    "corrective",
    "cosmetic-seam",
    "hard-both-ways",
)

# Classes that describe evidence we could not read. They must never spend budget
# or advance a stall counter, because four rounds once burned four of five
# budget units on measurements that never happened.
UNMEASURED = {"infra", "not-measured", "platform-stale"}

STATUSES = ("running", "converged", "escalated", "abandoned", "blocked-on-platform", "blocked",
            # Submitted, and now the reviewer's. Not converged -- nothing has
            # been accepted yet -- and not blocked, because nothing is wrong.
            # It is a hand-off, and the loop's part is over until they answer.
            "awaiting-review")

DEFAULT_BUDGET = 5

# Repair and harden are different commissions with opposite stop conditions.
# "Stopping with budget unspent is an unfinished job" is right for a hardening
# campaign and actively wrong for a five-finding repair: a review handed you the
# closure list, and open-ended difficulty research on top of it is scope creep
# that reads as diligence. One field run spent hours on two rejected levers
# while five findings sat open.
MODES = ("repair", "harden")
DEFAULT_MODE = "harden"
# A speculative lever gets this many local probes before it is abandoned. Field
# report: "cap speculative levers to 1-2 local probes."
DEFAULT_PROBE_BUDGET = 2
INEFFECTIVE_ROUND_CAP = 3


def _now() -> tuple[str, str]:
    return (
        datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        time.strftime("%Y-%m-%d %H:%M %Z"),
    )


@contextmanager
def _exclusive(path: Path):
    """Hold an exclusive advisory lock for one read-modify-write.

    Single-task use never needed this. With N workers and a coordinator the
    window between read and write is a corruption window, and the failure is
    silent: the loser's rounds vanish, so its stall counters and hardening budget
    reset to zero -- the two mechanisms that exist to stop a loop spinning.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fh = lock.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp + rename so a reader never sees a half-file.

    `write_text` truncates first: a concurrent reader between truncate and write
    gets valid-looking empty JSON, which `Journal.open` would treat as a missing
    journal and start fresh.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def round_key(sha: str, graded_hash: str, cls: str, fix: str) -> str:
    """Idempotency key for one round.

    Restart recovery re-runs the last action. Without a key the same round is
    appended twice, which inflates the round count, double-spends hardening
    budget and makes `noProgressStreak` compare a round against itself.
    """
    return hashlib.sha256(f"{sha}\x00{graded_hash}\x00{cls}\x00{fix}".encode()).hexdigest()[:16]


def journal_dir(repo_root: Path) -> Path:
    d = Path(repo_root) / ".benchsmith"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- surface hashes ---------------------------------------------------------

GRADED = ("tests", "task.toml")
VISIBLE = ("instruction.md",)


def _hash_paths(task_dir: Path, names: tuple[str, ...], globs: tuple[str, ...] = ()) -> tuple[str, int]:
    """Hash a surface deterministically: sorted relative path plus content."""
    h = hashlib.sha256()
    count = 0
    targets: list[Path] = []
    for name in names:
        p = task_dir / name
        if p.is_dir():
            targets.extend(sorted(q for q in p.rglob("*") if q.is_file()))
        elif p.is_file():
            targets.append(p)
    for pattern in globs:
        targets.extend(sorted(q for q in task_dir.glob(pattern) if q.is_file()))
    for p in sorted(set(targets)):
        h.update(str(p.relative_to(task_dir)).encode())
        h.update(p.read_bytes())
        count += 1
    return (h.hexdigest()[:32], count)


def surface_hashes(task_dir: Path) -> dict:
    """Graded and agent-visible surfaces, hashed separately.

    Separately, because the two excursions this catches are defined by their
    *difference*: a graded round-trip means the rounds netted zero, and a
    spec-only shrink means the task is getting easier by starving the spec.
    """
    task_dir = Path(task_dir)
    graded, gn = _hash_paths(task_dir, GRADED, ("steps/*/tests/**/*",))
    visible, vn = _hash_paths(task_dir, VISIBLE, ("steps/*/instruction.md",))
    spec_bytes = sum(
        p.stat().st_size
        for p in [task_dir / "instruction.md", *task_dir.glob("steps/*/instruction.md")]
        if p.is_file()
    )
    return {
        "gradedHash": graded,
        "gradedFiles": gn,
        "visibleHash": visible,
        "visibleFiles": vn,
        "specBytes": spec_bytes,
    }


# --- the journal ------------------------------------------------------------


@dataclass
class Journal:
    path: Path
    data: dict

    @classmethod
    def open(cls, repo_root: Path, task_name: str, *, family: str = "", adapter: dict | None = None):
        path = journal_dir(repo_root) / f"{task_name}.json"
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("task") != task_name:
                raise ValueError(
                    f"{path} names task {data.get('task')!r}; refusing to append another task's rounds"
                )
        else:
            data = {
                "task": task_name,
                "family": family,
                "adapter": adapter or {},
                "status": "running",
                "rounds": [],
                "noProgressStreak": 0,
                "hardeningStreak": 0,
                "hardeningTotal": 0,
                "hardeningUnmeasured": 0,
                "hardeningSpent": 0,
                "hardeningBudgetExhausted": False,
                "ineffectiveStreak": 0,
                "mode": DEFAULT_MODE,
                "findings": {},
                "probesSpent": 0,
                "waves": {},
                "lastGradedHash": None,
                "gradedHashHistory": [],
                "lastUpdateLocal": None,
            }
        return cls(path=path, data=data)

    def save(self) -> Path:
        with _exclusive(self.path):
            _atomic_write(self.path, json.dumps(self.data, indent=2) + "\n")
        return self.path

    @property
    def rounds(self) -> list:
        return self.data["rounds"]

    def budget(self) -> int:
        return int(os.environ.get("BENCHSMITH_HARDENING_BUDGET", DEFAULT_BUDGET))

    def probe_budget(self) -> int:
        return int(os.environ.get("BENCHSMITH_PROBE_BUDGET", DEFAULT_PROBE_BUDGET))

    @property
    def mode(self) -> str:
        return self.data.get("mode") or DEFAULT_MODE

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        self.data["mode"] = mode

    # -- closure ledger -----------------------------------------------------

    def open_finding(self, fid: str, symptom: str, acceptance: str) -> dict:
        """One review finding -> one edit -> one acceptance test.

        The acceptance test is required at open time, not at close time. A
        finding with no stated way to prove it gone cannot be closed, only
        asserted closed.
        """
        if not acceptance.strip():
            raise ValueError(f"finding {fid}: an acceptance test is required to open it")
        row = {"id": fid, "symptom": symptom, "acceptance": acceptance,
               "state": "open", "closedBy": None, "evidence": None}
        self.data.setdefault("findings", {})[fid] = row
        return row

    def close_finding(self, fid: str, sha: str, evidence: str) -> dict:
        row = (self.data.get("findings") or {}).get(fid)
        if row is None:
            raise ValueError(f"unknown finding {fid!r}; open it before closing it")
        if not evidence.strip():
            raise ValueError(f"finding {fid}: closing requires evidence, not an assertion")
        row.update(state="closed", closedBy=sha, evidence=evidence)
        return row

    def open_findings(self) -> list[str]:
        return [k for k, v in (self.data.get("findings") or {}).items() if v.get("state") != "closed"]

    def closure_summary(self) -> str:
        f = self.data.get("findings") or {}
        closed = [k for k, v in f.items() if v.get("state") == "closed"]
        return f"{len(closed)}/{len(f)} findings closed" if f else "no findings recorded"

    # -- excursions ---------------------------------------------------------

    def _excursion(self, hashes: dict, declared_revert: bool) -> tuple[str | None, bool]:
        """Detect a graded round-trip or a spec-only shrink.

        A hash merely equal to the previous round is not a round-trip -- that is
        an ordinary corrective round with no graded change. A round-trip is a
        return to a value the surface had changed *away* from: the rounds netted
        zero.
        """
        history = self.data.get("gradedHashHistory") or []
        gh = hashes["gradedHash"]
        prev = history[-1] if history else None
        round_trip = gh != prev and gh in history[:-1]

        shrink = False
        if self.rounds:
            last = self.rounds[-1]
            shrink = (
                gh == last.get("gradedHash")
                and hashes["specBytes"] < (last.get("specBytes") or 0)
            )

        if round_trip and not declared_revert:
            return ("round-trip", True)
        if shrink:
            return ("spec-only-shrink", False)
        return (None, round_trip)

    # -- recording ----------------------------------------------------------

    def record(
        self,
        *,
        task_dir: Path,
        sha: str,
        cls: str,
        fix: str,
        signals: dict | None = None,
        measurement: dict | None = None,
        evidence: dict | None = None,
        explain: str = "",
        hardening: bool = False,
    ) -> dict:
        if cls not in CLASSES:
            raise ValueError(f"unknown class {cls!r}; expected one of {', '.join(CLASSES)}")

        signals = signals or {}
        measurement = measurement or {}
        evidence = evidence or {}
        hashes = surface_hashes(task_dir)
        iso, local = _now()

        cloud_sha = str(signals.get("validationCommitSha") or "")
        matches = (not cloud_sha) or cloud_sha == sha
        overridden_from = None

        # Numbers measured on another tree read as this one's and are worse than
        # no numbers, so the class is rewritten and the counts are blanked.
        if not matches:
            overridden_from, cls = cls, "not-measured"
            measurement = {}
            evidence = {}

        # A difficulty class on a round nobody could measure is not a verdict.
        if cls in {"too-easy", "in-band"} and not measurement.get("rate"):
            overridden_from, cls = overridden_from or cls, "not-measured"

        declared_revert = cls == "revert"
        excursion, graded_round_trip = self._excursion(hashes, declared_revert)

        prev = self.rounds[-1] if self.rounds else None
        same = bool(
            prev
            and prev.get("sha") == sha
            and prev.get("cloudStatus") == signals.get("validationStatus")
            and prev.get("failing") == evidence.get("sharedFailures")
        )
        self.data["noProgressStreak"] = (self.data["noProgressStreak"] + 1) if same else 0

        # Budget: only a measured, pushed hardening round spends one. A lever that
        # died locally spent nothing, and stopping with budget unspent is an
        # unfinished job rather than a finding.
        spent = hardening and cls not in UNMEASURED and bool(measurement.get("rate"))
        if hardening and not spent:
            self.data["hardeningUnmeasured"] += 1
        if spent:
            self.data["hardeningSpent"] += 1
            self.data["hardeningTotal"] += 1
            self.data["hardeningStreak"] += 1
        elif cls not in UNMEASURED:
            self.data["hardeningStreak"] = 0
        self.data["hardeningBudgetExhausted"] = self.data["hardeningSpent"] >= self.budget()

        key = round_key(sha, hashes["gradedHash"], cls, fix)
        existing = next((r for r in self.rounds if r.get("roundKey") == key), None)
        if existing is not None:
            # Same commit, same graded surface, same class, same fix: this is a
            # replay of an already-recorded round, not a new one.
            existing["replayedAt"] = iso
            return existing

        entry = {
            "n": len(self.rounds) + 1,
            "roundKey": key,
            "sha": sha,
            "cloudCommitSha": cloud_sha,
            "measurementMatchesSha": matches,
            "at": iso,
            "at_local": local,
            "class": cls,
            "classOverriddenFrom": overridden_from,
            "fix": fix,
            "cloudStatus": signals.get("validationStatus"),
            "verdict": measurement.get("verdict"),
            "rate": measurement.get("rate"),
            "wilson": measurement.get("wilson"),
            "distanceToBand": measurement.get("distanceToBand"),
            "findings": [f.get("code") for f in measurement.get("findings", [])],
            "runsIncomplete": measurement.get("runsIncomplete", []),
            "failing": evidence.get("sharedFailures"),
            "evidenceComplete": evidence.get("evidenceComplete"),
            "topFailure": evidence.get("topFailure"),
            "topFailureSoleBlockerShare": evidence.get("topFailureSoleBlockerShare", 0.0),
            "concentrated": evidence.get("concentrated", False),
            "gradedRoundTrip": graded_round_trip,
            "revertDeclared": declared_revert,
            "hardeningIntent": hardening,
            "hardeningSpent": spent,
            **hashes,
        }
        if excursion:
            entry["excursion"] = excursion
        if explain:
            entry["explain"] = explain

        self._ineffective(entry)
        self.rounds.append(entry)
        self.data["lastGradedHash"] = hashes["gradedHash"]
        self.data.setdefault("gradedHashHistory", []).append(hashes["gradedHash"])
        self.data["lastUpdateLocal"] = local
        return entry

    def _ineffective(self, entry: dict) -> None:
        """Three measured rounds that fail to move d(p) toward the band stop the campaign."""
        d = entry.get("distanceToBand")
        if d is None:
            return
        prior = [r for r in self.rounds if r.get("distanceToBand") is not None]
        if not prior:
            self.data["ineffectiveStreak"] = 0
            return
        n = (entry.get("rate") or {}).get("slots") or 0
        threshold = (1 / n) if n else 0.0
        moved = (prior[-1]["distanceToBand"] - d) >= threshold
        self.data["ineffectiveStreak"] = 0 if moved else self.data["ineffectiveStreak"] + 1

    # -- stopping -----------------------------------------------------------

    def record_wave(self, wave) -> dict:
        """Persist a frozen replacement wave.

        Held only in memory, a wave dies with the worker -- and a restarted
        worker, seeing no wave, believes its one-per-SHA budget is unspent and
        can dispatch a second.
        """
        row = wave.as_dict() if hasattr(wave, "as_dict") else dict(wave)
        waves = self.data.setdefault("waves", {})
        waves.setdefault(row.get("sha", ""), []).append(row)
        return row

    def prior_wave(self, sha: str) -> dict | None:
        for row in (self.data.get("waves") or {}).get(sha, []):
            return row
        return None

    def stop_reason(self) -> str | None:
        # Repair terminates on closure, not on an exhausted budget. Continuing to
        # harden after the last finding closes is a new commission and needs a
        # new ask.
        if self.mode == "repair":
            f = self.data.get("findings") or {}
            if f and not self.open_findings():
                return f"repair complete: {self.closure_summary()} — hardening requires a new ask"
            if self.data.get("probesSpent", 0) >= self.probe_budget():
                return (f"probe budget of {self.probe_budget()} spent with "
                        f"{len(self.open_findings())} findings still open")
        """Why the campaign should stop, or None to keep going.

        Round count is never a reason. What is bounded is hardening.
        """
        if self.data["noProgressStreak"] >= 3:
            return "inert: three rounds with no change in sha, status or failing set"
        if self.data["ineffectiveStreak"] >= INEFFECTIVE_ROUND_CAP:
            return f"{INEFFECTIVE_ROUND_CAP} measured rounds moved d(p) less than one trial-equivalent"
        # The hardening budget is a HARDEN-mode stop. In repair mode the findings
        # decide, and an exhausted hardening budget carried over from an earlier
        # commission must not close a repair with findings still open.
        if self.mode != "repair" and self.data["hardeningBudgetExhausted"]:
            return f"hardening budget of {self.budget()} spent"
        last = self.rounds[-1] if self.rounds else None
        if last and last.get("excursion"):
            return f"unresolved excursion: {last['excursion']}"
        return None

    def set_status(self, status: str, *, oracle_passing: bool = True) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        if status == "abandoned" and oracle_passing and not self.data["hardeningBudgetExhausted"]:
            raise ValueError(
                "refusing to abandon: the oracle passes and hardening budget remains. "
                "A too-easy task with a clean oracle is under-hardened, which is a job, "
                "not a verdict. Use 'escalated' with the evidence pack instead."
            )
        self.data["status"] = status


def git_trailers(run_id: str, workflow: str) -> list[str]:
    return [
        "Created-Via: benchsmith",
        "benchsmith-Version: 1",
        f"benchsmith-Run-ID: {run_id}",
        f"benchsmith-Workflow: {workflow}",
    ]


def commit_scoped(repo_root: Path, task_name: str, journal_rel: str, message: str) -> None:
    """Stage and commit with a pathspec on BOTH operations.

    `git commit` commits the index, not the pathspec passed to `add` -- so
    scoping only the add protects nothing when a sibling run stages its own work
    in the window between. Measured on a two-loop race: 20 of 20 commits swept
    the sibling's files; with the pathspec on commit as well, 0 of 37.
    """
    repo_root = Path(repo_root)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo_root), *a], check=True, capture_output=True, text=True
    )
    run("add", "-A", "--", task_name)
    run("add", "-f", "--", journal_rel)
    run("commit", "-m", message, "--", task_name, journal_rel)
