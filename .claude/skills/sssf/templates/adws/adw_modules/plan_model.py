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

import re

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      model_validator)

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

#: The version that can declare a `tests` node. v1 and v2 stay frozen
#: without that kind: a tests node under those versions cannot parse, so
#: the pair cannot silently become two agent nodes. v2 remains runnable;
#: v3 is runnable as well. Plan-contract projection still emits v2 — the
#: IR has no tests/build split yet, and inventing one at projection would
#: change every shipped lane's graph.
SCHEMA_V3 = "maestro-plan.v3"

#: The version whose `tests` node carries an executable **test-strength
#: contract** — the coverage obligations its cases must discharge and the
#: falsifiability strategy a negative control must execute.
#:
#: It is a new version rather than an optional field on `TestsNode` for the
#: reason §3.6 B8 states and §6.3 enforces: a field added later is optional
#: forever, and an optional gate-strength contract is a gate-strength contract
#: nothing has. Under v3 a tests node's acceptance was "the new cases are red
#: at the parent commit", which run-8d1a71f463e4430f92a125a8f8b3731d shows is
#: not discrimination proof: `lane-acquisition-manifest-tests` reached MERGED
#: on four non-skipped cases while every implementation candidate its tests
#: were written to gate was independently rejected.
#:
#: v3 stays frozen and stays runnable, because a run created under it is
#: pinned to it (`runs.test_strength_contract`). What v3 cannot do is start a
#: *new* run: `maestro run start` refuses a plan whose tests nodes carry no
#: contract, and the remedy is to re-ship the plan, never to edit it.
SCHEMA_V4 = "maestro-plan.v4"

#: `maestro-plan.v5` — a tests node declares whether its files reach the
#: builders its cases judge (`test_visibility`).
#:
#: A new version rather than an optional field on `TestsNodeV4`, for the reason
#: v4 itself gives: an optional visibility is a visibility nothing declared, and
#: "merged" would then be indistinguishable from "the author never decided".
#:
#: **v4 stays registered and stays runnable, and that is a requirement rather
#: than a courtesy.** Dropping a shipped version is what made deployments' plans
#: unrunnable with `RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE` (#104), repairable only
#: by re-shipping from the IR and not repairable at all mid-run. A v4 plan
#: projects to `VISIBILITY_MERGED` and behaves exactly as it did.
SCHEMA_V5 = "maestro-plan.v5"

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


def unscoped_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    """Flags only: drop every token that selects cases.

    The integration gate is the plan's one unscoped command (§8.8). A plan
    that named paths or `-k` expressions still executes as the runner's
    default whole-tree collection. Concatenating lane specs is the other
    defect this function exists not to become: the union of what the lanes
    already ran cannot see a test no lane owned.

    Value flags travel with their values. Selector flags and path-shaped
    tokens are dropped. Nothing here injects `-o addopts=`; that flag is a
    collection-count concern and a full-suite integration run must not
    carry it.
    """
    ordered = tuple(argv)
    kept: List[str] = []
    index = 0
    while index < len(ordered):
        token = ordered[index]
        if token in SELECTOR_FLAGS and index + 1 < len(ordered):
            index += 2
            continue
        if token in VALUE_FLAGS and index + 1 < len(ordered):
            kept.extend(ordered[index:index + 2])
            index += 2
            continue
        if token.startswith("-"):
            kept.append(token)
            index += 1
            continue
        index += 1
    return tuple(kept)


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


# ── the test-strength contract (§6.2, and §3.6 B8's reason it is typed) ─────

#: The behavioural aspects a coverage obligation may name. Closed, because an
#: open string here would let an author discharge "negative behaviour" by
#: naming an aspect nobody checks, which is the shape of the defect this
#: contract exists to refuse.
COVERAGE_ASPECTS: Tuple[str, ...] = (
    "positive", "negative", "boundary", "precedence", "transition",
    "failure_mode")

#: The two aspects every covered requirement must carry. A test suite that
#: only ever asserts the happy path passes on an implementation that never
#: rejects anything, which is exactly how a MERGED test node failed to
#: discriminate four rejected implementation candidates.
REQUIRED_ASPECTS: Tuple[str, ...] = ("positive", "negative")


class CoverageObligation(BaseModel):
    """One requirement × one behavioural aspect, bound to a case selector.

    The selector is what makes this **measured** rather than claimed. The
    runtime collects the tests node's own cases, keeps those whose node id
    contains this selector, executes them, and counts. §1.2 forbids keying a
    transition on an agent's account of its own work, so the tester is never
    asked whether it covered a requirement — the plan says which cases would
    prove it did, and code counts them.
    """

    model_config = _STRICT

    requirement_id: str = Field(min_length=1)
    aspect: Literal["positive", "negative", "boundary", "precedence",
                    "transition", "failure_mode"]
    #: A substring of the case node id (`path::case`) that selects the cases
    #: discharging this obligation. A substring rather than a runner
    #: expression because it must mean the same thing under pytest and under
    #: vitest, and the two disagree about `-k` and `--testNamePattern`.
    case_selector: str = Field(min_length=1)
    min_cases: int = Field(ge=1, default=1)


class ControlledMutation(BaseModel):
    """A deterministic, reversible negative control over real behaviour.

    `revert_paths` restores the named paths to the plan's `base_commit` in an
    isolated scratch checkout. It is a behavioural reversal — the code under
    test genuinely is not there — rather than a source-text substitution,
    which the contract forbids as a stand-in whenever a runtime boundary is
    named.
    """

    model_config = _STRICT

    kind: Literal["revert_paths"]
    paths: Tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _paths_are_non_empty(self) -> "ControlledMutation":
        for path in self.paths:
            if not str(path).strip():
                raise ValueError(
                    "a controlled mutation names paths; an empty one reverts "
                    "nothing and would pass as a negative control")
        return self


class Falsifiability(BaseModel):
    """How this node's tests are proven able to fail for the right reason.

    `baseline_absent` runs the candidate's new cases against the attempt's
    own parent commit, where the implementation does not exist yet. It is the
    strategy a test-first node declares.

    `controlled_mutation` runs them against a named, reverted behaviour. It is
    the strategy a node whose implementation already exists declares, because
    its parent commit is already green.

    Either way the failure must **match** `expected_reason_pattern`. A random
    exception, an import error, or a collection failure is not proof that the
    tests discriminate: it is proof that the tree does not import.
    """

    model_config = _STRICT

    strategy: Literal["baseline_absent", "controlled_mutation"]
    mutation: Optional[ControlledMutation] = None
    #: Which of this node's cases the negative control must turn red. A
    #: substring of the case node id, as in `CoverageObligation`.
    expected_failing_selector: str = Field(min_length=1)
    #: A Python regular expression the failing case's reported reason must
    #: match. Compiled here so an unparseable pattern is a plan that does not
    #: parse, never a negative control that silently matches nothing.
    expected_reason_pattern: str = Field(min_length=1)

    @model_validator(mode="after")
    def _strategy_carries_its_own_mechanism(self) -> "Falsifiability":
        if self.strategy == "controlled_mutation" and self.mutation is None:
            raise ValueError(
                "a controlled_mutation strategy declares the mutation it "
                "applies; without one there is no negative control to run")
        if self.strategy == "baseline_absent" and self.mutation is not None:
            raise ValueError(
                "a baseline_absent strategy runs against the parent commit; a "
                "mutation here is a field nothing reads (§12.3)")
        try:
            re.compile(self.expected_reason_pattern)
        except re.error as exc:
            raise ValueError(
                "expected_reason_pattern is not a regular expression: "
                "{0}".format(exc)) from exc
        return self


class TestStrength(BaseModel):
    """The contract a tests node's candidate must discharge to be accepted.

    Both halves are required. Coverage without falsifiability accepts tests
    that assert nothing; falsifiability without coverage accepts one
    discriminating case standing in for a whole requirement set.
    """

    model_config = _STRICT

    coverage: Tuple[CoverageObligation, ...] = Field(min_length=1)
    falsifiability: Falsifiability

    @property
    def requirement_ids(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for item in self.coverage:
            if item.requirement_id not in seen:
                seen.append(item.requirement_id)
        return tuple(seen)

    def aspects_for(self, requirement_id: str) -> Tuple[str, ...]:
        return tuple(sorted({item.aspect for item in self.coverage
                             if item.requirement_id == requirement_id}))

    @model_validator(mode="after")
    def _every_requirement_states_both_polarities(self) -> "TestStrength":
        for requirement_id in self.requirement_ids:
            aspects = set(self.aspects_for(requirement_id))
            missing = [a for a in REQUIRED_ASPECTS if a not in aspects]
            if missing:
                raise ValueError(
                    "{0} declares no {1} obligation; a requirement covered "
                    "only by its happy path cannot refuse an implementation "
                    "that never rejects anything".format(
                        requirement_id, " or ".join(missing)))
        pairs = [(item.requirement_id, item.aspect, item.case_selector)
                 for item in self.coverage]
        if len(set(pairs)) != len(pairs):
            raise ValueError(
                "two coverage obligations are identical; a duplicate is two "
                "answers to one fact and counts the same cases twice")
        return self


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


class TestsNode(_NodeBase):
    """Authored tests, written before the implementation they will judge.

    Same fields as `AgentNode` because the agent is launched the same way.
    The kind is the evidence-chain discriminator (§1.1 item 4): this node
    is verified by new cases being red at the parent commit, never by a
    green post-gate of tests it authored itself.
    """

    kind: Literal["tests"]
    instruction: str = Field(min_length=1)
    gate: Gate
    prompt_assets: Tuple[PromptAsset, ...] = ()
    effects: Tuple[NodeEffect, ...] = ()


class TestsNodeV4(TestsNode):
    """A tests node that declares what its cases must prove.

    Subclasses the frozen v3 class rather than restating it, exactly as
    `PlanV2` subclasses `Plan`: v4's obligations *are* v3's obligations plus
    one required field. `TestsNode` itself cannot grow the field — §6.3
    freezes a shipped class, and an optional one would be optional forever.
    """

    model_config = _STRICT

    test_strength: TestStrength


class TestsNodeV5(TestsNodeV4):
    """A tests node that declares whether builders may read it.

    Subclasses `TestsNodeV4` for the reason that one subclasses `TestsNode`:
    v5's obligations *are* v4's obligations plus one required field. Required,
    not defaulted — a defaulted visibility could not tell an author who chose
    `merged` apart from an author who had never heard of the choice, and that
    distinction is the only thing the version string is here to record.
    """

    model_config = _STRICT

    test_visibility: Literal["merged", "hidden"]


Node = Annotated[Union[AgentNode, CodeNode], Field(discriminator="kind")]
V3Node = Annotated[Union[AgentNode, CodeNode, TestsNode],
                   Field(discriminator="kind")]
V4Node = Annotated[Union[AgentNode, CodeNode, TestsNodeV4],
                   Field(discriminator="kind")]
V5Node = Annotated[Union[AgentNode, CodeNode, TestsNodeV5],
                   Field(discriminator="kind")]


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
    #: The plan's name. Optional on the model because a field added later is
    #: optional forever (§3.6 B8); ship refuses an untitled plan instead of
    #: making this required, which would unparse every already-shipped file.
    #: Default None so a file written before the field still parses. Omitted
    #: from canonical bytes when absent — see `plan_canonical.canonicalize`.
    title: Optional[str] = None
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

    @property
    def tests_nodes(self) -> Tuple["TestsNode", ...]:
        return tuple(n for n in self.nodes if isinstance(n, TestsNode))

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
            elif isinstance(node, TestsNode):
                result = st.PlanNode(
                    kind=st.NodeKind.TESTS,
                    gate_command=(node.gate.runner,) + tuple(node.gate.argv),
                    gate_selector=selector_of(node.gate),
                    gate_min_cases=node.gate.min_cases,
                    instruction=node.instruction,
                    # Carried verbatim, and `None` on a v3 node that declares
                    # none. The projection does not invent one: a synthesised
                    # contract would be indistinguishable downstream from an
                    # authored one, which is §19 M26's shape.
                    test_strength=getattr(node, "test_strength", None),
                    # Carried the same way and defaulted the same way: a v3 or
                    # v4 node declares no visibility, and `merged` is what it
                    # has always meant rather than a decision invented here.
                    test_visibility=getattr(
                        node, "test_visibility", st.VISIBILITY_MERGED),
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


class PlanV3(Plan):
    """`maestro-plan.v3`. Adds the authored `tests` node kind.

    v1 and v2 stay frozen: their `nodes` union cannot represent `TestsNode`,
    so a tests/build pair cannot parse as two agent nodes under those
    versions. Methods are inherited; `nodes` is the structural difference
    §6.3 said a v3 would carry, and the version string is how a runtime
    that does not know this kind refuses rather than guessing.
    """

    model_config = _STRICT

    schema_version: Literal["maestro-plan.v3"]
    nodes: Tuple[V3Node, ...]  # type: ignore[assignment]


class PlanV4(Plan):
    """`maestro-plan.v4`. Its `tests` nodes declare a test-strength contract.

    v3 stays frozen: its `nodes` union carries `TestsNode`, which has no
    `test_strength` field and forbids extras, so a v4 tests node cannot parse
    as a v3 one and a v3 plan cannot acquire the contract by being re-read
    under this class. That is the whole point of the version string here —
    "this plan's tests nodes were authored knowing they would have to prove
    discrimination" is not a fact any structural check on v3 bytes can
    recover.
    """

    model_config = _STRICT

    schema_version: Literal["maestro-plan.v4"]
    nodes: Tuple[V4Node, ...]  # type: ignore[assignment]


class PlanV5(Plan):
    """`maestro-plan.v5`. Its `tests` nodes declare a visibility.

    v4 stays frozen and, unlike a version this project has dropped before,
    stays *runnable*: its `nodes` union carries `TestsNodeV4`, which forbids
    extras, so a v5 tests node cannot parse as a v4 one and a v4 plan cannot
    acquire a visibility by being re-read under this class. A v4 run therefore
    keeps behaving exactly as it did, which is what stops this change from
    doing to shipped plans what #104 did.
    """

    model_config = _STRICT

    schema_version: Literal["maestro-plan.v5"]
    nodes: Tuple[V5Node, ...]  # type: ignore[assignment]


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
             "on each branch; the enums now include tests as well as "
             "agent/code/review"),
    "gate": ("decomposed rather than dropped, into gate_command, "
             "gate_selector and gate_min_cases; `_GATE_PROJECTION` below "
             "accounts for every one of its own fields"),
    "reads": ("evidence ids, meaningful only against `Plan.evidence`, which "
              "is not on a node. Read from the plan by plan_validate "
              "(REFERENCES_RESOLVE_ONCE, HYPOTHESIS_QUARANTINE), so the ids "
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
    TestsNode, TestsNodeV4, MergePolicy, PromptAsset, NodeEffect,
    CoverageObligation, ControlledMutation, Falsifiability, TestStrength)


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
register_parser(SCHEMA_V3, PlanV3)
register_parser(SCHEMA_V4, PlanV4)
register_parser(SCHEMA_V5, PlanV5)


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
