"""Which plan amendments a run in flight may adopt, and which it must refuse.

**The defect class, in one line:** a run identifies its plan by the digest of a
*mutable file*, so amending the plan destroys the only handle the run has to its
own plan bytes.

The count is one, and the asymmetry is what makes it obvious. Every other
artifact a run depends on is stored immutably and addressed by content:
`candidate_sha`, `output_sha`, `base_sha` and `accepted_test_sha` are git
objects; `review_digest` and `subject_digest` resolve into the receipt store at
`{digest}.json`, which is why a finalization receipt survives any edit to the
plan it finalised. `runs.plan_digest` alone resolves through
`maestro._named_plan_file` to a path that `plan ship` overwrites. The run keeps
a digest and a node projection; the bytes that digest names are kept by nobody.

So `_resume_run_selection` searches the installed plan files for one whose
digest matches the run's, finds none after an amendment, and refuses: *"the plan
file has changed since the run started."* That refusal is **correct** — resuming
a run against different plan bytes silently is exactly the substitution it
exists to prevent — and it is not the thing to loosen. What is wrong is that the
only escape it leaves is abandoning the run, which on 2026-08-27 meant
discarding 9 MERGED and 4 ACCEPTED nodes to fix one lane's unsatisfiable gate.

This module answers the question that lets the refusal become selective:
**given what a run has already merged, is this amendment safe to adopt?**

## What "safe" means here

Not "the plans are similar". An amendment is safe when **no evidence the run has
already accepted is invalidated by it** (§1.1 item 4), and when **no dependency
decision already acted on is reopened** (§7.3, and §19 M42's shape). Those two
give the rules below, and each refusal names which of them it protects.

A merged node's evidence chain was measured against *that node's* gate, in
*that node's* declared outputs, at a base its dependants were admitted from. A
plan that changes any of those for a node that already merged is not amending
the future, it is retconning the past — and the merge already happened, so
there is nothing to re-measure it against. That is why the rule for settled
nodes is byte-identity rather than a migration: a migration implies the old
evidence can be mapped onto the new spec, and it cannot.

## What this deliberately does not do

It does not decide whether the amendment is a *good* plan — `plan_validate`
already answers eligibility, and this module refuses to duplicate it. It takes
projected `scheduler_types.PlanNode` values and node states, and nothing else,
so it is a pure function over typed records: no file reads, no ledger writes, no
prose examined anywhere (§1.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

from . import scheduler_types as st

#: States whose nodes carry evidence the run has already accepted. A node in one
#: of these has been measured, reviewed where its kind requires it, and merged
#: or accepted; its dependants were admitted on that basis.
SETTLED: Tuple[st.NodeState, ...] = (st.NodeState.MERGED, st.NodeState.ACCEPTED)


class Refusal(str, Enum):
    """Why an amendment cannot be adopted by this run.

    Every member names a property being protected rather than a diff that was
    seen, so an operator reading one is told what would have broken.
    """

    #: A node that already merged or was accepted is not byte-identical.
    SETTLED_NODE_CHANGED = "AMEND_SETTLED_NODE_CHANGED"
    #: A settled node is absent from the amended plan.
    SETTLED_NODE_REMOVED = "AMEND_SETTLED_NODE_REMOVED"
    #: A node with a live attempt would have its spec changed underneath it.
    RUNNING_NODE_CHANGED = "AMEND_RUNNING_NODE_CHANGED"
    #: An existing node's dependencies changed, reopening a graph decision.
    GRAPH_EDGE_CHANGED = "AMEND_GRAPH_EDGE_CHANGED"
    #: The whole-run acceptance bar changed while merged work stands under it.
    MERGE_POLICY_CHANGED = "AMEND_MERGE_POLICY_CHANGED"
    #: A different plan schema is a different contract, not an amendment.
    SCHEMA_VERSION_CHANGED = "AMEND_SCHEMA_VERSION_CHANGED"
    #: A settled node would gain an output. Its evidence records what it wrote;
    #: declaring a production it never made is the one direction of an outputs
    #: change that no completed measurement can support.
    SETTLED_OUTPUT_ADDED = "AMEND_SETTLED_OUTPUT_ADDED"
    #: A settled **tests** node's outputs are still read after it merges — by
    #: `compare_test_bytes` at every later pairing check and by
    #: `_append_needed_tests` when building a dependant's prompt — so they are
    #: live state, not a spent permission.
    SETTLED_TESTS_OUTPUT_CHANGED = "AMEND_SETTLED_TESTS_OUTPUT_CHANGED"
    #: A path left a settled node and no non-settled node picked it up, so the
    #: plan now accounts for merged content nothing owns.
    TRANSFER_WITHOUT_RECIPIENT = "AMEND_TRANSFER_WITHOUT_RECIPIENT"


@dataclass(frozen=True)
class Finding:
    """One reason, bound to the node it is about."""

    code: Refusal
    node_id: Optional[str]
    detail: str

    def as_mapping(self) -> dict:
        return {
            "code": self.code.value,
            "node_id": self.node_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Verdict:
    """Whether the amendment may be adopted, and everything it would change.

    `amendable` is derived rather than stored: a verdict that could report
    "safe" while carrying refusals would be a second representation of one fact
    (§4 RC1), and the shape §3.6 B8 warns about if the two ever disagree.
    """

    refusals: Tuple[Finding, ...] = ()
    changed: Tuple[str, ...] = ()
    added: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    #: `(path, donor, recipient)` for each finished path handed from a settled
    #: agent node to a live one. Reported separately from `changed` because it
    #: is the one amendment that touches a settled row, and an operator reading
    #: a verdict should see it named rather than buried in a diff count.
    transfers: Tuple[Tuple[str, str, str], ...] = ()

    @property
    def amendable(self) -> bool:
        return not self.refusals

    def as_mapping(self) -> dict:
        return {
            "amendable": self.amendable,
            "refusals": [finding.as_mapping() for finding in self.refusals],
            "changed": list(self.changed),
            "added": list(self.added),
            "removed": list(self.removed),
            "transfers": [list(t) for t in self.transfers],
        }


def _by_id(nodes: Sequence[st.PlanNode]) -> dict:
    return {node.node_id: node for node in nodes}


def _spec_differs(left: st.PlanNode, right: st.PlanNode) -> bool:
    """Do these two projections of one node differ in anything that matters?

    Compared by value over the whole frozen dataclass except `depth`, which is
    derived from the graph rather than authored: adding an unrelated node can
    lift a depth without changing what any node was asked to do, and convicting
    that would refuse safe amendments for a number nobody wrote.
    """
    if left == right:
        return False
    fields = set(vars(left)) | set(vars(right))
    return any(
        getattr(left, name, None) != getattr(right, name, None)
        for name in fields
        if name != "depth"
    )


def _ownership_transfer(node, replacement, amended_by_id, states):
    """Is this settled node's change a bare hand-over of a finished path?

    Returns `(transfers, refusal)`. A non-empty `transfers` is a permitted
    hand-over; a non-`None` refusal is a change that looked like one and is not.
    Both empty means "not a transfer at all", and the caller refuses it as an
    ordinary settled-node change.

    **Why this is sound, and it turns on a fact that was measured rather than
    assumed.** A node's `outputs` do two jobs: they are its *write permission*
    during the attempt (§8.3 convicts a diff touching an undeclared path), and
    they are its *ownership claim* in the plan (`SINGLE_OUTPUT_OWNER` gives each
    path to one node). For a MERGED node the first job is spent — the attempt is
    over, the delta was measured against the outputs as they stood, and the
    receipt records that measurement. The second job is ongoing and is a
    property of the graph rather than of the node's evidence. So removing a
    finished path re-judges nothing: the evidence still stands against the plan
    version it merged under, which `run_plan_versions` retains, and the lineage
    is what makes it auditable rather than merely unchanged.

    The scope is narrow because a grep decided it, not a preference. Every
    production reader of `node.outputs` runs at attempt time or review time —
    `permission_check`, the delta comparison, the reviewer handoff — **except
    two, and both read a *tests* node**: `compare_test_bytes` takes
    `tuple(tests_node.outputs)` at every later build lane's pairing check, and
    `_append_needed_tests` reads them when building a dependant's prompt. A
    merged tests node's outputs are therefore live state, and moving one would
    change what a later lane is paired against. A merged agent node's have no
    post-merge reader at all.
    """
    if tuple(node.needs) != tuple(replacement.needs):
        return (), None
    fields = set(vars(node)) | set(vars(replacement))
    for name in fields:
        if name in ("depth", "outputs"):
            continue
        if getattr(node, name, None) != getattr(replacement, name, None):
            return (), None

    before, after = set(node.outputs), set(replacement.outputs)
    gained = after - before
    if gained:
        return (), Finding(
            Refusal.SETTLED_OUTPUT_ADDED,
            node.node_id,
            "{0} is settled and the amendment adds {1} to its outputs; its "
            "evidence records what it wrote, and no completed measurement can "
            "support a production it never made".format(
                node.node_id, sorted(gained)
            ),
        )
    released = before - after
    if not released:
        return (), None

    if node.kind is st.NodeKind.TESTS:
        return (), Finding(
            Refusal.SETTLED_TESTS_OUTPUT_CHANGED,
            node.node_id,
            "{0} is a merged tests node and its outputs are still read after "
            "the merge — `compare_test_bytes` pairs every later build lane "
            "against them — so they are live state, not a spent permission"
            .format(node.node_id),
        )

    transfers = []
    for path in sorted(released):
        recipients = [
            other.node_id
            for other in amended_by_id.values()
            if other.node_id != node.node_id and path in tuple(other.outputs)
        ]
        live = [r for r in recipients if states.get(r) not in SETTLED]
        if len(live) != 1 or len(recipients) != len(live):
            return (), Finding(
                Refusal.TRANSFER_WITHOUT_RECIPIENT,
                node.node_id,
                "{0} releases {1} and exactly one non-settled node must take "
                "it; found {2}".format(node.node_id, path, recipients or "none"),
            )
        transfers.append((path, node.node_id, live[0]))
    return tuple(transfers), None


def classify(
    current: Sequence[st.PlanNode],
    amended: Sequence[st.PlanNode],
    states: Mapping[str, st.NodeState],
    *,
    current_merge_policy: Any = None,
    amended_merge_policy: Any = None,
    current_schema: Optional[str] = None,
    amended_schema: Optional[str] = None,
) -> Verdict:
    """Compare two projections of one run's plan against what it has merged.

    `states` maps node id to its current lifecycle state. A node absent from it
    is treated as unstarted, which is the safe direction: the rules below only
    ever *add* refusals for a node that has progressed, so an unknown state can
    never turn a refusal into a permission.
    """
    current_by_id = _by_id(current)
    amended_by_id = _by_id(amended)
    refusals: list = []
    changed: list = []
    transfers: list = []

    def state_of(node_id: str) -> Optional[st.NodeState]:
        return states.get(node_id)

    anything_settled = any(state_of(n) in SETTLED for n in current_by_id)

    # A different schema version is a different set of obligations, and §6.3
    # freezes a shipped version's class rather than migrating it. Adopting one
    # mid-run would judge merged nodes under rules they were never admitted
    # under, which is the retroactivity §7.3's rollout invariant forbids.
    if current_schema is not None and amended_schema is not None:
        if current_schema != amended_schema:
            refusals.append(
                Finding(
                    Refusal.SCHEMA_VERSION_CHANGED,
                    None,
                    "the run adopted {0} and the amendment declares {1}; a "
                    "schema change is a new contract, not an amendment".format(
                        current_schema, amended_schema
                    ),
                )
            )

    # The integration gate is the bar every merged node was admitted under
    # (§8.8). Changing it once work has merged re-judges that work by a rule it
    # never met. With nothing merged there is nothing to protect, so it is
    # permitted then and refused after.
    if anything_settled and current_merge_policy != amended_merge_policy:
        refusals.append(
            Finding(
                Refusal.MERGE_POLICY_CHANGED,
                None,
                "merged or accepted work stands under the current merge "
                "policy; changing it would re-judge that work by a bar it was "
                "never measured against",
            )
        )

    for node_id, node in current_by_id.items():
        state = state_of(node_id)
        replacement = amended_by_id.get(node_id)

        if replacement is None:
            if state in SETTLED:
                refusals.append(
                    Finding(
                        Refusal.SETTLED_NODE_REMOVED,
                        node_id,
                        "{0} is {1} and its evidence chain is part of this "
                        "run's acceptance; removing it from the plan would "
                        "leave merged work no plan accounts for".format(
                            node_id, state.value
                        ),
                    )
                )
            continue

        if not _spec_differs(node, replacement):
            continue

        changed.append(node_id)

        if state in SETTLED:
            handed_over, why = _ownership_transfer(
                node, replacement, amended_by_id, states
            )
            if why is not None:
                refusals.append(why)
                continue
            if handed_over:
                transfers.extend(handed_over)
                continue
            refusals.append(
                Finding(
                    Refusal.SETTLED_NODE_CHANGED,
                    node_id,
                    "{0} is {1}; its gate, outputs and acceptance were the "
                    "terms its evidence was measured against, and there is "
                    "nothing left to re-measure it against".format(
                        node_id, state.value
                    ),
                )
            )
            continue

        if state is st.NodeState.RUNNING:
            refusals.append(
                Finding(
                    Refusal.RUNNING_NODE_CHANGED,
                    node_id,
                    "{0} has a live attempt launched against the current "
                    "spec; amending it underneath the attempt would judge "
                    "that attempt by terms it was never given".format(node_id),
                )
            )
            continue

        # The graph itself is frozen among nodes that already exist, whatever
        # their state. §19 M42 is the executed instance: a projection that
        # rewired `needs_json` on resume reopened dependency decisions for
        # nodes that were already terminal. Extension is a different question
        # and is handled below -- a *new* node may declare needs, because
        # nothing was admitted in its absence.
        if tuple(node.needs) != tuple(replacement.needs):
            refusals.append(
                Finding(
                    Refusal.GRAPH_EDGE_CHANGED,
                    node_id,
                    "{0} depends on {1} and the amendment makes it depend on "
                    "{2}; the graph among existing nodes is what admitted "
                    "every node below it".format(
                        node_id, tuple(node.needs), tuple(replacement.needs)
                    ),
                )
            )

    added = tuple(
        sorted(node_id for node_id in amended_by_id if node_id not in current_by_id)
    )
    removed = tuple(
        sorted(node_id for node_id in current_by_id if node_id not in amended_by_id)
    )
    return Verdict(
        refusals=tuple(refusals),
        changed=tuple(sorted(changed)),
        added=added,
        removed=removed,
        transfers=tuple(sorted(transfers)),
    )
