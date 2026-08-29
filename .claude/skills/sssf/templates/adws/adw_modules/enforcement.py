"""Executed static detectors for the frozen factory CLI surface."""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple


class EnforcementViolation(AssertionError):
    pass


@dataclass(frozen=True)
class Obligation:
    check_id: str
    detector: str
    planted_violation: str
    green_control: str


OBLIGATION_LEDGER = (
    Obligation(
        "route-boundary", "detect_source", "violations_a.py:route", "launcher boundary"
    ),
    Obligation(
        "digest-over-audit", "detect_source", "violations_a.py:digest", "plan bytes"
    ),
    Obligation(
        "model-from-audit", "detect_source", "violations_a.py:model", "validated plan"
    ),
    Obligation(
        "stderr-classification",
        "detect_source",
        "violations_b.py:stderr",
        "typed signal",
    ),
    Obligation(
        "status-write-boundary",
        "detect_source",
        "violations_b.py:status",
        "lifecycle ledger",
    ),
    Obligation(
        "digest-import-boundary",
        "detect_source",
        "violations_b.py:json",
        "plan_digest.py",
    ),
    Obligation(
        "base-execution-import",
        "detect_source",
        "violations_b.py:agents",
        "Maestro modules",
    ),
    Obligation(
        "installed-bytes",
        "assert_installed_bytes",
        "wrong runtime root",
        "resolved runtime root",
    ),
    Obligation("verb-existence", "assert_verbs", "one omitted verb", "complete parser"),
)

REQUIRED_VERBS = (
    "run start",
    "run resume",
    "run amend",
    "run status",
)

INSTALLED_FACTORY_FILES = (
    "maestro.py",
    "adw_modules/publication.py",
    "adw_modules/scheduler.py",
    "adw_modules/launcher.py",
)

_BASE_MODULES = {
    "agents.py",
    "agent_pi.py",
    "agent_cc.py",
    "data_types.py",
    "gates.py",
    "git_helper.py",
    "permissions.py",
    "prompts.py",
    "quality.py",
    "runner.py",
    "utils.py",
}


def _names(node: ast.AST) -> Tuple[str, ...]:
    return tuple(child.id for child in ast.walk(node) if isinstance(child, ast.Name))


def _imports(tree: ast.AST) -> Tuple[str, ...]:
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def detect_source(check_id: str, path: Path) -> Tuple[str, ...]:
    source = Path(path).read_text()
    tree = ast.parse(source)
    findings: List[str] = []
    imports = _imports(tree)

    if check_id == "route-boundary":
        route_values = {"omp", "pi", "claude", "claude_code"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                constants = {
                    part.value
                    for part in ast.walk(node)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                }
                if constants & route_values:
                    findings.append("route comparison")
    elif check_id == "digest-over-audit":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in (
                "digest_of",
                "sha256",
            ):
                if any("audit" in name for arg in node.args for name in _names(arg)):
                    findings.append("audit-derived digest")
    elif check_id == "model-from-audit":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in (
                "Plan",
                "PlanNode",
                "parse_mapping",
            ):
                values = list(node.args) + [kw.value for kw in node.keywords]
                if any("audit" in name for value in values for name in _names(value)):
                    findings.append("audit-derived model")
    elif check_id == "stderr-classification":
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "stderr" in _names(node.test):
                findings.append("stderr classification")
    elif check_id == "status-write-boundary":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "write_status":
                findings.append("status write")
    elif check_id == "digest-import-boundary":
        forbidden = {"json", "pydantic", "plan_model", "adw_modules.plan_model"}
        if any(name in forbidden or name.endswith(".plan_model") for name in imports):
            findings.append("digest imports representation")
    elif check_id == "base-execution-import":
        if any(
            name.endswith("agents") or name.endswith("agent_pi") for name in imports
        ):
            findings.append("base execution import")
    else:
        raise KeyError(check_id)
    return tuple(findings)


def scan_real_tree(check_id: str, modules: Path) -> Tuple[str, ...]:
    candidates = sorted(Path(modules).glob("*.py"))
    if check_id == "route-boundary":
        candidates = [
            path
            for path in candidates
            if path.name not in _BASE_MODULES
            and not path.name.startswith(("launcher", "route_"))
        ]
    elif check_id == "digest-import-boundary":
        candidates = [Path(modules) / "plan_digest.py"]
    elif check_id == "base-execution-import":
        candidates = [path for path in candidates if path.name not in _BASE_MODULES]
    findings: List[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        findings.extend(
            "{}:{}".format(path.name, item) for item in detect_source(check_id, path)
        )
    return tuple(findings)


def _module_origin(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise EnforcementViolation("INSTALLED_BYTES_UNRESOLVED:{}".format(module_name))
    return Path(spec.origin).resolve()


def assert_installed_bytes(expected_root: Path) -> None:
    expected = Path(expected_root).resolve()
    runtime_root = _module_origin("adw_modules").parent.parent
    if runtime_root != expected:
        raise EnforcementViolation(
            "INSTALLED_BYTES_MISMATCH:{}!={}".format(runtime_root, expected)
        )
    for relative_path in INSTALLED_FACTORY_FILES:
        module_name = relative_path[:-3].replace("/", ".")
        runtime_path = _module_origin(module_name)
        expected_path = (expected / relative_path).resolve()
        if runtime_path != expected_path:
            raise EnforcementViolation(
                "INSTALLED_BYTES_FILE_MISMATCH:{}!={}".format(
                    runtime_path, expected_path
                )
            )


def assert_verbs(observed: Iterable[str]) -> None:
    found = set(observed)
    missing = [verb for verb in REQUIRED_VERBS if verb not in found]
    if missing:
        raise EnforcementViolation("MISSING_VERBS:{}".format(",".join(missing)))
