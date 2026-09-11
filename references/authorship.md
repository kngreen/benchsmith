# Authorship

The rule and the driver table are in §9. This is the mechanics.

## Delegating the spec

When the driving model may not author `instruction.md`, do not hand the round back to the user
and do not paste text you drafted yourself. Write a brief and let a 1P model author it, from the
task directory:

```
metacode models                      # what actually resolves here
metacode run --yolo -m meta/muse-spark-1.3-internal "<brief>"
```

The message is **positional** — `--prompt` prints help and writes nothing.

Approved authors, per the AAI Labs policy of 2026-09-09: Muse Spark 1.3, Avocado, and the three
OSS models treated as 1P for task authoring — **Kimi K3**, **GLM 5.3-Flash**, **Qwen 3.8 27B**.
Codex and Anthropic models are not approved for instructions; the earlier allowance for Codex was
explicitly withdrawn. Check `metacode models` before assuming an ID: policy approval and local
harness availability are different things, and the OSS three may not be wired in yet.

**The OSS models are for training data only.** Never use them on AAI Labs product-code repos or
any other Meta codebase. Task artifacts yes; tooling, libraries and skills no.

When you use one, say so in the task's report: provenance and contamination checks still flag
3P-looking authorship, and reviewers override those warnings only when the author tells them an
approved model was used.

### What goes in the brief

The behaviour to specify, plus the authoring rules:

- two plain paragraphs, 100–220 words;
- externally observable behaviour only;
- no markdown, no file names, no symbols, no test names;
- no hint at the trap;
- end by telling it to write `instruction.md` and do nothing else.

**Never ask for a word-count target or "substantially shorter" prose.** Rejecting a draft for
clarity and re-running with more specific direction is expected, not an exception.

### What Muse must not receive

The base repository, the current participant-facing bytes, and a behavioural description of the
finding — nothing else. Never hand it hidden tests, the grader, the reference solution, cloud
trajectories, or reviewer-proposed wording. Reviewer prose is diagnostic; the authoring model
writes the final prose independently.

### Verifying what comes back

Preserve the prior bytes, the exact brief, and the returned bytes. Inspect the complete diff
before applying it. Re-brief if it drifts — you own the result, you just may not type it.

Any manual typo fix, reflow or merge adjustment afterwards is a **new authoring write** and needs
a new run, not a hand edit.

## Never launder

Do not paste a non-approved model's text into a task file, and do not route a restricted edit
through a permitted model to land text you wrote. Delegation means the approved model authors the
prose from a brief — not that it transcribes yours.

The provenance write-log reads agent write logs and **a flagged write stays flagged even after
the text is replaced.** That is why the spec is authored under these rules from the first draft
rather than repaired later.
