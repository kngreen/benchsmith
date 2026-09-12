"""Resolve task controls without trusting the task's declaration as policy."""

from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
CONTROL_VERSION = "1"
MANIFEST = Path(".benchsmith/controls.json")
ROLLOUT_EPOCH = 1789257600

POLICY_REQUIRED = frozenset({"mutation_adequacy", "critic_receipt"})
SURFACE_MARKERS = (
    "instruction.md",
    "task.toml",
    "tests/",
    "solution/",
    "reference/",
    "environment/",
    "dockerfile",
    "grader",
    "verifier",
    "run-tests",
    str(MANIFEST),
)

TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".toml", ".py", ".go", ".swift", ".sh"}
)
SEMANTIC_DETECTORS = {
    "authorization": re.compile(
        r"\b(authenticated|unauthenticated|authori[sz](?:e|ed|ation)|permission|"
        r"ownership|access control)\b",
        re.I,
    ),
    "temporal_behavior": re.compile(
        r"\b(async(?:hronous)?|retry|replay|lifecycle|first render|before|after|stale|"
        r"eventual(?:ly| consistency))\b",
        re.I,
    ),
    "multi_entity_state": re.compile(
        r"\b(parent|child|tenant|cross[- ](?:user|entity|account)|dangling|cascade|"
        r"referential integrity|isolation)\b",
        re.I,
    ),
}


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def cache_identity(task_tree: str, resolved: dict, artifacts: dict) -> dict:
    """Identity for tree-invariant evidence; commit-relative evidence is excluded."""
    adapters = sorted(
        f"{(item.get('adapter') or {}).get('name')}@{(item.get('adapter') or {}).get('version')}:"
        f"{(item.get('adapter') or {}).get('digest')}"
        for item in resolved.get("obligations") or []
    )
    implementation_bytes = b"".join(
        path.read_bytes()
        for path in (
            Path(__file__),
            Path(__file__).with_name("gate.py"),
            Path(__file__).with_name("mutate.py"),
        )
    )
    implementation = hashlib.sha256(implementation_bytes).hexdigest()
    metamorphic = artifacts.get("metamorphicVariation") or {}
    closure = resolved.get("verifier_closure") or {}
    identity = {
        "evidence_class": "tree-invariant",
        "task_tree": task_tree,
        "manifest_schema": resolved.get("schema_version"),
        "manifest_digest": resolved.get("digest"),
        "control_version": CONTROL_VERSION,
        "control_implementation": implementation,
        "adapters": adapters,
        "runtime": resolved.get("runtime") or {},
        "verifier_closure_digest": _digest(closure),
        "metamorphic_seed": metamorphic.get("seed"),
        "metamorphic_fixture_digest": metamorphic.get("fixture_digest"),
    }
    identity["digest"] = _digest(identity)
    return identity


def _inside(task_dir: Path, relative: str) -> Path | None:
    candidate = (task_dir / relative).resolve()
    try:
        candidate.relative_to(task_dir.resolve())
    except ValueError:
        return None
    return candidate


def _texts(task_dir: Path):
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            yield path.relative_to(task_dir).as_posix(), path.read_text(
                errors="replace"
            )
        except OSError:
            continue


def detect(task_dir: Path) -> dict[str, list[str]]:
    """Detect capabilities from task bytes; absence is never evidence of absence."""
    task_dir = Path(task_dir)
    found: dict[str, list[str]] = {}
    native = [
        p.relative_to(task_dir).as_posix()
        for p in task_dir.rglob("*")
        if p.is_file()
        and (
            p.name == "vm.conf"
            or any(part.endswith((".xcodeproj", ".xcworkspace")) for part in p.parts)
        )
    ]
    if native:
        found["native_target"] = native[:8]
    for relative, text in _texts(task_dir):
        for name, pattern in SEMANTIC_DETECTORS.items():
            if pattern.search(text):
                found.setdefault(name, []).append(relative)
        if re.search(
            r"(?is)(candidate|solution|solve\.sh).{0,160}"
            r"(subprocess|exec\s*\(|system\s*\(|bash\b|sh\b|terraform\b|xcodebuild\b)",
            text,
        ):
            found.setdefault("candidate_execution", []).append(relative)
    return {name: sorted(set(paths))[:8] for name, paths in sorted(found.items())}


def policy_mode(repo_root: Path, task_name: str, task_dir: Path) -> tuple[str, str]:
    """Enforce new or control-bearing tasks; observe untouched legacy tasks."""
    if (task_dir / MANIFEST).is_file():
        return "enforce", "the task has a controls manifest"
    if os.environ.get("BENCHSMITH_CONTROLS_ENFORCE") == "1":
        return "enforce", "control enforcement was explicitly enabled"
    try:
        from .diffcheck import changeset

        cs = changeset(Path(repo_root))
    except (OSError, subprocess.SubprocessError):
        return "shadow", "the change set is unavailable"
    prefix = f"{task_name}/"
    touched = []
    for path in cs.paths:
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        lowered = relative.lower()
        if any(marker in lowered for marker in SURFACE_MARKERS):
            touched.append(relative)
    if not touched:
        return "shadow", "legacy task with no relevant surface change"
    if cs.source == "index":
        return "enforce", "a scored, contract, verifier, or runtime surface is staged"
    try:
        committed = subprocess.run(
            ["git", "-C", str(repo_root), "show", "-s", "--format=%ct", cs.new],
            capture_output=True,
            text=True,
            timeout=30,
        )
        timestamp = int(committed.stdout.strip()) if committed.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        timestamp = 0
    if timestamp >= ROLLOUT_EPOCH:
        return (
            "enforce",
            "a post-rollout scored, contract, verifier, or runtime surface changed",
        )
    return "shadow", "the relevant change predates the controls rollout"


def _witness(task_dir: Path, value, label: str, errors: list[str]) -> dict | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    relative = str(value.get("path") or "")
    control = str(value.get("control") or "")
    command = value.get("command")
    path = _inside(task_dir, relative) if relative else None
    if path is None or not path.is_file():
        errors.append(f"{label}.path does not name a task file: {relative!r}")
    if not control:
        errors.append(f"{label}.control is required")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(v, str) for v in command)
    ):
        errors.append(f"{label}.command must be a non-empty string list")
        command = []
    elif not any("{path}" in value for value in command):
        errors.append(f"{label}.command must consume {{path}}")
    return {"path": relative, "control": control, "command": command}


def _declared_capabilities(doc: dict, errors: list[str]) -> set[str]:
    raw = doc.get("capabilities")
    if not isinstance(raw, dict):
        errors.append("capabilities must be an object of capability -> boolean")
        return set()
    bad = sorted(name for name, value in raw.items() if not isinstance(value, bool))
    if bad:
        errors.append("capabilities must be boolean: " + ", ".join(bad))
    return {str(name) for name, value in raw.items() if value is True}


def resolve(repo_root: Path, task_name: str, task_dir: Path) -> dict:
    """Validate the declaration and compute the trusted effective manifest."""
    repo_root, task_dir = Path(repo_root), Path(task_dir)
    mode, reason = policy_mode(repo_root, task_name, task_dir)
    path = task_dir / MANIFEST
    detected = detect(task_dir)
    if not path.is_file():
        return {
            "ok": mode == "shadow",
            "mode": mode,
            "reason": reason,
            "declared": [],
            "detected": detected,
            "required": sorted(POLICY_REQUIRED | set(detected)),
            "effective": sorted(POLICY_REQUIRED | set(detected)),
            "errors": [] if mode == "shadow" else [f"missing {MANIFEST}"],
            "examined": 0,
        }
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return {
            "ok": False,
            "mode": mode,
            "reason": reason,
            "declared": [],
            "detected": detected,
            "required": sorted(POLICY_REQUIRED | set(detected)),
            "effective": sorted(POLICY_REQUIRED | set(detected)),
            "errors": [f"unreadable manifest: {error}"],
            "examined": 0,
        }

    errors: list[str] = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    declared = _declared_capabilities(doc, errors)
    required = POLICY_REQUIRED | set(detected)
    missing = sorted(required - declared)
    if missing:
        errors.append("under-declared capabilities: " + ", ".join(missing))

    obligations = doc.get("obligations")
    if not isinstance(obligations, list) or not obligations:
        errors.append("obligations must be a non-empty list")
        obligations = []
    ids: set[str] = set()
    normalized = []
    for index, obligation in enumerate(obligations):
        label = f"obligations[{index}]"
        if not isinstance(obligation, dict):
            errors.append(f"{label} must be an object")
            continue
        oid = str(obligation.get("id") or "")
        if not oid or oid in ids:
            errors.append(f"{label}.id is missing or duplicated")
        ids.add(oid)
        for field in ("contract_reference", "observation_boundary", "control_version"):
            if not str(obligation.get(field) or "").strip():
                errors.append(f"{label}.{field} is required")
        adapter = obligation.get("adapter")
        if (
            not isinstance(adapter, dict)
            or not adapter.get("name")
            or not adapter.get("version")
            or not re.fullmatch(r"[0-9a-f]{64}", str(adapter.get("digest") or ""))
        ):
            errors.append(f"{label}.adapter requires name, version, and SHA-256 digest")
        if str(obligation.get("control_version") or "") != CONTROL_VERSION:
            errors.append(f"{label}.control_version must be {CONTROL_VERSION}")
        applicability = obligation.get("applicability")
        if (
            not isinstance(applicability, dict)
            or applicability.get("state") not in {"applicable", "not_applicable"}
            or not applicability.get("evidence")
        ):
            errors.append(f"{label}.applicability requires state and evidence")
        accepting = _witness(
            task_dir,
            obligation.get("accepting_witness"),
            f"{label}.accepting_witness",
            errors,
        )
        rejecting = obligation.get("rejecting_witness")
        non_applicable = obligation.get("non_applicability")
        rejected = None
        non_applicable_proof = None
        if rejecting is not None:
            rejected = _witness(
                task_dir, rejecting, f"{label}.rejecting_witness", errors
            )
        elif not isinstance(non_applicable, dict) or not non_applicable.get("reason"):
            errors.append(f"{label} requires rejecting_witness or non_applicability")
        else:
            non_applicable_proof = _witness(
                task_dir,
                non_applicable.get("proof"),
                f"{label}.non_applicability.proof",
                errors,
            )
        freedoms = obligation.get("allowed_freedoms") or []
        if not isinstance(freedoms, list):
            errors.append(f"{label}.allowed_freedoms must be a list")
            freedoms = []
        normalized_freedoms = []
        for f_index, freedom in enumerate(freedoms):
            if not isinstance(freedom, dict) or not freedom.get("description"):
                errors.append(
                    f"{label}.allowed_freedoms[{f_index}].description is required"
                )
                continue
            witness = _witness(
                task_dir,
                freedom.get("witness"),
                f"{label}.allowed_freedoms[{f_index}].witness",
                errors,
            )
            normalized_freedoms.append(
                {"description": freedom.get("description"), "witness": witness}
            )
        normalized.append(
            {
                "id": oid,
                "contract_reference": obligation.get("contract_reference"),
                "observation_boundary": obligation.get("observation_boundary"),
                "accepting_witness": accepting,
                "rejecting_witness": rejected,
                "non_applicability": (
                    {
                        "reason": non_applicable.get("reason"),
                        "proof": non_applicable_proof,
                    }
                    if isinstance(non_applicable, dict)
                    else None
                ),
                "adapter": adapter,
                "control_version": obligation.get("control_version"),
                "applicability": applicability,
                "allowed_freedoms": normalized_freedoms,
            }
        )

    mutation = doc.get("mutation")
    if "mutation_adequacy" in declared:
        if not isinstance(mutation, dict):
            errors.append("mutation_adequacy requires mutation configuration")
        else:
            if not mutation.get("test_command"):
                errors.append("mutation.test_command must be non-empty")
            cases = mutation.get("cases")
            if not isinstance(cases, list) or not cases:
                errors.append("mutation.cases must be non-empty")
                cases = []
            covered = set()
            for case_index, case in enumerate(cases):
                case_label = f"mutation.cases[{case_index}]"
                if not isinstance(case, dict):
                    errors.append(f"{case_label} must be an object")
                    continue
                obligation = str(case.get("obligation") or "")
                if obligation not in ids:
                    errors.append(f"{case_label}.obligation is unknown: {obligation!r}")
                covered.add(obligation)
                if not case.get("targets"):
                    errors.append(f"{case_label}.targets must be non-empty")
                if not case.get("operators"):
                    errors.append(f"{case_label}.operators must be non-empty")
            missing_cases = sorted(ids - covered)
            if missing_cases:
                errors.append(
                    "mutation cases missing obligations: " + ", ".join(missing_cases)
                )

    closure = doc.get("verifier_closure")
    if "candidate_execution" in declared:
        if not isinstance(closure, dict):
            errors.append("candidate_execution requires verifier_closure configuration")
        else:
            runtime = closure.get("runtime")
            if not isinstance(runtime, dict) or not (
                runtime.get("image_digest") or runtime.get("toolchain_digest")
            ):
                errors.append(
                    "verifier_closure.runtime requires an image or toolchain digest"
                )
            for field in (
                "executables",
                "reads",
                "writes",
                "dependencies",
                "result_channels",
            ):
                if not isinstance(closure.get(field), list):
                    errors.append(f"verifier_closure.{field} must be a list")
            if not closure.get("candidate_roots"):
                errors.append("verifier_closure.candidate_roots must be non-empty")
            for field in ("valid_run", "tamper_probe"):
                probe = closure.get(field)
                if not isinstance(probe, dict) or not probe.get("command"):
                    errors.append(f"verifier_closure.{field}.command is required")

    metamorphic = doc.get("metamorphic")
    if "metamorphic_variation" in declared:
        if not isinstance(metamorphic, dict):
            errors.append("metamorphic_variation requires metamorphic configuration")
        else:
            command = [str(value) for value in metamorphic.get("command") or []]
            if not metamorphic.get("source"):
                errors.append("metamorphic.source is required")
            if not command or not any("{fixture}" in value for value in command):
                errors.append("metamorphic.command must consume {fixture}")
            if not metamorphic.get("transforms"):
                errors.append("metamorphic.transforms must be non-empty")

    effective = {
        "schema_version": SCHEMA_VERSION,
        "control_version": CONTROL_VERSION,
        "mode": mode,
        "declared": sorted(declared),
        "detected": detected,
        "required": sorted(required),
        "effective": sorted(declared | required),
        "obligations": normalized,
        "runtime": doc.get("runtime") or {},
        "verifier_closure": closure or {},
        "metamorphic": metamorphic or {},
        "mutation": mutation or {},
    }
    effective["digest"] = _digest(effective)
    return {
        "ok": not errors,
        "mode": mode,
        "reason": reason,
        **effective,
        "errors": errors,
        "examined": len(normalized),
    }


def mutation_adequacy(task_dir: Path, resolved: dict, *, runner=None) -> dict:
    """Run the adapter-selected mutation pack; empty and unsupported stay blocking."""
    from . import mutate

    config = resolved.get("mutation") or {}
    command = [str(value) for value in config.get("test_command") or []]
    results = []
    for case in config.get("cases") or []:
        result = mutate.probe(
            Path(task_dir),
            [str(value) for value in case.get("targets") or []],
            command,
            operators=[str(value) for value in case.get("operators") or []],
            runner=runner,
        )
        results.append({"obligation": case.get("obligation"), **result})
    examined = sum(int(result.get("viable") or 0) for result in results)
    failed = [
        str(result.get("obligation") or "<unknown>")
        for result in results
        if result.get("status") != "PASS" or not int(result.get("viable") or 0)
    ]
    return {
        "ok": bool(results) and not failed and examined > 0,
        "state": "FAIL" if failed else ("PASS" if examined else "NOT_RUN"),
        "examined": examined,
        "detail": (
            "inadequate obligations: " + ", ".join(failed)
            if failed
            else (
                f"all {examined} viable obligation-selected mutant(s) were caught"
                if examined
                else "zero obligation-selected mutants examined"
            )
        ),
        "results": results,
    }


def obligation_witnesses(task_dir: Path, resolved: dict, *, runner=None) -> dict:
    """Execute every manifest witness at its declared observation boundary."""
    task_dir = Path(task_dir)

    def execute(argv, cwd):
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=900
        )

    call = runner or execute
    outcomes = []

    def run_one(
        obligation: str, kind: str, witness: dict | None, expect_zero: bool
    ) -> None:
        if not witness:
            return
        target = _inside(task_dir, str(witness.get("path") or ""))
        command = [
            part.replace("{path}", str(target)) for part in witness.get("command") or []
        ]
        try:
            result = call(command, str(task_dir))
            code = result[0] if isinstance(result, tuple) else result.returncode
            passed = code == 0 if expect_zero else code != 0
            outcomes.append(
                {
                    "obligation": obligation,
                    "kind": kind,
                    "returncode": code,
                    "passed": passed,
                }
            )
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                {
                    "obligation": obligation,
                    "kind": kind,
                    "returncode": None,
                    "passed": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    for obligation in resolved.get("obligations") or []:
        oid = str(obligation.get("id") or "")
        run_one(oid, "accepting", obligation.get("accepting_witness"), True)
        run_one(oid, "rejecting", obligation.get("rejecting_witness"), False)
        run_one(
            oid,
            "non-applicability",
            (obligation.get("non_applicability") or {}).get("proof"),
            True,
        )
        for freedom in obligation.get("allowed_freedoms") or []:
            run_one(oid, "allowed-freedom", freedom.get("witness"), True)
    failed = [
        f"{item['obligation']}:{item['kind']}"
        for item in outcomes
        if not item["passed"]
    ]
    return {
        "state": "FAIL" if failed or not outcomes else "PASS",
        "ok": bool(outcomes) and not failed,
        "examined": len(outcomes),
        "detail": (
            "failed witnesses: " + ", ".join(failed)
            if failed
            else (
                f"{len(outcomes)} witness(es) fired"
                if outcomes
                else "zero witnesses fired"
            )
        ),
        "outcomes": outcomes,
    }


_TRACE_PATH = re.compile(r"\b(execve|openat)\([^\"]*\"([^\"]+)\"([^\n]*)")


def _trace(command: list[str], task_dir: Path) -> tuple[int, dict[str, set[str]], str]:
    tracer = shutil.which("strace")
    if not tracer:
        raise RuntimeError("strace is unavailable; verifier closure is unmeasured")
    with tempfile.NamedTemporaryFile() as output:
        result = subprocess.run(
            [
                tracer,
                "-f",
                "-qq",
                "-e",
                "trace=execve,openat",
                "-o",
                output.name,
                *command,
            ],
            cwd=str(task_dir),
            capture_output=True,
            text=True,
            timeout=900,
        )
        trace = Path(output.name).read_text(errors="replace")
    observed = {"executables": set(), "reads": set(), "writes": set()}
    for call, raw_path, tail in _TRACE_PATH.findall(trace):
        path = Path(raw_path)
        if not path.is_absolute():
            path = (task_dir / path).resolve()
        else:
            path = path.resolve()
        value = str(path)
        if call == "execve":
            observed["executables"].add(value)
        elif any(flag in tail for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC")):
            observed["writes"].add(value)
        else:
            observed["reads"].add(value)
    return result.returncode, observed, (result.stdout + result.stderr)[-1000:]


def _within_roots(path: str, roots: list[Path]) -> bool:
    candidate = Path(path)
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _declared_path(path: str, task_dir: Path) -> str:
    candidate = Path(path)
    return str(
        candidate if candidate.is_absolute() else (task_dir / candidate).resolve()
    )


def verifier_closure(task_dir: Path, resolved: dict, *, runner=None) -> dict:
    """Demonstrate one valid result and one rejected tampering attempt."""
    config = resolved.get("verifier_closure") or {}
    if not config:
        return {
            "state": "NOT_RUN",
            "ok": False,
            "examined": 0,
            "detail": "no verifier closure configured",
        }

    task_dir = Path(task_dir).resolve()
    roots = [
        _declared_path(str(value), task_dir)
        for value in config.get("candidate_roots") or []
    ]
    root_paths = [Path(value) for value in roots]
    allowed = {
        "executables": [
            _declared_path(str(value), task_dir)
            for value in config.get("executables") or []
        ],
        "reads": [
            _declared_path(str(value), task_dir)
            for value in (config.get("reads") or [])
            + (config.get("dependencies") or [])
        ],
        "writes": [
            _declared_path(str(value), task_dir)
            for value in (config.get("writes") or [])
            + (config.get("result_channels") or [])
        ],
    }
    outcomes = []
    observed_all = {"executables": set(), "reads": set(), "writes": set()}
    for name, expected_zero in (("valid_run", True), ("tamper_probe", False)):
        command = [
            str(value) for value in (config.get(name) or {}).get("command") or []
        ]
        if not command:
            return {
                "state": "NOT_RUN",
                "ok": False,
                "examined": len(outcomes),
                "detail": f"{name} has no command",
            }
        try:
            if runner is None:
                code, observed, log = _trace(command, task_dir)
            else:
                result = runner(command, str(task_dir))
                code = result[0] if isinstance(result, tuple) else result.returncode
                observed = getattr(result, "trace", {}) or {}
                log = ""
        except Exception as error:  # noqa: BLE001
            return {
                "state": "NOT_RUN",
                "ok": False,
                "examined": len(outcomes),
                "detail": f"{name} could not run: {type(error).__name__}: {error}",
            }
        passed = code == 0 if expected_zero else code != 0
        outcomes.append(
            {"name": name, "returncode": code, "passed": passed, "log": log}
        )
        for kind in observed_all:
            observed_all[kind].update(
                path
                for path in observed.get(kind, set())
                if _within_roots(path, root_paths)
            )
    failed = [outcome["name"] for outcome in outcomes if not outcome["passed"]]
    unexpected = []
    for kind, paths in observed_all.items():
        for path in sorted(paths):
            if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed[kind]):
                unexpected.append(f"{kind}:{path}")
    if unexpected:
        failed.append("undeclared-closure")
    return {
        "state": "FAIL" if failed else "PASS",
        "ok": not failed,
        "examined": len(outcomes),
        "detail": (
            "unexpected outcome: "
            + ", ".join(failed)
            + (("; " + ", ".join(unexpected[:4])) if unexpected else "")
            if failed
            else "valid run passed and tampering probe failed"
        ),
        "outcomes": outcomes,
        "observed": {kind: sorted(paths) for kind, paths in observed_all.items()},
    }


def _rename_ids(value, mapping: dict[str, str], rng: random.Random):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, str) and (
                key.lower() == "id" or key.lower().endswith("_id")
            ):
                out[key] = mapping.setdefault(item, f"bs-{rng.getrandbits(48):012x}")
            else:
                out[key] = _rename_ids(item, mapping, rng)
        return out
    if isinstance(value, list):
        return [_rename_ids(item, mapping, rng) for item in value]
    return mapping.get(value, value) if isinstance(value, str) else value


def _shuffle(value, rng: random.Random):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffle(item, rng) for key, item in items}
    if isinstance(value, list):
        items = [_shuffle(item, rng) for item in value]
        rng.shuffle(items)
        return items
    return value


def metamorphic_variants(
    source, transforms: list[str], seed: str | None = None
) -> dict:
    """Create reproducible generic JSON variants selected by an adapter manifest."""
    seed = seed or secrets.token_hex(16)
    rng = random.Random(seed)
    variants = []
    for name in transforms:
        if name == "rename_ids":
            value = _rename_ids(source, {}, rng)
        elif name == "ordering":
            value = _shuffle(source, rng)
        elif name == "multiplicity" and isinstance(source, list) and source:
            value = source + [_rename_ids(source[rng.randrange(len(source))], {}, rng)]
        elif name == "irrelevant_entity" and isinstance(source, dict):
            value = {**source, "__benchsmith_irrelevant__": {"id": f"bs-{seed[:8]}"}}
        else:
            continue
        variants.append({"transform": name, "value": value})
    fixture_digest = _digest(variants)
    return {"seed": seed, "variants": variants, "fixture_digest": fixture_digest}


def run_metamorphic(task_dir: Path, resolved: dict, *, runner=None) -> dict:
    """Generate selected variants and prove the configured observation accepts them."""
    config = resolved.get("metamorphic") or {}
    if not config:
        return {
            "state": "NOT_RUN",
            "ok": False,
            "examined": 0,
            "detail": "no metamorphic adapter configured",
        }
    relative = str(config.get("source") or "")
    source_path = _inside(Path(task_dir), relative)
    command = [str(value) for value in config.get("command") or []]
    if source_path is None or not source_path.is_file() or not command:
        return {
            "state": "NOT_RUN",
            "ok": False,
            "examined": 0,
            "detail": "metamorphic source or command is missing",
        }
    try:
        source = json.loads(source_path.read_text())
    except (OSError, ValueError) as error:
        return {
            "state": "FAIL",
            "ok": False,
            "examined": 0,
            "detail": f"metamorphic source is not JSON: {error}",
        }
    generated = metamorphic_variants(
        source, [str(v) for v in config.get("transforms") or []]
    )
    if not generated["variants"]:
        return {
            "state": "NOT_RUN",
            "ok": False,
            "examined": 0,
            "detail": "zero applicable metamorphic transforms",
            **generated,
        }

    def execute(argv, cwd):
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=600
        )

    call = runner or execute
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, variant in enumerate(generated["variants"]):
            fixture = Path(tmp) / f"variant-{index}.json"
            fixture.write_text(json.dumps(variant["value"], sort_keys=True) + "\n")
            argv = [part.replace("{fixture}", str(fixture)) for part in command]
            result = call(argv, str(task_dir))
            code = result[0] if isinstance(result, tuple) else result.returncode
            if code != 0:
                failures.append(variant["transform"])
    return {
        "state": "FAIL" if failures else "PASS",
        "ok": not failures,
        "examined": len(generated["variants"]),
        "detail": (
            "rejected transforms: " + ", ".join(failures)
            if failures
            else f"accepted {len(generated['variants'])} seeded transform(s)"
        ),
        **generated,
    }
