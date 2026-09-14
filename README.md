# benchsmith

Iterate one Codimango benchmark task until it is genuinely hard and every exact-head gate is
green — or say plainly, with evidence, why it is not.

Benchsmith is self-contained. It owns the round loop, the journal, the pre-push gate, the difficulty
bar, provenance tagging and the terminal verdict, with **no runtime dependency on any other
repository**. Judgement lives in `SKILL.md`; arithmetic lives in `lib/`; `bin/benchsmith` is the entry
point. Stdlib-only Python 3.11+, no install step.

## What it decides

Two questions, kept separate on purpose, because passing one says nothing about the other:

- **Is the task valid?** Head equals validation commit, structural and oracle green, TBR
  `GOOD`/`Accept`, Agentic Full-Task Review `GOOD` at 17/17, contamination LOW, no unresolved
  current-commit failure. → `references/gates.md`
- **Is the task hard?** Pooled completion in 0.20–0.50 with a Wilson interval, every frozen
  strongest cohort mixed, two model families failing for two independent semantic reasons, every
  step with a genuine pass and a genuine failure, no single decision explaining ≥80% of
  strongest-member failures. → `SKILL.md` §5

A platform that accepts model-family balance has not established that a task is hard. That gap
is what this skill exists to close.

## Usage

```bash
benchsmith probe                                   # resolve the platform CLI surface
benchsmith install-hooks --repo .                  # pre-push gate
benchsmith read --task <name> --task-id <id>       # fresh, identity-checked
benchsmith bar payload.json --target hard-only     # the difficulty verdict
benchsmith gate --repo . --task <name>             # before every push
benchsmith record --repo . --task <name> --sha <sha> --class in-band --fix "..."

# Personal T-Bench idea board (all mutations require --apply)
benchsmith ideas init --repo .
benchsmith ideas init --repo . --apply
benchsmith ideas harvest --repo . --limit 25
benchsmith ideas harvest --repo . --idea-id 252 --apply
benchsmith queue --repo . --fetch
benchsmith status --repo .                         # concise workers; table only when a row changed
benchsmith task-status --repo .                    # render the durable Markdown table explicitly
```

Fleet lifecycle commands maintain `.benchsmith/fleet/task-status.json` and the generated
`task-status.md`. Their `taskStatus.markdown` value is populated only for a new, not-yet-reported
table revision, so a coordinator can publish the table without reposting identical polls.

`benchsmith --help` lists everything. `BENCHSMITH_TARGET`, `BENCHSMITH_HARDENING_BUDGET`, `BENCHSMITH_TASK_ID`,
`BENCHSMITH_SOURCE_REPO` and `BENCHSMITH_ORACLE` are read from the environment when the flags are omitted.

The idea-board flow imports only human-originated T-Bench seeds from Idea Exchange. It does not
generate task ideas, claim them, or call an unbuilt seed “hard.” GSD cards are deduplicated by
`aai-idea:<id>` and remain in `Needs hardness screen` until the separate hardness screen records a
GO, DERISK, or KILL decision. By default, records without a MEDIUM/HIGH predicted novelty signal
are left out; `--include-unassessed` makes that intake broader, but novelty is never treated as
difficulty evidence. A Codimango summary can nominate a calibration reference for deeper
audit, but only the full exact-head Benchsmith bar can establish `hard calibrated`.

## Design commitments

**Never fabricate a rate.** A measurement that is incomplete, or invalidated by a spec defect or
a rejected valid alternative, returns `NO RATE` and names every missing slot. A planned slot with
zero rows is *incomplete*, never absent — shrinking the denominator to whatever arrived turns a
broken cohort into a flattering number.

**`NOT_RUN` is never a pass.** Every gate check is tri-state, so a check that could not run never
reads like one that ran and was satisfied.

**Errored is not failed.** An infrastructure trial is replaced, not counted; left in the
denominator it reads as a harder task.

**Empty is not unknown.** When a failing trial names no failures the shared set is *unknowable*,
so it is `null`, never `[]`.

**Never hardcode a subcommand.** The platform CLI announces itself as legacy and points at a
replacement. `benchsmith probe` resolves the surface once by probing `--help`.

**Never trust a name lookup.** Basenames collide across repos and survive renames, and the read
surface is name-only, so identity is verified on the way back: a record whose id disagrees with
the one bound at intake is another task.

**A round that is not recorded did not happen**, and a journal is never hand-written.

## Layout

```
SKILL.md              the loop and the judgement
bin/benchsmith             entry point
lib/benchsmith/            model · bar · snapshot · journal · gate · adapter · cli
references/           gates · classes · continuous · provenance · authorship
```

`references/continuous.md` matters more than its size suggests: most of the difficulty machinery
assumes binary reward, and applying it to a continuously-scored task measures the wrong thing.

## Install

```bash
git clone git@github.com:codimango/benchsmith.git ~/.claude/skills/benchsmith
ln -s ~/.claude/skills/benchsmith ~/.codex/skills/benchsmith
```

`/benchsmith` in Claude Code on the next session, and available to Codex via its skills directory.
`~/.llms/skills/claude-templates` is a symlink to `~/.claude/skills`, so one clone also surfaces
through the devmate mirror. Muse resolves through the agent marketplace rather than a local path,
so reaching it means landing the skill in `fbcode/claude-templates/components/skills/benchsmith/`.

## Prerequisites

An authenticated `codimango` CLI, a checkout of the task repository, and — for the skills benchsmith
delegates to at intake and attribution — the `team-aai` bundle.

## Acknowledgement

The failure modes encoded here were learned the expensive way by other people's loops, notably
`codimango/ripen`, whose incident log is the source of several rules above. No code is shared and
nothing here depends on it. Bugs are ours.
