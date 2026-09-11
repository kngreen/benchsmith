# Fleet coordinator — pinned analysis

Status: **design, not built.** Pinned 2026-09-11 so the reasoning survives the session.

## Scope boundary

benchsmith is a **single-task driver**: one task, one journal, one driver, one ledger. That
invariant is load-bearing and the coordinator must not erode it. The coordinator is a separate
concern — benchsmith is the worker, this is the supervisor.

benchsmith already supplies the substrate: `.benchsmith/<task>.json` is a resumable on-disk work
record whose `status` field (`running | converged | escalated | abandoned | blocked-on-platform
| blocked`) is a work queue. **A coordinator reads journals; it does not watch agents.**

## What the fleet audit established

A 20-session audit found the slowdown is not too little parallelism:

- **9 sessions at >=80% context**, 550-724K input tokens per model call, "even when mainly
  processing notifications and returning to wait";
- one agent polled a validation job every 20s for **four hours** — on a task whose validation
  already passed and whose real blocker was a human revision request;
- submissions **serialise on one shared push lane**.

So: max efficiency is *fewer, cheaper, shorter-lived* workers. Past ~3-4 concurrent workers you
are queueing on the push lane, and every waiting supervisor pays full context price to wait.

Design consequences, in order of weight:

1. **Stateless and re-entrant.** Run -> read state -> dispatch -> **exit**. No supervisor loop,
   no polling. Wake on completion notification and re-read. A coordinator that holds nothing
   costs nothing while idle.
2. **State from three cheap reads** — platform `tasks list` (one call returned all 29 tasks),
   local journals, the GSD board. Never from watching a child.
3. **The coordinator owns the push lane.** Workers do local rounds in parallel and hand back
   commits; the coordinator sequences pushes. This removes the serialisation rather than having
   each worker rediscover it.
4. **Cap concurrency at the push lane**, not at the number of tasks.
5. **Never cache task state across a wake.** Verified live: a task's `validationCommitSha` moved
   between two reads twenty minutes apart. A supervisor reasoning from cached state reasons
   about a commit that no longer exists.

## Queue priority

Measured queue at time of writing: 29 tasks — 2 `needs_revision`, 10 `draft`, 8 `accepted`,
6 `used_in_training`, 2 `needs_reviewers_assigned`, 1 `being_reviewed`.

| | Bucket | Why this order |
|---|---|---|
| 1 | `needs_revision` | A reviewer already said what is wrong — cheapest signal available |
| 2 | `draft` + validation `failed` | Known broken, evidence already on the SHA |
| 3 | `draft` + `pending` / `passing` | Needs a measurement round before anything is knowable |
| 4 | GSD board ideas | Most expensive: intake, screen, scaffold |

## Worker model selection

Delegation hops decide the worker, and the policy decides the hops:

| Worker | `instruction.md` | `tests/` | rest | hops |
|---|---|---|---|---|
| **Codex** | delegate | yes | yes | **1** |
| Claude | delegate | no | yes | 2 |
| Muse / Avocado (1P) | yes | yes | yes | 0, but blocked — see below |

Muse is the most permitted and the one that breaks: it strips `META_3PAI_AGENT_PLATFORM` in
plugin hook environments, so the `aai-long-horizon` guard resolves `NOT_PROVEN` and denies it
(upstream T287337880, no env workaround). **Codex is the widest-capability worker the guard
actually recognises** — `META_3PAI_AGENT_PLATFORM=codex` is a positive branch.

## Serial spine, parallel judgement

A round is ~40 minutes of platform time; judgement steps are seconds. Fan-out is free provided
it never touches the spine.

**Fan out** — cheap, parallel, and genuinely improved by disagreement:

- §3 intake hypothesis — §3 requires *two independent* hard cores; one model reliably finds one.
- §8 H2 hardening slate — already requires >=3 ranked levers frozen before replay. Three models
  propose, **Muse synthesises the ranked slate.**
- §8 H3 validation — must be a *different family* from the proposer, or it is a rubber stamp.
- §6 divergent fixture — a different model will not reproduce the reference's shape, which is
  the entire point of the fixture.
- §10a review critic — already a separate dispatch, already isolation-bound.

**Never fan out**: the bar (arithmetic, one answer), the push lane (already the bottleneck),
and `instruction.md` (policy admits one model class).

**Muse as synthesiser has a policy bonus**: Muse is 1P, so its synthesis can legally *become*
`instruction.md`. Claude's and Codex's cannot.

## Harness resolution

Resolve a capability profile once at STEP 0 — never branch per harness inline. Verified
signals on one devserver: `AGENT=claude_code`; `META_3PAI_ACTIVE_PROVIDER` /
`META_3PAI_AGENT_PLATFORM` for identity; `MUSE_PLUGIN_ROOT` for Muse; `~/.agentcloud` present
but **`acd` not on PATH**, so agentcloud dispatch was unavailable there despite the directory
existing.

The profile answers: where am I, what may I author, who can I dispatch to, who is on the
fan-out roster (deliberately heterogeneous families).

## Open question, blocking implementation

How dispatch actually happens: `acd`, the agentcloud web UI, or something else — and outside
agentcloud, whether a Codex worker is `codex exec`-style or a hand-driven session. Not guessed.

## Small additions to benchsmith this needs

- `benchsmith queue` — merge platform status + local journals into the prioritised list above.
- `benchsmith claim` / `release` — a lease file so dispatch is idempotent and two workers cannot
  take one task.

Both stay inside the single-task remit: benchsmith reports its own state, it does not coordinate.
