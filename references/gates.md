# Completion gates

The exact-head contract every flow ends on. §5's difficulty bar is *additional* to this — these
gates say the task is valid, the bar says it is hard. Both, or it is not done.

## Read fresh, always

**Every Codimango read carries `--no-cache`.** A cached read after a push is how one commit's
numbers get attributed to another. Ripen detects that after the fact (`measurementMatchesSha`
blanks the round); `--no-cache` prevents most of it upfront.

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
- Oracle / reference passes consistently — for binary tasks, `codimango bench run -a oracle -k 3`
  at 3/3 with reward exactly 1.0 where the format supports it
- agent results satisfy the format's difficulty requirement — **and §5's bar**
- AI Assessment / Quality Review: Accept
- Contamination LOW *(assay treats this as blocking; see §5 note)*
- required quality dimensions and TBR Build / Eval GT pass
- TBR GOOD / Accept, no error, no unresolved significant issue
- Agentic Full-Task Review GOOD, 17/17
- no unresolved current-commit failure or significant warning

Checks a task format does not have are not applicable — record that, never fabricate them.
Embedding dedup is informational unless project policy makes it blocking; **contamination is
not** — assay keeps ripen's rule that `NOT EVALUATED` leaves the box unchecked rather than green.

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
