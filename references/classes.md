# Round classes

One class per round, validated by `assay record --class` against this list. A label the budget
cannot count is rejected at the command line rather than written into the journal.

## Read from evidence

| Evidence | Class | Action |
|---|---|---|
| errored/inconclusive trials above threshold | `infra` | Re-run the affected stage. Spends no budget, advances no stall counter, and the round's rate is **not** a difficulty reading |
| jobs unreadable, or no trials at this commit | `not-measured` | Nothing ran to be classified. Re-fetch; do not re-run a stage on the strength of it |
| every trial truncated (`balanceIsMeasurement: false`) | `not-measured` | A partial measurement does not gate and is also not a rate |
| the cloud measured a different commit than you pushed | `not-measured` | Written automatically — `assay record` rewrites the class, blanks the counts and records `classOverriddenFrom` |
| file-not-found, bad shebang, reward file not written | `contract` | Fix the harness — cheap, high value |
| one shared failure and `n_minus_1` | `suspect-golden` | Validity-check that case. **Not** a difficulty signal |
| one test is the **sole** failure in over half the failing trials | `dominant-blocker` | Validity-check that assertion first: those trials would pass if it did, so the round measured the assertion, not the task. **Not** a difficulty signal |
| every trial fails the same set while the oracle passes | `contract-disagreement` | Escalate — spec and golden disagree |
| any attempt classified a grading failure | `grader-false-negative` | The rate is **not** a measurement. Fix the fixture. Never harden |
| a known-stale gate returned a verdict | `platform-stale` | Record and keep going. Not a finding, not a clearance, not an escalation |
| trials vary, at least one pass and one fail | `in-band` | Report. Check discriminator breadth |
| every agent passes every trial | `too-easy` | Harden — one lever per push, from the frozen slate |
| `noProgressStreak >= 3` | `inert` | Escalate. Stop pushing |
| deliberately returning the graded surface to an earlier state | `revert` | The return is journaled as `gradedRoundTrip` but raises **no** excursion. An undeclared return still does |

## Actions rather than evidence

| Class | When |
|---|---|
| `corrective` | The round fixed something the local gate establishes — a repaired fixture, a broadened assertion, a harness fix. Several belong in **one** push. A corrective streak of any length is not an escalation trigger |
| `cosmetic-seam` | The round changed only what no grader reads: README, comments, docs |
| `hard-both-ways` | Two independent problems at once — a grader false negative *and* a genuine difficulty problem. Name both, fix the one the evidence supports first |

## Two precedence rules the recorder enforces for you

**A measurement from another commit is not this round's.** `assay record` compares
`validationCommitSha` against the pushed SHA and, on mismatch, rewrites the class to
`not-measured`, blanks the rate and evidence, and records the original in `classOverriddenFrom`.
Numbers from another tree read as this one's and are worse than no numbers.

**A difficulty class on a round nobody could measure is not a verdict.** `too-easy` or `in-band`
with no rate is rewritten to `not-measured`, so it spends no hardening budget. Four consecutive
rounds once spent four of five budget units this way and the task was still too easy at the end.

## Known-stale platform gates

The platform's answer is not always a fact about the task. Where a gate is known stale, its
output is not a signal — and both reflexes are wrong: one gate gets treated as a hard stop, the
other as a pass.

| Gate | State | What to do |
|---|---|---|
| Provenance against a non-1P-authored `instruction.md` | **not stale — a real finding since 2026-09-08** | Re-author the spec through a 1P model. The write log keeps the old flag even after the text is replaced |
| Contamination | pipeline paused; nothing recent evaluated | Show the box **unchecked**, never green. Do not wait for it |

A known-stale gate neither escalates nor clears. Say which gate left the box unchecked. Re-check
this list when a platform change lands: a gate that has come back is a signal again, and a gate
that lives here forever is an excuse.
