# The repos' hooks, absorbed

The task repos wire hooks with `core.hooksPath` — `scripts/hooks` in the swe-bench repos,
`.githooks` in t-bench. Two different things live there, and only one is a formatter.

### 15a. The diff-shaped checks (the ones that catch things)

**Is this change worse than the last one?** Every other control here asks whether the current tree
is bad. None asked that, which is why coverage shrinking, fixture-arm removal and assertion
weakening were invisible to them.

These compare the **staged index against HEAD**, or a clean committed `HEAD` against its parent.
`check_test_ratchet` is *not* a substitute: it compares against the last round benchsmith
**recorded**, so anything committed between rounds is invisible to it.

| Check | Catches |
|---|---|
| `diff-ratchet` | a graded test deleted; a test removed with no recorded reason; assertion count falling with no test removed |
| `diff-weakening` | `or True`, `\|\| true`, `pytest.skip`, `# noqa`; an assertion broadened with a **new** `or`; a widened numeric tolerance |

Both are **push-required whenever a graded path changed**, because a check that did not run is how
these got through before. A commit with no graded path is inapplicable rather than unsafe.

A removal is allowed when the *same commit* records why, in `.benchsmith/removals.jsonl`:

```json
{"test": "test_two", "reason": "duplicated by test_one"}
```

Only proofs **added or changed by the staged commit** count — a standing allowlist would let one
old entry authorise every future removal of that name. A record with no `reason` proves nothing.

Two judgements kept deliberately from the original: a graded file that **will not parse is a
finding, not a skip** (a line-based check would happily "examine" it and report clean — the exact
silent-inert shape these exist to catch), and **nothing examined is `NOT_RUN`**, reported and never
printed as a pass.

Python files are parsed with `ast`; Go is parsed with `gofmt`; JavaScript and TypeScript are
validated with Node/TypeScript parsers and inspected as tokenized test/assertion calls; Swift
receives a structural balance check. Code embedded in either `tests/config.json.test_patch` or
`tests/test.patch` is extracted and examined too. Unsupported syntax, parse failure, tests with
zero examined assertions, or a changed graded container with zero examined files blocks. A diff
with no graded path is explicitly inapplicable (`NOT_RUN`) and does not prevent an otherwise valid
receipt.

Cost: pure Python plus a couple of `git show` calls, scoped to graded files. Sub-millisecond when
the diff has no supported graded source in it.

### 15b. Snapshot integrity

| Check | Catches | Push-required |
|---|---|---|
| `contamination` | a `solve.sh` symlink; a `solve.sh` blob carrying `variant overlay` / `mutant overlay` / `mutant (` / `mutant applied`; a tracked `.solve.sh.*` or `.run-*.lock` | **yes** |
| `untracked-deps` | a committed tooling file that invokes a path nobody committed | no |
| `structural` | upstream `codimango bench validate --structural-only` | no |
| `task-author` | whether this task is yours to change | n/a — never blocks |

**Contamination reads git blobs, never the working tree.** That is the point: a `solve.sh` restored
on disk after a mutant run still ships the mutant if the index holds the contaminated blob.
`gate-fixtures/` is excluded — those are contaminated on purpose. Only paths that could possibly be
a finding are inspected; the original scanned every index entry and cost ~9.5 s per commit.

**`untracked-deps` is anchored on invocation, not on path-shaped text.** Matching any path-like
substring false-positives on docstrings and message strings *and* misses the real defect, whose
line is `source "$GATE_SCRIPT_DIR/gate-status.sh"` — a variable-built path matching no literal. So:
match the verb, then resolve by basename. The defect it ends is a tracked gate script sourcing an
untracked helper — shipped, and dead on every machine but the author's.

**`structural` exemptions are named, never silent.** A macOS VM task has no Dockerfile by design.
There is deliberately **no** exemption for a missing `*.pem`: an earlier one claimed the key was
injected at build time; it is not, and the build dies at that `COPY`. An undocumented exemption is
indistinguishable from a bug, and a gate that fires known false positives is one people learn to
skim.

### 15c. Whose task is this?

Two independent answers, and benchsmith uses both:

- **`currentUserIsTaskOwner`** — computed by the platform for your credential (`references/coordinator.md`). Authoritative,
  needs the network.
- **`task-author`** — `task.toml`'s `authors[].name` against `git config user.name`. Works in a
  fresh clone with no connectivity, which is exactly where a worker is when it decides whether to
  touch a directory.

A foreign task is **skipped, never failed**, and that distinction is load-bearing. Failing it
deadlocks the moment you merge a colleague's commits: their directory appears in the range, the
gate refuses to assess a task whose intent you do not hold, so the required receipt can never
exist, and the only escape is a bypass that disables the check for *your* tasks too. A gate that
cannot go green is not a gate.

### 15c-2. The fixture corpus (G2/G5)

```bash
benchsmith corpus --repo . --task <name>    # minutes per fixture; opt-in
```

Two questions no generated mutant answers:

- **Is the grader too loose?** Every `qa/negative/*.sh` is a cheat vector someone already thought
  of. It must not score 1.0.
- **Is the grader too strict?** Every `qa/positive/*.sh` and `qa/variants/*.sh` is a
  correct-but-different solution. It must score 1.0.

This **complements `benchsmith mutate`** rather than repeating it: a generated mutant finds a hole
nobody anticipated; a corpus fixture stops a hole that was already found from reopening.

Four rules carried over intact, each of which has already been got wrong once:

- **Glob, never hardcode.** A fixed name list is one task's cheat vectors imposed on every task —
  and it does not merely fail the wrong task, it **skips the fixtures the task does own**, so the
  gate goes green having run nothing.
- **A timeout is a third state**, alongside pass, fail and not-run. Narrating one into "it was
  expected to score 0.0 anyway" is how a reward-hack fixture stops being checked.
- **Aggregate with MIN.** A trial passes only when *every* step scores 1.0. A mean against a
  threshold lets a cheat through; a max picks the best step and does the same.
- **A `*suffix.sh` negative appends to the last step** (gold, then tamper). Any other negative
  replaces **every** step — otherwise a later gold step silently repairs the cheat and it scores
  1.0.

An empty corpus is `NOT_RUN`, and not run is not passed. The corpus is opt-in from the gate because
it runs the benchmark once per fixture; its absence is reported, never treated as clean.

### 15c-3. The controls roster

`scripts/controls/EXPECTED` lists the controls that must exist. A control that quietly disappears
leaves no trace — the gate simply stops running it and goes green faster. The roster is the only
thing that notices, which is why a **missing roster is itself a finding**, not a reason to skip.

The repository hook bridge uses a mode-0700 temporary directory and a mode-0600 receipt, passes its
path only to that hook process, and deletes the whole directory on success or failure. There is no
shared `/tmp/gate-receipt-<task>.json`. A private outer-authority marker can defer a nested
`gate --verify-receipt` arm only when its mode, owner, token, repository, task and exact HEAD match,
and the recorded issuer PID is still a live ancestor of the verifier process. This is process-tree
hardening, not a credential boundary against code that already controls such an ancestor. Canonical
and chained hooks therefore avoid recursion while every sibling command still executes.

The top-level hook identity covers the executable Git invokes; Benchsmith deliberately does not
parse shell to discover and hash arbitrary sourced helpers, because static shell parsing would claim
coverage it cannot prove. Mitigation: evidence authentication reruns the current outer hook, Git runs
the current hook again on the real push, and any top-level hook identity change invalidates the
cached gate receipt.

When `--branch` is omitted, the gate resolves and records the publication remote's advertised
default branch instead of assuming `main`. An explicitly named missing branch still fails closed. A
repository with neither an executable pre-push hook nor a configured publication remote retains the
local-only `not-applicable` hook result.

### 15c-4. Separate-verifier artifact transfer

A task with `tests/Dockerfile` must provide executable, non-symlink `qa/artifact-transfer`. The gate
runs that task-local contract from a full disposable export of the exact candidate, with only an
allowlisted environment, and stores its content digest and result in the gate receipt. The source
checkout is compared before and after; any reach-back mutation is a contract failure. The contract
must exercise the repository's Harbor-equivalent export/import path, prove the candidate repository
is present at `/app`, and smoke the oracle there. Missing or stale proof blocks publication.
Benchsmith does not infer a universal Docker command or claim more parity than the task's executable
contract proved.

### 15d. The formatters

The scratch and base-tree clones also carry a `.githooks/pre-commit` running prettier and eslint
over `web/src`. Benchsmith reproduces it so the repo copy can be deleted and every worker gets the
same enforcement whether or not its clone wired anything up.

```bash
benchsmith hooks --repo . --time      # what the repo ships, what we run, what it costs
benchsmith fmt --repo .               # apply formatters to staged files, then re-stage
benchsmith gate --repo . --task TASK-NAME   # includes a check-only `formatting` step
```

### Check and fix are different commands

`benchsmith fmt` writes. The gate only checks. **The gate may not rewrite files** — its surface
hashes and its receipt are computed against the tree as read, so a formatter running inside it
would attest to a tree that no longer exists.

### Why this does not slow the loop down

| Situation | Cost |
|---|---|
| Staged files match no hook glob | **~18 ms, zero subprocesses** — measured |
| Match, unchanged bytes since last pass | cache hit, no `npx` start |
| Match, changed | hooks run **concurrently**; wall clock is the slowest, not the sum |
| A formatter hangs | `NOT_RUN` at the budget, never a hung loop |

The fast path is the one that matters: a benchsmith round touches `tests/`, `instruction.md` or
`solution/`, none of which match a `web/src` glob, so the whole step is one `git diff --cached` and
no process spawns. The cache is keyed on the tool's argv plus the **exact bytes** of the files it
saw, so re-gating an unchanged tree never pays a second cold start — and changed bytes always
invalidate it.

A missing toolchain is **`NOT_RUN`, not a pass**: "node is not installed" and "the code is
formatted" are different findings. `formatting` is deliberately **not** in the push-required set,
so a devserver without node is not locked out of pushing.

### Before you delete the repo copy

```bash
benchsmith hooks --repo .
```

**A hook file is not a hook** — but check which repo you are in before concluding anything. The
live task repos **do** wire theirs: `core.hooksPath` is `scripts/hooks` (swe-bench) or `.githooks`
(t-bench), and those hooks have been catching real defects. It is the *scratch, review and
base-tree* clones where `core.hooksPath` is unset, `.git/hooks/` is empty, and the shipped
`.githooks/pre-commit` is inert — one such copy is broken outright (`$STAGED` never assigned, so it
invokes prettier with no files). `benchsmith hooks` reports `active` versus inert per repo, which
is what makes sunsetting a decision rather than a guess.

Do not delete a repo hook until the equivalent benchsmith check is green on the same commit.

### Configuring them

Hooks live under `hooks` in the same config file as the board. The default mirrors the repos'
prettier/eslint setup; an **explicit empty list** means "no hooks", while a **missing key** means
"use the default" — conflating those would make opting out impossible.

```json
{"hooks": [
  {"name": "prettier", "globs": ["web/src/**/*.ts", "web/src/**/*.tsx", "web/src/**/*.css"],
   "cwd": "web", "strip": "web/",
   "check": ["npx", "prettier", "--check", "--log-level", "warn"],
   "fix": ["npx", "prettier", "--write", "--log-level", "warn"]}
]}
```

`**/` spans directories and `*` does not — `fnmatch` conflates them, which is why the matcher is
hand-written.
