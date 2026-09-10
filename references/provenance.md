# Provenance

Two layers: the task carries tags, the commits carry trailers. Tags say *this task was built
under assay*; trailers say *this commit was*. Neither substitutes for the other — tags survive
into Codimango, trailers survive into `git log` after a task is renamed or moved.

## Task tags

See §4 for the required set. The rule that matters here: **add, never replace.** Ripen writes
`ripen-v1`; a task may already carry track and author tags. Append yours and preserve the rest.

A worked check, since only `aai-labs` is gate-enforced:

```bash
python3 - "$TASK_DIR/task.toml" <<'EOF'
import sys, tomllib
required = {"assay-v1", "aai-labs", "semi-synthetic", "private_repos_1p"}
doc = tomllib.load(open(sys.argv[1], "rb"))
tags = set(doc.get("metadata", {}).get("tags") or [])
missing = sorted(required - tags)
team = [t for t in tags if t.startswith("aai-labs-")]
if not team:
    missing.append("aai-labs-<project>")
print("MISSING:", ", ".join(missing) if missing else "none")
EOF
```

Treat a missing tag as **blocking completion**, not as a warning to note in the report. The
control plane in taskvenger flips readiness back to `working` with "Completion blocked:
task.toml must include …"; assay has no control plane, so the check has to run here.

## Commit trailers

Every commit an assay-driven run creates carries exactly one of each:

```text
Created-Via: assay
assay-Version: 1
assay-Run-ID: <run id>
assay-Workflow: <flow from the §0 entry-point table>
```

Preserve them across amend and rebase. Do not bypass with `--no-verify`.

### Installing the hook without breaking ripen's gate

**Do not set `core.hooksPath` to a new directory.** Ripen installs its gate as a `pre-push` hook
and treats a foreign or missing hook as a FAIL — repointing `core.hooksPath` silently disables
the gate, which is the one thing worse than not having trailers.

Install `commit-msg` **into whatever hooks directory the repo already uses**:

```bash
HOOKS="$(git -C "$REPO_ROOT" config --get core.hooksPath \
        || git -C "$REPO_ROOT" rev-parse --git-path hooks)"
cat > "$HOOKS/commit-msg" <<EOF
#!/bin/sh
set -eu
message_file="\$1"
git interpret-trailers --in-place --if-exists=replace --if-missing=add \\
  --trailer 'Created-Via: assay' \\
  --trailer 'assay-Version: 1' \\
  --trailer "assay-Run-ID: \$ASSAY_RUN_ID" \\
  --trailer "assay-Workflow: \$ASSAY_WORKFLOW" \\
  "\$message_file"
EOF
chmod 755 "$HOOKS/commit-msg"
```

`--if-exists=replace` makes it idempotent across amends. If a `commit-msg` hook already exists
and you did not write it, **chain it rather than overwriting** — same rule ripen applies to
`pre-push`. Overwriting another tool's hook to add provenance is a bad trade.

## Authorship metadata

Record the actual model identifier where the repository's metadata convention has a field for
generating models, and **preserve the task's real human authors**.

Add `human-reviewed` only when a human actually reviewed or curated the task. An Agentic Review
is not a human review, and neither is an assay run.
