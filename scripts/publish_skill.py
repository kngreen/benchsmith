#!/usr/bin/env python3
"""Publish any local skill directory to the Skills SDK.

`~/.claude/skills/` is a Claude Code root, not an AgentCloud one. A skill that
lives only there resolves fine in the terminal and not at all in a session --
which is not a clear failure. An agent hunts the Skillbook registry, finds
nothing, and asks for an alias that does not exist. One did that for three
hours.

Written generally because this is now the second skill needing it, and the
binding preamble is the part that is easy to get wrong: the published body must
locate the CLI and any unshipped files on whatever host it runs on, never at the
publisher's own path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MAX_ARG = 128 * 1024

PREAMBLE = """
> **This skill is installed on a host.** The instructions below are the
> judgement; anything they invoke lives on disk and is not delivered with this
> skill. Find the installation before acting:
>
> ```bash
> for d in "$SKILL_ROOT" "$HOME/.claude/skills/{name}" "$HOME/.codex/skills/{name}" {hint}; do
>   [ -n "$d" ] && [ -d "$d" ] && export SKILL_ROOT="$d" && break
> done
> [ -d "$SKILL_ROOT" ] || {{ echo "{name} is not installed on this host"; exit 1; }}
> ```
>
> Where the body names `{unshipped}`, read it from `$SKILL_ROOT/`. If nothing
> above resolves you are not on a host where {name} is installed: say so and
> stop, rather than improvising a substitute.

"""


def build(root: Path, *, ship: tuple = ("references",)) -> list:
    text = (root / "SKILL.md").read_text()
    end = text.find("\n---\n", 3)
    if end < 0:
        raise SystemExit(f"{root}/SKILL.md has no frontmatter fence")
    head, body = text[: end + 5], text[end + 5:]

    desc = ""
    for line in head.splitlines():
        if line.startswith("description:"):
            desc = line
    if '"' in desc:
        # The service parses frontmatter with a hand-rolled reader: an unquoted
        # scalar containing double quotes reads as empty and publish is
        # rejected. Cheaper to catch here than at the API.
        raise SystemExit("description contains double quotes; the service will read it as empty")

    shipped = []
    for d in ship:
        shipped += sorted((root / d).glob("*.md")) if (root / d).is_dir() else []
    unshipped = ", ".join(sorted({p.name for p in root.iterdir()
                                  if p.is_dir() and p.name not in ship
                                  and not p.name.startswith(".")})) or "any other file"

    published = head + PREAMBLE.format(name=root.name, hint=f'"{root}"',
                                       unshipped=unshipped) + body
    files = [{"path": "SKILL.md", "content": published}]
    files += [{"path": f"{p.parent.name}/{p.name}", "content": p.read_text()} for p in shipped]
    return files


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: publish_skill.py <skill-dir> [--apply]")
    root = Path(sys.argv[1]).expanduser().resolve()
    apply = "--apply" in sys.argv
    files = build(root)
    blob = json.dumps(files)
    if len(blob) > MAX_ARG:
        raise SystemExit(f"file-set is {len(blob)} bytes, over the {MAX_ARG} single-argument limit")
    print(f"{root.name}: {len(files)} file(s), {len(blob)} bytes "
          f"({100 * len(blob) // MAX_ARG}% of the limit)")
    for f in files:
        print(f"  {f['path']}  ({len(f['content'])} bytes)")
    if not apply:
        print("\n(dry run — pass --apply to publish)")
        return 0
    # revise if it exists, create if it does not.
    exists = subprocess.run(["meta", "skills.sdk", "load", "--alias", root.name, "--output", "json"],
                            capture_output=True, text=True).returncode == 0
    verb = ["revise", "--alias", root.name] if exists else ["create", "--visibility", "Only Me"]
    r = subprocess.run(["meta", "skills.sdk", *verb, f"--files-json={blob}", "--output", "json"],
                       capture_output=True, text=True, timeout=300)
    print(r.stdout.strip()[:400] or r.stderr.strip()[:400])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
