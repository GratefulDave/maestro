"""The twelve deterministic obligations, and the typed blockers (§6.4).

`maestro plan validate` runs every obligation and emits exactly one outcome
(§11.1). `FINALIZATION_ELIGIBLE` carries the canonical digest and publishes
nothing. `AUTHORING_BLOCKED` carries typed blockers with JSON pointers into
the plan, launches no reviewer, and publishes nothing — the blocked result
has no digest field to publish, so "publishes nothing" is a shape rather
than a promise.

Eleven of the twelve are computed from git objects alone. The two facts that
are not git facts enter through injected protocol seams rather than being
reached for directly:

* `ReceiptIndex` — whether a receipt exists for the superseded digest. The
  finalization store is Step 3's (§6.5, §6.6); this module needs one bit of
  it and takes only that bit.
* `GateCollector` — how many cases a selector collects. Running a runner is
  an environment fact, and **environment checks are not eligibility
  obligations** (§6.4): a collector that cannot run raises
  `CollectorUnavailable`, which propagates as an operational refusal with no
  identity consequence, rather than being recorded as a blocker that would
  make a missing binary look like a defect in the authored bytes.

Two obligations needed their terms fixed before they could be implemented,
and §6.4 fixes both:

* **Command core := (runner, cwd, argv normalized)**, defined in
  `plan_model.command_core`, so the twelfth obligation is not evaded by a
  reordered flag. The same comparison runs against the integration gate.
* **Gate executability has two arms.** For a gate whose selector resolves
  entirely within paths the plan declares as produced, only well-formedness
  is checked — a nonzero collection count there would mean the node had
  nothing to do. Every other gate is checked for the collection count as
  stated. Both arms require a selector to exist at all.

Blockers are collected rather than raised one at a time: §11.1 emits typed
blockers, plural, and a fail-fast validator makes an author fix twelve plans
instead of one. The single exception is a plan that does not parse, where
nothing downstream has a model to run against.
"""

from __future__ import annotations

import hashlib
import posixpath
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:  # pragma: no cover - typing only
    from typing import Protocol
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore

from . import plan_canonical as pc
from . import plan_digest as pd
from . import plan_model as pm


class Obligation(str, Enum):
    """The twelve, in §6.4's order."""

    CLOSED_PARSE = "CLOSED_PARSE"
    REFERENCES_RESOLVE_ONCE = "REFERENCES_RESOLVE_ONCE"
    GRAPH_ACYCLIC = "GRAPH_ACYCLIC"
    SINGLE_OUTPUT_OWNER = "SINGLE_OUTPUT_OWNER"
    EVIDENCE_TYPED_AGAINST_GIT = "EVIDENCE_TYPED_AGAINST_GIT"
    HYPOTHESIS_QUARANTINE = "HYPOTHESIS_QUARANTINE"
    GATE_EXECUTABLE = "GATE_EXECUTABLE"
    BASE_COMMIT_EXISTS = "BASE_COMMIT_EXISTS"
    BRANCHES_EXIST = "BRANCHES_EXIST"
    REVIEW_PAYLOAD_BUDGET = "REVIEW_PAYLOAD_BUDGET"
    LINEAGE_RESOLVES = "LINEAGE_RESOLVES"
    GATE_CORE_UNSHARED = "GATE_CORE_UNSHARED"


#: Named as a tuple as well as an enum so "twelve" is a checkable count
#: rather than a sentence in a docstring.
OBLIGATIONS: Tuple[Obligation, ...] = (
    Obligation.CLOSED_PARSE,
    Obligation.REFERENCES_RESOLVE_ONCE,
    Obligation.GRAPH_ACYCLIC,
    Obligation.SINGLE_OUTPUT_OWNER,
    Obligation.EVIDENCE_TYPED_AGAINST_GIT,
    Obligation.HYPOTHESIS_QUARANTINE,
    Obligation.GATE_EXECUTABLE,
    Obligation.BASE_COMMIT_EXISTS,
    Obligation.BRANCHES_EXIST,
    Obligation.REVIEW_PAYLOAD_BUDGET,
    Obligation.LINEAGE_RESOLVES,
    Obligation.GATE_CORE_UNSHARED,
)


class Outcome(str, Enum):
    """Exactly one outcome per run of the verb (§11.1)."""

    FINALIZATION_ELIGIBLE = "FINALIZATION_ELIGIBLE"
    AUTHORING_BLOCKED = "AUTHORING_BLOCKED"


@dataclass(frozen=True)
class Blocker:
    """One typed blocker with a JSON pointer into the authored file."""

    obligation: Obligation
    pointer: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """`AUTHORING_BLOCKED` has no digest, so there is nothing to publish."""

    outcome: Outcome
    digest: Optional[str]
    blockers: Tuple[Blocker, ...]

    @property
    def eligible(self) -> bool:
        return self.outcome is Outcome.FINALIZATION_ELIGIBLE


@dataclass(frozen=True)
class ValidationConfig:
    """Configuration, never plan content — the same reason retry budgets are
    configuration (§6.2). Tuning a budget must not mint a new digest."""

    review_payload_budget_bytes: int = 262144


class CollectorUnavailable(RuntimeError):
    """The runner could not be run at all — a missing binary, an unreadable
    working directory. An operational refusal with no identity consequence
    (§6.4, §9.5), never an eligibility answer."""


class ReceiptIndex(Protocol):
    """Step 3's receipt store, narrowed to the one bit lineage needs."""

    def has_receipt(self, digest: str) -> bool:  # pragma: no cover - protocol
        ...


class GateCollector(Protocol):
    """How many cases a gate's selector collects on a tree."""

    def collect(self, gate: "pm.Gate", tree: Path) -> int:  # pragma: no cover
        ...


#: How each runner is asked to enumerate without executing.
#:
#: pytest reads ``addopts`` from the repository's own ini file and prepends it,
#: so a repository carrying ``-v`` there cancels the ``-q`` here: verbosity nets
#: to the default and ``--collect-only`` prints its tree form (``<Function x>``)
#: instead of flat ``path::case`` identifiers. ``_count`` reads identifiers, so
#: that renders every gate in such a repository as zero collected cases.
#: ``-o addopts=`` clears the inherited options, making collection output depend
#: on this argv alone.
COLLECT_ARGV: Dict[str, Tuple[str, ...]] = {
    "pytest": ("pytest", "--collect-only", "-q", "-o", "addopts="),
    "vitest": ("vitest", "list", "--run"),
}


@dataclass(frozen=True)
class SubprocessCollector:
    """The real collector: asks the runner to enumerate, never to execute.

    It is a seam rather than a hard dependency because collection is the one
    obligation that must touch the environment, and §6.4 puts environment
    facts outside eligibility. A missing runner raises rather than blocks.
    """

    timeout_s: float = 120.0

    def argv_for(self, gate: "pm.Gate") -> Tuple[str, ...]:
        return COLLECT_ARGV[gate.runner] + tuple(gate.argv)

    def collect(self, gate: "pm.Gate", tree: Path) -> int:
        cwd = Path(tree) / gate.cwd
        if not cwd.is_dir():
            raise CollectorUnavailable(
                "the gate's working directory does not exist: {0}".format(cwd))
        try:
            result = subprocess.run(list(self.argv_for(gate)), cwd=str(cwd),
                                    capture_output=True, text=True,
                                    timeout=self.timeout_s)
        except FileNotFoundError as exc:
            raise CollectorUnavailable(
                "{0} could not be run: {1}".format(gate.runner, exc))
        except subprocess.TimeoutExpired:
            raise CollectorUnavailable(
                "{0} did not finish collecting in {1}s".format(
                    gate.runner, self.timeout_s))
        return self._count(result.stdout)

    @staticmethod
    def _count(stdout: str) -> int:
        """Count enumerated cases. Both runners print one identifier per
        line; the trailing summary lines carry neither `::` nor a path."""
        count = 0
        for line in stdout.splitlines():
            token = line.strip()
            if not token or token.startswith(("=", "-")):
                continue
            if "::" in token or token.endswith((".py", ".ts", ".js", ".tsx")):
                count += 1
        return count


# ── git facts ───────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> Tuple[int, bytes]:
    result = subprocess.run(["git", *args], cwd=str(repo),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode, result.stdout


def commit_exists(repo: Path, sha: str) -> bool:
    """A commit, specifically — a tree sha is not a base commit."""
    code, _ = _git(repo, "cat-file", "-e", "{0}^{{commit}}".format(sha))
    return code == 0


def branch_exists(repo: Path, name: str) -> bool:
    code, _ = _git(repo, "rev-parse", "--verify", "--quiet",
                   "refs/heads/{0}".format(name))
    return code == 0


def blob_at(repo: Path, commit: str, path: str) -> Optional[bytes]:
    """The bytes of `path` at `commit`, or `None` when it is absent there."""
    code, out = _git(repo, "cat-file", "blob", "{0}:{1}".format(commit, path))
    return out if code == 0 else None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ── the obligations ─────────────────────────────────────────────────────────

def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _escapes_tree(path: str) -> bool:
    return path.startswith("/") or path == ".." or path.startswith("../")


def _references_resolve_once(plan: "pm.Plan") -> List[Blocker]:
    blockers: List[Blocker] = []
    node_counts: Dict[str, int] = {}
    for node in plan.nodes:
        node_counts[node.node_id] = node_counts.get(node.node_id, 0) + 1
    evidence_counts: Dict[str, int] = {}
    for item in plan.evidence:
        evidence_counts[item.evidence_id] = evidence_counts.get(
            item.evidence_id, 0) + 1

    for index, node in enumerate(plan.nodes):
        if node_counts[node.node_id] > 1:
            blockers.append(Blocker(
                Obligation.REFERENCES_RESOLVE_ONCE,
                "/nodes/{0}/node_id".format(index),
                "{0} is declared {1} times; a reference to it resolves more "
                "than once".format(node.node_id, node_counts[node.node_id])))
        for position, need in enumerate(node.needs):
            if need not in node_counts:
                blockers.append(Blocker(
                    Obligation.REFERENCES_RESOLVE_ONCE,
                    "/nodes/{0}/needs/{1}".format(index, position),
                    "{0} needs {1}, which no node declares".format(
                        node.node_id, need)))
        for position, read in enumerate(node.reads):
            if read not in evidence_counts:
                blockers.append(Blocker(
                    Obligation.REFERENCES_RESOLVE_ONCE,
                    "/nodes/{0}/reads/{1}".format(index, position),
                    "{0} reads {1}, which no evidence entry declares".format(
                        node.node_id, read)))

    for index, item in enumerate(plan.evidence):
        if evidence_counts[item.evidence_id] > 1:
            blockers.append(Blocker(
                Obligation.REFERENCES_RESOLVE_ONCE,
                "/evidence/{0}/evidence_id".format(index),
                "{0} is declared {1} times".format(
                    item.evidence_id, evidence_counts[item.evidence_id])))
        if isinstance(item, pm.Produced) and item.producer not in node_counts:
            blockers.append(Blocker(
                Obligation.REFERENCES_RESOLVE_ONCE,
                "/evidence/{0}/producer".format(index),
                "{0} names producer {1}, which no node declares".format(
                    item.evidence_id, item.producer)))
    return blockers


def _graph_acyclic(plan: "pm.Plan") -> List[Blocker]:
    """Cycles only. An unresolved `needs` is the previous obligation's, and
    reporting it twice would make one defect look like two."""
    known = {node.node_id: node for node in plan.nodes}
    colour: Dict[str, int] = {}
    blockers: List[Blocker] = []

    def walk(node_id: str, trail: List[str]) -> bool:
        colour[node_id] = 1
        for need in known[node_id].needs:
            if need not in known:
                continue
            state = colour.get(need, 0)
            if state == 1:
                blockers.append(Blocker(
                    Obligation.GRAPH_ACYCLIC,
                    "/nodes/{0}/needs".format(
                        list(known).index(node_id)),
                    "the graph is not acyclic: {0}".format(
                        " -> ".join(trail + [node_id, need]))))
                colour[node_id] = 2
                return True
            if state == 0 and walk(need, trail + [node_id]):
                colour[node_id] = 2
                return True
        colour[node_id] = 2
        return False

    for node_id in list(known):
        if colour.get(node_id, 0) == 0:
            walk(node_id, [])
    return blockers


def _single_output_owner(plan: "pm.Plan") -> List[Blocker]:
    owners: Dict[str, List[Tuple[int, str]]] = {}
    for index, node in enumerate(plan.nodes):
        for position, path in enumerate(node.outputs):
            owners.setdefault(_norm(path), []).append((index, node.node_id))
    blockers: List[Blocker] = []
    for path, claims in sorted(owners.items()):
        if len(claims) > 1:
            blockers.append(Blocker(
                Obligation.SINGLE_OUTPUT_OWNER,
                "/nodes/{0}/outputs".format(claims[0][0]),
                "{0} is claimed by {1}; exactly one node owns an output "
                "path".format(path, ", ".join(name for _, name in claims))))
    return blockers


def _evidence_typed_against_git(plan: "pm.Plan", repo: Path) -> List[Blocker]:
    blockers: List[Blocker] = []
    known = plan.node_by_id()
    for index, item in enumerate(plan.evidence):
        pointer = "/evidence/{0}".format(index)
        if isinstance(item, pm.Observed):
            stored = blob_at(repo, plan.base_commit, item.path)
            if stored is None:
                blockers.append(Blocker(
                    Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                    pointer + "/path",
                    "{0} cites {1}, which is absent at {2}".format(
                        item.evidence_id, item.path, plan.base_commit)))
            elif _sha256(stored) != item.sha256:
                blockers.append(Blocker(
                    Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                    pointer + "/sha256",
                    "{0} cites sha256 {1} for {2}, but the object at {3} "
                    "hashes to {4}".format(item.evidence_id, item.sha256,
                                           item.path, plan.base_commit,
                                           _sha256(stored))))
        elif isinstance(item, pm.Produced):
            producer = known.get(item.producer)
            if producer is not None:
                owned = {_norm(p) for p in producer.outputs}
                if _norm(item.path) not in owned:
                    blockers.append(Blocker(
                        Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                        pointer + "/producer",
                        "{0} names {1} as producer of {2}, which {1} does not "
                        "declare as an output".format(
                            item.evidence_id, item.producer, item.path)))
            stored = blob_at(repo, plan.base_commit, item.path)
            if stored is not None:
                if item.base_sha256 is None:
                    blockers.append(Blocker(
                        Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                        pointer + "/base_sha256",
                        "{0} is produced at {1}, which already exists at base; "
                        "a produced path present at base declares the base "
                        "sha256 it replaces".format(item.evidence_id, item.path)))
                elif _sha256(stored) != item.base_sha256:
                    blockers.append(Blocker(
                        Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                        pointer + "/base_sha256",
                        "{0} declares base sha256 {1} for {2}, but the object "
                        "at {3} hashes to {4}".format(
                            item.evidence_id, item.base_sha256, item.path,
                            plan.base_commit, _sha256(stored))))
    return blockers


def _hypothesis_quarantine(plan: "pm.Plan") -> List[Blocker]:
    """A hypothesis is quarantined *to* an agent node's `reads` — so a code
    node holding one is a violation, and so is one nobody holds at all."""
    blockers: List[Blocker] = []
    readers: Dict[str, List[Tuple[int, Any]]] = {}
    for index, node in enumerate(plan.nodes):
        for read in node.reads:
            readers.setdefault(read, []).append((index, node))
    for index, item in enumerate(plan.evidence):
        if not isinstance(item, pm.Hypothesis):
            continue
        held = readers.get(item.evidence_id, [])
        if not held:
            blockers.append(Blocker(
                Obligation.HYPOTHESIS_QUARANTINE,
                "/evidence/{0}".format(index),
                "{0} is a hypothesis no node reads; it is quarantined to an "
                "agent node's reads, and to nothing else".format(
                    item.evidence_id)))
        for node_index, node in held:
            if not isinstance(node, pm.AgentNode):
                blockers.append(Blocker(
                    Obligation.HYPOTHESIS_QUARANTINE,
                    "/nodes/{0}/reads".format(node_index),
                    "{0} is a code node reading hypothesis {1}; a hypothesis "
                    "is dischargeable only by a rubric check".format(
                        node.node_id, item.evidence_id)))
    return blockers


def _gate_executable(plan: "pm.Plan", repo: Path,
                     collector: GateCollector) -> List[Blocker]:
    blockers: List[Blocker] = []
    produced = set(plan.declared_outputs())

    def check(gate: "pm.Gate", pointer: str, label: str,
              selector_required: bool) -> None:
        selector = pm.selector_of(gate)
        if selector is None:
            if selector_required:
                blockers.append(Blocker(
                    Obligation.GATE_EXECUTABLE, pointer + "/argv",
                    "{0}'s argv names no selector, so it falls back to the "
                    "runner's default whole-tree collection; that is not a "
                    "node gate (§6.2)".format(label)))
            return
        paths = pm.selector_paths(gate)
        ill_formed = [p for p in paths if _escapes_tree(p)]
        if ill_formed:
            blockers.append(Blocker(
                Obligation.GATE_EXECUTABLE, pointer + "/argv",
                "{0}'s selector does not resolve to a declarable path in the "
                "repository: {1}".format(label, ", ".join(ill_formed))))
            return
        if _escapes_tree(_norm(gate.cwd)):
            blockers.append(Blocker(
                Obligation.GATE_EXECUTABLE, pointer + "/cwd",
                "{0}'s working directory leaves the repository: {1}".format(
                    label, gate.cwd)))
            return
        if paths and all(path in produced for path in paths):
            # The produced arm: a collection count here would have to be zero,
            # because the node exists to create what the selector names.
            return
        collected = collector.collect(gate, repo)
        if collected < gate.min_cases:
            blockers.append(Blocker(
                Obligation.GATE_EXECUTABLE, pointer + "/min_cases",
                "{0}'s selector collects {1} case(s) at base, below the "
                "declared min_cases of {2}".format(
                    label, collected, gate.min_cases)))

    for index, node in enumerate(plan.nodes):
        if isinstance(node, pm.AgentNode):
            check(node.gate, "/nodes/{0}/gate".format(index), node.node_id,
                  selector_required=True)
    # The integration gate is the plan's one whole-suite gate (§6.2, §8.8),
    # so it is the single gate permitted to name no selector.
    check(plan.merge_policy.integration_gate,
          "/merge_policy/integration_gate", "the integration gate",
          selector_required=False)
    return blockers


def _gate_core_unshared(plan: "pm.Plan") -> List[Blocker]:
    blockers: List[Blocker] = []
    integration_core = pm.command_core(plan.merge_policy.integration_gate)
    seen: Dict[Any, Tuple[int, str]] = {}
    for index, node in enumerate(plan.nodes):
        if not isinstance(node, pm.AgentNode):
            continue
        core = pm.command_core(node.gate)
        pointer = "/nodes/{0}/gate".format(index)
        if core == integration_core:
            blockers.append(Blocker(
                Obligation.GATE_CORE_UNSHARED, pointer,
                "{0} declares the plan's integration gate as its own "
                "acceptance; a whole-suite gate cannot pass while a sibling "
                "is unmerged (§7.4)".format(node.node_id)))
        if core in seen:
            blockers.append(Blocker(
                Obligation.GATE_CORE_UNSHARED, pointer,
                "{0} and {1} share a gate command core; two agent nodes "
                "accepting on one cloned command is one acceptance, not "
                "two".format(seen[core][1], node.node_id)))
        else:
            seen[core] = (index, node.node_id)
    return blockers


def validate_plan(stored: bytes, repo: Union[str, Path], *,
                  receipts: ReceiptIndex, collector: GateCollector,
                  config: Optional[ValidationConfig] = None) -> ValidationResult:
    """Run every deterministic obligation and emit exactly one outcome.

    `repo` is the working repository the git facts are read from. Nothing is
    written anywhere, no reviewer is launched, and nothing is published: the
    verb's whole output is the outcome below (§11.1).
    """
    config = config or ValidationConfig()
    repo_path = Path(repo)
    blockers: List[Blocker] = []

    try:
        plan = pm.parse_bytes(stored)
    except pm.PlanParseError as exc:
        for pointer, message in (exc.pointers or (("", str(exc)),)):
            blockers.append(Blocker(Obligation.CLOSED_PARSE, pointer, message))
        return ValidationResult(Outcome.AUTHORING_BLOCKED, None, tuple(blockers))

    if not pc.is_canonical(stored):
        blockers.append(Blocker(
            Obligation.CLOSED_PARSE, "",
            "the stored bytes are not in canonical form; the digest is taken "
            "over stored bytes (§6.3), so two spellings of one plan would be "
            "two identities"))

    blockers.extend(_references_resolve_once(plan))
    blockers.extend(_graph_acyclic(plan))
    blockers.extend(_single_output_owner(plan))

    base_present = commit_exists(repo_path, plan.base_commit)
    if not base_present:
        blockers.append(Blocker(
            Obligation.BASE_COMMIT_EXISTS, "/base_commit",
            "{0} is not a commit in this repository".format(plan.base_commit)))
    else:
        # Every evidence duty is stated at `base_commit`; with no base commit
        # there is no tree to verify against, and reporting each citation as
        # fabricated would misattribute one defect to many.
        blockers.extend(_evidence_typed_against_git(plan, repo_path))

    blockers.extend(_hypothesis_quarantine(plan))
    blockers.extend(_gate_executable(plan, repo_path, collector))

    branch = plan.merge_policy.integration_branch
    if not branch_exists(repo_path, branch):
        blockers.append(Blocker(
            Obligation.BRANCHES_EXIST, "/merge_policy/integration_branch",
            "{0} is not a branch in this repository".format(branch)))

    if len(stored) > config.review_payload_budget_bytes:
        blockers.append(Blocker(
            Obligation.REVIEW_PAYLOAD_BUDGET, "",
            "the review payload is {0} bytes, over the {1}-byte budget; "
            "oversized plans are refused, not chunked (§6.5)".format(
                len(stored), config.review_payload_budget_bytes)))

    if plan.supersedes is not None and not receipts.has_receipt(plan.supersedes):
        blockers.append(Blocker(
            Obligation.LINEAGE_RESOLVES, "/supersedes",
            "no receipt exists for the superseded digest {0}".format(
                plan.supersedes)))

    blockers.extend(_gate_core_unshared(plan))

    if blockers:
        return ValidationResult(Outcome.AUTHORING_BLOCKED, None, tuple(blockers))
    return ValidationResult(Outcome.FINALIZATION_ELIGIBLE,
                            pd.digest_of(stored), ())
