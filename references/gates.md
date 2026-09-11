# Completion gates

The exact-head contract every flow ends on. §5's difficulty bar is *additional* to this — these
gates say the task is valid, the bar says it is hard. Both, or it is not done.

## Do not hardcode the CLI

The commands below are the **legacy** `codimango` surface. The installed binary prints a banner
announcing it is legacy and pointing at a replacement fbcode CLI that keeps the `codimango`
command and changes the subcommand shape. Treat every literal invocation here as an *example of
the invariant*, never as the contract.

Resolve the surface once with `benchsmith probe`, which reads `--help` and caches the result, and
route every read through `benchsmith read`. **An unresolved capability is declared and degraded,
never substituted with a command you have not run**, and a command that errors is `not_run`,
not a pass.

What must hold regardless of surface: the read is uncached, it is addressed by `TASK_ID`/
`TASK_UUID` rather than basename (§1), and every number it returns is attributed to `ACTIVE_SHA`.

## The oracle headline is not bound to the commit

`oracleStatus`, `oraclePassRate`, `oraclePassCount` and the `Oracle validation` row in
`validationDetails` **carry forward across a SHA change**. Measured on one task, two reads
twenty minutes apart:

| | read 1 | read 2 |
|---|---|---|
| `validationCommitSha` | `0012092313ae` | `d66fefbe6487` **changed** |
| `validationStatus` | `failed` | `pending` |
| cohort rates | 0.8 / 1.0 | all `null` |
| **`oracleStatus`** | **`validated`** | **`validated`** — unchanged |
| **`oraclePassRate`** | **1** | **1** — unchanged |

Every SHA-scoped signal reset. The oracle did not. At that moment the platform UI was showing
`Reference solution resolves task: 0`, `Fail-to-pass (binary): 0` for the live state, while the
record still claimed `validated`, `3/3`.

**Eval-GT is a separate stage and it is not in `job list`, `task show` or `tasks errors`.**
Nothing in those three carries a SHA-bound reference result. It lives in:

```
codimango ... trials artifacts <TASK> --commit <FULL_SHA> --json
```

under `.passAtK.stageResults[]` — the row named `evalgt`, whose `rawResult.result.raw_output`
is a **JSON-encoded string** holding the honest counts: `{resolved, reward, f2p_passed,
f2p_total, p2p_passed, p2p_total}`. `stageResults[].jobId` is a multimango tracking id in a
different namespace from cohort job ids; join on `commitSha` + `workflowRunId`, never on jobId.

`benchsmith bar` therefore reports the `oracle` review row as `fallback` unless the caller
supplies a SHA-bound Eval-GT result. Unbound is an absent verdict, not a weak pass — the same
rule every other row obeys.

## Never read cohort rates from the task record

The headline fields on `tasks show` — `agentPassCount/Rate`, `metacodePassCount/Rate`,
`avocadoPass*` — are **not** the cohort set. There is no codex field among them, so a task where
codex ran and saturated reads as though that cohort never existed.

Measured on `ollo-behavior-log-anonymization @ 2e4a9850`: the headline fields give
2/5 + 0/5 = **20% pooled**; the SHA-scoped jobs give **7/15 = 46%**, because codex went 5/5. The
first reads as comfortably in band; the second is a saturated strongest cohort and a reject. Two
further tasks showed the same distortion (50% -> 60%, and one more).

Rates come from the job list, scoped and deduplicated: `status == "completed"`, job commit equal
to the validation commit, the one-attempt `reviewIdentity.stage == "agentic-review"` job excluded,
and the newest batch kept per `config.agentName`. `benchsmith bar` does this in `select_jobs` and keeps
an auditable note for every job it drops — a silently dropped cohort is indistinguishable from one
that never ran.

## Read fresh, always

**Every Codimango read carries `--no-cache`.** A cached read after a push is how one commit's
numbers get attributed to another. `benchsmith record` detects it after the fact — it compares the
validation commit against the pushed SHA and blanks the round — but `--no-cache` prevents most
of it upfront.

```bash
codimango --site nest api tasks show     "$TASK" --json --no-cache
codimango --site nest api tasks errors   "$TASK" --json --no-cache
codimango --site nest api tasks comments "$TASK" --json --no-cache
codimango --site nest api tasks reviews  "$TASK" --json --no-cache
codimango --site nest api jobs  list     "$TASK" --json --include-agentic-review latest --no-cache
```

For a failing trial:

```bash
codimango --site nest api trials list      <JOB_ID>   --json --no-cache
codimango --site nest api trials artifacts <TRIAL_ID> --download <TMPDIR> --no-cache
codimango --site nest api trials analysis  <TRIAL_ID> --no-cache
```

Accept only evidence belonging to the exact latest pushed commit. As soon as one definitive
current-commit failure is readable, diagnose it — do not wait for unrelated jobs to settle.

## The two reviews are different objects

Conflating them is the common error: one being green says nothing about the other.

### TBR — `.qualitativeResult` on the task record

```bash
codimango --site nest api tasks show "$TASK" --json --no-cache | jq '{
  headCommitSha, validationCommitSha, tbr: .qualitativeResult
}'
```

Require exact head, `overall == "GOOD"`, `verdict == "Accept"`, no `error`, every required
`results[]` dimension green, and no unresolved significant `issues[]` entry. Also check `track`
and `rubricVersion` — a verdict from another track's rubric is not your verdict.

For commit-level evidence select exact-head jobs only. A dedicated TBR job is identifiable by
`config.env.__validation_stage == "agentic-review"`; with no dedicated row, use the exact-head
validation row carrying the same structured result.

### Agentic Full-Task Review — a separate job, 17 rubrics

```bash
codimango --site nest api jobs review "$TASK" \
  --commit <FULL_HEAD_SHA> \
  --source-repo <SOURCE_REPO_FROM_TASK_RECORD> \
  --wait --fail-on-bad --json --timeout 1800 --no-cache | jq '{
    state,
    commit: .task.commitSha,
    verdict: .review.verdict,
    summary: .review.rubrics.summary,
    failed: [.review.rubrics.items[] | select(.verdict != "PASS") | {id, focus, statement, evidence}],
    attemptResults: .review.attemptResults,
    gradingAssessment: .review.gradingAssessment,
    authorFeedback: .review.authorFeedback
  }'
```

Require `state == "completed"`, `.task.commitSha == headCommitSha`, `review.verdict == "GOOD"`,
`nPassed == 17`, `nTotal == 17`, and PASS on every **R01–R13 and N01–N04** item.

A report that is stale, selected from an older attempt, nonterminal, malformed, fallback, or
BAD **is not a weaker pass — it is the absence of one.** Read the full JSON, not the condensed
Markdown: `authorFeedback` and `gradingAssessment` carry findings the summary drops.

## The gate

Every item, on one exact commit, from fresh reads:

- `headCommitSha == validationCommitSha`
- `validationStatus == "passing"`; structural and build checks pass
- Oracle / reference passes consistently — **and never read `oracleStatus` to establish it**
  (see below)
- agent results satisfy the format's difficulty requirement — **and §5's bar**
- AI Assessment / Quality Review: Accept
- Contamination LOW *(benchsmith treats this as blocking; see §5 note)*
- required quality dimensions and TBR Build / Eval GT pass
- TBR GOOD / Accept, no error, no unresolved significant issue
- Agentic Full-Task Review GOOD, 17/17
- no unresolved current-commit failure or significant warning

Checks a task format does not have are not applicable — record that, never fabricate them.
Embedding dedup is informational unless project policy makes it blocking; **contamination is
not** — `NOT EVALUATED` leaves the box unchecked rather than green.

## Per-test names are not in `trial list`

`trial list`'s `ctrfResults` is a **projection**: the `summary` counts are real, the
per-test statuses are not. Observed live — `summary {tests: 11, passed: 10,
failed: 1}` with not a single `FAILED` entry in `tests[]`.

So a reader that trusts `tests[]` concludes nothing failed, on a trial that failed.
`failing_tests()` returns `None` when the summary declares failures and none are
named — unknowable, never empty. Real names come from the artifact:

```
codimango ... trials artifacts <TRIAL_ID> --key verifier/ctrf.json
```

which carries `{"results": {"summary": {...}, "tests": [{"name", "status"}]}}` with
UPPERCASE `PASSED|FAILED|ERROR`.

## The review manifest

A prose claim that "reviews passed" is not auditable and has been wrong. Emit one row per review
the track requires, and treat a missing row as a missing pass:

| field | meaning |
|---|---|
| `review` | canonical name — `tbr`, `agentic-full-task`, `quality`, `review-critic`, plus the track's `review-task-*` and `aai-code-review` where required |
| `jobId` | the job the verdict came from |
| `reviewedSha` | the SHA the review actually ran on |
| `matchesActive` | `reviewedSha == ACTIVE_SHA` |
| `verdict` | verbatim, not paraphrased |
| `state` | `completed` / `pending` / `errored` / `absent` |
| `selection` | `exact-head` or `fallback` — a fallback report is not a pass |
| `stale` | true when the head moved after the review ran |

`review-critic` is operator-supplied, not parsed: `codimango-review-critic` runs in a separate
session (§10a) and its Accept / Request changes / Reject decision is recorded into the manifest
by hand. Every other row comes from a payload. That asymmetry is deliberate — the critic audits
the canonical reviewer, so it cannot be sourced from the same place the canonical reviewer is.

The gate passes only when every required row is `state: completed`, `matchesActive: true`,
`selection: exact-head`, `stale: false`, and a passing `verdict`. Any other combination is the
**absence** of a verdict, not a weaker one — including `NOT_RUN`, `STALE_COMMIT`, and a review
selected from an older attempt.

## Freeze the slot plan before results

Capture from configuration, **before the first participant result exists**, and persist it:

- the exact **job IDs** and validation stages that will produce participant rows;
- **family and exact model build** per cohort, resolved from aliases;
- **attempts per cohort** and stable **slot ordinals**;
- stable **step IDs** and their order, plus the mapping from display names and runner
  boundaries onto them — a renamed label never creates, merges, drops or reorders a step;
- **replacement authority**: which scopes are available (replay / named slot / cohort / full
  generation), and that exactly one replacement wave is permitted per SHA.

Then bind actual rows to planned slots by (stage, family, build, step, ordinal). **A planned slot
with zero rows is incomplete, not absent** — an empty cohort must appear in `runsIncomplete`
rather than silently shrinking the denominator, and a measurement missing any planned slot
carries no rate.

**A replacement generation supersedes the original mechanically**, by slot key, not by judgment:
a cohort retry supersedes every original row in that cohort, a full-generation retry supersedes
every row in the generation, and superseded rows are diagnostic only — never pooled, never
counted, never hardness evidence. Original and replacement generations of the same SHA never
pool with each other.

## Reward unforgeability — the full closure

Only required when the verifier runs candidate-controlled commands pre-grade, or resolves a
dependency from candidate-writable bytes (§6). When it is:

1. **Freeze the execution-point set** — every command, hook, generator, package lifecycle
   action, build/test setup step or child process at which candidate-writable bytes can execute
   or select executable bytes. Descendants are separate points. An omitted or reordered point
   fails closed.
2. **Freeze the trusted manifest** — path, mode, type, symlink target and content hash for the
   complete graded surface, plus the expected bytes in a solver-inaccessible read-only store.
3. **Freeze the runtime read/exec closure** — every path the grader can read or execute, closed
   under generated inputs, imports, symlinks, compile units, dynamic loading and child
   execution. No manual exclusions.
4. **Capture candidate output once** after the last execution point, copy it into a
   solver-inaccessible read-only root, and close candidate write access.
5. **Verify byte equality immediately before grading** — trusted rows against the trusted
   manifest, candidate rows against the captured one. Missing, extra, mutable, aliased or
   unclassified bytes fail with a stable integrity ID before reward is parsed.
6. **Run no candidate-selected command afterwards.** The handoff is one-way: the grader may
   execute only the sealed artifact through its frozen absolute invocation.

Every verifier dependency is invoked by absolute resolved path. `PATH`, current-directory
lookup, mutable caches and ambient package resolution must not be able to select verifier bytes.

## The documentation commit re-opens everything

After the final README or report update: commit, rebase, push, and **run this entire gate again
on the new SHA.** A commit touching only `README.md` has triggered a full re-validation that came
back failing on a task already marked converged.
