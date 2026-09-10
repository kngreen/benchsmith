# assay

The hard-task bar for [`ripen`](https://github.com/codimango/ripen) loops.

Ripen owns the loop — rounds, signals, classification, the pre-push gate, the journal, the
wait, the endings. Assay owns the three things ripen has no opinion about:

- **what counts as hard** — pooled band with a Wilson interval, strongest-cohort mixedness,
  two-family and two-category hardness, per-step pass-and-failure, single-gate coverage;
- **the integrity checks ripen's gate does not carry** — the `tests/config.json` config-integrity
  block, and reward unforgeability where a candidate-controlled pre-grade execution point exists;
- **how ripen's endings map to a reportable terminal state** — `converged` / `escalated` /
  `abandoned` / `blocked-on-platform` → GREEN — HARD / GREEN — MEDIUM / IMPROVED / ESCALATED /
  REJECTED — NOT HARD / BLOCKED — PLATFORM.

Where the two disagree: ripen wins on mechanics, assay wins on the bar.

## Not a second loop spec

Ripen's `SKILL.md` says a task repo must follow exactly one loop spec, and it is right. Assay is
deliberately **not** one — it drives no rounds, keeps no ledger, and pushes nothing. It is
invoked at named points inside ripen's loop:

| Phase | Section |
|---|---|
| Before scaffolding a new task | §1–§4 |
| Ripen STEP 2, terminal check | §5 |
| Ripen STEP 3, classifying a round | §7 |
| Ripen STEP 5, before a graded-surface push | §6 |
| A too-easy or too-hard round | §8 |
| Any ending | §10 |

Ripen's journal (`.ripen/<task>.json`) stays the only ledger. Assay writes one file, the intake
hypothesis at `$REPO_ROOT/.ripen/<task>-intake.md`, deliberately outside the task tree.

## Composes with

Assay delegates rather than restating, so each rubric drifts independently:

| Concern | Owner |
|---|---|
| Pre-build idea screening — kill-tests, residual hard core, GO/DERISK/KILL | `task-hardness-screen` |
| Per-step multi-turn calibration at k≥10 | `mt-calibrate` |
| Genuine failure vs grader false negative | `task-fairness-signal` |
| Contamination, recall, portfolio dedup | `swebench-idea-triage` or the track's own check |
| The loop itself | `ripen` |

## Install

```bash
git clone git@github.com:kngreen/assay.git ~/.claude/skills/assay
ln -s ~/.claude/skills/assay ~/.codex/skills/assay
```

Available as `/assay` in Claude Code on the next session, and to Codex via its skills directory.
`~/.llms/skills/claude-templates` is a symlink to `~/.claude/skills`, so the single clone also
surfaces through the devmate mirror.

Muse resolves components through the agent marketplace rather than a local path, so reaching it
means landing the skill in `fbcode/claude-templates/components/skills/assay/` and then
`claude-templates skill install assay --agent muse`.

## Prerequisites

`ripen` installed and driving the task. Assay refers to ripen's scripts by name —
`infra_check.sh`, `graded_hash.sh`, `record_round.sh`, `gate.sh --mutation` — and to the
`team-aai` bundle for the skills in the table above.

## Provenance

Distilled from a hard-first Codimango task-quality workflow (v1 and v5.1) by keeping what ripen
does not enforce and deleting what it does. The removed material — exact-SHA lifecycle, push
hygiene, bounded-wait deadlines, the round ledger and its pooling bookkeeping — is all carried
by `gate.sh`, `watch_sha.sh` and `record_round.sh`, and prose copies of it drift out of sync
with the scripts.

Named `assay` rather than `temper` because `components/skills/temper` is already a published
convergence-loop skill, and a second thing called temper that insists it is not a loop is the
collision worth avoiding.
