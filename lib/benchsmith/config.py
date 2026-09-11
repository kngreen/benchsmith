"""Where benchsmith is told about things it cannot discover.

Only one thing genuinely needs configuring today: which GSD board holds this
person's task cards. There is no way to infer that -- a board is a project id,
and a devserver user typically owns or watches many.

The default is therefore **no board**, not a guessed one. An early version
defaulted to "every open task you own", which pulled 94 oncall parents,
translation requests and unrelated work items into a task queue. A wrong board
is worse than no board: no board is visibly empty, a wrong one looks like work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

USER_CONFIG = Path.home() / ".config" / "benchsmith" / "config.json"
REPO_CONFIG = ".benchsmith/config.json"

# GSD section names -> queue kind. These are the fleet blueprint's column names;
# a board that spells them differently supplies its own map.
DEFAULT_SECTIONS = {
    "Task needs review": "gsd_review",
    "Task is ready to scaffold": "gsd_scaffold",
    "Task ideas (auto-generated)": "idea",
}


@dataclass
class GsdConfig:
    project_id: str = ""
    sections: dict = field(default_factory=lambda: dict(DEFAULT_SECTIONS))
    assignee: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.project_id)

    def as_dict(self) -> dict:
        return {"projectId": self.project_id, "sections": self.sections,
                "assignee": self.assignee, "configured": self.configured}


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load(repo_root: Path | None = None, *, project_id: str = "", assignee: str = "") -> GsdConfig:
    """Resolve the board. Precedence: flag, env, repo config, user config."""
    layers = [_read(USER_CONFIG)]
    if repo_root:
        layers.append(_read(Path(repo_root) / REPO_CONFIG))

    cfg = GsdConfig()
    for layer in layers:  # later layers win
        g = layer.get("gsd") or {}
        cfg.project_id = str(g.get("projectId") or g.get("project_id") or cfg.project_id)
        cfg.assignee = str(g.get("assignee") or cfg.assignee)
        if isinstance(g.get("sections"), dict) and g["sections"]:
            cfg.sections = dict(g["sections"])

    cfg.project_id = project_id or os.environ.get("BENCHSMITH_GSD_PROJECT", "") or cfg.project_id
    cfg.assignee = (assignee or os.environ.get("BENCHSMITH_GSD_ASSIGNEE", "")
                    or cfg.assignee or os.environ.get("USER", ""))
    return cfg


HOWTO = """No GSD board is configured, so no board cards are queued.

Find your project id:
    meta tasks.gsd.project list --owner-is-me --output=json
    meta tasks.gsd.project list --name-contains='<part of the name>' --output=json

Then either export it:
    export BENCHSMITH_GSD_PROJECT=<project-id>

or write ~/.config/benchsmith/config.json:
    {
      "gsd": {
        "projectId": "<project-id>",
        "assignee": "<your unixname>",
        "sections": {
          "Task needs review": "gsd_review",
          "Task is ready to scaffold": "gsd_scaffold",
          "Task ideas (auto-generated)": "idea"
        }
      }
    }

Check the section names against your board -- they are its column names:
    meta tasks.gsd.task list --project-id=<project-id> --columns=number,title,section
A section that is not in the map falls to the idea tier, which is the cheapest
place for a mis-mapping to land."""
