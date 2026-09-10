# Open-ended (continuous-reward) tasks

**Read this before applying §5 or §8 to a task whose `[metadata].reward_type` is
`bounded_continuous` or `unbounded_continuous`.** Most of assay's difficulty machinery assumes
binary reward and is wrong here.

## What changes

§8 says *"reward is binary across the whole suite; adding easy cases can never raise a pass
rate."* That is a theorem about `P(all tests correct)` and it **does not hold** on a continuous
task — partial credit means adding a graded dimension can move a score in either direction. Do
not reason about levers with the binary rules.

| §5 / §8 rule | On a continuous task |
|---|---|
| Pooled completion in 0.20–0.50 | Replaced by **score separation** — see the band below |
| Pass = every graded assertion passes | Pass = clears that step's own `min_reward`, which may be a float or a per-key dict. `min_reward = 1.0` is the *binary* default, not a universal constant |
| Adding cases can only lower the rate | False. Model the effect on the metric before adding anything |
| Avocado must record a failure per step | **Does not apply.** The gate is meaningful score spread; avocado clearing every `min_reward`, or matching the oracle's reference score, is not a failure |
| The single-gate 80% rule | Still applies, over the decisions that move the score |

The README must carry a **reference score** — what the oracle actually scores — because "the
oracle passes" is not a threshold on a continuous task.

## The open-ended standard

A continuous task must be genuinely open-ended optimisation, **not binary behaviour hidden
behind a float**. Require all of:

- multiple materially different approaches that can earn credit;
- protected held-out cases or data;
- a primary behavioural metric that moves meaningfully with solution quality;
- clean no-op / naive / partial / strong / reference separation;
- intermediate solutions scoring intermediate — this is the one most often missing;
- real modelling, optimisation, statistical, systems or domain reasoning;
- reproducibility under fixed seeds and bounded resources;
- CPU-only automated evaluation unless GPU use is explicitly justified;
- a strong reproducible reference demonstrating achievable headroom.

**Reject or redesign** if the effective solution is one exact constant, one expected artifact, a
copied golden output, a metadata declaration, a hidden-answer lookup, or pass/fail wrapped in a
number — or if any shortcut unrelated to the intended capability can maximise the score.

## Design the evaluation before the implementation

Fix the metric, the held-out set, the resource budget, the valid output format, the grading
dimensions, and the treatment of invalid or non-reproducible submissions **before** writing the
task. The instruction states the objective, public contract, budget, output format, grading
dimensions and invalid-submission handling — what must be achieved, never an optimal algorithm,
a hidden threshold, or a held-out answer.

Distinguish hard validity requirements from optimisation opportunities. A solver that fails a
validity requirement scores nothing; one that optimises poorly scores badly. Conflating them
turns the task binary again.

## Calibration

Run, at minimum, a baseline/no-op evaluation, a representative partial candidate, a deliberate
cheating candidate, and repeated seeded reference evaluation. The bar is **useful separation
between weak, partial and strong across repeated seeded runs**, in the required difficulty band
— not a pass rate.

The §5 completion gate in `references/gates.md` applies unchanged: exact head, TBR GOOD/Accept,
Full-Task Review 17/17, contamination LOW.
