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

PREAMBLE = """
> **Binding — do this first.** This skill's commands are a package on the host,
> not files delivered with this skill. Bind the CLI before anything else:
>
> ```bash
> export BENCHSMITH_BIN={root}/bin/benchsmith
> export no_proxy="$no_proxy,.internalmeta.com"
> "$BENCHSMITH_BIN" preflight --json
> ```
>
> Every `benchsmith <cmd>` below means `"$BENCHSMITH_BIN" <cmd>`.
>
> **`references/` is not shipped with this skill — read it from disk.** Where the
> body says `references/<file>.md`, the file is at `{root}/references/<file>.md`.
> The full checkout, including `lib/`, is at `{root}`.
>
> If that path does not exist you are not on the host benchsmith is installed on:
> say so and stop, rather than improvising a substitute for the gate.

"""


def build() -> list[dict]:
    text = (ROOT / "SKILL.md").read_text()
    end = text.find("\n---\n", 3)
    if end < 0:
        raise SystemExit("SKILL.md has no frontmatter fence")
    head, body = text[: end + 5], text[end + 5:]
    published = head + PREAMBLE.format(root=ROOT) + body

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
