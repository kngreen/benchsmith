# Executable control manifest

Benchsmith resolves three capability sets:

- `declared`: claims in `<task>/.benchsmith/controls.json`;
- `detected`: evidence found independently in the contract, tests, and runtime files;
- `required`: policy controls plus every detected capability.

The effective set is their union. A detected or policy-required capability missing from the
declaration is blocking; setting a capability to `false` cannot suppress it. The task file is an
input, not the trust root. The resolved manifest and its digest are written into the gate receipt
outside the worktree.

`benchsmith controls --repo REPO --task TASK` prints all three sets, the effective manifest,
applicability evidence, errors, and the number of obligations examined.

Legacy tasks without a manifest run in shadow mode. A staged relevant surface change is enforced
immediately; committed changes are enforced after the rollout epoch in `controls.py`. Set
`BENCHSMITH_CONTROLS_ENFORCE=1` to exercise enforcement during migration. Documentation-only edits
do not activate it.

## Schema version 1

```json
{
  "schema_version": 1,
  "capabilities": {
    "mutation_adequacy": true,
    "critic_receipt": true,
    "authorization": true,
    "metamorphic_variation": true
  },
  "obligations": [
    {
      "id": "AUTH-DELETE-1",
      "contract_reference": "instruction.md#delete",
      "observation_boundary": "DELETE response and persisted resource graph",
      "accepting_witness": {
        "path": "qa/positive/owner-delete.sh",
        "control": "command",
        "command": ["bash", "{path}"]
      },
      "rejecting_witness": {
        "path": "qa/negative/anonymous-delete.sh",
        "control": "command",
        "command": ["bash", "{path}"]
      },
      "adapter": {
        "name": "http-state",
        "version": "1",
        "digest": "<64 lowercase hex>"
      },
      "control_version": "1",
      "applicability": {
        "state": "applicable",
        "evidence": ["instruction.md requires ownership checks"]
      },
      "allowed_freedoms": [
        {
          "description": "authorization may be implemented in middleware or the handler",
          "witness": {
            "path": "qa/positive/middleware-auth.sh",
            "control": "command",
            "command": ["bash", "{path}"]
          }
        }
      ]
    }
  ],
  "mutation": {
    "test_command": ["tests/test.sh"],
    "cases": [
      {
        "obligation": "AUTH-DELETE-1",
        "targets": ["solution/check.py"],
        "operators": ["truth-flip", "return-none"]
      }
    ]
  }
}
```

Every semantic obligation needs an accepting witness and either a rejecting witness or a structured
non-applicability proof whose `proof` has the same `{path, control, command}` shape. The command
must consume `{path}`. Free-text freedoms do
not count; every listed freedom needs a positive witness.

Mutation adequacy is universal, but mutant types are selected per obligation by its adapter. Every
obligation needs a mutation case naming its targets and relevant operators. The gate blocks when an
applicable adapter is unsupported, no viable mutant is examined, or any viable mutant survives.

## Conditional controls

`candidate_execution` requires `verifier_closure`. It declares immutable runtime identity,
candidate-influenced executable/read/write/dependency/result-channel closure, and commands for a
valid run and tampering probe. It also declares `candidate_roots`, which bound the paths the
candidate or task can influence. On Linux, Benchsmith traces both commands with `strace` and blocks
any influenced path outside the declared closure. The valid run must exit zero and the tampering
probe nonzero; an unavailable tracer is `NOT_RUN`, not a pass.

`metamorphic_variation` requires a JSON source, a command containing `{fixture}`, and selected
transforms from `rename_ids`, `ordering`, `multiplicity`, and `irrelevant_entity`. Benchsmith chooses
the seed, records it and the generated fixture digest in the receipt, and blocks zero generated
variants.

The receipt cache identity includes the task tree, schema and manifest versions, control
implementation digest, adapter versions and digests, runtime identity, verifier-closure digest,
metamorphic seed, and generated fixture digest. Commit- and diff-relative checks are not covered by
that tree-invariant identity and rerun after a rebase.

## Critic receipt

The critic ends its isolated session with one `BENCHSMITH_CRITIC_RECEIPT=` JSON line containing the
task ID, exact SHA, critic version, session ID, decision, evidence digest, and timestamp.
`benchsmith critic-receipt` fetches the terminal AgentCloud transcript itself and stores the bound
receipt outside the worktree. A task file or stdin value cannot make the review row green.
