"""Receipt-verified projection from plan-contract.v1 onto SCHEMA_VERSION.

This is an authoring boundary, not a lossy converter. It emits lanes, not
nodes. Totality is guarded by `_assert_ingress_projection_is_total`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from . import plan_author
from . import scheduler_types as st
from .plan_model import LANE_KEYS, SCHEMA_VERSION


EXECUTABLE_KINDS = frozenset({"implementation", "brownfield", "prd", "workflow"})
RECEIPT_VERSION = "plan-contract-review.v1"


class IngressError(plan_author.AuthoringError):
    """A plan-contract package cannot become a Maestro plan."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IngressProjectionIncomplete(RuntimeError):
    """`project_draft` did not account for a field the IR declares."""


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


def _require_text(value: object, code: str, label: str) -> str:
    """A field the projection carries verbatim: present, a string, non-empty."""
    if not isinstance(value, str) or not value:
        raise IngressError("{}:{}".format(code, label))
    return value


def _require_count(value: object, code: str, label: str) -> int:
    """A threshold the adjudicator will count against. `bool` is not an `int`."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IngressError("{}:{}".format(code, label))
    return value


def _require_id_list(value: object, code: str, label: str) -> list:
    """A list of ids, or nothing. Absence is legal and means the empty list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise IngressError("{}:{}".format(code, label))
    for item in value:
        if not isinstance(item, str) or not item:
            raise IngressError("{}:{}".format(code, label))
    return list(value)


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


def _requirements_by_id(ir: Mapping[str, Any]) -> dict:
    """`{requirement_id: requirement}` over the IR's declared requirements."""
    declared = ir.get("requirements")
    index: dict = {}
    for item in declared if isinstance(declared, list) else ():
        if not isinstance(item, dict):
            continue
        requirement_id = item.get("requirement_id")
        if isinstance(requirement_id, str) and requirement_id:
            index.setdefault(requirement_id, item)
    return index


REQUIREMENT_HEADING = "Requirement {0}, in the plan's own words:"


def _node_instruction(ir: Mapping[str, Any], lane: Mapping[str, Any],
                      lane_id: str) -> str:
    """The lane's title as a label, then the requirement text that bounds it."""
    title = _require_text(
        lane.get("title"), "UNMAPPABLE_LANES", "{}.title".format(lane_id))
    bound = _require_id_list(
        lane.get("requirement_ids"), "UNMAPPABLE_LANES",
        "{}.requirement_ids".format(lane_id))
    index = _requirements_by_id(ir)
    blocks = [title]
    seen = set()
    for requirement_id in bound:
        if requirement_id in seen:
            continue
        seen.add(requirement_id)
        requirement = index.get(requirement_id)
        if not isinstance(requirement, dict):
            raise IngressError(
                "UNMAPPABLE_REQUIREMENTS:{}.requirement_ids:{}".format(
                    lane_id, requirement_id))
        text = _require_text(
            requirement.get("text"), "UNMAPPABLE_REQUIREMENTS",
            "{}.text".format(requirement_id)).strip()
        if not text:
            raise IngressError(
                "UNMAPPABLE_REQUIREMENTS:{}.text".format(requirement_id))
        blocks.append("{0}\n{1}".format(
            REQUIREMENT_HEADING.format(requirement_id), text))
    return "\n\n".join(blocks)


def _node_effects(ir: Mapping[str, Any], lane: Mapping[str, Any],
                  lane_id: str) -> list:
    """Every prohibited effect, with a non-performed disposition from bound requirements."""
    extensions = ir.get("extensions")
    maestro = extensions.get("maestro") if isinstance(extensions, dict) else {}
    declared = maestro.get("prohibited_effects") if isinstance(maestro, dict) else None
    prohibited: list = []
    meanings = {}
    seen = set()
    for entry in declared if isinstance(declared, list) else ():
        if not isinstance(entry, dict):
            continue
        effect = entry.get("effect")
        if not isinstance(effect, str) or not effect or effect in seen:
            continue
        seen.add(effect)
        prohibited.append(effect)
        meaning = entry.get("meaning")
        if isinstance(meaning, str) and meaning.strip():
            meanings[effect] = meaning.strip()
    by_id = _requirements_by_id(ir)
    bound = _require_id_list(
        lane.get("requirement_ids"), "UNMAPPABLE_LANES",
        "{}.requirement_ids".format(lane_id))
    dispositions = {}
    for requirement_id in bound:
        requirement = by_id.get(requirement_id)
        if not isinstance(requirement, dict):
            continue
        declared_effects = {}
        for entry in requirement.get("effects") or []:
            if not isinstance(entry, dict):
                continue
            effect = entry.get("effect")
            if isinstance(effect, str) and effect:
                declared_effects.setdefault(effect, entry.get("disposition"))
        for effect in prohibited:
            disposition = declared_effects.get(effect)
            if disposition == "performed" or not isinstance(disposition, str) or not disposition:
                raise IngressError(
                    "UNMAPPABLE_EFFECTS:{}.{}".format(lane_id, effect))
            dispositions.setdefault(effect, disposition)
    emitted = []
    for effect in prohibited:
        if effect not in meanings:
            continue
        item = {"effect": effect, "meaning": meanings[effect]}
        if effect in dispositions:
            item["disposition"] = dispositions[effect]
        emitted.append(item)
    return emitted


_LANE_PROJECTION: Dict[str, Optional[str]] = {
    "lane_id": "id",
    "title": "spec.goal, spec.instruction",
    "lane_kind": "lane_kind",
    "execution_context": "spec.gate.cwd",
    "depends_on": "needs",
    "requirement_ids": "spec.instruction, spec.bindings.requirement_ids",
    "verifier_ids": "spec.bindings.verifier_ids",
    "claim_ids": "acceptance, spec.bindings.claim_ids",
    "seam_ids": "spec.seams, spec.bindings.seam_ids",
    "fixture_ids": None,
}
_LANE_PROJECTION_EXEMPT: Dict[str, str] = {
    "fixture_ids": (
        "a builder never receives fixtures (docs/plan-authoring.md:148-151); "
        "the ids and records reach the paired tester via "
        "spec.obligations.for_build_lanes"),
}
_LANE_PROJECTION_TESTS = dict(_LANE_PROJECTION, **{
    "fixture_ids": "spec.obligations.observed_baseline, spec.bindings.fixture_ids",
})
_LANE_PROJECTION_TESTS_EXEMPT: Dict[str, str] = {}


_REQUIREMENT_PROJECTION: Dict[str, Optional[str]] = {
    "requirement_id": "spec.bindings.requirement_ids",
    "text": "spec.instruction",
    "source_ids": "spec.sources",
    "surface": None,
    "effects": "spec.effects",
}
_REQUIREMENT_PROJECTION_EXEMPT: Dict[str, str] = {
    "surface": (
        "Plan Contract SKILL.md:48-53 still names Maestro ingress as the "
        "reader; validate_contract_surface was deleted in e7b477e and this "
        "port does not re-add SURFACE_REACHABLE. No factory reader remains."),
}


_VERIFIER_REVERSE_EXEMPT: Dict[str, str] = {
    "requirement_ids": (
        "reverse index of the lane's requirement_ids, already carried "
        "via instruction, effects and spec.sources; a second copy is two answers"),
    "fixture_ids": (
        "reverse index of the lane's fixture_ids. A build lane never receives "
        "fixture records (docs/plan-authoring.md:148-151); a tests lane receives "
        "them via spec.obligations.observed_baseline from the lane binding, not "
        "this reverse index"),
    "seam_ids": (
        "reverse index of the lane's seam_ids, already carried on spec.seams, "
        "spec.bindings.seam_ids, and spec.instruction; a second copy is two answers"),
    "claim_ids": (
        "reverse index of the lane's claim_ids, already carried on acceptance "
        "and spec.bindings.claim_ids; a second copy is two answers"),
}
_VERIFIER_TEST_FACING = (
    "test-facing; docs/plan-authoring.md:283-286 forbids these on a "
    "builder prompt")
_VERIFIER_PROJECTION: Dict[str, Optional[str]] = {
    "verifier_id": "acceptance[0]",
    "oracle": "acceptance[0]",
    "lane_ids": "spec.gate",
    "command": "spec.gate",
    "min_executed": "spec.gate",
    "source_ids": "spec.sources",
    "falsifiability": None,
    "independent": None,
    "test_strength": None,
    "requirement_ids": None,
    "fixture_ids": None,
    "seam_ids": None,
    "claim_ids": None,
}
_VERIFIER_PROJECTION_EXEMPT: Dict[str, str] = {
    "falsifiability": _VERIFIER_TEST_FACING,
    "independent": _VERIFIER_TEST_FACING,
    "test_strength": (
        "refused on a build verifier as UNMAPPABLE_VERIFIERS:<lane>.test_strength; "
        "not dropped and not carried"),
    **_VERIFIER_REVERSE_EXEMPT,
}
_VERIFIER_PROJECTION_TESTS: Dict[str, Optional[str]] = {
    "verifier_id": "acceptance[0], spec.obligations.verifier",
    "oracle": "acceptance[0], spec.obligations.verifier",
    "lane_ids": "spec.gate",
    "command": "spec.gate",
    "min_executed": "spec.gate",
    "source_ids": "spec.sources",
    "falsifiability": "spec.obligations.verifier",
    "independent": "spec.obligations.verifier",
    "test_strength": "spec.obligations.verifier",
    "requirement_ids": None,
    "fixture_ids": None,
    "seam_ids": None,
    "claim_ids": None,
}
_VERIFIER_PROJECTION_TESTS_EXEMPT: Dict[str, str] = dict(_VERIFIER_REVERSE_EXEMPT)


_CLAIM_FIELDS = (
    "antecedent_claim_id",
    "claim_id",
    "compression",
    "domain",
    "exception_ids",
    "identifier_pattern",
    "kind",
    "mutation_kinds",
    "object",
    "owner",
    "polarity",
    "postconditions",
    "preconditions",
    "predicate",
    "rendered_binding_ids",
    "schema_ref",
    "serializer",
    "source_ids",
    "source_requirement_ids",
    "state_from",
    "state_to",
    "subject",
    "unit",
    "value",
    "verifier_ids",
    "witness",
)
_CLAIM_TEST_FACING = (
    "test-facing; delivered verbatim to the paired tester via "
    "spec.obligations.for_build_lanes")
_CLAIM_PROJECTION: Dict[str, Optional[str]] = {
    "claim_id": "acceptance",
    "subject": "acceptance",
    "predicate": "acceptance",
    "object": "acceptance",
    "polarity": "acceptance",
    "value": "acceptance",
    "unit": "acceptance",
    **{name: None for name in _CLAIM_FIELDS if name not in {
        "claim_id", "subject", "predicate", "object", "polarity", "value", "unit",
    }},
}
_CLAIM_PROJECTION_EXEMPT: Dict[str, str] = {
    name: _CLAIM_TEST_FACING
    for name in _CLAIM_FIELDS
    if name not in {
        "claim_id", "subject", "predicate", "object", "polarity", "value", "unit",
    }
}
_CLAIM_PROJECTION_TESTS: Dict[str, Optional[str]] = {
    name: "spec.obligations.claims" for name in _CLAIM_FIELDS
}
_CLAIM_PROJECTION_TESTS_EXEMPT: Dict[str, str] = {}


_SEAM_REVERSE = (
    "reverse index; seam_id/producer/consumer/contract already sit on "
    "spec.seams and in spec.instruction. This index is not a second copy of those four.")
_SEAM_PROJECTION: Dict[str, Optional[str]] = {
    "seam_id": "spec.seams",
    "producer": "spec.seams",
    "consumer": "spec.seams",
    "contract": "spec.seams",
    "claim_ids": None,
    "fixture_ids": None,
    "requirement_ids": None,
    "source_ids": None,
    "verifier_ids": None,
}
_SEAM_PROJECTION_EXEMPT: Dict[str, str] = {
    "claim_ids": _SEAM_REVERSE,
    "fixture_ids": _SEAM_REVERSE,
    "requirement_ids": _SEAM_REVERSE,
    "source_ids": _SEAM_REVERSE,
    "verifier_ids": _SEAM_REVERSE,
}
_SEAM_PROJECTION_TESTS: Dict[str, Optional[str]] = {
    "seam_id": "spec.seams",
    "producer": "spec.seams",
    "consumer": "spec.seams",
    "contract": "spec.seams",
    "claim_ids": "spec.obligations.seams",
    "fixture_ids": "spec.obligations.seams",
    "requirement_ids": "spec.obligations.seams",
    "source_ids": "spec.obligations.seams",
    "verifier_ids": "spec.obligations.seams",
}
_SEAM_PROJECTION_TESTS_EXEMPT: Dict[str, str] = {}


_FIXTURE_FIELDS = (
    "fixture_id",
    "affected_lane_ids",
    "consumer_obligation",
    "meaning",
    "observed_value",
    "path",
    "producer_metadata",
    "prohibited_behavior",
    "record_selector",
    "seam_ids",
    "source_id",
    "verifier_ids",
)
_FIXTURE_PROJECTION: Dict[str, Optional[str]] = {
    name: None for name in _FIXTURE_FIELDS
}
_FIXTURE_PROJECTION_EXEMPT: Dict[str, str] = {
    name: _LANE_PROJECTION_EXEMPT["fixture_ids"] for name in _FIXTURE_FIELDS
}
_FIXTURE_PROJECTION_TESTS: Dict[str, Optional[str]] = {
    name: "spec.obligations.observed_baseline" for name in _FIXTURE_FIELDS
}
_FIXTURE_PROJECTION_TESTS_EXEMPT: Dict[str, str] = {}


_EXTENSION_PROJECTION: Dict[str, Optional[str]] = {
    "outputs": "outputs",
    "integration_branch": "spec.integration.integration_branch",
    "prohibited_effects": "spec.effects",
    "repo": None,
    "integration_gate": None,
}
_EXTENSION_PROJECTION_EXEMPT: Dict[str, str] = {
    "repo": "the repository is --repo at run start (MAESTRO_architecture.md §10); no plan field",
    "integration_gate": (
        "no plan-level reader: the factory's run-level gate is a tests lane no "
        "build lane consumes (scheduler.py:2447-2461); the value is parsed and "
        "shape-checked (kept refusals) and then discarded"),
}

_IR_PROJECTION: Dict[str, Optional[str]] = {
    "schema_version": "IR_SCHEMA",
    "plan_id": "trace.plan_id",
    "title": "trace.title",
    "plan_kind": "EXECUTABLE_KINDS",
    "author": None,
    "source_artifacts": "spec.sources",
    "requirements": "spec.instruction, spec.effects, spec.bindings.requirement_ids",
    "claims": "acceptance, spec.obligations.claims",
    "fixtures": "spec.obligations.observed_baseline, spec.obligations.for_build_lanes",
    "seams": "spec.seams, spec.obligations.seams",
    "lanes": "lanes",
    "verifiers": "spec.gate, acceptance[0]",
    "traceability": None,
    "rendered_bindings": "spec.obligations.rendered_bindings",
    "links": None,
    "approval": None,
    "extensions": "_EXTENSIONS_PROJECTION",
}
_IR_PROJECTION_EXEMPT: Dict[str, str] = {
    "author": (
        "identity of the IR author; no factory reader. The receipt names the "
        "bytes and --repo names the worktree"),
    "traceability": (
        "Plan Contract reverse index; lane bindings already carried as "
        "spec.bindings. A second copy is two answers"),
    "links": "HTML/view hyperlinks; no factory reader",
    "approval": (
        "the PASS receipt is the approval; _verify_receipt checks it against "
        "the IR bytes"),
}
_EXTENSIONS_PROJECTION: Dict[str, Optional[str]] = {
    "claim_kinds": None,
    "maestro": "_EXTENSION_PROJECTION",
}
_EXTENSIONS_PROJECTION_EXEMPT: Dict[str, str] = {
    "claim_kinds": (
        "Plan Contract vocabulary for claim.kind; no factory reader. The "
        "compiler digests spec whole and does not interpret claim kinds"),
}


def _records_by_id(ir: Mapping[str, Any], collection: str,
                   id_key: str) -> dict:
    declared = ir.get(collection)
    index: dict = {}
    for item in declared if isinstance(declared, list) else ():
        if not isinstance(item, dict):
            continue
        record_id = item.get(id_key)
        if isinstance(record_id, str) and record_id:
            index.setdefault(record_id, item)
    return index


def _bound_records(ir: Mapping[str, Any], lane: Mapping[str, Any],
                   ids_key: str, collection: str, id_key: str) -> list:
    ids = lane.get(ids_key)
    if not isinstance(ids, list):
        return []
    index = _records_by_id(ir, collection, id_key)
    records = []
    seen = set()
    for record_id in ids:
        if not isinstance(record_id, str) or record_id in seen:
            continue
        seen.add(record_id)
        item = index.get(record_id)
        if isinstance(item, dict):
            records.append(item)
    return records


def _extend_unique(target: list, values: Sequence[str],
                   seen: set) -> None:
    for item in values:
        if item not in seen:
            seen.add(item)
            target.append(item)


def _lane_source_reads(ir: Mapping[str, Any], lane: Mapping[str, Any],
                       verifier: Mapping[str, Any], lane_id: str) -> list:
    """Union of the verifier's source_ids and each bound requirement's."""
    reads: list = []
    seen: set = set()
    _extend_unique(reads, _require_id_list(
        verifier.get("source_ids"), "UNMAPPABLE_VERIFIERS",
        "{}.source_ids".format(lane_id)), seen)
    index = _requirements_by_id(ir)
    for requirement_id in _require_id_list(
            lane.get("requirement_ids"), "UNMAPPABLE_LANES",
            "{}.requirement_ids".format(lane_id)):
        requirement = index.get(requirement_id)
        if not isinstance(requirement, dict):
            continue
        _extend_unique(reads, _require_id_list(
            requirement.get("source_ids"), "UNMAPPABLE_REQUIREMENTS",
            "{}.source_ids".format(requirement_id)), seen)
    return reads


def _lane_kind(lane: Mapping[str, Any], lane_id: str) -> str:
    kind = lane.get("lane_kind", "build")
    if kind not in ("build", "tests"):
        raise IngressError("UNMAPPABLE_LANE_KIND:{}".format(lane_id))
    return kind


def _gate(verifier: Mapping[str, Any], cwd: str, lane_id: str) -> dict:
    runner, argv = _parse_verifier_command(verifier.get("command"))
    if not argv:
        raise IngressError("BROAD_GATE:{}".format(verifier.get("verifier_id")))
    min_cases = _require_count(
        verifier.get("min_executed"), "UNMAPPABLE_VERIFIERS", lane_id)
    return {"runner": runner, "argv": list(argv), "cwd": cwd, "min_cases": min_cases}


def _sources_for(ir: Mapping[str, Any], ids: Sequence[str]) -> list:
    index = _records_by_id(ir, "source_artifacts", "source_id")
    records = []
    for source_id in ids:
        item = index.get(source_id)
        if not isinstance(item, dict):
            raise IngressError("UNMAPPABLE_SOURCES:{}".format(source_id))
        records.append(item)
    return records


def _claim_sentence(claim: Mapping[str, Any]) -> str:
    claim_id = _require_text(
        claim.get("claim_id"), "UNMAPPABLE_CLAIMS", "claim_id")
    polarity = _require_text(
        claim.get("polarity"), "UNMAPPABLE_CLAIMS",
        "{}.polarity".format(claim_id))
    subject = _require_text(
        claim.get("subject"), "UNMAPPABLE_CLAIMS",
        "{}.subject".format(claim_id))
    predicate = _require_text(
        claim.get("predicate"), "UNMAPPABLE_CLAIMS",
        "{}.predicate".format(claim_id))
    obj = _require_text(
        claim.get("object"), "UNMAPPABLE_CLAIMS",
        "{}.object".format(claim_id))
    sentence = "{} ({}): {} {} {}".format(
        claim_id, polarity, subject, predicate, obj)
    value, unit = claim.get("value"), claim.get("unit")
    has_value = value is not None and value != ""
    has_unit = isinstance(unit, str) and unit
    if has_value or has_unit:
        tail = []
        if has_value:
            tail.append(str(value))
        if has_unit:
            tail.append(str(unit))
        sentence += " = " + " ".join(tail)
    return sentence


def _acceptance(verifier: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]) -> list:
    verifier_id = _require_text(
        verifier.get("verifier_id"), "UNMAPPABLE_VERIFIERS", "verifier_id")
    oracle = _require_text(
        verifier.get("oracle"), "UNMAPPABLE_VERIFIERS",
        "{}.oracle".format(verifier_id))
    return ["{}: {}".format(verifier_id, oracle)] + [
        _claim_sentence(claim) for claim in claims
    ]


def _seam_prose(seams: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for seam in seams:
        seam_id = _require_text(
            seam.get("seam_id"), "UNMAPPABLE_SEAMS", "seam_id")
        producer = _require_text(
            seam.get("producer"), "UNMAPPABLE_SEAMS",
            "{}.producer".format(seam_id))
        consumer = _require_text(
            seam.get("consumer"), "UNMAPPABLE_SEAMS",
            "{}.consumer".format(seam_id))
        contract = _require_text(
            seam.get("contract"), "UNMAPPABLE_SEAMS",
            "{}.contract".format(seam_id))
        blocks.append(
            "Seam {} ({} -> {}):\n{}".format(
                seam_id, producer, consumer, contract))
    return "\n\n".join(blocks)


def _paired_build_lanes(ir: Mapping[str, Any], tests_lane_id: str) -> list:
    ids = []
    for item in ir.get("lanes") if isinstance(ir.get("lanes"), list) else ():
        if not isinstance(item, dict):
            continue
        lane_id = item.get("lane_id")
        if not isinstance(lane_id, str) or not lane_id:
            continue
        if _lane_kind(item, lane_id) != "build":
            continue
        depends = item.get("depends_on")
        if isinstance(depends, list) and tests_lane_id in depends:
            ids.append(lane_id)
    return ids


def _bindings(lane: Mapping[str, Any], kind: str) -> dict:
    lane_id = lane.get("lane_id")
    label = lane_id if isinstance(lane_id, str) and lane_id else "lane"
    bindings = {
        "requirement_ids": _require_id_list(
            lane.get("requirement_ids"), "UNMAPPABLE_LANES",
            "{}.requirement_ids".format(label)),
        "verifier_ids": _require_id_list(
            lane.get("verifier_ids"), "UNMAPPABLE_LANES",
            "{}.verifier_ids".format(label)),
        "claim_ids": _require_id_list(
            lane.get("claim_ids"), "UNMAPPABLE_LANES",
            "{}.claim_ids".format(label)),
        "seam_ids": _require_id_list(
            lane.get("seam_ids"), "UNMAPPABLE_LANES",
            "{}.seam_ids".format(label)),
    }
    if kind == "tests":
        bindings["fixture_ids"] = _require_id_list(
            lane.get("fixture_ids"), "UNMAPPABLE_LANES",
            "{}.fixture_ids".format(label))
    return bindings


def _pack_obligations(
        ir: Mapping[str, Any], source_lane: Mapping[str, Any],
        source_verifier: Mapping[str, Any]) -> dict:
    return {
        "claims": _bound_records(
            ir, source_lane, "claim_ids", "claims", "claim_id"),
        "observed_baseline": _bound_records(
            ir, source_lane, "fixture_ids", "fixtures", "fixture_id"),
        "seams": _bound_records(
            ir, source_lane, "seam_ids", "seams", "seam_id"),
        "verifier": dict(source_verifier),
    }


def _obligations(
        ir: Mapping[str, Any], lane: Mapping[str, Any],
        verifier: Mapping[str, Any],
        projected_build_lanes: Sequence[Mapping[str, Any]]) -> dict:
    lanes_by_id = {}
    for item in ir.get("lanes") if isinstance(ir.get("lanes"), list) else ():
        if isinstance(item, dict) and isinstance(item.get("lane_id"), str):
            lanes_by_id.setdefault(item["lane_id"], item)
    verifiers = ir.get("verifiers") if isinstance(ir.get("verifiers"), list) else []
    paired = []
    for projected in projected_build_lanes:
        build_lane = lanes_by_id[projected["id"]]
        build_verifiers = [
            item for item in verifiers
            if isinstance(item, dict) and projected["id"] in item.get("lane_ids", [])
        ]
        packed = _pack_obligations(ir, build_lane, build_verifiers[0])
        packed["lane_id"] = projected["id"]
        packed["acceptance"] = list(projected["acceptance"])
        paired.append(packed)
    own = _pack_obligations(ir, lane, verifier)
    rendered = ir.get("rendered_bindings")
    return {
        "claims": own["claims"],
        "observed_baseline": own["observed_baseline"],
        "seams": own["seams"],
        "verifier": own["verifier"],
        "for_build_lanes": paired,
        "rendered_bindings": list(rendered) if isinstance(rendered, list) else [],
    }


def _bound_id_set(lanes: Sequence[Mapping[str, Any]], key: str) -> set:
    ids = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        for item in lane.get(key) or []:
            if isinstance(item, str) and item:
                ids.add(item)
    return ids


def _assert_binding_closure(ir: Mapping[str, Any], lanes: Sequence[Any]) -> None:
    """Every claim/fixture/seam/requirement/verifier is named by some lane.

    Chosen over a silent exemption: an unbound record is how extra keys were
    dropped. IngressProjectionIncomplete already names this class of drop.
    """
    bound = {
        "claim": (_bound_id_set(lanes, "claim_ids"), "claims", "claim_id"),
        "fixture": (_bound_id_set(lanes, "fixture_ids"), "fixtures", "fixture_id"),
        "seam": (_bound_id_set(lanes, "seam_ids"), "seams", "seam_id"),
        "requirement": (_bound_id_set(lanes, "requirement_ids"), "requirements", "requirement_id"),
        "verifier": (_bound_id_set(lanes, "verifier_ids"), "verifiers", "verifier_id"),
    }
    for label, (ids, collection, id_key) in bound.items():
        for item in ir.get(collection) or []:
            if not isinstance(item, dict):
                continue
            record_id = item.get(id_key)
            if isinstance(record_id, str) and record_id and record_id not in ids:
                raise IngressProjectionIncomplete(
                    "{0} {1} is bound to no lane".format(label, record_id))


def _assert_table_reasons(projection: Dict[str, Optional[str]],
                          exempt: Dict[str, str], table: str) -> None:
    """Every dropped field has a reason; a carried field is not also exempt."""
    for name, dest in projection.items():
        if dest is None:
            reason = exempt.get(name, "")
            if not str(reason).strip():
                raise IngressProjectionIncomplete(
                    "{0}: {1} is listed as dropped with no reason; an "
                    "empty reason cannot distinguish decided from "
                    "forgotten.".format(table, name))
        elif name in exempt:
            raise IngressProjectionIncomplete(
                "{0}: {1} has a destination ({2}) and an exemption; "
                "those are two answers for one field.".format(
                    table, name, dest))
    extra_exempt = sorted(set(exempt) - set(projection))
    if extra_exempt:
        raise IngressProjectionIncomplete(
            "{0}: exemption {1} names a field the projection table "
            "does not declare; the table is the schema.".format(
                table, ", ".join(extra_exempt)))


def _assert_payload_accounted(payload: Mapping[str, Any],
                              projection: Dict[str, Optional[str]],
                              exempt: Dict[str, str], table: str,
                              label: str) -> None:
    _assert_table_reasons(projection, exempt, table)
    unaccounted = sorted(set(payload) - set(projection))
    if unaccounted:
        raise IngressProjectionIncomplete(
            "{0}: {1} is declared in the IR but project_draft neither "
            "carries it onto the node nor lists it in {2} with a "
            "reason. A dropped field reads as a default somewhere "
            "downstream instead of failing here.".format(
                label, ", ".join(unaccounted), table))


def _tables_for(kind: str):
    if kind == "tests":
        return (
            (_LANE_PROJECTION_TESTS, _LANE_PROJECTION_TESTS_EXEMPT, "_LANE_PROJECTION"),
            (_VERIFIER_PROJECTION_TESTS, _VERIFIER_PROJECTION_TESTS_EXEMPT, "_VERIFIER_PROJECTION"),
            (_CLAIM_PROJECTION_TESTS, _CLAIM_PROJECTION_TESTS_EXEMPT, "_CLAIM_PROJECTION"),
            (_SEAM_PROJECTION_TESTS, _SEAM_PROJECTION_TESTS_EXEMPT, "_SEAM_PROJECTION"),
            (_FIXTURE_PROJECTION_TESTS, _FIXTURE_PROJECTION_TESTS_EXEMPT, "_FIXTURE_PROJECTION"),
        )
    return (
        (_LANE_PROJECTION, _LANE_PROJECTION_EXEMPT, "_LANE_PROJECTION"),
        (_VERIFIER_PROJECTION, _VERIFIER_PROJECTION_EXEMPT, "_VERIFIER_PROJECTION"),
        (_CLAIM_PROJECTION, _CLAIM_PROJECTION_EXEMPT, "_CLAIM_PROJECTION"),
        (_SEAM_PROJECTION, _SEAM_PROJECTION_EXEMPT, "_SEAM_PROJECTION"),
        (_FIXTURE_PROJECTION, _FIXTURE_PROJECTION_EXEMPT, "_FIXTURE_PROJECTION"),
    )


def _assert_ingress_projection_is_total(
        ir: Mapping[str, Any], lane: Mapping[str, Any],
        verifier: Mapping[str, Any], projected: Mapping[str, Any],
        maestro: Mapping[str, Any]) -> None:
    """Raise unless every field this lane binds is carried or exempted."""
    lane_id = projected.get("id")
    kind = projected["lane_kind"]
    (lane_proj, lane_exempt, lane_table), (ver_proj, ver_exempt, ver_table), (
        claim_proj, claim_exempt, claim_table), (seam_proj, seam_exempt, seam_table), (
        fix_proj, fix_exempt, fix_table) = _tables_for(kind)
    _assert_payload_accounted(
        lane, lane_proj, lane_exempt, lane_table, "lane {}".format(lane_id))
    _assert_payload_accounted(
        verifier, ver_proj, ver_exempt, ver_table,
        "verifier {}".format(verifier.get("verifier_id")))
    for requirement in _bound_records(
            ir, lane, "requirement_ids", "requirements", "requirement_id"):
        _assert_payload_accounted(
            requirement, _REQUIREMENT_PROJECTION,
            _REQUIREMENT_PROJECTION_EXEMPT, "_REQUIREMENT_PROJECTION",
            "requirement {}".format(requirement.get("requirement_id")))
    for claim in _bound_records(
            ir, lane, "claim_ids", "claims", "claim_id"):
        _assert_payload_accounted(
            claim, claim_proj, claim_exempt, claim_table,
            "claim {}".format(claim.get("claim_id")))
    for seam in _bound_records(
            ir, lane, "seam_ids", "seams", "seam_id"):
        _assert_payload_accounted(
            seam, seam_proj, seam_exempt, seam_table,
            "seam {}".format(seam.get("seam_id")))
    for fixture in _bound_records(
            ir, lane, "fixture_ids", "fixtures", "fixture_id"):
        _assert_payload_accounted(
            fixture, fix_proj, fix_exempt, fix_table,
            "fixture {}".format(fixture.get("fixture_id")))
    _assert_payload_accounted(
        maestro, _EXTENSION_PROJECTION, _EXTENSION_PROJECTION_EXEMPT,
        "_EXTENSION_PROJECTION", "extensions.maestro")

    if set(projected) != set(LANE_KEYS):
        raise IngressProjectionIncomplete(
            "lane {0}: projected keys {1!r} != LANE_KEYS".format(
                lane_id, sorted(projected)))

    def _fail(field: str, expected: object, actual: object) -> None:
        raise IngressProjectionIncomplete(
            "lane {0}: {1} is {2!r} in the IR but {3!r} on the projected "
            "lane; the projection carries the field name and not its "
            "value.".format(lane_id, field, expected, actual))

    if projected.get("id") != lane.get("lane_id"):
        _fail("id", lane.get("lane_id"), projected.get("id"))
    declared_needs = _require_id_list(
        lane.get("depends_on"), "UNMAPPABLE_LANES",
        "{}.depends_on".format(lane_id))
    if list(projected.get("needs")) != declared_needs:
        _fail("needs", declared_needs, projected.get("needs"))
    declared_outputs = maestro.get("outputs", {}).get(lane.get("lane_id"))
    if list(projected.get("outputs")) != list(declared_outputs or []):
        _fail("outputs", declared_outputs, projected.get("outputs"))
    spec = projected["spec"]
    if spec.get("gate", {}).get("cwd") != lane.get("execution_context"):
        _fail("spec.gate.cwd", lane.get("execution_context"),
              spec.get("gate", {}).get("cwd"))
    if spec.get("gate", {}).get("min_cases") != verifier.get("min_executed"):
        _fail("spec.gate.min_cases", verifier.get("min_executed"),
              spec.get("gate", {}).get("min_cases"))
    title = lane.get("title")
    instruction = spec.get("instruction") or ""
    if spec.get("goal") != title:
        _fail("spec.goal", title, spec.get("goal"))
    if isinstance(title, str) and title and title not in instruction:
        _fail("spec.instruction", title, instruction)
    for seam in _bound_records(ir, lane, "seam_ids", "seams", "seam_id"):
        seam_id = seam.get("seam_id")
        if isinstance(seam_id, str) and seam_id and seam_id not in instruction:
            _fail("spec.instruction", seam_id, instruction)
    bindings = spec.get("bindings") or {}
    carried = ["requirement_ids", "verifier_ids", "claim_ids", "seam_ids"]
    if kind == "tests":
        carried.append("fixture_ids")
    for name in carried:
        expected = _require_id_list(
            lane.get(name), "UNMAPPABLE_LANES",
            "{}.{}".format(lane_id, name))
        actual = bindings.get(name)
        if name not in bindings or list(actual) != expected:
            _fail("spec.bindings.{}".format(name), expected, actual)
    expected_reads = _lane_source_reads(ir, lane, verifier, lane_id)
    actual_reads = [item.get("source_id") for item in spec.get("sources") or []]
    if actual_reads != expected_reads:
        _fail("spec.sources", expected_reads, actual_reads)
    acceptance = projected.get("acceptance") or []
    expected_head = "{}: {}".format(
        verifier.get("verifier_id"), verifier.get("oracle"))
    if not acceptance or acceptance[0] != expected_head:
        _fail("acceptance[0]", expected_head,
              acceptance[0] if acceptance else None)
    claim_ids = _require_id_list(
        lane.get("claim_ids"), "UNMAPPABLE_LANES",
        "{}.claim_ids".format(lane_id))
    if len(acceptance) != 1 + len(claim_ids):
        _fail("len(acceptance)", 1 + len(claim_ids), len(acceptance))
    branch = maestro.get("integration_branch")
    if spec.get("integration", {}).get("integration_branch") != branch:
        _fail("spec.integration.integration_branch", branch,
              spec.get("integration", {}).get("integration_branch"))
    if kind == "tests":
        obligations = spec.get("obligations") or {}
        if [item.get("claim_id") for item in obligations.get("claims") or []] != claim_ids:
            _fail("obligations.claims", claim_ids,
                  [item.get("claim_id") for item in obligations.get("claims") or []])
        fixture_ids = _require_id_list(
            lane.get("fixture_ids"), "UNMAPPABLE_LANES",
            "{}.fixture_ids".format(lane_id))
        if [item.get("fixture_id") for item in obligations.get("observed_baseline") or []] != fixture_ids:
            _fail("obligations.observed_baseline", fixture_ids,
                  [item.get("fixture_id") for item in obligations.get("observed_baseline") or []])
        seam_ids = _require_id_list(
            lane.get("seam_ids"), "UNMAPPABLE_LANES",
            "{}.seam_ids".format(lane_id))
        if [item.get("seam_id") for item in obligations.get("seams") or []] != seam_ids:
            _fail("obligations.seams", seam_ids,
                  [item.get("seam_id") for item in obligations.get("seams") or []])
        paired = _paired_build_lanes(ir, lane.get("lane_id"))
        actual_paired = [
            item.get("lane_id") for item in obligations.get("for_build_lanes") or []
        ]
        if actual_paired != paired:
            _fail("obligations.for_build_lanes", paired, actual_paired)
    else:
        if "obligations" in spec:
            raise IngressProjectionIncomplete(
                "lane {0}: build spec carries obligations".format(lane_id))
        for key in bindings:
            if "fixture" in key:
                raise IngressProjectionIncomplete(
                    "lane {0}: build bindings carry {1}".format(lane_id, key))
    try:
        st._reject_private_keys(projected["spec"])
    except st.CanonicalIdentityError as exc:
        raise IngressProjectionIncomplete(
            "lane {0}: projected spec carries a private key ({1}); the "
            "projection controls every key it emits, so this is a "
            "projection defect".format(lane_id, exc)) from None


def project_draft(ir: Mapping[str, Any], repo: Path) -> dict:
    """Map one approved executable Plan IR onto a Maestro draft mapping."""
    del repo
    if ir.get("schema_version") != "plan-contract.v1":
        raise IngressError("IR_SCHEMA")
    kind = ir.get("plan_kind")
    if kind == "architecture":
        raise IngressError("ARCHITECTURE_NOT_EXECUTABLE")
    if kind not in EXECUTABLE_KINDS:
        raise IngressError("IR_PLAN_KIND:{}".format(kind))
    maestro = _maestro_extension(ir)
    _assert_payload_accounted(
        ir, _IR_PROJECTION, _IR_PROJECTION_EXEMPT, "_IR_PROJECTION", "ir")
    _assert_payload_accounted(
        ir["extensions"], _EXTENSIONS_PROJECTION, _EXTENSIONS_PROJECTION_EXEMPT,
        "_EXTENSIONS_PROJECTION", "extensions")
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

    for index, item in enumerate(verifiers):
        if not isinstance(item, dict):
            raise IngressError("UNMAPPABLE_VERIFIERS:verifier[{}]".format(index))
        verifier_id = _require_text(
            item.get("verifier_id"), "UNMAPPABLE_VERIFIERS",
            "verifier[{}].verifier_id".format(index))
        bound = _require_id_list(
            item.get("lane_ids"), "UNMAPPABLE_VERIFIERS",
            "{}.lane_ids".format(verifier_id))
        if not bound:
            raise IngressError(
                "UNMAPPABLE_VERIFIERS:{}.lane_ids".format(verifier_id))

    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise IngressError("UNMAPPABLE_SOURCES:{}".format(index))
        source_id = _require_text(
            source.get("source_id"), "UNMAPPABLE_SOURCES",
            "source[{}].source_id".format(index))
        path = _require_text(
            source.get("path"), "UNMAPPABLE_SOURCES",
            "{}.path".format(source_id))
        _require_relative(path, source_id)
        required = source.get("required")
        if required is not True:
            raise IngressError(
                "UNMAPPABLE_SOURCES:{}.required".format(source_id))
        digest = _require_text(
            source.get("sha256"), "UNMAPPABLE_SOURCES",
            "{}.sha256".format(source_id))
        if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest):
            raise IngressError(
                "UNMAPPABLE_SOURCES:{}.sha256".format(source_id))

    projected_lanes = []
    records = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise IngressError("UNMAPPABLE_LANES")
        lane_id = lane.get("lane_id")
        if not isinstance(lane_id, str) or not lane_id:
            raise IngressError("UNMAPPABLE_LANES")
        cwd = lane.get("execution_context")
        if not isinstance(cwd, str) or cwd in {"", "lane worktree root"}:
            raise IngressError("AMBIENT_CWD:{}".format(lane_id))
        _require_relative(cwd, lane_id) if cwd != "." else cwd
        outputs = outputs_by_lane.get(lane_id)
        if not isinstance(outputs, list) or not outputs:
            raise IngressError("UNMAPPABLE_OUTPUTS:{}".format(lane_id))
        for output in outputs:
            if not isinstance(output, str):
                raise IngressError("UNMAPPABLE_OUTPUTS:{}".format(lane_id))
            _require_relative(output, lane_id)
        lane_verifiers = [item for item in verifiers
                          if lane_id in item["lane_ids"]]
        if len(lane_verifiers) != 1:
            raise IngressError("UNMAPPABLE_VERIFIERS:{}".format(lane_id))
        verifier = lane_verifiers[0]
        lane_kind = _lane_kind(lane, lane_id)
        needs = _require_id_list(
            lane.get("depends_on"), "UNMAPPABLE_LANES",
            "{}.depends_on".format(lane_id))
        claims = _bound_records(ir, lane, "claim_ids", "claims", "claim_id")
        seams = _bound_records(ir, lane, "seam_ids", "seams", "seam_id")
        instruction = _node_instruction(ir, lane, lane_id)
        prose = _seam_prose(seams)
        if prose:
            instruction = instruction + "\n\n" + prose
        spec = {
            "goal": _require_text(
                lane.get("title"), "UNMAPPABLE_LANES",
                "{}.title".format(lane_id)),
            "instruction": instruction,
            "integration": {"integration_branch": branch},
            "gate": _gate(verifier, cwd, lane_id),
            "effects": _node_effects(ir, lane, lane_id),
            "sources": _sources_for(
                ir, _lane_source_reads(ir, lane, verifier, lane_id)),
            "seams": [
                {key: seam[key] for key in (
                    "seam_id", "producer", "consumer", "contract")}
                for seam in seams
            ],
            "bindings": _bindings(lane, lane_kind),
        }
        projected = {
            "id": lane_id,
            "needs": needs,
            "outputs": list(outputs),
            "lane_kind": lane_kind,
            "spec": spec,
            "acceptance": _acceptance(verifier, claims),
        }
        strength = verifier.get("test_strength")
        if lane_kind == "tests":
            if strength is None or not isinstance(strength, Mapping):
                raise IngressError(
                    "UNMAPPABLE_VERIFIERS:{0}.test_strength".format(lane_id))
        elif strength is not None:
            raise IngressError(
                "UNMAPPABLE_VERIFIERS:{}.test_strength".format(lane_id))
        projected_lanes.append(projected)
        records.append((lane, verifier, projected))

    _assert_binding_closure(ir, lanes)
    for lane, verifier, projected in records:
        if projected["lane_kind"] == "tests":
            paired_ids = _paired_build_lanes(ir, projected["id"])
            by_id = {item["id"]: item for item in projected_lanes}
            paired = [by_id[item] for item in paired_ids]
            projected["spec"]["obligations"] = _obligations(
                ir, lane, verifier, paired)
        _assert_ingress_projection_is_total(
            ir, lane, verifier, projected, maestro)

    spelled = [key for key in ("runner", "command") if key in integration]
    if len(spelled) != 1:
        raise IngressError("UNMAPPABLE_INTEGRATION:runner-or-command")
    if spelled[0] == "runner":
        _require_text(
            integration.get("runner"), "UNMAPPABLE_INTEGRATION", "runner")
        tuple(_require_id_list(
            integration.get("argv"), "UNMAPPABLE_INTEGRATION", "argv"))
    else:
        if "argv" in integration:
            raise IngressError("UNMAPPABLE_INTEGRATION:command-and-argv")
        _parse_verifier_command(integration["command"])
    ig_argv = None
    if spelled[0] == "runner":
        ig_argv = tuple(_require_id_list(
            integration.get("argv"), "UNMAPPABLE_INTEGRATION", "argv"))
    else:
        _ig_runner, ig_argv = _parse_verifier_command(integration["command"])
    if not ig_argv:
        raise IngressError("UNMAPPABLE_INTEGRATION:argv")
    if "cwd" in integration:
        ig_cwd = _require_text(
            integration.get("cwd"), "UNMAPPABLE_INTEGRATION", "cwd")
        if ig_cwd != ".":
            _require_relative(ig_cwd, "integration_gate")
    declared = [key for key in ("min_cases", "min_executed")
                if key in integration]
    if len(declared) != 1:
        raise IngressError("UNMAPPABLE_INTEGRATION:min_cases")
    _require_count(
        integration[declared[0]], "UNMAPPABLE_INTEGRATION", declared[0])
    _require_text(ir.get("plan_id"), "IR_SCHEMA", "plan_id")
    _require_text(ir.get("title"), "IR_SCHEMA", "title")
    return {"schema_version": SCHEMA_VERSION, "lanes": projected_lanes}


def project_canonical_plan(
        ir_path: Path, receipt_path: Path, repo: Path,
        rendered_path: Optional[Path] = None) -> Tuple[bytes, dict, dict]:
    """Verify the receipt and project, without writing anything."""
    ir_bytes = Path(ir_path).read_bytes()
    ir = _load_json(ir_path, "IR_UNREADABLE")
    receipt = _load_json(receipt_path, "RECEIPT_UNREADABLE")
    rendered = Path(rendered_path).read_bytes() if rendered_path else None
    _verify_receipt(ir_bytes, receipt, rendered)
    draft = project_draft(ir, repo)
    stored = plan_author.author_plan(draft)
    return stored, draft, ir


def author_from_plan_contract(
        ir_path: Path, receipt_path: Path, destination: Path, repo: Path,
        rendered_path: Optional[Path] = None) -> Tuple[bytes, dict]:
    """Verify the receipt, project, canonicalize, and write a Maestro plan."""
    stored, draft, ir = project_canonical_plan(
        ir_path, receipt_path, repo, rendered_path)
    receipt = _load_json(receipt_path, "RECEIPT_UNREADABLE")
    plan_author.write_canonical_plan(destination, stored)
    trace = {
        "plan_id": ir.get("plan_id"),
        "title": ir.get("title"),
        "repo": str(repo),
        "receipt_ir_sha256": receipt.get("ir_sha256"),
        "lanes": [lane["id"] for lane in draft["lanes"]],
        "sources": [item["source_id"] for item in ir["source_artifacts"]],
    }
    return stored, trace
