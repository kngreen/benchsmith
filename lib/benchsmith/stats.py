"""What this loop has actually done.

A loop with no aggregate record cannot be compared to anything -- not to another
loop, not to its own past. This reads the journals and reports the same shape of
numbers a field report would quote: tasks, rounds, how rounds were classified,
and where tasks ended up.

It is descriptive only. Nothing here decides anything; it exists so that claims
about the loop can be checked against it instead of recalled.
"""

from __future__ import annotations

import json
from pathlib import Path

from .backoff import NOT_THE_TASK


def _median(xs: list[int]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def collect(root: Path) -> dict:
    """Aggregate every `.benchsmith/<task>.json` under `root`."""
    journals: list[tuple[str, dict]] = []
    unreadable: list[str] = []
    for path in sorted(Path(root).rglob(".benchsmith/*.json")):
        if path.name.endswith(".receipt.json"):
            continue
        try:
            journals.append((path.stem, json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError) as e:
            # An unreadable journal is an unknown task, not an absent one.
            unreadable.append(f"{path}: {type(e).__name__}: {e}")

    classes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    per_task: list[int] = []
    for _, data in journals:
        rounds = data.get("rounds") or []
        per_task.append(len(rounds))
        for r in rounds:
            key = str(r.get("class") or r.get("cls") or "unrecorded")
            classes[key] = classes.get(key, 0) + 1
        statuses[str(data.get("status") or "unset")] = statuses.get(str(data.get("status") or "unset"), 0) + 1

    total = sum(per_task)
    not_task = sum(v for k, v in classes.items() if k in NOT_THE_TASK)
    return {
        "tasks": len(journals),
        "rounds": total,
        "medianRoundsPerTask": _median(per_task),
        "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
        "statuses": dict(sorted(statuses.items(), key=lambda kv: -kv[1])),
        # The single number the ripen field report calls the highest-leverage
        # thing in the loop. Ours is measured, not estimated.
        "notTheTaskShare": round(not_task / total, 3) if total else None,
        "unreadable": unreadable,
    }
