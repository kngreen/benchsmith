# assay/lib

Assay's own implementation, so the skill does not depend on a repository we do not own.

Stdlib-only Python 3.11+. No install step, no virtualenv, no third-party packages — it has to
run from a skill directory on whatever a devserver happens to have.

## Status

| Module | State | Replaces |
|---|---|---|
| `assay/model.py` | done | — (no ripen equivalent) |
| `assay/bar.py` | done | — (no ripen equivalent; this is the §5 computation) |
| `assay/adapter.py` | **not built** | ripen's four STEP 0 slots — CLI discovery, uncached reads, identity verification |
| `assay/snapshot.py` | **not built** | `signals.sh`, `infra_check.sh`, `evidence.sh`, `mm_review.sh` |
| `assay/journal.py` | **not built** | `record_round.sh`, `journal_dir.sh`, `graded_hash.sh` |
| `assay/gate.py` | **not built** | `gate.sh` Tier 1, `labs_scope.sh` |
| `tests/` | **blocked** | `selftest.sh` |

**Assay still depends on ripen today.** Until `snapshot`, `journal` and `gate` land, §5 and §6
name ripen's scripts and the loop is ripen's. Do not read this directory as independence yet.

Deliberately out of scope: the mac_swe/iOS family, the mutation probe, the repo queue, and
`skill_dirty`. Those stay optional accelerators, invoked if present, never required.

## Why a clean-room rewrite rather than a fork

Three reasons, in order of weight:

1. **The bar has no ripen equivalent.** Pooled rates, Wilson intervals, cohort and step
   projections, single-gate coverage, two-category attribution — ripen defers difficulty to the
   platform's balance row. There is nothing to fork for the part that matters most.
2. **A review of ripen's collectors found five concrete defects** — `runsIncomplete: []` while
   every planned slot was absent, `TASK_REF` conflated with `TASK_NAME`, a hardcoded legacy CLI
   shape, `watch_sha` reporting "last measured commit" when no trials exist, and a fixed-hour
   wait instead of observed p95. Forking inherits all five.
3. **Ripen is codimango-owned and moves.** It changed on disk mid-session. A published skill
   should not break because someone else's repo advanced.

## What is ported, and from where

The *lessons* are ported; the code is not. Ripen's `DESIGN.md` records incidents that cost real
tasks, and each one is a rule here:

- errored trials sit in the denominator and read as a harder task (31 of 417, 36 of 754) →
  `Kind.F` is not calibration-bearing, and an all-F slot is incomplete rather than failed;
- empty and unknown are different answers → a plan slot with no rows is `incomplete`, never a
  shrunken denominator;
- one assertion carrying the round is a false negative, not difficulty (10 of 13, 5 of 6) →
  single-gate coverage is computed and kills at 0.80;
- a partial measurement is not a rate → `evaluate()` returns `NO RATE` and refuses to emit a
  number for an incomplete or D/E-invalidated measurement.

Credit to `codimango/ripen` for the incidents. Bugs here are ours.

## Running it

```bash
cd lib && python3 -c "from assay.bar import evaluate; ..."
```

A proper test entry point is pending — see below.

## Note on `tests/`

The `aai-long-horizon` provenance guard blocks this session from authoring anything under a
`tests/` path, because graded task tests are a 1P/Codex/human artifact. That rule is aimed at
benchmark task suites, not at a skill library's own unit tests, but the guard matches on path
and the correct response to a guard is not to route around it. The fixtures need to be written
by Codex, by a 1P model, or by a human.
