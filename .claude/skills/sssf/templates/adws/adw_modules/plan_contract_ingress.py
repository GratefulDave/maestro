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
        source_reads = _require_id_list(
            verifier.get("source_ids"), "UNMAPPABLE_VERIFIERS",
            "{}.source_ids".format(lane_id))
        needs = _require_id_list(
            lane.get("depends_on"), "UNMAPPABLE_LANES",
            "{}.depends_on".format(lane_id))
        # The lane's title becomes the agent's `instruction` — the prompt it
        # works from. Falling back to the lane id handed an agent the string
        # `lane-freeze` as its whole brief, which is not a defaulted field but
        # an absent one, silently.
        instruction = _require_text(
            lane.get("title"), "UNMAPPABLE_LANES",
            "{}.title".format(lane_id))
        nodes.append({
            "kind": "agent",
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
        })

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
        "schema_version": "maestro-plan.v1",
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
