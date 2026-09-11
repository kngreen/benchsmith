# Attribution, levers, and the near-miss question

Three rules that decide whether a round produced evidence or just activity.

### A measurement must cover your commit

The platform validates the **branch tip**, not your SHA. With several loops pushing to one
repository your commit is buried before the import sweep runs — so demanding an exact SHA match
does not merely lose rows, it **deadlocks**: you wait forever for a verdict that will never be
addressed to you.

Pass `--repo` and `--task` to `benchsmith bar` and a row counts when your commit is an **ancestor**
of the job's commit **and** the graded and agent-visible surface hashes are identical between the
two. Surfaces, not whole trees: a README edit between two commits did not change what was measured
and must not discard a good row.

| Verdict | Counts? | Meaning |
|---|---|---|
| `exact` | yes | the job ran on your commit |
| `ancestor-identical` | yes | a descendant, with both surfaces unchanged |
| `stale` | no | the job predates your push, or is on a fork |
| `divergent` | no | a surface moved between the two commits |
| `unknown` | **no** | ancestry or hashes could not be computed |

`unknown` never joins the covering set. An attribution you could not compute is an open question,
and scoring an open question as coverage is the fail-open every other gate here exists to prevent.
Admission by ancestry is written to the notes, so an audit can always see which rule let a row in.

### One difficulty lever per hardening round

The levers are the **graded surface** (`tests/`, `task.toml`), the **spec** (`instruction.md`) and
the **reference** (`solution/`, `reference/`, `*.patch`). In `harden` mode the gate fails if a round
moves more than one.

This is not tidiness. Move two and the next measurement is unattributable — the band shifted, and
nothing in the record says which change shifted it. Corrective rounds are exempt and batch freely;
they are not claiming to have moved anything.

### A green suite has not been shown to discriminate

Passing the reference and failing the base demonstrates the suite detects **one** difference: the
whole solution. It says nothing about whether the suite can tell the solution from a plausible
wrong version of it.

```bash
benchsmith mutate --repo . --task <name> --target src/thing.py --test-cmd "pytest -q"
```

A **survivor** is a hole in the grader, reported as a file and a line. Two distinctions the probe
keeps:

- A mutant the compiler rejects is **not viable** — it leaves the denominator rather than counting
  as a catch. The compiler rejecting a mutant is not the tests discriminating.
- Go and Python only. Any other language is **`NOT_RUN`**, which is *uncovered*, not clean.

**The probe proves the suite passes unmutated before believing anything it says.** Without that
check a harness that cannot run at all fails on every mutant, every mutant reads as caught, and the
probe issues a clean bill for a suite that never executed. That is not hypothetical — it is how the
first version of this probe fooled itself.

### Backoff, and the streak that ends the round

`benchsmith backoff --repo . --task <name>` reads the journal and says how long to wait.
`infra`, `not-measured` and `platform-stale` are not the task's fault; the wait doubles with the
streak, is capped, and is **jittered** so concurrent workers do not retry in lockstep and keep a
rate-limited registry rate-limited. At five consecutive such rounds, stop at
`blocked-on-platform` — past that the loop is not iterating, it is polling an outage.

`benchsmith stats --root <dir>` reports what the loop has actually done, including the share of
rounds that were not the task's fault. Quote that number rather than recalling it.
