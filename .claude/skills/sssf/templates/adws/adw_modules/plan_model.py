"""The `maestro-plan.v1` model — ten in-plan types and nothing else (§6.2).

Three planning modes converge here (§6.1). Each terminates by emitting one
plan file, and there is no second authored structure anywhere in the system:
validate, finalize, and run accept exactly one input shape, and no parser
exists for anything else. The nodes below project directly onto the
scheduler's own `PlanNode` (`scheduler_types`), so there is no converter and
no parallel node type to drift from this one.

The ten types:

    Plan  Observed  Produced  Hypothesis  Gate  AgentNode  CodeNode
    MergePolicy  PromptAsset  NodeEffect

`NodeEffect` is the tenth, and it arrived after the other nine. §6.2 as
written says nine and has to be brought level; `IN_PLAN_TYPES` is the enforced
count and `tests/test_step2_plan_model.py` asserts it. Two other statements in
§6 are in tension with it and are recorded here rather than left for someone to
rediscover: §6.3 says a shipped version's model class is frozen forever, and
`AgentNode.effects` is an added field on one; and the same section says a new
field means a new version string. Both were weighed against leaving the
reviewer unable to see what the code inside a node may do — the failure that
let an executing object materializer read as compliant against its own brief —
and the field was landed on v1 rather than behind a v2 that every consumer
would have to learn. A `maestro-plan.v1` file written before the field still
parses, because the field defaults to empty.

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
from typing import (Annotated, Any, Dict, Iterable, List, Literal, Mapping,
                    Optional, Sequence, Tuple, Type, Union)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import scheduler_types as st

#: The first shipped schema version. A v1 file under v2 code dispatches to
#: this frozen class with v1 obligations (§6.3). It is still parsed, still
#: validated, still finalizable; it is no longer *runnable* — see
#: `maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS` for why and where that is refused.
SCHEMA_V1 = "maestro-plan.v1"

#: The version the plan-contract projection emits and the runtime executes.
#:
#: It exists because a semantic change to projected output that leaves the
#: version string alone is undetectable downstream. `plan_contract_ingress`
#: used to map a lane's *title* onto `AgentNode.instruction` and drop
#: `requirements[].text` (§19 M26); the fix widened the field, and emitted the
#: same `maestro-plan.v1` the broken projection emitted, so a plan shipped
#: before the fix and a plan shipped after it were indistinguishable to a
#: fully-fixed runtime — 51 agent nodes across four shipped plans in the
#: lexgenius-pipeline deployment carry a title-only instruction under that
#: version. The version string is the only channel on which that difference
#: can travel, because the difference is in what a field *means*, not in
#: whether it is present, and B15-style reader sweeps cannot see it (§19 M26:
#: "a populated field cannot be audited by its consumers").
SCHEMA_V2 = "maestro-plan.v2"

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


def _selector_path_tokens(tokens: Sequence[str]) -> Tuple[str, ...]:
    """The path-shaped tokens of a selector, verbatim and in argv order.

    One walk, three callers: `selector_paths` normalizes it,
    `selector_string_paths` re-derives it from a projected selector string,
    and `restrict_selector` uses the same walk to decide which tokens survive.
    A second walk would be a second answer to "which token is a path", and
    the selector is exactly where an all-or-nothing reading of that question
    has already cost a run (§6.4).
    """
    ordered = tuple(tokens)
    paths: List[str] = []
    index = 0
    while index < len(ordered):
        token = ordered[index]
        if token in SELECTOR_FLAGS and index + 1 < len(ordered):
            index += 2
            continue
        if token in VALUE_FLAGS and index + 1 < len(ordered):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        paths.append(token)
        index += 1
    return tuple(paths)


def _normalized_selector_path(token: str) -> str:
    """One selector token as a comparable path: `::` test id stripped."""
    return posixpath.normpath(token.split("::", 1)[0])


def selector_paths(gate: Gate) -> Tuple[str, ...]:
    """The selector's path-shaped groups, with any `::` test id stripped.

    Used by §6.4's executability check to partition a selector into the paths
    the plan declares as produced and the paths that already exist at base.
    """
    return tuple(_normalized_selector_path(token)
                 for token in _selector_path_tokens(gate.argv))


def selector_string_paths(selector: str) -> Tuple[str, ...]:
    """The same paths, from `selector_of`'s joined string rather than a gate.

    `st.PlanNode` carries the selector as that string and no `Gate`, so the
    scheduler cannot call `selector_paths`. It asked instead whether the
    whole string equalled one declared output, which is true only of a
    single-path selector: a two-path selector never matched, its pre-gate
    exited 4 with no counts, and the attempt retried an identically absent
    file until the ENVIRONMENTAL budget was gone.
    """
    return tuple(_normalized_selector_path(token)
                 for token in _selector_path_tokens(selector.split()))


def restrict_selector(gate: Gate, keep: Iterable[str]) -> Gate:
    """`gate` with its selector narrowed to `keep`, everything else intact.

    Flags, flag values, and `-k`-style selector flags travel unchanged; only
    path-shaped tokens are filtered, and they are matched by the same
    normalization `selector_paths` returns. The result is an ordinary `Gate`,
    so it reaches the runner through `ResolvedRunner.collect_argv` like any
    other gate — there is still exactly one place that turns a gate into a
    collection invocation (`runner_resolution`, "one producer, one carrier").
    """
    wanted = {_normalized_selector_path(path) for path in keep}
    ordered = tuple(gate.argv)
    argv: List[str] = []
    index = 0
    while index < len(ordered):
        token = ordered[index]
        if ((token in SELECTOR_FLAGS or token in VALUE_FLAGS)
                and index + 1 < len(ordered)):
            argv.extend(ordered[index:index + 2])
            index += 2
            continue
        if token.startswith("-"):
            argv.append(token)
            index += 1
            continue
        if _normalized_selector_path(token) in wanted:
            argv.append(token)
        index += 1
    return gate.model_copy(update={"argv": tuple(argv)})


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


class NodeEffect(BaseModel):
    """One act this plan forbids, and what this node may do about it.

    Three closed fields and no prose the reviewer has to weigh. `meaning` is
    transcribed from the plan's source document by the plan author, and is
    carried here rather than resolved from an effect name at the far end —
    without it the plan reviewer and the node reviewer would resolve
    `canonical_object_write` against two different documents, which is the
    same failure this exists to fix, one level down.

    Every field is required and non-empty. A `NodeEffect` is only ever built
    from a prohibition, and a prohibition without its transcribed act is
    already refused at admission, so an empty `meaning` here would be a
    projection that dropped a field rather than a plan that omitted one.
    """

    model_config = _STRICT

    effect: str = Field(min_length=1)
    disposition: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


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
    #: What this node is authorised to do about each act its plan forbids.
    #:
    #: Defaulting to empty is not the optional-field-forever shape B8
    #: convicts: the *authored* field is `requirements[].effects` in the
    #: contract IR, which admission requires of every requirement. This
    #: default exists so a `maestro-plan.v1` file written before the field
    #: existed still parses, and an empty tuple renders no block rather than
    #: an empty one.
    effects: Tuple[NodeEffect, ...] = ()


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

        `st.PlanNode`'s constructor owns the refusals (§7.3, §7.4); the
        projection copies fields and lets them fire, rather than restating
        them here where the two copies could disagree.

        Every projected node is then checked against the model it came from
        by `_assert_projection_is_total`, so a declared field this function
        forgets is a raised `ProjectionIncomplete` on the first projection of
        every run rather than a value that quietly reads as its default
        somewhere downstream.
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
                result = st.PlanNode(
                    kind=st.NodeKind.AGENT,
                    gate_command=(node.gate.runner,) + tuple(node.gate.argv),
                    gate_selector=selector_of(node.gate),
                    # The gate's threshold travels with the gate. Dropping it
                    # here is what made §10.2's counting rule unenforceable:
                    # the runner, argv and selector were copied, `min_cases`
                    # was not, and the scheduler fell back to a per-run scalar
                    # that no caller ever set (§10.2, §7.3 clause 3).
                    gate_min_cases=node.gate.min_cases,
                    # The goal. Dropped exactly as `min_cases` was, and with
                    # the same shape of consequence one layer further on: the
                    # reviewer's B9 contract read it through a `getattr`
                    # default and every agent node in every run was reviewed
                    # against a goal derived from its own gate.
                    instruction=node.instruction,
                    # Carried verbatim rather than re-encoded, so the totality
                    # check below compares the same objects by value and there
                    # is one representation of the fact.
                    effects=tuple(node.effects), **common)
            else:
                result = st.PlanNode(
                    kind=st.NodeKind.CODE, command=tuple(node.command),
                    expects_changes=node.expects_changes, **common)
            _assert_projection_is_total(node, result)
            projected.append(result)
        return tuple(projected)


class PlanV2(Plan):
    """`maestro-plan.v2`. Structurally identical to v1, and that is the point.

    Nothing a parser can see separates the two: the same ten in-plan types,
    the same fields, the same closed-parse rules, the same obligations. What
    changed is what the projection *puts in* `AgentNode.instruction` — under
    v1 the lane's title, a summary of the requirement the lane was bound to;
    under v2 the requirement text itself (§19 M26). A change to the meaning of
    a field's contents is still a change to the artifact, and the version
    string is the only place it can be recorded, because every structural
    check passes either way.

    It subclasses `Plan` rather than restating its forty lines of fields, and
    the inheritance is the honest encoding of "v2's obligations *are* v1's
    obligations". §6.3 freezes a shipped class, so `Plan` cannot move under
    this one, and a duplicated field list would be one rule with two
    representations — RC1's shape, at the layer that defines what a plan is.
    A v3 that genuinely differs in structure gets a standalone class; the
    inheritance is available here only because nothing structural differs.

    No `schema_version` value is read off this model, and none may be (§5.3's
    carve-out). The version's whole job is done before parsing: it selects
    this class in the registry, and the run-start guard reads it from the
    stored bytes rather than from a parsed plan.
    """

    model_config = _STRICT

    schema_version: Literal["maestro-plan.v2"]


# ── the projection is total, or it raises (§6.2, §3.6 B15) ─────────────────

class ProjectionIncomplete(RuntimeError):
    """`to_plan_nodes` did not account for a field the plan node declares.

    This class of defect has now been paid for twice. `Gate.min_cases` was
    projected nowhere, so §10.2's counting rule read a per-run scalar no
    caller set and a plan demanding 70 passing cases was verified at 1.
    `AgentNode.instruction` was projected nowhere, so the reviewer's B9
    contract fell back to "make your own gate pass" for every agent node in
    every run. Neither was a hard failure. Both were a field that read as its
    default, in a subsystem three modules away, discovered in production.

    A hand-written projection cannot be trusted to stay total, because the
    author of a new field is not the author of this function. So totality is
    checked instead of maintained: for every field the plan node declares,
    either `st.PlanNode` carries a field of the same name **holding the same
    value**, or the field is listed in `_NODE_PROJECTION_EXEMPT` with the
    reason it is not carried. A new field is neither until someone decides
    which, and until then it raises here — on the first projection, which
    every run and every plan validation performs.

    The value comparison is what makes this stronger than a name check: a
    field that exists on both types but is never copied (the `min_cases`
    shape, had the names matched) fails too.
    """


def _normalized(value: Any) -> Any:
    """Sequences compare by content, so a tuple and a list of the same items
    are the same projected value; everything else compares as itself."""
    if isinstance(value, (list, tuple)):
        return tuple(_normalized(item) for item in value)
    return value


#: Node fields deliberately **not** carried onto `st.PlanNode`, each with the
#: reason. A reason is required because "we did not need it" and "we forgot"
#: are indistinguishable in an empty set — this table is where that
#: distinction is written down and reviewed.
_NODE_PROJECTION_EXEMPT: Dict[str, str] = {
    "kind": ("carried as `st.NodeKind`, which the projection sets explicitly "
             "on each branch; the two enums are the same two kinds"),
    "gate": ("decomposed rather than dropped, into gate_command, "
             "gate_selector and gate_min_cases; `_GATE_PROJECTION` below "
             "accounts for every one of its own fields"),
    "reads": ("evidence ids, meaningful only against `Plan.evidence`, which "
              "is not on a node. Read from the plan by plan_validate and by "
              "the plan rubric's node.reads_are_sufficient check, so the ids "
              "resolve to the evidence they name rather than travelling as "
              "opaque strings"),
    "cwd": ("a code node's command runs in that attempt's own worktree "
            "(§8.3), which is the isolation guarantee; the plan's cwd is not "
            "the runtime cwd and carrying it would offer a second answer"),
    "prompt_assets": ("consumed while authoring the plan, not while running "
                      "it; nothing on the scheduler's side reads one"),
}

#: Every `Gate` field, and where it lands on `st.PlanNode`. `None` means the
#: gate's own value is deliberately not carried. Checked for completeness
#: against `Gate.model_fields`, so a new gate field raises here too.
_GATE_PROJECTION: Dict[str, Optional[str]] = {
    "runner": "gate_command",
    "argv": "gate_command",
    "min_cases": "gate_min_cases",
    "cwd": None,  # the gate runs in the attempt worktree, as above
}


def _assert_projection_is_total(node: "_NodeBase", projected: st.PlanNode) -> None:
    """Raise unless every field `node` declares is carried or exempted."""
    for name in sorted(type(node).model_fields):
        if name in _NODE_PROJECTION_EXEMPT:
            continue
        if not hasattr(projected, name):
            raise ProjectionIncomplete(
                "{0}: {1}.{2} is declared in the plan but `to_plan_nodes` "
                "neither carries it onto scheduler_types.PlanNode nor lists "
                "it in _NODE_PROJECTION_EXEMPT with a reason. A dropped field "
                "reads as a default somewhere downstream instead of failing "
                "here.".format(node.node_id, type(node).__name__, name))
        declared = _normalized(getattr(node, name))
        carried = _normalized(getattr(projected, name))
        if declared != carried:
            raise ProjectionIncomplete(
                "{0}: {1}.{2} is {3!r} in the plan but {4!r} on the projected "
                "PlanNode; the projection carries the field name and not its "
                "value.".format(node.node_id, type(node).__name__, name,
                                declared, carried))

    gate = getattr(node, "gate", None)
    gate_fields = set(Gate.model_fields)
    if gate is not None:
        gate_fields |= set(type(gate).model_fields)
    unaccounted = sorted(gate_fields - set(_GATE_PROJECTION))
    if unaccounted:
        raise ProjectionIncomplete(
            "Gate declares {0} with no entry in _GATE_PROJECTION; a gate "
            "field with nowhere to land is how `min_cases` came to be "
            "declared at 70 and enforced at 1.".format(", ".join(unaccounted)))
    if gate is not None:
        if projected.gate_min_cases != gate.min_cases:
            raise ProjectionIncomplete(
                "{0}: the gate declares min_cases={1}, the projected node "
                "carries {2}.".format(node.node_id, gate.min_cases,
                                      projected.gate_min_cases))
        if tuple(projected.gate_command) != (gate.runner,) + tuple(gate.argv):
            raise ProjectionIncomplete(
                "{0}: the projected gate_command {1!r} is not this gate's "
                "runner and argv.".format(node.node_id,
                                          tuple(projected.gate_command)))


IN_PLAN_TYPES: Tuple[Type[BaseModel], ...] = (
    Plan, Observed, Produced, Hypothesis, Gate, AgentNode, CodeNode,
    MergePolicy, PromptAsset, NodeEffect)


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
register_parser(SCHEMA_V2, PlanV2)


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
