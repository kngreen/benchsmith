# benchsmith/lib

Benchsmith's own implementation, so the skill does not depend on a repository we do not own.

Stdlib-only Python 3.11+. No install step, no virtualenv, no third-party packages — it has to
run from a skill directory on whatever a devserver happens to have.

## Status

| Module | State | Owns |
|---|---|---|
| `benchsmith/model.py` | done | measurement types and the outcome classifier |
| `benchsmith/bar.py` | done | the §5 difficulty computation |
| `benchsmith/adapter.py` | done | CLI discovery, uncached reads, identity verification |
| `benchsmith/snapshot.py` | done | platform records → measurement, evidence, review manifest |
| `benchsmith/journal.py` | done | rounds, streaks, excursions, surface hashes, budget |
| `benchsmith/gate.py` | done | the pre-push gate and hook installation |
| `benchsmith/cli.py` | done | `bin/benchsmith` |
| `selftest.py` | done | 112 fixtures, mutation-verified |

Deliberately out of scope: platform-family specialisations (macOS VM / iOS), the mutation probe,
and repo-level run queuing. Those stay optional accelerators, invoked if present, never required.

## What is ported, and from where

No code is shared with any other loop. The rules below each cost a real task somewhere before
they were written down, and each is enforced in code rather than remembered:

- errored trials sit in the denominator and read as a harder task (31 of 417, 36 of 754) →
  `Kind.F` is not calibration-bearing, and an all-F slot is incomplete rather than failed;
- empty and unknown are different answers → a plan slot with no rows is `incomplete`, never a
  shrunken denominator;
- one assertion carrying the round is a false negative, not difficulty (10 of 13, 5 of 6) →
  single-gate coverage is computed and kills at 0.80;
- a partial measurement is not a rate → `evaluate()` returns `NO RATE` and refuses to emit a
  number for an incomplete or D/E-invalidated measurement.

See the acknowledgement in the top-level README.