"""The `maestro-plan.v1` model — nine in-plan types and nothing else (§6.2).

Three planning modes converge here (§6.1). Each terminates by emitting one
plan file, and there is no second authored structure anywhere in the system:
validate, finalize, and run accept exactly one input shape, and no parser
exists for anything else. The nodes below project directly onto the
scheduler's own `PlanNode` (`scheduler_types`), so there is no converter and
no parallel node type to drift from this one.

The nine types:

    Plan  Observed  Produced  Hypothesis  Gate  AgentNode  CodeNode
    MergePolicy  PromptAsset

Evidence is a **discriminated union of three types with disjoint mechanical
duties** (§6.2) rather than an enum an author can mislabel. The duty is a
property of the class, so `Hypothesis` is structurally incapable of carrying
a path, a sha, or a producer — under `extra=forbid` those keys are refused at
parse rather than parsed and ignored.

Two absences are load-bearing:

* **No retry budget has a field here.** Budgets are operational
  configuration (`SchedulerConfig`); putting them in canonical bytes would
  mean tuning a crash budget mints a new digest and forces re-review of
  semantically unchanged work (§6.2).
* **No non-semantic field exists at all** — no timestamp, authorship,
  routing, or review result. Nothing is excluded from the digest, and that
  is abuse-proof by construction rather than by discipline, because there is
  no excluded channel to smuggle semantics through (§6.3).

Schema evolution is an **append-only parser registry**. A shipped version's
model class is frozen forever — no added, removed, or re-defaulted field. A
new field means a new version string and a new class. There is no upgrade
function, and `register_parser` refuses to rebind a version so that "frozen"
is enforced rather than documented.
"""

from __future__ import annotations

import json
import posixpath
from typing import (Annotated, Any, Dict, List, Literal, Mapping, Optional,
                    Sequence, Tuple, Type, Union)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import scheduler_types as st

#: The only shipped schema version. A v1 file under v2 code dispatches to
#: this frozen class with v1 obligations (§6.3).
SCHEMA_V1 = "maestro-plan.v1"

#: The closed set of gate runners. §6.2 deleted the plain-argv arm for agent
#: nodes: an exit-code-only gate cannot satisfy the counting rule.
RUNNERS = ("pytest", "vitest")


class PlanParseError(ValueError):
    """A plan file that does not parse closed (§6.4's first obligation).

    Carries `(json_pointer, message)` pairs so validation can emit typed
    blockers that point into the authored file rather than a stack trace.
    """

    def __init__(self, message: str,
                 pointers: Sequence[Tuple[str, str]] = ()) -> None:
        super().__init__(message)
        self.pointers: Tuple[Tuple[str, str], ...] = tuple(pointers)


class SchemaVersionUnknown(PlanParseError):
    """No parser is registered for this version string. There is no upgrade
    function and no guess: an unregistered version is a refusal (§6.3)."""


class SchemaVersionFrozen(RuntimeError):
    """A shipped version's model class is frozen forever (§6.3)."""


_STRICT = ConfigDict(extra="forbid", frozen=True)


# ── the three evidence types, with disjoint mechanical duties (§6.2) ─────────

class Observed(BaseModel):
    """Its sha256 must match a real git object at `base_commit`, which the
    validator re-reads. A fabricated citation fails."""

    model_config = _STRICT

    kind: Literal["observed"]
    evidence_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)


class Produced(BaseModel):
    """A producer node must own the path, and the path must be absent at base
    *or* match a declared base sha256."""

    model_config = _STRICT

    kind: Literal["produced"]
    evidence_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    base_sha256: Optional[str] = None


class Hypothesis(BaseModel):
    """Carries no path, no sha, no producer. Structurally incapable of
    satisfying any mechanical duty, quarantined to an agent node's `reads`,
    and dischargeable only by a rubric check (§6.2)."""

    model_config = _STRICT

    kind: Literal["hypothesis"]
    evidence_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)


Evidence = Annotated[Union[Observed, Produced, Hypothesis],
                     Field(discriminator="kind")]

EVIDENCE_TYPES: Tuple[Type[BaseModel], ...] = (Observed, Produced, Hypothesis)


# ── gates (§6.2) ────────────────────────────────────────────────────────────

class Gate(BaseModel):
    """A runner, an argv, a working directory, and `min_cases >= 1`.

    A node's gate is scoped to that node's own work: its argv carries an
    explicit selector naming the paths or test identifiers whose behaviour
    this node's own outputs supply. The scope is a property of the selector
    the argv already carries — there is no separate field for it, because a
    second representation of one fact is the shape this design convicts.
    """

    model_config = _STRICT

    runner: Literal["pytest", "vitest"]
    argv: Tuple[str, ...]
    cwd: str = Field(min_length=1)
    min_cases: int = Field(ge=1)


#: Flags whose *value* is part of the selection, so flag and value travel
#: together: `-k greeting` selects cases exactly as a path does.
SELECTOR_FLAGS = ("-k", "--deselect", "-t", "--testNamePattern")

#: Flags that take a following value which is *not* selection.
VALUE_FLAGS = ("-p", "-o", "-c", "--rootdir", "--reporter", "--config")

#: Noise: flags that cannot change which cases are collected (§6.4).
NOISE_FLAGS = ("-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-s",
               "--no-header", "--no-summary", "--silent")
NOISE_PREFIXES = ("--color", "--colour", "--tb", "--reporter", "-r")


def _selector_groups(gate: Gate) -> Tuple[str, ...]:
    """The argv tokens that select cases, each as one group.

    A bare token is a path or a test identifier. A selector flag carries its
    value with it. Everything else is a flag, and flags select nothing.
    """
    groups: List[str] = []
    index = 0
    argv = tuple(gate.argv)
    while index < len(argv):
        token = argv[index]
        if token in SELECTOR_FLAGS and index + 1 < len(argv):
            groups.append("{0} {1}".format(token, argv[index + 1]))
            index += 2
            continue
        if token in VALUE_FLAGS and index + 1 < len(argv):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        groups.append(token)
        index += 1
    return tuple(groups)


def selector_of(gate: Gate) -> Optional[str]:
    """The gate's selector, or `None` when the argv names none.

    An argv naming no selector at all — falling back to the runner's default
    whole-tree collection — is not a node gate (§6.2) and fails eligibility
    (§6.4). Returning `None` here is what makes that refusable.
    """
    groups = _selector_groups(gate)
    return " ".join(groups) if groups else None


def selector_paths(gate: Gate) -> Tuple[str, ...]:
    """The selector's path-shaped groups, with any `::` test id stripped.

    Used by §6.4's two-armed executability check to ask whether a selector
    resolves entirely within paths the plan declares as produced.
    """
    paths: List[str] = []
    for group in _selector_groups(gate):
        if group.startswith("-"):
            continue
        paths.append(posixpath.normpath(group.split("::", 1)[0]))
    return tuple(paths)


def _is_noise(token: str) -> bool:
    return token in NOISE_FLAGS or token.startswith(NOISE_PREFIXES)


def command_core(gate: Gate) -> Tuple[str, str, Tuple[str, ...]]:
    """**Command core := (runner, cwd, argv normalized)** (§6.4).

    Normalization sorts flags, strips noise flags that cannot change which
    cases are collected, and canonicalizes the selector. Two cores are the
    same if their normalized tuples are equal — so the obligation forbidding
    two agent nodes from sharing a core is not evaded by a reordered flag.
    """
    selector = set(_selector_groups(gate))
    flags = []
    index = 0
    argv = tuple(gate.argv)
    while index < len(argv):
        token = argv[index]
        if token in SELECTOR_FLAGS and index + 1 < len(argv):
            index += 2
            continue
        if token in VALUE_FLAGS and index + 1 < len(argv):
            if not _is_noise(token):
                flags.append("{0} {1}".format(token, argv[index + 1]))
            index += 2
            continue
        if token.startswith("-"):
            if not _is_noise(token):
                flags.append(token)
            index += 1
            continue
        index += 1
    canonical_selector = tuple(sorted(
        group if group.startswith("-") else posixpath.normpath(group)
        for group in selector))
    return (gate.runner, posixpath.normpath(gate.cwd),
            tuple(sorted(flags)) + canonical_selector)


# ── prompt assets, which are part of the identity (§6.3) ────────────────────

class PromptAsset(BaseModel):
    """An agent-facing prompt referenced **by content digest**.

    The digest is a field of the plan, so it is an input to the plan digest
    by construction. Editing a prompt therefore produces a different plan
    digest, which finds no receipt, which refuses the run (§6.3).
    """

    model_config = _STRICT

    role: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)


# ── nodes over a common base (§6.2) ─────────────────────────────────────────

class _NodeBase(BaseModel):
    """id, needs, reads, outputs — the base both kinds share. Not an in-plan
    type: nothing authors one, and nothing parses to one."""

    model_config = _STRICT

    node_id: str = Field(min_length=1)
    needs: Tuple[str, ...] = ()
    reads: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()


class AgentNode(_NodeBase):
    """Agent work is never self-certified: the gate is a required field, so a
    gateless agent node is unrepresentable rather than rejected later."""

    kind: Literal["agent"]
    instruction: str = Field(min_length=1)
    gate: Gate
    prompt_assets: Tuple[PromptAsset, ...] = ()


class CodeNode(_NodeBase):
    """A code node's acceptance is its exit code (§6.2), so there is no gate
    field here to carry one and no `min_cases` to satisfy."""

    kind: Literal["code"]
    command: Tuple[str, ...]
    cwd: str = Field(min_length=1)
    expects_changes: bool


Node = Annotated[Union[AgentNode, CodeNode], Field(discriminator="kind")]


class MergePolicy(BaseModel):
    """The integration branch and the plan's one whole-suite gate (§8.8).

    The integration gate is the only gate permitted to name no selector: the
    whole suite has exactly one place in this design and this is it (§6.2).
    """

    model_config = _STRICT

    integration_branch: str = Field(min_length=1)
    integration_gate: Gate


class Plan(BaseModel):
    """`maestro-plan.v1`. Frozen forever from the moment it ships (§6.3)."""

    model_config = _STRICT

    schema_version: Literal["maestro-plan.v1"]
    plan_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    evidence: Tuple[Evidence, ...]
    nodes: Tuple[Node, ...]
    merge_policy: MergePolicy
    #: The canonical digest of the plan this one supersedes, or `None`.
    #: Lineage resolves to an existing receipt (§6.4's eleventh obligation).
    supersedes: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    # ── queries the obligations and the scheduler both need ──────────────

    @property
    def agent_nodes(self) -> Tuple[AgentNode, ...]:
        return tuple(n for n in self.nodes if isinstance(n, AgentNode))

    @property
    def code_nodes(self) -> Tuple[CodeNode, ...]:
        return tuple(n for n in self.nodes if isinstance(n, CodeNode))

    def node_by_id(self) -> Dict[str, Any]:
        return {node.node_id: node for node in self.nodes}

    def evidence_by_id(self) -> Dict[str, Any]:
        return {item.evidence_id: item for item in self.evidence}

    def declared_outputs(self) -> Tuple[str, ...]:
        paths: List[str] = []
        for node in self.nodes:
            paths.extend(posixpath.normpath(p) for p in node.outputs)
        return tuple(paths)

    def node_depths(self) -> Dict[str, int]:
        """Longest-path depth per node — §8.5's deterministic merge order key.

        Raises on a cycle or an unresolved `needs`, because a depth is not
        defined for either. Validation reports both as typed blockers first;
        this is the projection refusing to invent an answer.
        """
        known = self.node_by_id()
        depths: Dict[str, int] = {}
        visiting: List[str] = []

        def depth_of(node_id: str) -> int:
            if node_id in depths:
                return depths[node_id]
            if node_id in visiting:
                raise ValueError(
                    "the graph is not acyclic: {0}".format(
                        " -> ".join(visiting + [node_id])))
            if node_id not in known:
                raise ValueError(
                    "{0} is needed but no node declares it".format(node_id))
            visiting.append(node_id)
            needs = known[node_id].needs
            value = 0 if not needs else 1 + max(depth_of(n) for n in needs)
            visiting.pop()
            depths[node_id] = value
            return value

        for node in self.nodes:
            depth_of(node.node_id)
        return depths

    def to_plan_nodes(self) -> Tuple[st.PlanNode, ...]:
        """Project onto the scheduler's own node — no second authored type.

        `st.PlanNode`'s constructor owns the three refusals (§7.3, §7.4); the
        projection copies fields and lets them fire, rather than restating
        them here where the two copies could disagree.
        """
        depths = self.node_depths()
        projected: List[st.PlanNode] = []
        for node in self.nodes:
            # Annotated because the values are heterogeneous: unannotated,
            # the inferred value type is a union and every parameter filled
            # through `**common` is reported as a type error.
            common: Dict[str, Any] = dict(
                node_id=node.node_id, depth=depths[node.node_id],
                needs=tuple(node.needs), outputs=tuple(node.outputs))
            if isinstance(node, AgentNode):
                projected.append(st.PlanNode(
                    kind=st.NodeKind.AGENT,
                    gate_command=(node.gate.runner,) + tuple(node.gate.argv),
                    gate_selector=selector_of(node.gate),
                    # The gate's threshold travels with the gate. Dropping it
                    # here is what made §10.2's counting rule unenforceable:
                    # the runner, argv and selector were copied, `min_cases`
                    # was not, and the scheduler fell back to a per-run scalar
                    # that no caller ever set (§10.2, §7.3 clause 3).
                    gate_min_cases=node.gate.min_cases, **common))
            else:
                projected.append(st.PlanNode(
                    kind=st.NodeKind.CODE, command=tuple(node.command),
                    expects_changes=node.expects_changes, **common))
        return tuple(projected)


IN_PLAN_TYPES: Tuple[Type[BaseModel], ...] = (
    Plan, Observed, Produced, Hypothesis, Gate, AgentNode, CodeNode,
    MergePolicy, PromptAsset)


# ── the append-only parser registry (§6.3) ──────────────────────────────────

_PARSERS: Dict[str, Type[BaseModel]] = {}


def register_parser(version: str, model: Type[BaseModel]) -> None:
    """Append a version. Rebinding one is refused, which is what makes a
    shipped model class frozen rather than merely documented as frozen."""
    if version in _PARSERS:
        raise SchemaVersionFrozen(
            "{0} is already registered to {1}; a shipped version's model class "
            "is frozen forever (§6.3). A new field means a new version string "
            "and a new class.".format(version, _PARSERS[version].__name__))
    _PARSERS[version] = model


def parser_for(version: str) -> Type[BaseModel]:
    try:
        return _PARSERS[version]
    except KeyError:
        raise SchemaVersionUnknown(
            "no parser is registered for schema_version {0!r}; registered: {1}"
            .format(version, ", ".join(sorted(_PARSERS))),
            (("/schema_version", "unregistered schema version"),))


def registered_versions() -> Tuple[str, ...]:
    return tuple(sorted(_PARSERS))


register_parser(SCHEMA_V1, Plan)


def _pointer(loc: Sequence[Any]) -> str:
    return "/" + "/".join(str(part) for part in loc)


def parse_mapping(data: Mapping[str, Any]) -> Plan:
    """Dispatch on `schema_version` and parse closed.

    No canonicalization happens here, and that is deliberate: this is the
    function the runtime uses, and the runtime never re-canonicalizes (§6.3).
    """
    if not isinstance(data, Mapping):
        raise PlanParseError(
            "a plan file is a JSON object, got {0}".format(type(data).__name__),
            (("", "not an object"),))
    version = data.get("schema_version")
    if version is None:
        raise SchemaVersionUnknown(
            "the plan declares no schema_version; there is no default and no "
            "upgrade function (§6.3)",
            (("/schema_version", "missing"),))
    model = parser_for(version)
    try:
        return model.model_validate(dict(data))  # type: ignore[return-value]
    except ValidationError as exc:
        pointers = tuple((_pointer(error["loc"]), error["msg"])
                         for error in exc.errors())
        raise PlanParseError(
            "the plan does not parse closed under {0}: {1}".format(
                model.__name__,
                "; ".join("{0} {1}".format(p, m) for p, m in pointers)),
            pointers)


def parse_bytes(stored: bytes) -> Plan:
    """Parse the plan file's stored bytes. Never re-serialises them."""
    try:
        data = json.loads(stored.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlanParseError(
            "the plan file is not UTF-8 JSON: {0}".format(exc),
            (("", "not UTF-8 JSON"),))
    return parse_mapping(data)
