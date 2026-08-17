"""Receipt-verified projection from plan-contract.v1 to maestro-plan.v1.

This is an authoring boundary, not a lossy converter. Missing Maestro
extensions, ambient paths, receiptless input, and unparseable gates refuse.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from . import plan_author


EXECUTABLE_KINDS = frozenset({"implementation", "brownfield", "prd", "workflow"})
RECEIPT_VERSION = "plan-contract-review.v1"


class IngressError(plan_author.AuthoringError):
    """A plan-contract package cannot become a Maestro plan."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, code: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IngressError("{}:{}".format(code, path)) from exc
    if not isinstance(payload, dict):
        raise IngressError("{}:not-object:{}".format(code, path))
    return payload


def _require_relative(path: str, label: str) -> str:
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(part in (".", "..") for part in path.split("/"))):
        raise IngressError("AMBIENT_PATH:{}:{}".format(label, path))
    return path


def _verify_receipt(ir_bytes: bytes, receipt: Mapping[str, Any],
                    rendered: Optional[bytes]) -> None:
    if receipt.get("schema_version") != RECEIPT_VERSION:
        raise IngressError("RECEIPT_SCHEMA")
    if receipt.get("verdict") != "PASS":
        raise IngressError("RECEIPT_NOT_PASS")
    digest = receipt.get("ir_sha256")
    if not isinstance(digest, str) or _sha256(ir_bytes) != digest:
        raise IngressError("RECEIPT_IR_MISMATCH")
    if rendered is not None:
        rendered_digest = receipt.get("rendered_sha256")
        if (not isinstance(rendered_digest, str)
                or _sha256(rendered) != rendered_digest):
            raise IngressError("RECEIPT_RENDERED_MISMATCH")


def _maestro_extension(ir: Mapping[str, Any]) -> dict:
    extensions = ir.get("extensions")
    if not isinstance(extensions, dict):
        raise IngressError("MAESTRO_EXTENSION_MISSING")
    maestro = extensions.get("maestro")
    if not isinstance(maestro, dict):
        raise IngressError("MAESTRO_EXTENSION_MISSING")
    return maestro


def _parse_verifier_command(command: object) -> Tuple[str, Tuple[str, ...]]:
    if isinstance(command, str):
        tokens = command.split()
    elif isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
        tokens = [str(item) for item in command]
    else:
        raise IngressError("UNMAPPABLE_COMMAND")
    if not tokens:
        raise IngressError("UNMAPPABLE_COMMAND")
    if tokens[0] == "pytest" or tokens[:3] == ["python3", "-m", "pytest"]:
        argv = tuple(tokens[1:] if tokens[0] == "pytest" else tokens[3:])
        return "pytest", argv
    if tokens[0] == "vitest" or tokens[:2] == ["npx", "vitest"]:
        start = 1 if tokens[0] == "vitest" else 2
        rest = tokens[start:]
        if rest[:1] == ["run"]:
            rest = rest[1:]
        return "vitest", tuple(rest)
    raise IngressError("UNMAPPABLE_COMMAND:{}".format(tokens[0]))


def project_draft(ir: Mapping[str, Any], repo: Path) -> dict:
    """Map one approved executable Plan IR onto a Maestro draft mapping."""
    if ir.get("schema_version") != "plan-contract.v1":
        raise IngressError("IR_SCHEMA")
    kind = ir.get("plan_kind")
    if kind == "architecture":
        raise IngressError("ARCHITECTURE_NOT_EXECUTABLE")
    if kind not in EXECUTABLE_KINDS:
        raise IngressError("IR_PLAN_KIND:{}".format(kind))
    maestro = _maestro_extension(ir)
    outputs_by_lane = maestro.get("outputs")
    if not isinstance(outputs_by_lane, dict) or not outputs_by_lane:
        raise IngressError("UNMAPPABLE_OUTPUTS")
    integration = maestro.get("integration_gate")
    branch = maestro.get("integration_branch")
    if not isinstance(branch, str) or not branch:
        raise IngressError("UNMAPPABLE_INTEGRATION")
    if not isinstance(integration, dict):
        raise IngressError("UNMAPPABLE_INTEGRATION")

    lanes = ir.get("lanes")
    verifiers = ir.get("verifiers")
    sources = ir.get("source_artifacts")
    if not isinstance(lanes, list) or not lanes:
        raise IngressError("UNMAPPABLE_LANES")
    if not isinstance(verifiers, list) or not verifiers:
        raise IngressError("UNMAPPABLE_VERIFIERS")
    if not isinstance(sources, list) or not sources:
        raise IngressError("UNMAPPABLE_SOURCES")

    evidence = []
    for source in sources:
        if not isinstance(source, dict):
            raise IngressError("UNMAPPABLE_SOURCES")
        path = source.get("path")
        if not isinstance(path, str):
            raise IngressError("UNMAPPABLE_SOURCES")
        _require_relative(path, source.get("source_id") or "source")
        evidence.append({
            "kind": "observed",
            "evidence_id": source["source_id"],
            "path": path,
        })

    nodes = []
    lane_ids = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise IngressError("UNMAPPABLE_LANES")
        lane_id = lane.get("lane_id")
        if not isinstance(lane_id, str) or not lane_id:
            raise IngressError("UNMAPPABLE_LANES")
        lane_ids.append(lane_id)
        cwd = lane.get("execution_context")
        if not isinstance(cwd, str) or cwd in {"", "lane worktree root"}:
            raise IngressError("AMBIENT_CWD:{}".format(lane_id))
        _require_relative(cwd, lane_id) if cwd != "." else cwd
        outputs = outputs_by_lane.get(lane_id)
        if not isinstance(outputs, list) or not outputs:
            raise IngressError("UNMAPPABLE_OUTPUTS:{}".format(lane_id))
        produced_ids = []
        for index, output in enumerate(outputs):
            if not isinstance(output, str):
                raise IngressError("UNMAPPABLE_OUTPUTS:{}".format(lane_id))
            _require_relative(output, lane_id)
            evidence_id = "produced-{}-{}".format(lane_id, index)
            produced_ids.append(evidence_id)
            evidence.append({
                "kind": "produced",
                "evidence_id": evidence_id,
                "path": output,
                "producer": lane_id,
            })
        lane_verifiers = [
            item for item in verifiers
            if isinstance(item, dict) and lane_id in (item.get("lane_ids") or [])
        ]
        if len(lane_verifiers) != 1:
            raise IngressError("UNMAPPABLE_VERIFIERS:{}".format(lane_id))
        verifier = lane_verifiers[0]
        runner, argv = _parse_verifier_command(verifier.get("command"))
        if not argv:
            raise IngressError("BROAD_GATE:{}".format(verifier.get("verifier_id")))
        min_cases = verifier.get("min_executed")
        if not isinstance(min_cases, int) or min_cases < 1:
            raise IngressError("UNMAPPABLE_VERIFIERS:{}".format(lane_id))
        source_reads = [
            item for item in (verifier.get("source_ids") or [])
            if isinstance(item, str)
        ]
        nodes.append({
            "kind": "agent",
            "node_id": lane_id,
            "needs": list(lane.get("depends_on") or []),
            "reads": source_reads,
            "outputs": list(outputs),
            "instruction": lane.get("title") or lane_id,
            "gate": {
                "runner": runner,
                "argv": list(argv),
                "cwd": cwd,
                "min_cases": min_cases,
            },
            "prompt_assets": [],
        })

    if integration.get("runner"):
        ig_runner = integration["runner"]
        ig_argv = tuple(integration.get("argv") or ())
    else:
        ig_runner, ig_argv = _parse_verifier_command(
            integration.get("command") or integration.get("argv"))
    if not ig_argv:
        raise IngressError("UNMAPPABLE_INTEGRATION")
    ig_cwd = integration.get("cwd") or "."
    ig_min = integration.get("min_cases") or integration.get("min_executed") or 1
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": ir["plan_id"],
        "repo": maestro.get("repo") or Path(repo).name,
        "intent": ir.get("title") or ir["plan_id"],
        "evidence": evidence,
        "nodes": nodes,
        "merge_policy": {
            "integration_branch": branch,
            "integration_gate": {
                "runner": ig_runner,
                "argv": list(ig_argv),
                "cwd": ig_cwd,
                "min_cases": ig_min,
            },
        },
    }


def author_from_plan_contract(
        ir_path: Path, receipt_path: Path, destination: Path, repo: Path,
        rendered_path: Optional[Path] = None) -> Tuple[bytes, dict]:
    """Verify the receipt, project, canonicalize, and write maestro-plan.v1."""
    ir_bytes = Path(ir_path).read_bytes()
    ir = _load_json(ir_path, "IR_UNREADABLE")
    receipt = _load_json(receipt_path, "RECEIPT_UNREADABLE")
    rendered = Path(rendered_path).read_bytes() if rendered_path else None
    _verify_receipt(ir_bytes, receipt, rendered)
    draft = project_draft(ir, repo)
    stored = plan_author.author_plan(draft, repo)
    plan_author.write_canonical_plan(destination, stored)
    trace = {
        "plan_id": ir.get("plan_id"),
        "receipt_ir_sha256": receipt.get("ir_sha256"),
        "lanes": [node["node_id"] for node in draft["nodes"]],
        "sources": [item["evidence_id"] for item in draft["evidence"]
                    if item["kind"] == "observed"],
    }
    return stored, trace
