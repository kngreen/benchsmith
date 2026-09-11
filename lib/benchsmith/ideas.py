"""Personal GSD intake for human-originated T-Bench ideas.

The board is a work queue, not an idea generator.  Seeds come from Idea
Exchange, where their human origin is recorded.  This module preserves that
record, makes mutations opt-in, and never promotes a task to "hard calibrated"
from a declared difficulty or a summary pass rate.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


BOARD_FILE = Path(".benchsmith/config.json")
DEFAULT_BOARD_NAME = "T-Bench Idea Foundry"
IDEA_URL = "https://aai-ideation-network.internalmeta.com/ideas/{id}"
CARD_PREFIX = "[T-Bench seed #{id}] "

STAGES = (
    "Needs hardness screen",
    "DERISK probes",
    "Ready to scaffold",
    "Building and calibrating",
    "Hard calibrated",
    "Rejected",
)
SECTION_KINDS = {
    "Needs hardness screen": "idea",
    "DERISK probes": "idea",
    "Ready to scaffold": "gsd_scaffold",
    "Building and calibrating": "gsd_review",
    "Hard calibrated": "done",
    "Rejected": "done",
}

_SECTION_ALIASES = {
    "idea": "idea",
    "domain": "domain",
    "domain / sub-domain": "domain",
    "capability under test": "capability",
    "why sota should fail": "why_fail",
    "why sota should fail this": "why_fail",
    "difficulty levers": "levers",
    "difficulty levers (conceptual)": "levers",
    "verification intent": "verification",
    "language": "language",
    "novelty risk": "novelty",
}
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_ID_KEYS = ("id", "projectId", "project_id", "fbid", "number")
_Run = Callable[[list[str]], tuple[int, str, str]]


class IdeasError(ValueError):
    pass


def _run(argv: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


def _json(stdout: str, label: str):
    text = stdout.strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise IdeasError(f"{label} returned no JSON")
    try:
        return json.loads(text[min(starts):])
    except json.JSONDecodeError as exc:
        raise IdeasError(f"{label} returned invalid JSON: {exc}") from exc


def _call_json(run: _Run, argv: list[str], label: str):
    code, out, err = run(argv)
    if code != 0:
        detail = err.strip() or out.strip() or f"exit {code}"
        raise IdeasError(f"{label} failed: {detail[:500]}")
    return _json(out, label)


def _rows(document, *keys: str) -> list[dict]:
    if isinstance(document, list):
        return [row for row in document if isinstance(row, dict)]
    if isinstance(document, dict):
        for key in keys:
            value = document.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _entity(document, key: str) -> dict:
    if isinstance(document, dict) and isinstance(document.get(key), dict):
        return document[key]
    return document if isinstance(document, dict) else {}


def _selector(entity: dict) -> str:
    for key in _ID_KEYS:
        value = entity.get(key)
        if value not in (None, ""):
            return str(value)
    raise IdeasError("GSD project response contained no project identifier")


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(document, indent=2) + "\n")
    os.replace(tmp, path)


def load_board(repo: Path) -> dict:
    path = Path(repo) / BOARD_FILE
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise IdeasError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise IdeasError(f"{path} must contain a JSON object")
    board = document.get("gsd") or {}
    return board if isinstance(board, dict) else {}


def _save_board(repo: Path, board: dict) -> None:
    path = Path(repo) / BOARD_FILE
    document = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, dict):
                document = existing
        except (OSError, json.JSONDecodeError):
            pass
    document["gsd"] = board
    _write_json(path, document)


def _find_project(run: _Run, name: str, owner: str) -> dict | None:
    argv = [
        "meta", "tasks.gsd.project", "list", f"--name-is={name}",
        "--limit=10", "--output=json", "--no-truncate",
    ]
    argv.append(f"--owner={owner}" if owner else "--owner-is-me")
    rows = _rows(_call_json(run, argv, "GSD project lookup"), "projects")
    if len(rows) > 1:
        raise IdeasError(f"found {len(rows)} personal GSD projects named {name!r}; pass --project")
    return rows[0] if rows else None


def _section_names(run: _Run, project: str) -> set[str]:
    document = _call_json(
        run,
        ["meta", "tasks.gsd.section", "list", f"--project={project}",
         "--limit=100", "--output=json", "--no-truncate"],
        "GSD section list",
    )
    return {str(row.get("name") or "").casefold() for row in _rows(document, "sections")}


def init_board(repo: Path, *, name: str = DEFAULT_BOARD_NAME, owner: str = "",
               project: str = "", apply: bool = False, run: _Run = _run) -> dict:
    repo = Path(repo).resolve()
    saved = load_board(repo)
    found = None
    if project:
        found = {"id": project, "name": name}
    elif saved.get("projectId"):
        found = {"id": saved["projectId"], "name": saved.get("name") or name,
                 "url": saved.get("url")}
    else:
        found = _find_project(run, name, owner)

    actions: list[dict] = []
    if found is None:
        actions.append({"action": "create_project", "name": name, "owner": owner or "me"})
        if not apply:
            actions.extend({"action": "create_section", "name": section}
                           for section in STAGES)
            return {"applied": False, "project": None, "actions": actions,
                    "next": "re-run with --apply to create the standalone personal board"}
        argv = [
            "meta", "tasks.gsd.project", "create", f"--name={name}",
            "--description=Personal queue for human-originated T-Bench seeds and Benchsmith calibration.",
            "--plan=Preserve the human seed; screen before scaffolding; reserve hard-calibrated for an exact-head Benchsmith verdict.",
            "--output=json",
        ]
        if owner:
            argv.append(f"--owner={owner}")
        found = _entity(_call_json(run, argv, "GSD project creation"), "project")

    selector = _selector(found)
    existing = _section_names(run, selector) if apply else set()
    for section in STAGES:
        if section.casefold() in existing:
            continue
        actions.append({"action": "create_section", "name": section})
        if apply:
            _call_json(
                run,
                ["meta", "tasks.gsd.section", "create", f"--name={section}",
                 f"--project={selector}", "--type=static", "--output=json"],
                f"create GSD section {section}",
            )

    url = str(found.get("url") or "")
    if url.startswith("/"):
        url = f"https://www.internalfb.com{url}"
    elif not url and selector.isdigit():
        url = f"https://www.internalfb.com/intern/gsd/{selector}/"
    config = {
        "name": str(found.get("name") or name),
        "projectId": selector,
        "url": url,
        "assignee": owner or os.environ.get("USER", ""),
        "sections": SECTION_KINDS,
        "source": "Idea Exchange human-originated T-Bench seeds",
    }
    if apply:
        _save_board(repo, config)
    return {"applied": apply, "project": config, "actions": actions,
            "config": str(repo / BOARD_FILE)}


def parse_idea_description(description: str) -> dict[str, str]:
    clean = re.sub(r"<!--.*?-->", "", description or "", flags=re.DOTALL).strip()
    matches = list(_HEADING.finditer(clean))
    sections: dict[str, str] = {}
    if matches:
        preamble = clean[:matches[0].start()].strip()
        if preamble:
            sections["idea"] = preamble
    else:
        sections["idea"] = clean
        return sections
    for index, match in enumerate(matches):
        label = _SECTION_ALIASES.get(match.group(1).strip().casefold())
        if not label:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        value = clean[match.end():end].strip()
        if value:
            sections[label] = value
    return sections


@dataclass(frozen=True)
class Candidate:
    idea_id: str
    title: str
    author: str
    novelty: str
    fields: dict[str, str]
    raw: dict

    @property
    def missing(self) -> list[str]:
        required = ("idea", "domain", "capability", "why_fail", "levers", "verification")
        return [name for name in required if not self.fields.get(name, "").strip()]

    @property
    def screen_ready(self) -> bool:
        return bool(self.author) and not self.missing

    def as_dict(self) -> dict:
        return {
            "ideaId": self.idea_id,
            "title": self.title,
            "author": self.author,
            "predictedNovelty": self.novelty or "UNAVAILABLE",
            "noveltyEligible": self.novelty in {"MEDIUM", "HIGH"},
            "screenReady": self.screen_ready,
            "missing": self.missing,
            "url": IDEA_URL.format(id=self.idea_id),
        }


def candidate_from_idea(row: dict) -> Candidate:
    creator = row.get("creator") or row.get("author") or {}
    author = str(creator.get("username") or "") if isinstance(creator, dict) else ""
    return Candidate(
        idea_id=str(row.get("id") or ""),
        title=str(row.get("title") or "").strip(),
        author=author,
        novelty=str(row.get("noveltyLevel") or "").upper(),
        fields=parse_idea_description(str(row.get("description") or "")),
        raw=row,
    )


def _fetch_ideas(run: _Run, limit: int, idea_ids: list[str]) -> list[dict]:
    if idea_ids:
        rows = []
        for idea_id in idea_ids:
            document = _call_json(
                run,
                ["meta", "ideation.idea", "get", f"--id={idea_id}", "--output=json"],
                f"Idea Exchange idea {idea_id}",
            )
            rows.append(_entity(document, "idea"))
        return rows
    document = _call_json(
        run,
        ["meta", "ideation.idea", "search", "--track=tbench", "--status=up_for_grabs",
         f"--limit={limit}", "--output=json"],
        "Idea Exchange search",
    )
    return _rows(document, "ideas")


def _card_description(candidate: Candidate) -> str:
    f = candidate.fields
    return "\n".join([
        "Human-originated T-Bench seed imported from Idea Exchange.",
        "This card is not a claim that the idea is hard or calibrated.",
        "",
        f"Idea ID: {candidate.idea_id}",
        f"Idea author: @{candidate.author}",
        f"Source: {IDEA_URL.format(id=candidate.idea_id)}",
        f"Predicted novelty: {candidate.novelty or 'UNAVAILABLE'} (not measured difficulty)",
        "",
        "Idea",
        f.get("idea", ""),
        "",
        f"Domain: {f.get('domain', '')}",
        f"Capability: {f.get('capability', '')}",
        "",
        "Why models may struggle",
        f.get("why_fail", ""),
        "",
        "Conceptual difficulty levers",
        f.get("levers", ""),
        "",
        "Verification intent",
        f.get("verification", ""),
        "",
        "Benchsmith intake",
        "Hardness screen: NOT RUN",
        "Hard core 1: NOT RECORDED",
        "Hard core 2: NOT RECORDED",
        "Calibration: NOT MEASURED",
    ]).rstrip()


def _existing_card(run: _Run, idea_id: str, board_rows: list[dict]) -> dict | None:
    prefix = CARD_PREFIX.format(id=idea_id)
    matches = [row for row in board_rows if str(row.get("title") or "").startswith(prefix)]
    if len(matches) > 1:
        raise IdeasError(f"Idea {idea_id} already has {len(matches)} cards on this GSD board")
    if matches:
        return matches[0]
    document = _call_json(
        run,
        ["meta", "tasks.task", "list", f"--external-identifier=aai-idea:{idea_id}",
         "--limit=2", "--output=json", "--no-truncate"],
        f"GSD duplicate lookup for idea {idea_id}",
    )
    rows = _rows(document, "tasks")
    if len(rows) > 1:
        raise IdeasError(f"Idea {idea_id} already has {len(rows)} GSD cards; refusing another")
    return rows[0] if rows else None


def harvest(repo: Path, *, project: str = "", owner: str = "", limit: int = 25,
            max_cards: int = 10, idea_ids: list[str] | None = None,
            include_unassessed: bool = False, apply: bool = False,
            run: _Run = _run) -> dict:
    if not 1 <= limit <= 100:
        raise IdeasError("--limit must be between 1 and 100")
    if max_cards < 1:
        raise IdeasError("--max-cards must be positive")
    repo = Path(repo).resolve()
    board = load_board(repo)
    selector = project or str(board.get("projectId") or "")
    card_owner = owner or str(board.get("assignee") or "")
    if not selector:
        raise IdeasError("no GSD board configured; run `benchsmith ideas init --repo . --apply`")

    lock = None
    if apply:
        lock_path = repo / ".benchsmith" / "idea-harvest.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        rows = _fetch_ideas(run, limit, idea_ids or [])
        candidates = [candidate_from_idea(row) for row in rows]
        board_doc = _call_json(
            run,
            ["meta", "tasks.gsd.task", "list", f"--project-id={selector}",
             "--limit=500", "--output=json", "--no-truncate"],
            "GSD board card list",
        )
        board_rows = _rows(board_doc, "tasks")
        planned, skipped, existing = [], [], []
        seen_titles: set[str] = set()
        for candidate in candidates:
            if candidate.raw.get("track") != "tbench" or candidate.raw.get("status") != "up_for_grabs":
                skipped.append({**candidate.as_dict(), "reason": "not an unclaimed T-Bench idea"})
                continue
            if not candidate.screen_ready:
                skipped.append({**candidate.as_dict(), "reason": "missing human intake fields"})
                continue
            if not include_unassessed and candidate.novelty not in {"MEDIUM", "HIGH"}:
                skipped.append({**candidate.as_dict(),
                                "reason": "predicted novelty is not MEDIUM/HIGH; use --include-unassessed to include it"})
                continue
            title_key = candidate.title.casefold()
            if title_key in seen_titles:
                skipped.append({**candidate.as_dict(), "reason": "duplicate title in this harvest"})
                continue
            seen_titles.add(title_key)
            if len(planned) >= max_cards:
                skipped.append({**candidate.as_dict(), "reason": "max-cards limit reached"})
                continue
            duplicate = _existing_card(run, candidate.idea_id, board_rows)
            if duplicate:
                existing.append({**candidate.as_dict(), "task": duplicate.get("number") or duplicate.get("id")})
                continue
            item = {**candidate.as_dict(), "action": "create_gsd_card"}
            if apply:
                argv = [
                    "meta", "tasks.task", "create",
                    f"--title={CARD_PREFIX.format(id=candidate.idea_id)}{candidate.title}",
                    f"--description={_card_description(candidate)}",
                    f"--project={selector}",
                    "--project-section-name=Needs hardness screen",
                    f"--external-identifier=aai-idea:{candidate.idea_id}",
                    "--priority=LOW", "--output=json",
                ]
                if card_owner:
                    argv.append(f"--owner={card_owner}")
                created = _entity(_call_json(run, argv, f"create GSD card for idea {candidate.idea_id}"), "task")
                item["task"] = created.get("number") or created.get("id")
                board_rows.append(created)
            planned.append(item)
        return {
            "applied": apply,
            "project": selector,
            "source": "Idea Exchange (human-originated seeds only)",
            "created" if apply else "planned": planned,
            "existing": existing,
            "skipped": skipped,
            "next": "run task-hardness-screen on each card; only GO cards should be marked ready to scaffold",
        }
    finally:
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def mark(repo: Path, task: str, verdict: str, *, evidence: str, core_one: str = "",
         core_two: str = "", project: str = "", apply: bool = False,
         run: _Run = _run) -> dict:
    verdict = verdict.upper()
    sections = {
        "GO": "Ready to scaffold",
        "DERISK": "DERISK probes",
        "KILL": "Rejected",
    }
    if verdict not in sections:
        raise IdeasError("--verdict must be GO, DERISK, or KILL")
    if not evidence.strip():
        raise IdeasError("--evidence is required")
    if verdict == "GO" and (not core_one.strip() or not core_two.strip()):
        raise IdeasError("GO requires --core-one and --core-two")
    board = load_board(Path(repo))
    selector = project or str(board.get("projectId") or "")
    if not selector:
        raise IdeasError("no GSD board configured; run `benchsmith ideas init --repo . --apply`")
    receipt = ["", "Benchsmith hardness screen", f"Verdict: {verdict}", f"Evidence: {evidence.strip()}"]
    if core_one:
        receipt.append(f"Hard core 1: {core_one.strip()}")
    if core_two:
        receipt.append(f"Hard core 2: {core_two.strip()}")
    receipt_text = "\n".join(receipt)
    if not apply:
        return {"applied": False, "task": task, "verdict": verdict,
                "project": selector, "moveTo": sections[verdict], "receipt": receipt[1:]}
    _call_json(
        run,
        ["meta", "tasks.task", "update", f"--task={task}",
         f"--append-description={receipt_text}", "--output=json"],
        f"append hardness screen to {task}",
    )
    document = _call_json(
        run,
        ["meta", "tasks.gsd.task", "move", f"--task={task}", f"--project={selector}",
         f"--to-section={sections[verdict]}", "--output=json"],
        f"move GSD card {task}",
    )
    return {"applied": True, "task": task, "verdict": verdict,
            "section": sections[verdict], "result": _entity(document, "task")}


def inspect_reference(task_ids: list[str], *, binary: str = "/usr/local/bin/codimango",
                      run: _Run = _run) -> dict:
    rows = []
    for task_id in task_ids:
        task = _call_json(run, [binary, "task", "show", task_id, "--json"],
                          f"Codimango task {task_id}")
        measured = ((task.get("qualitativeResult") or {}).get("difficulty") or {})
        rate = measured.get("passRate")
        failures = []
        checks = {
            "terminal": task.get("status") in {"accepted", "used_in_training"},
            "exactHead": bool(task.get("headCommitSha"))
            and task.get("headCommitSha") == task.get("validationCommitSha"),
            "validation": task.get("validationStatus") == "passing",
            "oracle": task.get("oracleStatus") == "validated",
            "tbr": task.get("tbdReviewStatus") == "pass",
            "declaredHard": task.get("difficulty") == "hard",
            "measuredInBenchsmithBand": isinstance(rate, (int, float)) and 0.20 <= rate <= 0.50,
            "contaminationLow": ((task.get("qualitativeResult") or {}).get("contaminationV2") or {}).get("level") == "LOW",
            "provenanceClean": ((task.get("qualitativeResult") or {}).get("provenanceCheck") or {}).get("verdict") == "CLEAN",
        }
        failures.extend(name for name, ok in checks.items() if not ok)
        rows.append({
            "id": str(task.get("id") or task_id),
            "name": task.get("name"),
            "declaredDifficulty": task.get("difficulty"),
            "measuredClassification": measured.get("classification") or "UNAVAILABLE",
            "measuredPassRate": rate,
            "checks": checks,
            "eligibleForDeepAudit": not failures,
            "failedChecks": failures,
            "hardCalibrated": False,
            "note": "Summary fields can only select a deep-audit candidate; the Benchsmith §5 bar, semantic attribution, and critic are still required.",
        })
    return {"references": rows}


_LANDSCAPE_TOKEN = re.compile(r"[a-z0-9]+")
_LANDSCAPE_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
    "build", "create", "implement", "task", "tool",
}


def _landscape_tokens(*values: object) -> set[str]:
    words: set[str] = set()
    for value in values:
        if isinstance(value, list):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value or "")
        words.update(
            token for token in _LANDSCAPE_TOKEN.findall(text.casefold())
            if len(token) > 2 and token not in _LANDSCAPE_STOPWORDS
        )
    return words


def _lexical_neighbor(seed_tokens: set[str], task: dict) -> dict:
    task_tokens = _landscape_tokens(task.get("name"), task.get("tags") or [])
    overlap = sorted(seed_tokens & task_tokens)
    union = seed_tokens | task_tokens
    return {
        "id": str(task.get("id") or ""),
        "name": task.get("name"),
        "status": task.get("status"),
        "githubUrl": task.get("githubUrl"),
        "overlap": overlap,
        "score": round(len(overlap) / len(union), 3) if union else 0.0,
    }


def _coverage_rows(document: dict, key: str) -> list[dict]:
    rows = document.get(key) or []
    return [row for row in rows if isinstance(row, dict)]


def _under_target(document: dict, key: str, limit: int = 15) -> list[dict]:
    return sorted(
        (row for row in _coverage_rows(document, key)
         if row.get("rag") in {"red", "yellow"}),
        key=lambda row: (
            float(row.get("gap_pct") or 0),
            float(row.get("current_pct") or 0),
            -float(row.get("target_pct") or 0),
            str(row.get("label") or ""),
        ),
    )[:limit]


def _task_list_for_tag(run: _Run, binary: str, tag: str, limit: int) -> tuple[list[dict], dict]:
    document = _call_json(
        run,
        [binary, "task", "list", "--tag", tag, "--track", "tbench", "--compact",
         "--limit", str(limit), "--json"],
        f"Codimango T-Bench tag {tag}",
    )
    return _rows(document, "tasks"), document if isinstance(document, dict) else {}


def landscape(*, tags: list[str] | None = None, seed: str = "",
              task_ids: list[str] | None = None, limit: int = 20,
              idea_limit: int = 10, binary: str = "/usr/local/bin/codimango",
              run: _Run = _run) -> dict:
    """Read-only coverage and collision radar for a human-authored seed."""
    if not 1 <= limit <= 20:
        raise IdeasError("--limit must be between 1 and 20; larger live pages are unreliable")
    if not 1 <= idea_limit <= 100:
        raise IdeasError("--idea-limit must be between 1 and 100")

    requested_tags = []
    for tag in tags or ["ripen-v1"]:
        clean = tag.strip().casefold()
        if clean and clean not in requested_tags:
            requested_tags.append(clean)

    taxonomy = _call_json(
        run,
        [binary, "taxonomy", "--hackathon", "t-bench", "--axis", "sub-domains", "--json"],
        "Codimango T-Bench taxonomy",
    )
    subdomains = _coverage_rows(taxonomy, "sub_domains")
    saturated = sorted(
        (row for row in subdomains if float(row.get("gap_pct") or 0) > 0),
        key=lambda row: (-float(row.get("gap_pct") or 0), str(row.get("label") or "")),
    )

    samples = []
    corpus: dict[str, dict] = {}
    for tag in requested_tags:
        tasks, document = _task_list_for_tag(run, binary, tag, limit)
        for task in tasks:
            corpus[str(task.get("id") or task.get("name"))] = task
        incomplete = bool(document.get("hasMore")) or document.get("totalExact") is False
        samples.append({
            "tag": tag,
            "observed": len(tasks),
            "count": f">={len(tasks)}" if incomplete else len(tasks),
            "complete": not incomplete,
            "tasks": [
                {
                    "id": str(task.get("id") or ""),
                    "name": task.get("name"),
                    "status": task.get("status"),
                    "tags": task.get("tags") or [],
                    "subdomains": sorted({
                        str(item.get("subDomain")) for item in (task.get("domainContext") or [])
                        if isinstance(item, dict) and item.get("subDomain")
                    }),
                    "useCases": sorted({
                        str(item.get("specificTask")) for item in (task.get("useCases") or [])
                        if isinstance(item, dict) and item.get("specificTask")
                    }),
                    "githubUrl": task.get("githubUrl"),
                }
                for task in tasks
            ],
        })

    exact_references = []
    for task_id in task_ids or []:
        task = _call_json(
            run, [binary, "task", "show", str(task_id), "--json"],
            f"Codimango task {task_id}",
        )
        corpus[str(task.get("id") or task_id)] = task
        exact_references.append({
            "id": str(task.get("id") or task_id),
            "name": task.get("name"),
            "status": task.get("status"),
            "tags": task.get("tags") or [],
            "classification": task.get("classification"),
            "githubUrl": task.get("githubUrl"),
        })

    tag_counts = Counter(
        str(tag).casefold() for task in corpus.values() for tag in (task.get("tags") or [])
    )
    result = {
        "policy": {
            "seedOrigin": "human-required",
            "use": "coverage map and duplicate warning, not automatic task generation",
            "taskContentRead": False,
        },
        "coverage": {
            "asOf": taxonomy.get("last_synced_at"),
            "totalTasks": taxonomy.get("total_tasks"),
            "underTargetSubdomains": _under_target(taxonomy, "sub_domains"),
            "underTargetUseCases": _under_target(taxonomy, "use_cases"),
            "underTargetLanguages": _under_target(taxonomy, "languages"),
            "underTargetDomains": _under_target(taxonomy, "domains"),
            "overTargetSubdomains": saturated[:15],
        },
        "samples": samples,
        "exactReferences": exact_references,
        "repeatedTags": [
            {"tag": tag, "observed": count}
            for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:25]
        ],
        "limitations": [
            "Coverage is global, but tag samples may be truncated; incomplete samples are lower bounds.",
            "Only task metadata is read. Similarity here is lexical and is not a novelty verdict.",
            "A human must supply the task seed before hardness screening or scaffolding.",
        ],
    }

    if not seed.strip():
        result["seedCheck"] = {"state": "NOT_RUN", "reason": "no human seed supplied"}
        return result

    seed_tokens = _landscape_tokens(seed)
    neighbors = sorted(
        (_lexical_neighbor(seed_tokens, task) for task in corpus.values()),
        key=lambda row: (-row["score"], -len(row["overlap"]), str(row["name"] or "")),
    )
    idea_document = _call_json(
        run,
        ["meta", "ideation.idea", "search", "--track=tbench", f"--query={seed.strip()}",
         f"--limit={idea_limit}", "--output=json"],
        "Idea Exchange seed search",
    )
    idea_neighbors = []
    for row in _rows(idea_document, "ideas"):
        tokens = _landscape_tokens(row.get("title"), row.get("description"))
        overlap = sorted(seed_tokens & tokens)
        union = seed_tokens | tokens
        idea_neighbors.append({
            "id": str(row.get("id") or ""),
            "title": row.get("title"),
            "status": row.get("status"),
            "overlap": overlap,
            "score": round(len(overlap) / len(union), 3) if union else 0.0,
            "url": IDEA_URL.format(id=row.get("id")),
        })
    idea_neighbors.sort(key=lambda row: (-row["score"], -len(row["overlap"]), str(row["title"] or "")))
    strongest = max(
        [row["score"] for row in neighbors[:10]] + [row["score"] for row in idea_neighbors[:10]] + [0.0]
    )
    result["seedCheck"] = {
        "state": "REVIEW_REQUIRED" if strongest >= 0.25 else "NO_LEXICAL_COLLISION_IN_SAMPLE",
        "seed": seed.strip(),
        "tokens": sorted(seed_tokens),
        "nearestTasks": neighbors[:10],
        "nearestIdeas": idea_neighbors[:10],
        "strongestScore": strongest,
        "requiresHumanReview": True,
        "next": "If the seed is genuinely yours and materially distinct, run task-hardness-screen before creating a card.",
    }
    return result
