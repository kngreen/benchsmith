# Provenance

Two layers: the task carries tags, the commits carry trailers. Tags say *this task was built
under benchsmith*; trailers say *this commit was*. Neither substitutes for the other — tags survive
into Codimango, trailers survive into `git log` after a task is renamed or moved.

## Task tags

See §4 for the required set. The rule that matters here: **add, never replace.** A task may already carry track, author or
prior-recipe tags. Append yours and preserve every one of them.

A worked check, since only `aai-labs` is gate-enforced:

```bash
python3 - "$TASK_DIR/task.toml" <<'EOF'
import sys, tomllib
required = {"benchsmith-v1", "aai-labs", "semi-synthetic", "private_repos_1p"}
doc = tomllib.load(open(sys.argv[1], "rb"))
tags = set(doc.get("metadata", {}).get("tags") or [])
missing = sorted(required - tags)
team = [t for t in tags if t.startswith("aai-labs-")]
if not team:
    missing.append("aai-labs-<project>")
print("MISSING:", ", ".join(missing) if missing else "none")
EOF
```

`benchsmith gate` runs this check and **blocks the push** on a missing tag — a task that reaches its
first cloud round untagged is already mis-attributed, and Labs cannot see it at all.

## Commit trailers

Every commit a benchsmith-driven run creates carries exactly one of each:

```text
Created-Via: benchsmith
benchsmith-Version: 1
benchsmith-Run-ID: <run id>
benchsmith-Workflow: <flow from the §0 entry-point table>
```

Preserve them across amend and rebase. Do not bypass with `--no-verify`.

### Installing the hook without disabling another tool's gate

**Do not set `core.hooksPath` to a new directory.** Another tool's gate may already live in the
repo's hooks directory, and repointing `core.hooksPath` silently disables it — which is the one
thing worse than not having trailers. `benchsmith install-hooks` resolves the existing path and
installs alongside.

Install `commit-msg` **into whatever hooks directory the repo already uses**:

```bash
HOOKS="$(git -C "$REPO_ROOT" config --get core.hooksPath \
        || git -C "$REPO_ROOT" rev-parse --git-path hooks)"
cat > "$HOOKS/commit-msg" <<EOF
#!/bin/sh
set -eu
message_file="\$1"
git interpret-trailers --in-place --if-exists=replace --if-missing=add \\
  --trailer 'Created-Via: benchsmith' \\
  --trailer 'benchsmith-Version: 1' \\
  --trailer "benchsmith-Run-ID: \$BENCHSMITH_RUN_ID" \\
  --trailer "benchsmith-Workflow: \$BENCHSMITH_WORKFLOW" \\
  "\$message_file"
EOF
chmod 755 "$HOOKS/commit-msg"
```

`--if-exists=replace` makes it idempotent across amends. If a `commit-msg` hook already exists
and you did not write it, **chain it rather than overwriting**. Overwriting another tool's hook
to add provenance is a bad trade, and reordering someone else's tooling unasked is worse than
saying so loudly.

## Authorship metadata

Record the actual model identifier where the repository's metadata convention has a field for
generating models, and **preserve the task's real human authors**.

Add `human-reviewed` only when a human actually reviewed or curated the task. An Agentic Review
is not a human review, and neither is a benchsmith run.
