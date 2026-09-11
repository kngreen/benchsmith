# The local iOS / macOS-VM track

`codimango bench ios run` executes a task through `run.sh` locally, so this track has no platform
jobs. `benchsmith passatk` normalises local runs into the same rows §5 already consumes — a second
difficulty implementation is how two tracks come to disagree about what "hard" means.

Three differences, each enforced rather than noted:

- **`oracle` is the reference, not a cohort.** It must pass every run, and it is never difficulty
  evidence. A failing oracle is a task defect; fix it before reading any rate.
- **The roster is exactly `claude-code` and `metacode`.** The two-family requirement is satisfiable
  only exactly, so losing a cohort does not weaken the evidence — it ends it.
- **A local run reports pass or fail and nothing about why**, so a failure is classified `G`, not
  `A`. Attributing a semantic cause nobody observed is how a harness failure becomes fake hardness.

`metacode` maps to the `avocado` family and is therefore subject to the same model-under-test
saturation block as the hosted tracks.

**Not yet exercised against live iOS infrastructure.** The normalisation and the blocks are
fixture-tested; the `run.sh` integration is not. Treat a first real run as a probe of this code,
not only of the task.
