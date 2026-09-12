# Coordinator — one supervisor, many workers

### Personal GSD idea foundry

The coordinator may source T-Bench work from a standalone personal GSD board. This is an intake
and state-tracking surface, not an idea generator: every seed must already exist as a
human-originated Idea Exchange record.

```bash
benchsmith ideas init --repo .                         # plan only
benchsmith ideas init --repo . --apply                 # create/connect board + search sections
benchsmith ideas harvest --repo .                      # preview screen-ready seeds
benchsmith ideas harvest --repo . --idea-id 252 --apply
benchsmith ideas mark --gsd-task T123 --verdict GO \
  --core-one "..." --core-two "..." --evidence "..." --apply
benchsmith queue --repo . --fetch                      # includes the configured board
```

`ideas harvest` preserves the Idea ID and human creator, refuses incomplete or non-T-Bench
records, deduplicates by `external_identifier`, and never claims an idea. Its default
MEDIUM/HIGH predicted-novelty filter is only an intake filter, never difficulty evidence;
`--include-unassessed` broadens it. Imported cards land in `Needs hardness screen`; a `GO`
requires two named independent hard cores. Use
`claim-task-idea` for the later claim/scaffold transaction so Direction credit remains attached to
the original creator.

`benchsmith ideas references <task-id>...` is a fail-closed precheck for examples. It can select a
task for deeper pattern mining, but always reports `hardCalibrated: false`: task summaries cannot
prove semantic failure attribution, the frozen strongest set, or the independent critic. Only the
full §5 + §11 loop can promote a built task to the board's `Hard calibrated` section.

Everything above is one task. This section is the other axis: many tasks, limited attention. Use
it when the ask is "work the backlog", not "loop this task".

**The supervisor never does task work.** It orders the queue, starts workers, reads back a small
handoff, and publishes. The moment it starts editing a task itself it has stopped supervising, and
the other N-1 items stall behind it.

### Finding the work

```bash
benchsmith queue --repo . --fetch --json          # discover from codimango + GSD
benchsmith queue --repo . --input payload.json    # or order a payload you already have
```

`--fetch` reads `codimango api tasks list` and the GSD board; ordering itself stays a pure function
of that data, so the same inputs always produce the same plan and a restarted coordinator never
disagrees with itself.

**Only work you own is queued.** `currentUserIsTaskOwner` is computed by the platform for your
credential, so it is an ownership answer rather than an inference from a name — and it matters
because `tasks list` does not only return your own tasks: `--filter reviewing`, `--pod` and `--tag`
all return other people's. A task you do not own is reported with its owner and left out.
`--include-others` waives this deliberately. A row with **no** ownership field is kept: absent is
not `False`.

### Telling benchsmith where your board is

**The GSD board is not hard-coded, and there is no default.** A board is a project id, which cannot
be inferred — a devserver user owns or watches many.

```bash
benchsmith config --repo .                                    # what is set, and how to set it
meta tasks.gsd.project list --owner-is-me --output=json       # find your project id
```

Then any one of these, highest precedence first: `--gsd-project <id>`, `BENCHSMITH_GSD_PROJECT`,
`<repo>/.benchsmith/config.json`, `~/.config/benchsmith/config.json`.

```json
{"gsd": {"projectId": "<id>", "assignee": "<unixname>",
         "sections": {"Task needs review": "gsd_review",
                      "Task is ready to scaffold": "gsd_scaffold",
                      "Task ideas (auto-generated)": "idea"}}}
```

`sections` are your board's **column names** — check them with
`meta tasks.gsd.task list --project-id=<id> --columns=number,title,section`. An unmapped section
falls to the idea tier and is named in the notes, so the map can be corrected rather than silently
mis-firing.

With no board configured, **no cards are queued and preflight says so.** That is deliberate. An
earlier version defaulted to "every open task you own" and pulled 94 oncall parents, translation
requests and unrelated work items into a task queue. A wrong board is worse than no board: no board
is visibly empty, a wrong one looks like work.

| Tier | Meaning | Why here |
|---|---|---|
| 10 | needs revision | A reviewer is already waiting. Latency is the whole cost. |
| 20 | draft, failing | Known-broken and already scaffolded — the shortest path to a submission. |
| 30 | draft, pending | Work in flight; may need only a read. |
| 40 | draft, passing | Passing is not the goal. **Too easy is still a defect**, and these need hardening. |
| 50 | GSD, needs review | A board card someone asked to have looked at. |
| 60 | GSD, ready to scaffold | Screened, but not yet a task tree. |
| 70 | idea | Nothing exists yet. Most expensive, least certain. |

**Board cards sort below every platform task.** A card is a claim that work exists; a platform row
is work that demonstrably exists.

Three things are surfaced rather than swallowed:

- A journal that cannot be read is **flagged, never skipped** — an unreadable ledger is an unknown
  item, not an absent one.
- A platform status the queue does not recognise is **reported**, not dropped. Silently ignoring a
  new status is how a whole class of work disappears from the backlog.
- There is **no link field between a GSD card and a Codimango task**, so a duplicate can only be
  guessed from the wording. A suspected duplicate is queued and marked non-dispatchable, never
  deleted: a wrong guess that deletes loses real work silently, while a wrong guess that keeps
  costs an idea-tier slot.

### Dispatch

```bash
benchsmith dispatch --repo <path> --task <name>            # plan only; writes nothing
benchsmith dispatch --repo <path> --task <name> --apply    # actually start it
```

Planning is the default so a plan can always be inspected, but **you are not waiting for anyone to
read it.** Dispatch, then supervise. The safety is structural — workers cannot push, publishing
needs a gate receipt, one lane per repository — not a confirmation step.

Three backends, and the choice is a capability question, not a preference:

- `agentcloud` (default) — `meta agentcloud.session create --harness codex`. Fleet-visible,
  pollable by session id, and the only backend a second person can watch.
- `codex` — `codex exec`. Local, no session record, blocks until the worker finishes.
- `metacode` — the 1P delegation hop **only**. Never a task worker.

`--harness` accepts `codex` and `native`; it rejects `claude` and `metacode`. So the 1P hop cannot
be an agentcloud session, and benchsmith refuses that combination when the plan is built rather
than letting the API fail after a fan-out has already started.

**`--skills` cannot deliver benchsmith, and is off by default.** SkillsService serves a skill's
`SKILL.md` body only; nested files are withheld from remote nodes unless `--skill-materialization`
is on, and it is off by default and not exposed on the session CLI. benchsmith is a package, so a
body-only delivery produces a worker with the judgement and none of the commands. Remote workers
therefore **clone benchsmith themselves** as the first step of their prompt. Passing an alias that
resolves to nothing would be worse than passing none: the session starts, the skill is silently
absent, and the worker improvises without a gate.

### Handoff

A worker returns **one JSON object under 4 KiB** and nothing else — `work_item`, `state`,
`base_sha`, `commit_sha`, `gate_receipt`, `next_action`, `note`. A worker that returns its
transcript instead is refused: a supervisor holding N transcripts runs out of context before the
queue drains, which is the failure this design exists to prevent.

`state=ready_to_publish` **requires a `commit_sha`**. It is the single claim the supervisor acts
on, so it is the one claim that may not be taken on trust.

### Resuming

**AgentCloud sessions cannot be resumed programmatically.** `create`, `describe`, `list` and `poll`
are the whole surface and journal events are immutable, so there is no way to send work into a
session that already exists. Durability therefore does not live in the session — it lives in the
journal, which is strictly better: it survives the session being lost entirely, and any worker on
any host can pick the task up.

A dispatch against a task with prior rounds prepends a resume block naming the round count, the
mode and the last classification, and tells the worker to read `.benchsmith/<task>.json` before
doing anything. A fresh worker continues the history; it does not restart the task.

### Publishing

**Workers do not push.** They prepare a commit, run `benchsmith gate`, and stop.

```bash
benchsmith publish --repo . --task <name> --handoff h.json          # plan
benchsmith publish --repo . --task <name> --handoff h.json --apply  # push
benchsmith reconcile --repo .                                        # after a crash
```

One lane **per repository**, not per task. The platform validates the branch tip, so two loops
pushing to one repository invalidate each other's evidence and the second push quietly turns the
first one's measurement into somebody else's.

An applied publication first claims a fresh remote task lease, passes that exact lease into the
publisher for revalidation immediately before the push, and releases it afterwards. Repositories
without a shared remote must opt out explicitly with `--no-remote-lease`.
If the push result is ambiguous, the durable intent retains the exact lease token. `reconcile`
releases it only after confirming that the push landed; unknown and retryable-not-landed outcomes
keep the claim so another worker cannot enter the task during recovery.

Publishing refuses unless all of these hold: the handoff says `ready_to_publish`, it carries a
`commit_sha`, it carries a **`gate_receipt`** (without which the lane's one job — that only gated
work reaches the remote — was never done), the lane is free, and the remote head still matches the
base the commit was prepared on. A moved remote means rebase and re-gate; pushing anyway would
measure a tree nobody gated.

**A rebase does not always need a full re-gate.** When the remote moved and the rebase leaves this
task's tree object byte-identical, only explicitly whitelisted tree-invariant evidence is carried
(`oracle`, `config-integrity`, and `tags`). Scope, diff ratchet/weakening, contamination, and review
findings rerun against the rebased commit. A new exact-commit receipt is issued only if they pass;
then `publish --rebase` returns `needsRegate: false`. The old receipt never stands for a new SHA.

That matters because **the coordinator has no task oracle and cannot re-gate**. Returning every
rebase to a worker meant that by the time the worker answered, main had moved again: one task went
round four times before landing, and every lap was real work that was stale on arrival. Only a
rebase that actually moves the task's tree needs fresh evidence.

### Nobody waits on a wave

Codimango is the largest wall-clock cost here, and a worker that sits through a validation wave
holds a slot for tens of minutes to hours while learning nothing it could not read on arrival. One
run spent 48 minutes and then 1h32m exactly that way.

A worker whose commit has been published returns **`state=awaiting_validation`** with the SHA and
stops. You watch, cheaply:

```bash
benchsmith watch --repo REPO --task TASK-NAME --sha SHA --pushed-at UNIXTIME
```

| State | What it means | What you do |
|---|---|---|
| `absent` | the platform has not imported it | wait; at 45 minutes it reports `orphaned` and one `rerun` is authorised |
| `running` | the wave is in flight | wait |
| `terminal` | finished, pass **or** fail | start a fresh worker to read every signal |
| `unknown` | could not read, or an unrecognised status | resolve it; do not treat it as either |

Only `terminal` exits zero. A fresh worker arrives with a clean context and reads the finished
evidence in one pass, which is strictly better than one that spent an hour watching it accumulate.

**This applies in fleet mode only.** A single-task invocation has no slot to free and should stay
in-session through STEP 7.

### A hold on the branch, not on one task

```bash
benchsmith hold show --repo REPO
benchsmith hold take --repo REPO --why "landing task 207170" --minutes 45
benchsmith hold release --repo REPO
```

Task leases answer "is anyone working this task". They cannot answer "is anyone about to land a
stack on main" — which was being coordinated by announcement, so every worker that did not read the
message kept preparing pushes into a claimed branch.

`publish` refuses into a held branch, and refuses when it cannot read the hold: unreadable is not
permission, here as everywhere. Holds are always bounded — an open-ended one is one somebody
forgets to release.

**The intent is written down before the push.** The hard part is not the lock, it is crashing while
holding it. `benchsmith reconcile` compares the recorded intent against the actual remote head and
returns one of four answers:

| State | Meaning |
|---|---|
| `landed` | the push went through; clear the intent and record the round |
| `not-landed` | the remote is still at our base; safe to retry |
| `diverged` | someone else published; rebase and re-gate, the evidence is stale |
| `unknown` | the remote could not be read |

`unknown` is not `not-landed`, and `reconcile` exits non-zero for it. Conflating the two is exactly
how a crash becomes a double push.
