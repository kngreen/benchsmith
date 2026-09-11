# assay

Iterate one Codimango benchmark task until it is genuinely hard and every exact-head gate is
green — or say plainly, with evidence, why it is not.

Assay is self-contained. It owns the round loop, the journal, the pre-push gate, the difficulty
bar, provenance tagging and the terminal verdict, with **no runtime dependency on any other
repository**. Judgement lives in `SKILL.md`; arithmetic lives in `lib/`; `bin/assay` is the entry
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
assay probe                                   # resolve the platform CLI surface
assay install-hooks --repo .                  # pre-push gate
assay read --task <name> --task-id <id>       # fresh, identity-checked
assay bar payload.json --target hard-only     # the difficulty verdict
assay gate --repo . --task <name>             # before every push
assay record --repo . --task <name> --sha <sha> --class in-band --fix "..."
```

`assay --help` lists everything. `ASSAY_TARGET`, `ASSAY_HARDENING_BUDGET`, `ASSAY_TASK_ID`,
`ASSAY_SOURCE_REPO` and `ASSAY_ORACLE` are read from the environment when the flags are omitted.

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
replacement. `assay probe` resolves the surface once by probing `--help`.

**Never trust a name lookup.** Basenames collide across repos and survive renames, and the read
surface is name-only, so identity is verified on the way back: a record whose id disagrees with
the one bound at intake is another task.

**A round that is not recorded did not happen**, and a journal is never hand-written.

## Layout

```
SKILL.md              the loop and the judgement
bin/assay             entry point
lib/assay/            model · bar · snapshot · journal · gate · adapter · cli
references/           gates · classes · continuous · provenance · authorship
```

`references/continuous.md` matters more than its size suggests: most of the difficulty machinery
assumes binary reward, and applying it to a continuously-scored task measures the wrong thing.

## Install

```bash
git clone git@github.com:kngreen/assay.git ~/.claude/skills/assay
ln -s ~/.claude/skills/assay ~/.codex/skills/assay
```

`/assay` in Claude Code on the next session, and available to Codex via its skills directory.
`~/.llms/skills/claude-templates` is a symlink to `~/.claude/skills`, so one clone also surfaces
through the devmate mirror. Muse resolves through the agent marketplace rather than a local path,
so reaching it means landing the skill in `fbcode/claude-templates/components/skills/assay/`.

## Prerequisites

An authenticated `codimango` CLI, a checkout of the task repository, and — for the skills assay
delegates to at intake and attribution — the `team-aai` bundle.

## Acknowledgement

The failure modes encoded here were learned the expensive way by other people's loops, notably
`codimango/ripen`, whose incident log is the source of several rules above. No code is shared and
nothing here depends on it. Bugs are ours.
