#!/usr/bin/env python3
"""Build the file-set that registers benchsmith with the Skills SDK.

Why register at all: `~/.claude/skills/` is a Claude Code root, not an AgentCloud
one. AgentCloud resolves a skill name through SkillsService, and only a
registered skill is in that namespace -- which is why `/benchsmith` failed there
while working fine in the terminal.

Why a generated file-set rather than a second copy of the skill: the body IS
SKILL.md. Two copies of the judgement drift, and the copy people edit would stop
being the copy the agent gets.

Two deliberate choices about WHAT gets published:

  * **Only SKILL.md.** `lib/`, `bin/` and `references/` are all on the host
    already, an AgentCloud session runs on that host, and the preamble names the
    absolute path. A published second copy could be served from a different
    revision than the one that actually runs, which is worse than no copy -- and
    the skill cannot work off-host regardless, because it needs `bin/benchsmith`.
    Publishing everything cost 81% of the single-argument limit; this costs 38%,
    which is the difference between having room to grow and not.
  * **The published SKILL.md gets a binding preamble.** Every command in the
    body reads `benchsmith <cmd>`, and `benchsmith` is not on PATH in a session,
    so an unmodified body would tell the agent to run something that does not
    exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_ARG = 128 * 1024  # a single argv entry; the whole file-set travels as one

# Discovery, not a literal path. Baking in the publisher's home directory means
# the skill only works for the person who published it -- everyone else gets a
# CLI at a path that does not exist on their machine.
PREAMBLE = """
> **Binding — do this first.** This skill's commands are a package installed on a
> host, not files delivered with this skill. Find the CLI before anything else:
>
> ```bash
> export no_proxy="$no_proxy,.internalmeta.com"
> for c in "$BENCHSMITH_BIN" "$HOME/.claude/skills/benchsmith/bin/benchsmith" \\
>          "$HOME/.codex/skills/benchsmith/bin/benchsmith" \\
>          "$(command -v benchsmith 2>/dev/null)" {hint}; do
>   [ -n "$c" ] && [ -x "$c" ] && export BENCHSMITH_BIN="$c" && break
> done
> [ -x "$BENCHSMITH_BIN" ] || {{ echo "benchsmith is not installed on this host"; exit 1; }}
> export BENCHSMITH_ROOT="$(cd "$(dirname "$BENCHSMITH_BIN")/.." && pwd)"
> "$BENCHSMITH_BIN" preflight --json
> ```
>
> Every `benchsmith <cmd>` below means `"$BENCHSMITH_BIN" <cmd>`.
>
> **`references/` is not shipped with this skill — read it from disk.** Where the
> body says `references/<file>.md`, the file is at `$BENCHSMITH_ROOT/references/<file>.md`.
> The full checkout, including `lib/`, is at `$BENCHSMITH_ROOT`.
>
> If nothing above resolves, benchsmith is not installed on the host you are on.
> Say so and stop — do not improvise a substitute for the gate. Install it with:
> `git clone <your benchsmith remote> ~/.claude/skills/benchsmith`

"""


def build() -> list[dict]:
    text = (ROOT / "SKILL.md").read_text()
    end = text.find("\n---\n", 3)
    if end < 0:
        raise SystemExit("SKILL.md has no frontmatter fence")
    head, body = text[: end + 5], text[end + 5:]
    # The publisher's own install is offered LAST, as a hint for colleagues on the
    # same host, never as the answer.
    published = head + PREAMBLE.format(hint=f'"{ROOT}/bin/benchsmith"') + body

    return [{"path": "SKILL.md", "content": published}]


if __name__ == "__main__":
    files = build()
    blob = json.dumps(files)
    if len(blob) > MAX_ARG:
        raise SystemExit(
            f"file-set is {len(blob)} bytes, over the {MAX_ARG} single-argument limit. "
            "Trim references/ or split SKILL.md before publishing."
        )
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/benchsmith-files.json")
    out.write_text(blob)
    print(f"wrote {out} — {len(files)} files, {len(blob)} bytes "
          f"({100 * len(blob) // MAX_ARG}% of the argument limit)")
