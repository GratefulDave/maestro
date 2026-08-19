"""Executed static detectors and their obligation ledger (§13.4)."""

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
    Obligation("route-boundary", "detect_source", "violations_a.py:route", "launcher boundary"),
    Obligation("digest-over-audit", "detect_source", "violations_a.py:digest", "plan bytes"),
    Obligation("model-from-audit", "detect_source", "violations_a.py:model", "validated plan"),
    Obligation("stderr-classification", "detect_source", "violations_b.py:stderr", "typed signal"),
    Obligation("status-write-boundary", "detect_source", "violations_b.py:status", "lifecycle ledger"),
    Obligation("finalization-import-boundary", "detect_source", "violations_a.py:import", "finalization.py"),
    Obligation("digest-import-boundary", "detect_source", "violations_b.py:json", "plan_digest.py"),
    Obligation("base-execution-import", "detect_source", "violations_b.py:agents", "Maestro modules"),
    Obligation("installed-bytes", "assert_installed_bytes", "wrong runtime root", "resolved runtime root"),
    Obligation("verb-existence", "assert_verbs", "one omitted verb", "complete parser"),
    Obligation("workspace-verb-existence", "assert_workspace_verbs", "workspace publish omitted", "all workspace parser verbs"),
    Obligation("workspace-foundation-execution-boundary", "detect_source", "workspace_foundation_bad.py:workspace_runtime", "workspace model, canonical, and digest"),
    Obligation("workspace-receipt-execution-boundary", "detect_source", "workspace_receipt_bad.py:coordinator", "signed receipt module"),
    Obligation("coordinator-execution-boundary", "detect_source", "coordinator_execution_bad.py:sqlite3", "coordinator delegation"),
    Obligation("publication-authority-boundary", "detect_source", "publication_authority_bad.py:workspace_digest", "coordinator store and workspace model"),
    Obligation("participant-independence-boundary", "detect_source", "participant_independence_bad.py:publication", "independent participant runner"),
)

REQUIRED_VERBS = (
    "bootstrap", "plan author",
    "plan validate", "plan finalize", "plan show", "plan list",
    "run start", "run status", "run list", "run cancel", "run resume",
    "retry", "skip", "abandon", "attempt salvage",
)

WORKSPACE_REQUIRED_VERBS = (
    "workspace author", "workspace validate", "workspace finalize",
    "workspace start",
    "workspace status", "workspace cancel", "workspace resume",
    "workspace publish", "workspace rollback",
)

INSTALLED_WORKSPACE_FILES = (
    "maestro.py",
    "adw_modules/workspace_model.py",
    "adw_modules/workspace_canonical.py",
    "adw_modules/workspace_digest.py",
    "adw_modules/workspace_receipt.py",
    "adw_modules/coordinator_store.py",
    "adw_modules/participant.py",
    "adw_modules/workspace_runtime.py",
    "adw_modules/coordinator.py",
    "adw_modules/publication.py",
)

_BASE_MODULES = {
    "agents.py", "agent_pi.py", "agent_cc.py", "data_types.py", "gates.py",
    "git_helper.py", "permissions.py", "prompts.py", "quality.py", "runner.py",
    "utils.py",
}


def _names(node: ast.AST) -> Tuple[str, ...]:
    return tuple(child.id for child in ast.walk(node) if isinstance(child, ast.Name))


def _imports(tree: ast.AST) -> Tuple[str, ...]:
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            names.extend("{}.{}".format(prefix, alias.name).strip(".")
                         for alias in node.names)
    return tuple(names)


def _imports_module(imports: Iterable[str], module: str) -> bool:
    return any(
        name == module or name.startswith(module + ".")
        or name.endswith("." + module)
        or ".{}.".format(module) in name
        for name in imports
    )


def _matching_modules(imports: Iterable[str],
                      modules: Iterable[str]) -> Tuple[str, ...]:
    return tuple(module for module in modules
                 if _imports_module(imports, module))


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
                constants = {part.value for part in ast.walk(node)
                             if isinstance(part, ast.Constant) and isinstance(part.value, str)}
                if constants & route_values:
                    findings.append("route comparison")
    elif check_id == "digest-over-audit":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in ("digest_of", "sha256"):
                if any("audit" in name for arg in node.args for name in _names(arg)):
                    findings.append("audit-derived digest")
    elif check_id == "model-from-audit":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in ("Plan", "PlanNode", "parse_mapping"):
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
    elif check_id == "finalization-import-boundary":
        if any(name.endswith("plan_canonical") for name in imports):
            findings.append("finalization imports canonicalizer")
    elif check_id == "digest-import-boundary":
        forbidden = {"json", "pydantic", "plan_model", "adw_modules.plan_model"}
        if any(name in forbidden or name.endswith(".plan_model") for name in imports):
            findings.append("digest imports representation")
    elif check_id == "base-execution-import":
        if any(name.endswith("agents") or name.endswith("agent_pi") for name in imports):
            findings.append("base execution import")
    elif check_id == "workspace-foundation-execution-boundary":
        for module in _matching_modules(
                imports, ("coordinator", "coordinator_store", "participant",
                          "publication", "workspace_runtime")):
            findings.append("execution import:{}".format(module))
    elif check_id == "workspace-receipt-execution-boundary":
        for module in _matching_modules(
                imports, ("coordinator", "workspace_runtime", "publication")):
            findings.append("receipt execution import:{}".format(module))
    elif check_id == "coordinator-execution-boundary":
        for module in _matching_modules(imports, ("sqlite3", "subprocess")):
            findings.append("direct {} import".format(
                "sqlite" if module == "sqlite3" else "subprocess"))
    elif check_id == "publication-authority-boundary":
        if not _imports_module(imports, "coordinator_store"):
            findings.append("missing coordinator store authority")
        if not _imports_module(imports, "workspace_model"):
            findings.append("missing workspace model authority")
        for module in _matching_modules(
                imports, ("workspace_canonical", "workspace_digest")):
            findings.append("workspace reconstruction import:{}".format(module))
    elif check_id == "participant-independence-boundary":
        for module in _matching_modules(imports, ("coordinator", "publication")):
            findings.append("participant execution import:{}".format(module))
    else:
        raise KeyError(check_id)
    return tuple(findings)


def scan_real_tree(check_id: str, modules: Path) -> Tuple[str, ...]:
    candidates = sorted(Path(modules).glob("*.py"))
    if check_id == "route-boundary":
        candidates = [path for path in candidates
                      if path.name not in _BASE_MODULES
                      and not path.name.startswith(("launcher", "route_"))]
    elif check_id == "finalization-import-boundary":
        candidates = [Path(modules) / "finalization.py"]
    elif check_id == "digest-import-boundary":
        candidates = [Path(modules) / "plan_digest.py"]
    elif check_id == "base-execution-import":
        candidates = [path for path in candidates if path.name not in _BASE_MODULES]
    elif check_id == "workspace-foundation-execution-boundary":
        candidates = [
            Path(modules) / "workspace_model.py",
            Path(modules) / "workspace_canonical.py",
            Path(modules) / "workspace_digest.py",
        ]
    elif check_id == "workspace-receipt-execution-boundary":
        candidates = [Path(modules) / "workspace_receipt.py"]
    elif check_id == "coordinator-execution-boundary":
        candidates = [Path(modules) / "coordinator.py"]
    elif check_id == "publication-authority-boundary":
        candidates = [Path(modules) / "publication.py"]
    elif check_id == "participant-independence-boundary":
        candidates = [Path(modules) / "participant.py"]
    findings: List[str] = []
    for path in candidates:
        findings.extend("{}:{}".format(path.name, item)
                        for item in detect_source(check_id, path))
    return tuple(findings)


def _module_origin(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise EnforcementViolation(
            "INSTALLED_BYTES_UNRESOLVED:{}".format(module_name))
    return Path(spec.origin).resolve()


def assert_installed_bytes(expected_root: Path) -> None:
    expected = Path(expected_root).resolve()
    runtime_root = _module_origin("adw_modules").parent.parent
    if runtime_root != expected:
        raise EnforcementViolation("INSTALLED_BYTES_MISMATCH:{}!={}".format(
            runtime_root, expected))
    for relative_path in INSTALLED_WORKSPACE_FILES:
        module_name = relative_path[:-3].replace("/", ".")
        runtime_path = _module_origin(module_name)
        expected_path = (expected / relative_path).resolve()
        if runtime_path != expected_path:
            raise EnforcementViolation(
                "INSTALLED_BYTES_FILE_MISMATCH:{}!={}".format(
                    runtime_path, expected_path))


def assert_verbs(observed: Iterable[str]) -> None:
    found = set(observed)
    missing = [verb for verb in REQUIRED_VERBS if verb not in found]
    if missing:
        raise EnforcementViolation("MISSING_VERBS:{}".format(",".join(missing)))


def assert_workspace_verbs(observed: Iterable[str]) -> None:
    found = set(observed)
    missing = [verb for verb in WORKSPACE_REQUIRED_VERBS if verb not in found]
    if missing:
        raise EnforcementViolation(
            "MISSING_WORKSPACE_VERBS:{}".format(",".join(missing)))
