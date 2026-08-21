"""Receipt-verified projection from plan-contract.v1 to maestro-plan.v2.

This is an authoring boundary, not a lossy converter. Missing Maestro
extensions, ambient paths, receiptless input, and unparseable gates refuse.

**It emits v2, and the version moved because what this module puts in
`nodes[].instruction` changed meaning.** It used to write the lane's title
there and drop `requirements[].text` — the sentence that bounds the lane's
scope — so every builder and every reviewer downstream faithfully relayed a
summary of the contract instead of the contract (§19 M26). `_node_instruction`
below fixed that. Emitting `maestro-plan.v1` afterwards would have left a plan
projected before the fix byte-indistinguishable *in version* from one
projected after it, and a fully-fixed runtime would have executed the
degenerate one without a word: the field is populated either way, so no
consumer and no reader sweep can separate them. The version string is
the only channel that carries this, and
`maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS` is the reader that acts on it.

`_assert_ingress_projection_is_total` now guards this projection the
way `plan_model._assert_projection_is_total` guards the next one: a
lane-bound IR field with no destination and no named exemption is a
raise, not a silent drop (§3.6 B15, §19 M1, §19 M26, issue #96).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from . import plan_author
from . import plan_model
from . import plan_validate


EXECUTABLE_KINDS = frozenset({"implementation", "brownfield", "prd", "workflow"})
RECEIPT_VERSION = "plan-contract-review.v1"


class IngressError(plan_author.AuthoringError):
    """A plan-contract package cannot become a Maestro plan.

    `blockers` carries the typed admission blockers when the refusal is an
    admission refusal, and is empty otherwise. The joined message stays for an
    operator reading a terminal; a caller that needs to branch on which
    obligation fired reads the tuple, because thirteen blockers rendered into
    one string is a wall no code can take apart and `validate_plan` already
    returns its blockers typed.
    """

    def __init__(self, message: str,
                 blockers: Sequence["plan_validate.AdmissionBlocker"] = ()
                 ) -> None:
        super().__init__(message)
        self.blockers: Tuple["plan_validate.AdmissionBlocker", ...] = tuple(
            blockers)


class IngressProjectionIncomplete(RuntimeError):
    """`project_draft` did not account for a field the IR declares.

    The same shape as `plan_model.ProjectionIncomplete`, one projection
    earlier. A declared field with no destination reads as a default
    downstream instead of failing here (§3.6 B15, §19 M1, §19 M26).
    """


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
    """A field the projection carries verbatim: present, a string, non-empty.

    `x.get("k") or <fallback>` is three different bugs wearing one operator.
    It picks quietly between synonyms, it invents a value the plan never
    declared, and it treats a deliberate falsy value — `0`, `""`, `[]` — as
    absence. §7.4 records what the second one costs: a per-gate threshold the
    destination had no field for was dropped, every gate in every run was
    adjudicated against a default of 1 while plans declared 70, and a run
    reached ACCEPTED that way. This function and its siblings below are how
    that operator stops appearing on this path.
    """
    if not isinstance(value, str) or not value:
        raise IngressError("{}:{}".format(code, label))
    return value


def _require_count(value: object, code: str, label: str) -> int:
    """A threshold the adjudicator will count against. `bool` is not an `int`
    here even though Python says otherwise: `min_cases: true` is a typo, not a
    demand for one passing case."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IngressError("{}:{}".format(code, label))
    return value


def _require_id_list(value: object, code: str, label: str) -> list:
    """A list of ids, or nothing. Absence is legal and means the empty list;
    a malformed value is not absence.

    `list(lane.get("depends_on") or [])` accepted a *string* and spelled it
    out one character per dependency, so `"depends_on": "lane-a"` projected
    six phantom node ids. And a filtered comprehension over `source_ids`
    dropped every non-string entry without a word, so evidence a lane declared
    it reads simply vanished from `reads`.
    """
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
    """`{requirement_id: requirement}` over the IR's declared requirements.

    One index, built once and read by both consumers below. Two indexes built
    from the same list are two chances to disagree about which record a lane's
    `requirement_ids` names, and this projection now resolves that binding
    twice — once for the effects a requirement authorises, once for the words
    that bound the node's scope.

    A malformed entry is skipped rather than refused here. Both callers refuse
    in their own vocabulary when the id they were looking for is missing, and
    a refusal raised from the index would name the plan's requirement list
    instead of the lane whose contract is incomplete.
    """
    declared = ir.get("requirements")
    index: dict = {}
    for item in declared if isinstance(declared, list) else ():
        if not isinstance(item, dict):
            continue
        requirement_id = item.get("requirement_id")
        if isinstance(requirement_id, str) and requirement_id:
            index.setdefault(requirement_id, item)
    return index


#: How a requirement's own words are labelled inside a node's instruction.
#: A label rather than a bare paste: the builder and the reviewer both need to
#: know that what follows is the plan's transcribed requirement and not a
#: paraphrase written for them, because the whole value of the text is that it
#: is the sentence the plan author actually wrote.
REQUIREMENT_HEADING = "Requirement {0}, in the plan's own words:"


def _node_instruction(ir: Mapping[str, Any], lane: Mapping[str, Any],
                      lane_id: str) -> str:
    """The lane's title as a label, then the requirement text that bounds it.

    §3.6 B9 makes the reviewer's input a declared contract — goal, `produces`,
    acceptance — and §19 M1 records the cost of a projection that dropped the
    goal: every agent-node reviewer in every run was told "make the gate pass"
    and nothing else. This is that same defect one projection earlier. The
    field was populated, so nothing downstream could tell a summary from the
    contract, and every consumer faithfully relayed a lossy value.

    Measured over the four executable plans in the `lexgenius-pipeline`
    deployment, 2,256 bytes of lane titles stood in for 18,824 bytes of
    `requirements[].text` across 51 agent nodes. `lane-p4-enrichment-ordering`
    carried 199 bytes of a 971-byte requirement, and the dropped remainder is
    the sentence deferring the production wiring to a later plan. Its reviewer
    rejected the diff six times for omitting exactly that wiring, on
    `diff.implements_the_stated_instruction`, and burned the lane's whole
    review ceiling.

    Of the three shapes issue #87 lists, this is the third, and the other two
    are refused on the design's own terms:

    * A bare `requirement_id` on the node makes the bounding text a *reference*
      to bytes that live outside the plan. `maestro-plan.v1` is frozen from the
      moment it ships and its bytes are its identity (§6.3) — that is why a
      `PromptAsset` is carried by content digest. Text recoverable only from
      the authoring IR is text that can change without changing the plan
      digest, and the IR "takes no further part" after this projection.
    * `prompt_assets` is a channel with no runtime reader. `plan_model`'s
      `_NODE_PROJECTION_EXEMPT` records the reason in the code — "consumed
      while authoring the plan, not while running it; nothing on the
      scheduler's side reads one" — and a `PromptAsset` is a `path` plus a
      `sha256`, so routing text through it would mean writing files from a
      projection documented as a pure function of the IR, the receipt and the
      repository, *and* building the reader that does not exist. A channel
      with no writers is B15's field with no readers; the repair is not to
      write into it, it is to use the channel that already has both.

    `instruction` has both. `maestro._agent_node_prompt` opens the builder's
    prompt with it and `code_review._node_goal` is the reviewer's declared
    goal, so widening this one field reaches both halves of the contract with
    no new channel, no new field, and no new reader to keep in step.

    One recorded objection is answered rather than ignored.
    `scheduler_types.PlanNode.effects` says handing the reviewer the
    requirement's own text "was considered and declined", because the text of
    `lane-p1-canonical-object-key` said both "pure derivation and policy
    module" and "server-side copy it", which would put the reviewer in the
    builder's position adjudicating a contradiction. That objection was about
    a *self-contradictory* requirement, and admission now refuses exactly that
    IR: `EFFECT_AUTHORIZED` blocks a requirement that prescribes an effect its
    own plan forbids, and it runs below, before any plan file is written. The
    contradiction the objection feared cannot reach a node any more, so what
    survives admission is text a reviewer can read without weighing one clause
    against another. `effects` stays typed and stays carried; this adds the
    scope the typed field was never able to express — which lane owns which
    work, and which work a later plan owns.

    §1.2 is unaffected. No lifecycle transition keys on these bytes: they are
    plan content, canonicalized and digested like every other field, read by
    the two prompt builders and by nothing that moves a node between states.
    """
    title = _require_text(
        lane.get("title"), "UNMAPPABLE_LANES", "{}.title".format(lane_id))
    bound = _require_id_list(
        lane.get("requirement_ids"), "UNMAPPABLE_LANES",
        "{}.requirement_ids".format(lane_id))
    index = _requirements_by_id(ir)
    blocks = [title]
    seen = set()
    for requirement_id in bound:
        # Declared order, first occurrence only. The plan's bytes are its
        # identity, so the instruction must be a pure function of the IR: a
        # set here would order it by hash and a duplicate id would paste one
        # requirement twice.
        if requirement_id in seen:
            continue
        seen.add(requirement_id)
        requirement = index.get(requirement_id)
        if not isinstance(requirement, dict):
            # The lane binds an id the plan does not declare. Refused rather
            # than skipped: skipping is how the title became the whole brief.
            raise IngressError(
                "UNMAPPABLE_REQUIREMENTS:{}.requirement_ids:{}".format(
                    lane_id, requirement_id))
        text = _require_text(
            requirement.get("text"), "UNMAPPABLE_REQUIREMENTS",
            "{}.text".format(requirement_id)).strip()
        if not text:
            # `_require_text` refuses `""`; a requirement whose text is only
            # whitespace is the same absence spelled differently, and it would
            # otherwise project a heading with nothing under it.
            raise IngressError(
                "UNMAPPABLE_REQUIREMENTS:{}.text".format(requirement_id))
        blocks.append("{0}\n{1}".format(
            REQUIREMENT_HEADING.format(requirement_id), text))
    # A lane binding no requirement at all is deliberately not refused here.
    # Admission's `SURFACE_REACHABLE` already refuses it below, naming the
    # output no requirement claims — a message about the plan's requirement
    # coverage, which is what is actually wrong. Refusing here first would
    # replace that with a worse one and would send an author back twice for
    # two defects in one document, which §11.1 rejects.
    return "\n\n".join(blocks)


def _node_effects(ir: Mapping[str, Any], lane: Mapping[str, Any]) -> list:
    """The lane's dispositions toward each act the plan forbids.

    A lane's effects are the union of the requirements it binds. Admission has
    already refused two requirements on one lane that disagree about an
    effect, so the union is well defined by the time this runs — the first
    disposition found is the only one there is.

    Only prohibited effects are carried. A disposition toward an act the plan
    does not forbid is not a prohibition, and every prohibited effect has a
    transcribed `meaning`, so every projected record is complete by
    construction rather than by a later check.
    """
    extensions = ir.get("extensions")
    maestro = extensions.get("maestro") if isinstance(extensions, dict) else {}
    declared = maestro.get("prohibited_effects") if isinstance(maestro, dict) else None
    meanings = {}
    for entry in declared if isinstance(declared, list) else ():
        if isinstance(entry, dict) and isinstance(entry.get("effect"), str):
            meaning = entry.get("meaning")
            if isinstance(meaning, str) and meaning.strip():
                meanings[entry["effect"]] = meaning.strip()
    if not meanings:
        return []
    by_id = _requirements_by_id(ir)
    dispositions = {}
    for requirement_id in lane.get("requirement_ids") or []:
        requirement = by_id.get(requirement_id)
        if not isinstance(requirement, dict):
            continue
        for entry in requirement.get("effects") or []:
            if not isinstance(entry, dict):
                continue
            effect, disposition = entry.get("effect"), entry.get("disposition")
            if effect in meanings and isinstance(disposition, str):
                dispositions.setdefault(effect, disposition)
    return [{"effect": effect, "disposition": dispositions[effect],
             "meaning": meanings[effect]}
            for effect in sorted(dispositions)]


#: Kind this projection emits. `maestro-plan.v3` added `tests`, but
#: plan-contract.v1 has no tests/build split, and inventing one here would
#: change every shipped lane's graph (`plan_model.SCHEMA_V3`).
_EMITTED_NODE_KIND = "agent"

#: Node kinds a `maestro-plan` can declare that this projection does not
#: emit, each with the reason. Checked so a kind PR #124 added cannot hide
#: from the guard by not being the one kind we currently write.
_UNEMITTED_NODE_KINDS: Dict[str, str] = {
    "tests": ("plan-contract.v1 has no tests/build split; SCHEMA_V3 is "
              "authored in maestro-plan, not projected from the IR"),
    "code": ("a plan-contract lane is agent work; a code node's command is "
             "not a lane and has no IR binding to project"),
}

#: Destination node types the next projection (`to_plan_nodes`) accepts.
#: The guard covers every kind, not only `agent`, so a TestsNode field
#: cannot be dropped by being out of this module's current emit set.
_DESTINATION_NODE_TYPES: Tuple[type, ...] = (
    plan_model.AgentNode, plan_model.CodeNode, plan_model.TestsNode)


#: Lane fields this projection carries onto the node, or `None` if dropped.
#: Completeness is this table, not a list of the six known drops: a field
#: added to a lane later with neither a destination nor an exemption
#: raises. Follows `_GATE_PROJECTION`.
_LANE_PROJECTION: Dict[str, Optional[str]] = {
    "lane_id": "node_id",
    "title": "instruction",
    "execution_context": "cwd",
    "depends_on": "needs",
    "requirement_ids": "instruction",
    "verifier_ids": None,
    "claim_ids": None,
    "seam_ids": None,
    "fixture_ids": None,
}

_LANE_PROJECTION_EXEMPT: Dict[str, str] = {
    "verifier_ids": (
        "the gate is bound by verifiers[].lane_ids, which this "
        "projection already requires to name exactly one verifier; "
        "lane.verifier_ids is the reverse index and carrying both would "
        "be two answers for one fact"),
    "claim_ids": (
        "consumed by plan_validate's CLAIM_UNWITNESSABLE obligation at "
        "admission, deliberately not carried into the plan"),
    "seam_ids": (
        "seams are authoring-time producer/consumer/contract prose; "
        "maestro-plan has no seam type, and pasting them into a prompt "
        "is a channel with no runtime reader (§3.6 B15 the other way)"),
    "fixture_ids": (
        "fixtures are authoring-time records; CLAIM_UNWITNESSABLE reads "
        "typed witness cells, not fixture prose, and maestro-plan has "
        "no fixture type to carry one into"),
}


#: Bound `requirements[]` fields. `source_ids` lands on `reads` so a
#: requirement cannot name evidence its node then silently cannot read.
_REQUIREMENT_PROJECTION: Dict[str, Optional[str]] = {
    "requirement_id": "requirement_ids",
    "text": "instruction",
    "source_ids": "reads",
    "surface": None,
    "effects": "effects",
}

_REQUIREMENT_PROJECTION_EXEMPT: Dict[str, str] = {
    "surface": (
        "consumed by admission SURFACE_DECLARED and SURFACE_REACHABLE; "
        "maestro-plan has no surface field, and the reader is admission "
        "rather than the node"),
}


#: Bound `verifiers[]` fields. Gate carries runner, argv, cwd, min_cases
#: only; oracle/falsifiability/independent have no destination there.
_VERIFIER_PROJECTION: Dict[str, Optional[str]] = {
    "verifier_id": "gate",
    "lane_ids": "gate",
    "command": "gate",
    "min_executed": "min_cases",
    "source_ids": "reads",
    "oracle": None,
    "falsifiability": None,
    "independent": None,
    "requirement_ids": None,
    "fixture_ids": None,
    "seam_ids": None,
    "claim_ids": None,
}

_VERIFIER_NOT_CARRIED = (
    "Gate carries runner, argv, cwd and min_cases only. This field is "
    "free text or an authoring reverse-index; §1.2 forbids a transition "
    "keyed on prose, and CLAIM_UNWITNESSABLE is the typed stand-in for "
    "falsifiability at admission")

_VERIFIER_PROJECTION_EXEMPT: Dict[str, str] = {
    "oracle": _VERIFIER_NOT_CARRIED,
    "falsifiability": _VERIFIER_NOT_CARRIED,
    "independent": _VERIFIER_NOT_CARRIED,
    "requirement_ids": (
        "reverse index of the lane's requirement_ids, already carried "
        "via instruction, effects and reads; a second copy is two answers"),
    "fixture_ids": (
        "reverse index of the lane's fixture_ids, which are themselves "
        "exempt; carrying the copy would be a destination the original "
        "binding does not have"),
    "seam_ids": (
        "reverse index of the lane's seam_ids, which are themselves "
        "exempt; carrying the copy would be a destination the original "
        "binding does not have"),
    "claim_ids": (
        "reverse index of the lane's claim_ids, consumed at admission "
        "by CLAIM_UNWITNESSABLE and not carried into the plan"),
}


_CLAIM_NOT_CARRIED = (
    "consumed by plan_validate's CLAIM_UNWITNESSABLE obligation at "
    "admission, deliberately not carried into the plan")

_CLAIM_PROJECTION: Dict[str, Optional[str]] = {
    "claim_id": None,
    "kind": None,
    "mutation_kinds": None,
    "object": None,
    "polarity": None,
    "predicate": None,
    "rendered_binding_ids": None,
    "source_ids": None,
    "source_requirement_ids": None,
    "subject": None,
    "unit": None,
    "value": None,
    "verifier_ids": None,
    "witness": None,
}
_CLAIM_PROJECTION_EXEMPT: Dict[str, str] = {
    name: _CLAIM_NOT_CARRIED for name in _CLAIM_PROJECTION}


_SEAM_NOT_CARRIED = (
    "seams are authoring-time producer/consumer/contract prose; "
    "maestro-plan has no seam type, and pasting them into a prompt is "
    "a channel with no runtime reader (§3.6 B15 the other way)")

_SEAM_PROJECTION: Dict[str, Optional[str]] = {
    "seam_id": None,
    "contract": None,
    "producer": None,
    "consumer": None,
    "claim_ids": None,
    "fixture_ids": None,
    "requirement_ids": None,
    "source_ids": None,
    "verifier_ids": None,
}
_SEAM_PROJECTION_EXEMPT: Dict[str, str] = {
    name: _SEAM_NOT_CARRIED for name in _SEAM_PROJECTION}


_FIXTURE_NOT_CARRIED = (
    "fixtures are authoring-time records; CLAIM_UNWITNESSABLE reads "
    "typed witness cells, not fixture prose, and maestro-plan has no "
    "fixture type to carry one into")

_FIXTURE_PROJECTION: Dict[str, Optional[str]] = {
    "fixture_id": None,
    "affected_lane_ids": None,
    "consumer_obligation": None,
    "meaning": None,
    "observed_value": None,
    "path": None,
    "producer_metadata": None,
    "prohibited_behavior": None,
    "record_selector": None,
    "seam_ids": None,
    "source_id": None,
    "verifier_ids": None,
}
_FIXTURE_PROJECTION_EXEMPT: Dict[str, str] = {
    name: _FIXTURE_NOT_CARRIED for name in _FIXTURE_PROJECTION}


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
    """Union of the verifier's source_ids and each bound requirement's.

    `reads` used to be built from the verifier alone, so a requirement
    that named evidence its node then could not read was invisible:
    the field was populated, and `reads_are_sufficient` passed on the
    ids that did arrive (issue #96 instance 2, the #87 sub-shape).
    """
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


def _assert_ingress_projection_is_total(
        ir: Mapping[str, Any], lane: Mapping[str, Any],
        verifier: Mapping[str, Any], node: Mapping[str, Any]) -> None:
    """Raise unless every field this lane binds is carried or exempted."""
    lane_id = node.get("node_id")
    _assert_payload_accounted(
        lane, _LANE_PROJECTION, _LANE_PROJECTION_EXEMPT,
        "_LANE_PROJECTION", "lane {}".format(lane_id))
    _assert_payload_accounted(
        verifier, _VERIFIER_PROJECTION, _VERIFIER_PROJECTION_EXEMPT,
        "_VERIFIER_PROJECTION",
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
            claim, _CLAIM_PROJECTION, _CLAIM_PROJECTION_EXEMPT,
            "_CLAIM_PROJECTION", "claim {}".format(claim.get("claim_id")))
    for seam in _bound_records(
            ir, lane, "seam_ids", "seams", "seam_id"):
        _assert_payload_accounted(
            seam, _SEAM_PROJECTION, _SEAM_PROJECTION_EXEMPT,
            "_SEAM_PROJECTION", "seam {}".format(seam.get("seam_id")))
    for fixture in _bound_records(
            ir, lane, "fixture_ids", "fixtures", "fixture_id"):
        _assert_payload_accounted(
            fixture, _FIXTURE_PROJECTION, _FIXTURE_PROJECTION_EXEMPT,
            "_FIXTURE_PROJECTION",
            "fixture {}".format(fixture.get("fixture_id")))

    kind = node.get("kind")
    if kind != _EMITTED_NODE_KIND:
        reason = _UNEMITTED_NODE_KINDS.get(kind, "")
        raise IngressProjectionIncomplete(
            "lane {0}: project_draft emitted kind {1!r}, which is not "
            "{2!r}. Unemitted kinds are named in _UNEMITTED_NODE_KINDS"
            "{3}.".format(
                lane_id, kind, _EMITTED_NODE_KIND,
                (": " + reason) if reason else " (this one is not)"))

    # AgentNode and TestsNode share a launch shape; a field TestsNode
    # declares that the emitted node does not carry is the #96 drop
    # arriving on a kind this projection does not yet emit.
    emitted_names = set(node)
    for cls in (plan_model.AgentNode, plan_model.TestsNode):
        if cls not in _DESTINATION_NODE_TYPES:
            raise IngressProjectionIncomplete(
                "{0} is not in _DESTINATION_NODE_TYPES; the guard must "
                "cover every node kind, including those PR #124 "
                "added.".format(cls.__name__))
        missing = sorted(set(cls.model_fields) - emitted_names)
        if missing:
            raise IngressProjectionIncomplete(
                "lane {0}: {1} declares {2} which the emitted node "
                "does not carry. A new field on a destination kind is "
                "a raise, not a silent drop.".format(
                    lane_id, cls.__name__, ", ".join(missing)))

    if node.get("node_id") != lane.get("lane_id"):
        raise IngressProjectionIncomplete(
            "lane {0}: lane_id is {1!r} in the IR but node_id is {2!r} "
            "on the projected node; the projection carries the field "
            "name and not its value.".format(
                lane_id, lane.get("lane_id"), node.get("node_id")))
    title = lane.get("title")
    instruction = node.get("instruction") or ""
    if isinstance(title, str) and title and title not in instruction:
        raise IngressProjectionIncomplete(
            "lane {0}: title {1!r} is not in the projected "
            "instruction; the projection carries the field name and "
            "not its value.".format(lane_id, title))
    needs = node.get("needs")
    declared_needs = _require_id_list(
        lane.get("depends_on"), "UNMAPPABLE_LANES",
        "{}.depends_on".format(lane_id))
    if list(needs) != declared_needs:
        raise IngressProjectionIncomplete(
            "lane {0}: depends_on is {1!r} in the IR but needs is {2!r} "
            "on the projected node; the projection carries the field "
            "name and not its value.".format(
                lane_id, declared_needs, needs))
    cwd = (node.get("gate") or {}).get("cwd")
    if cwd != lane.get("execution_context"):
        raise IngressProjectionIncomplete(
            "lane {0}: execution_context is {1!r} in the IR but "
            "gate.cwd is {2!r} on the projected node; the projection "
            "carries the field name and not its value.".format(
                lane_id, lane.get("execution_context"), cwd))
    expected_reads = _lane_source_reads(ir, lane, verifier, lane_id)
    if list(node.get("reads") or []) != expected_reads:
        raise IngressProjectionIncomplete(
            "lane {0}: bound source_ids union to {1!r} but the "
            "projected node carries reads {2!r}; the projection "
            "carries the field name and not its value.".format(
                lane_id, expected_reads, node.get("reads")))
    min_cases = (node.get("gate") or {}).get("min_cases")
    if min_cases != verifier.get("min_executed"):
        raise IngressProjectionIncomplete(
            "lane {0}: the verifier declares min_executed={1}, the "
            "projected node carries min_cases={2}.".format(
                lane_id, verifier.get("min_executed"), min_cases))


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

    # Every verifier is validated once, here, rather than absorbed by an
    # `or []` inside the per-lane comprehension below. A malformed `lane_ids`
    # there matched no lane, so the *lane* refused for having no verifier while
    # the verifier that was actually malformed went unnamed — fail-closed, but
    # pointing at the wrong object, which is how a plan defect gets read as a
    # missing binding and edited in the wrong place.
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

    evidence = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise IngressError("UNMAPPABLE_SOURCES:{}".format(index))
        # `source["source_id"]` was a bare index guarded by nothing, so an IR
        # omitting it left this boundary as an untyped `KeyError` rather than
        # as a refusal naming what was unmappable.
        source_id = _require_text(
            source.get("source_id"), "UNMAPPABLE_SOURCES",
            "source[{}].source_id".format(index))
        path = _require_text(
            source.get("path"), "UNMAPPABLE_SOURCES",
            "{}.path".format(source_id))
        _require_relative(path, source_id)
        # `required` had no reader at all, so a source declared optional was
        # projected as an ordinary `Observed` and then refused downstream with
        # `OBSERVED_PATH_ABSENT` if it happened to be missing — a refusal about
        # a file, for a plan defect. Maestro's `Observed` evidence has no
        # optional form: its path must exist at base and hash. §12.3 makes
        # that a loud refusal rather than a silent upgrade to required.
        required = source.get("required")
        if required is not True:
            raise IngressError(
                "UNMAPPABLE_SOURCES:{}.required".format(source_id))
        # The pin, carried rather than dropped. `docs/plan-authoring.md` makes
        # a hash-pinned `source_artifacts` entry the only way a document enters
        # the pipeline, and the projection was discarding the hash — so the
        # plan's `Observed.sha256` was filled from the repository by
        # `plan_author.fill_git_facts` and the IR's declaration was never
        # compared to anything. Carrying it is what arms
        # `plan_validate`'s EVIDENCE_TYPED_AGAINST_GIT obligation, and
        # `fill_git_facts` now refuses `OBSERVED_DIGEST_MISMATCH` before any
        # plan file is written.
        #
        # The comparison deliberately does not happen here. It needs the blob
        # at the base commit, `fill_git_facts` already reads exactly that, and
        # a second copy of the rule at this boundary would be the same fact in
        # two places — resolved from HEAD twice, which is also two answers if
        # HEAD moves between them.
        digest = _require_text(
            source.get("sha256"), "UNMAPPABLE_SOURCES",
            "{}.sha256".format(source_id))
        if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest):
            raise IngressError(
                "UNMAPPABLE_SOURCES:{}.sha256".format(source_id))
        evidence.append({
            "kind": "observed",
            "evidence_id": source_id,
            "path": path,
            "sha256": digest,
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
        lane_verifiers = [item for item in verifiers
                          if lane_id in item["lane_ids"]]
        if len(lane_verifiers) != 1:
            raise IngressError("UNMAPPABLE_VERIFIERS:{}".format(lane_id))
        verifier = lane_verifiers[0]
        runner, argv = _parse_verifier_command(verifier.get("command"))
        if not argv:
            raise IngressError("BROAD_GATE:{}".format(verifier.get("verifier_id")))
        min_cases = _require_count(
            verifier.get("min_executed"), "UNMAPPABLE_VERIFIERS", lane_id)
        source_reads = _lane_source_reads(ir, lane, verifier, lane_id)
        needs = _require_id_list(
            lane.get("depends_on"), "UNMAPPABLE_LANES",
            "{}.depends_on".format(lane_id))
        # The node's `instruction` — the prompt the builder works from and the
        # goal the reviewer judges against. This field has been widened twice
        # now, each time because what it carried was smaller than the brief:
        # first from the lane id, which handed an agent the string
        # `lane-freeze` as its whole brief, then from the lane's title, which
        # is a headline for a requirement rather than the requirement. It now
        # carries the title as a label and the bound requirements' own words
        # beneath it; `_node_instruction` records why, and why the two
        # cheaper shapes were declined.
        instruction = _node_instruction(ir, lane, lane_id)
        node = {
            "kind": _EMITTED_NODE_KIND,
            "node_id": lane_id,
            "needs": needs,
            "reads": source_reads,
            "outputs": list(outputs),
            "instruction": instruction,
            "gate": {
                "runner": runner,
                "argv": list(argv),
                "cwd": cwd,
                "min_cases": min_cases,
            },
            "prompt_assets": [],
            # What the code inside this node may do. The reviewer's contract
            # answered where work could happen and nothing answered this, so a
            # node told only "make the gate pass over these outputs" judged an
            # executing materializer compliant.
            "effects": _node_effects(ir, lane),
        }
        _assert_ingress_projection_is_total(ir, lane, verifier, node)
        nodes.append(node)

    # Admission, before anything is written and therefore before a run can
    # start. Two predicates over two domains, both answering "can any correct
    # attempt satisfy this contract?": a lane whose requirement names a
    # repository path the lane cannot write, and a requirement that prescribes
    # an external act its own plan forbids. Both were paid for by the same run,
    # in which one node could not write the file its behaviour needed and
    # another was told in one paragraph to be a pure derivation module and to
    # perform a server-side copy. Both read declared paths, declared
    # enumerated values, declared ids, and the depends_on graph — no free
    # text, §1.2.
    #
    # Both blocker sets are collected into one refusal rather than raised in
    # turn: an author sent back twice for two defects in one document is the
    # fail-fast validator §11.1 rejects.
    #
    # It runs here rather than on one caller because this is the chokepoint
    # every route crosses: `plan author --from-plan-contract` and `plan ship`
    # both reach a plan file only through `project_draft`. §19 M6 is the
    # recorded cost of siting a check on a single launch path instead.
    #
    # It runs after the per-lane loop so that a malformed lane, verifier or
    # output set is still refused in its own vocabulary; a surface blocker
    # about a lane whose outputs never parsed would name the wrong defect.
    admission_blockers = plan_validate.validate_admission(ir)
    if admission_blockers:
        raise IngressError(
            "ADMISSION_REFUSED:" + " | ".join(
                "{} {} {}".format(blocker.obligation.value, blocker.pointer,
                                  blocker.message)
                for blocker in admission_blockers),
            admission_blockers)

    # §8.8's one integration gate, in exactly one of two forms. It used to
    # admit three overlapping ones — `runner` plus `argv`, a `command` line to
    # parse, or `argv` treated as that command line — chosen by an `or` chain,
    # so `argv` meant the gate's *selector* on one branch and the whole
    # command including its binary on the other, and an IR carrying both a
    # `runner` and a `command` had the `command` silently ignored.
    spelled = [key for key in ("runner", "command") if key in integration]
    if len(spelled) != 1:
        raise IngressError("UNMAPPABLE_INTEGRATION:runner-or-command")
    if spelled[0] == "runner":
        ig_runner = _require_text(
            integration.get("runner"), "UNMAPPABLE_INTEGRATION", "runner")
        ig_argv = tuple(_require_id_list(
            integration.get("argv"), "UNMAPPABLE_INTEGRATION", "argv"))
    else:
        if "argv" in integration:
            raise IngressError("UNMAPPABLE_INTEGRATION:command-and-argv")
        ig_runner, ig_argv = _parse_verifier_command(integration["command"])
    if not ig_argv:
        raise IngressError("UNMAPPABLE_INTEGRATION:argv")
    # The only judged-legitimate default on this path, and it is keyed on the
    # key's absence rather than on its falsiness. `.` is not a guess about what
    # the author meant: §8.8 runs this gate once over the integrated tree, and
    # the repository root is the only place that tree is whole. An empty
    # string is a malformed value rather than an omission and refuses, and a
    # declared cwd is now validated — it never was, so the integration gate
    # was the one path on which `../elsewhere` reached a plan unchecked while
    # every lane cwd beside it was refused `AMBIENT_PATH`.
    if "cwd" in integration:
        ig_cwd = _require_text(
            integration.get("cwd"), "UNMAPPABLE_INTEGRATION", "cwd")
        if ig_cwd != ".":
            _require_relative(ig_cwd, "integration_gate")
    else:
        ig_cwd = "."
    # One spelling, required, counted. Two accepted keys with `or` between
    # them made a disagreement invisible, made an explicit `0` read as absent,
    # and made an integration gate that declared no threshold adjudicate at 1
    # — §7.4's failure exactly, at the one gate that speaks for the whole
    # tree. The lane verifier above has always refused this way; this is that
    # rule applied to the gate every lane ends at.
    declared = [key for key in ("min_cases", "min_executed")
                if key in integration]
    if len(declared) != 1:
        raise IngressError("UNMAPPABLE_INTEGRATION:min_cases")
    ig_min = _require_count(
        integration[declared[0]], "UNMAPPABLE_INTEGRATION", declared[0])
    plan_id = _require_text(ir.get("plan_id"), "IR_SCHEMA", "plan_id")
    draft = {
        # Read from the model rather than spelled here: the constant and the
        # registered parser class are the same fact, and a literal beside
        # them is a second place for it to be wrong.
        "schema_version": plan_model.SCHEMA_V2,
        "plan_id": plan_id,
        "intent": _require_text(ir.get("title"), "IR_SCHEMA", "title"),
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
    # Absent means "the repository this is being authored in", and
    # `plan_author.fill_git_facts` already resolves exactly that from the
    # `repo` it is handed. Resolving it here too would be one fact in two
    # places, disagreeing the first time either moved, so the key is omitted
    # rather than filled twice — RC1's shape, at the smallest scale it comes in.
    if "repo" in maestro:
        draft["repo"] = _require_text(
            maestro["repo"], "MAESTRO_EXTENSION_MISSING", "repo")
    return draft


def project_canonical_plan(
        ir_path: Path, receipt_path: Path, repo: Path,
        rendered_path: Optional[Path] = None) -> Tuple[bytes, dict, dict]:
    """Verify the receipt and project, without writing anything.

    Split out from `author_from_plan_contract` so a caller can learn what the
    plan *would* be before deciding whether to write it. `plan author` is
    create-once by design -- the bytes are the plan's identity, and silently
    overwriting them would change a digest out from under whatever already
    refers to it -- but that left `plan ship` unable to resume: a ship whose
    finalize step failed could not be re-run, because its author step refused
    with `PLAN_EXISTS` against the file it had itself written moments earlier.
    Projection is a pure function of the IR, the receipt, and the repository,
    so computing it twice costs nothing and decides the question exactly.
    """
    ir_bytes = Path(ir_path).read_bytes()
    ir = _load_json(ir_path, "IR_UNREADABLE")
    receipt = _load_json(receipt_path, "RECEIPT_UNREADABLE")
    rendered = Path(rendered_path).read_bytes() if rendered_path else None
    _verify_receipt(ir_bytes, receipt, rendered)
    draft = project_draft(ir, repo)
    stored = plan_author.author_plan(draft, repo)
    return stored, draft, ir


def author_from_plan_contract(
        ir_path: Path, receipt_path: Path, destination: Path, repo: Path,
        rendered_path: Optional[Path] = None) -> Tuple[bytes, dict]:
    """Verify the receipt, project, canonicalize, and write maestro-plan.v2."""
    stored, draft, ir = project_canonical_plan(
        ir_path, receipt_path, repo, rendered_path)
    receipt = _load_json(receipt_path, "RECEIPT_UNREADABLE")
    plan_author.write_canonical_plan(destination, stored)
    trace = {
        "plan_id": ir.get("plan_id"),
        "receipt_ir_sha256": receipt.get("ir_sha256"),
        "lanes": [node["node_id"] for node in draft["nodes"]],
        "sources": [item["evidence_id"] for item in draft["evidence"]
                    if item["kind"] == "observed"],
    }
    return stored, trace
