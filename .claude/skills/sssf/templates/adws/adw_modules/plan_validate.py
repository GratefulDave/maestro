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
from typing import (Any, Dict, List, Mapping, Optional, Sequence, Set,
                    Tuple, Union)

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


class GitReadFailed(RuntimeError):
    """git failed. This is a fact about the machine, never about the repository.

    §7.5: "Only git's documented not-found exit code means 'the object is
    absent.' Every other nonzero exit is ENVIRONMENTAL, never a fact about the
    repository. Without it, a transient git failure is recorded as a missing
    object, and a deterministic read over git objects — which every
    eligibility obligation depends on (§6.4) — silently returns a wrong answer
    instead of failing."
    """


class GitPathNotAFile(ValueError):
    """The path exists at that commit and is not a blob.

    A distinct answer from absence, because a plan citing a directory is a
    different defect from a plan citing a file that is not there, and an
    operator told the wrong one goes looking in the wrong place.
    """


def blob_at(repo: Path, commit: str, path: str) -> Optional[bytes]:
    """The bytes of `path` at `commit`, or `None` when it is proven absent.

    Absence is derived from a **successful** command's empty output, never
    from a failure exit — which is the whole point. This used to be
    `git cat-file blob <commit>:<path>` with `return out if code == 0 else
    None`, and that form cannot support the distinction §7.5 requires:
    `cat-file blob` exits 128 for a missing path, for a directory, for an
    invalid revision, and for a path outside the repository alike, so every
    one of those became "the object is absent" — as did a git that failed for
    any reason at all.

    `ls-tree` answers the question the caller is actually asking, and answers
    it with exit zero: no record means the path is not in that tree, one
    record carries git's own word for what it is. A nonzero exit is therefore
    unambiguously an environmental failure and raises rather than resolving to
    a fact. The blob is then read by object id rather than by path, so the
    second call reads something already proven to exist.
    """
    code, out = _git(repo, "ls-tree", "-z", "--full-tree", commit, "--", path)
    if code != 0:
        raise GitReadFailed(
            "GIT_READ_FAILED:ls-tree {0} -- {1}".format(commit, path))
    records = [record for record in out.split(b"\x00") if record]
    if not records:
        return None
    if len(records) > 1:
        # A pathspec matched several entries, so it was a pattern rather than
        # a path. Not absence, and not a file.
        raise GitPathNotAFile(
            "GIT_PATH_NOT_A_FILE:{0}@{1}:matched {2} entries".format(
                path, commit, len(records)))
    try:
        meta, _, _name = records[0].partition(b"\t")
        _mode, kind, object_id = meta.split(b" ", 2)
        kind_name = kind.decode("ascii", "replace")
        object_name = object_id.decode("ascii", "replace")
    except ValueError as exc:
        raise GitReadFailed(
            "GIT_READ_FAILED:unparseable ls-tree record for {0}@{1}".format(
                path, commit)) from exc
    if kind_name != "blob":
        raise GitPathNotAFile(
            "GIT_PATH_NOT_A_FILE:{0}@{1}:is a {2}".format(
                path, commit, kind_name))
    code, out = _git(repo, "cat-file", "blob", object_name)
    if code != 0:
        # The object id came from `ls-tree`, so it exists; a failure to read it
        # is the machine, not the repository.
        raise GitReadFailed(
            "GIT_READ_FAILED:cat-file blob {0} for {1}@{2}".format(
                object_name, path, commit))
    return out


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
            # `GitPathNotAFile` is a fact about the repository, proven by a
            # command that exited zero, so it is a blocker like any other. A
            # `GitReadFailed` is not, and deliberately propagates: §6.4 keeps
            # environment failures out of the blocker list, because a blocker
            # says the authored bytes are wrong and a broken git does not.
            try:
                stored = blob_at(repo, plan.base_commit, item.path)
            except GitPathNotAFile as exc:
                blockers.append(Blocker(
                    Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                    pointer + "/path",
                    "{0} cites {1}, which at {2} is not a file: {3}".format(
                        item.evidence_id, item.path, plan.base_commit, exc)))
                continue
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
            try:
                stored = blob_at(repo, plan.base_commit, item.path)
            except GitPathNotAFile as exc:
                blockers.append(Blocker(
                    Obligation.EVIDENCE_TYPED_AGAINST_GIT,
                    pointer + "/path",
                    "{0} produces {1}, which at {2} is not a file: {3}".format(
                        item.evidence_id, item.path, plan.base_commit, exc)))
                continue
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


# ── contract-IR admission: a lane's declared surface must be reachable ──────
#
# Recorded failure, run-0120c32064d144c2aa55c344087e0b0a. Plan
# `cmo-consolidation-l` carried a lane, `lane-p1-freeze-and-run-log`, whose
# requirement was behaviour *over the legacy writers* — freeze them at a
# high-water mark, and prove no code path updates a historical record in
# place. Its declared outputs were one new module and that module's test, and
# the legacy writers appeared in the declared outputs of none of the plan's
# fourteen lanes. The builder therefore could not write the file the behaviour
# needed; §8.3's permission-delta check would have rejected it if it had
# tried; and every attempt produced an out-of-contract workaround that the
# reviewer correctly rejected. The node burned its whole retry budget on a
# task no correct attempt could pass, and nothing before the run said so.
#
# The gap was invisible to every relation the IR carried, and that is the
# finding rather than an implementation detail. `seams[].producer` and
# `.consumer`, `fixtures[].consumer_obligation` and `.prohibited_behavior`,
# `claims[].object`, `verifiers[].oracle` — all prose. The typed relations
# that do carry paths — `fixtures[].path` with `affected_lane_ids`,
# `claims[].subject`, `source_artifacts[].path`, the verifier's argv, and
# `extensions.maestro.outputs` — were every one of them *satisfied* by that
# lane: it read `tests/conftest.py`, which is a pinned source artifact, and it
# gated on the test file it produced. Nothing structural named the legacy
# writers. So the fact the check needs was not merely hard to reach; it was
# not declared anywhere, and no predicate over the existing relations could
# have recovered it.
#
# Deciding it from the requirement's prose is refused by §1.2: no lifecycle
# transition may be caused by free text, and an admission decision is a
# lifecycle transition. The repair is therefore a declared field —
# `requirements[].surface`, a list of `{path, mutation}` records — checked
# structurally against what the plan says each lane may write. The field is
# **required from v1** rather than optional, on §3.6 B8: a field added later
# is optional forever, and an optional write surface would be declared by
# exactly the plans that had one to declare.
#
# Two independent declarations are what make the check non-trivial. The
# requirement says where its behaviour lives; `extensions.maestro.outputs`
# says what the lane may write; containment between them is a fact neither
# author of either field states directly. A requirement that names a path no
# lane can write is refused here, before a run exists. A requirement whose
# prose and whose declared surface disagree is a semantic question, and it is
# a single reviewable cell for the plan-contract reviewer (§3.6 B12) rather
# than an invisible gap — which is the most a structural check can honestly
# claim, and it is stated here so the claim is not read as more.


class SurfaceObligation(str, Enum):
    """The two admission obligations over a `plan-contract.v1` IR."""

    #: The IR states a surface at all, in the declared shape.
    SURFACE_DECLARED = "SURFACE_DECLARED"
    #: Every declared path is reachable from the lane that must satisfy it.
    SURFACE_REACHABLE = "SURFACE_REACHABLE"


SURFACE_OBLIGATIONS: Tuple[SurfaceObligation, ...] = (
    SurfaceObligation.SURFACE_DECLARED,
    SurfaceObligation.SURFACE_REACHABLE,
)


#: The three ways a requirement's behaviour can relate to a repository path,
#: and the containment each one asserts. There is no fourth, and no default:
#: an unrecognised value is a blocker rather than a guess, because the whole
#: point of the field is that the author states which of the three it is.
#:
#: * ``written`` — the lane must create or modify this path. It must be one of
#:   that lane's own declared outputs. A dependency's output does not satisfy
#:   this: writing another lane's output from this worktree is exactly what
#:   §8.3's permission delta rejects, and §6.4's single-output-owner
#:   obligation forbids two lanes claiming it.
#: * ``inherited`` — the behaviour lives in a path some lane in this lane's
#:   `depends_on` closure produces. The lane reads it; it does not write it.
#: * ``unmodified`` — the behaviour is asserted over a pre-existing path that
#:   the plan changes nowhere. It must be a declared, hash-pinned
#:   `source_artifacts` entry, and it must not appear in any lane's outputs —
#:   a path some lane rewrites is not unmodified, whoever says otherwise.
MUTATIONS: Tuple[str, ...] = ("written", "inherited", "unmodified")


@dataclass(frozen=True)
class SurfaceBlocker:
    """One typed blocker with a JSON pointer into the authored IR.

    Deliberately not `Blocker`: those twelve are obligations over
    `maestro-plan.v1` stored bytes and their count is asserted as twelve.
    These two are obligations over the contract IR, which is a different
    document with different pointers, and merging the two enums would make
    "the twelve" a sentence again rather than a checkable count.
    """

    obligation: SurfaceObligation
    pointer: str
    message: str


def _normalized_path(value: str) -> str:
    return posixpath.normpath(value)


def _is_declarable(path: Any) -> bool:
    """A relative repository path that does not leave the tree."""
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    return not any(part in (".", "..") for part in path.split("/"))


def _lane_outputs(ir: Mapping[str, Any]) -> Dict[str, Set[str]]:
    """`extensions.maestro.outputs`, normalized, keyed by lane id.

    Malformed shapes resolve to no outputs rather than raising: the ingress
    boundary already refuses those with `UNMAPPABLE_OUTPUTS`, naming the lane,
    and a second refusal for the same defect would report one plan error as
    two in two vocabularies.
    """
    extensions = ir.get("extensions")
    maestro = extensions.get("maestro") if isinstance(extensions, dict) else None
    declared = maestro.get("outputs") if isinstance(maestro, dict) else None
    outputs: Dict[str, Set[str]] = {}
    if not isinstance(declared, dict):
        return outputs
    for lane_id, paths in declared.items():
        if not isinstance(lane_id, str) or not isinstance(paths, list):
            continue
        outputs[lane_id] = {_normalized_path(path) for path in paths
                            if isinstance(path, str) and path}
    return outputs


def _source_paths(ir: Mapping[str, Any]) -> Set[str]:
    sources = ir.get("source_artifacts")
    if not isinstance(sources, list):
        return set()
    paths: Set[str] = set()
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            paths.add(_normalized_path(source["path"]))
    return paths


def _depends_closure(lane_id: str,
                     depends: Mapping[str, Sequence[str]]) -> Set[str]:
    """Every lane reachable through `depends_on`, excluding the lane itself.

    Iterative and visited-guarded because a cycle is §6.4's own obligation to
    report against the projected plan; discovering one here must not hang the
    admission check that runs before it.
    """
    seen: Set[str] = set()
    frontier: List[str] = list(depends.get(lane_id, ()))
    while frontier:
        current = frontier.pop()
        if current in seen or current == lane_id:
            continue
        seen.add(current)
        frontier.extend(depends.get(current, ()))
    return seen


def _requirement_surface(index: int, requirement: Mapping[str, Any],
                         blockers: List[SurfaceBlocker]
                         ) -> List[Tuple[int, str, str]]:
    """The requirement's surface as `(entry index, path, mutation)` triples.

    Every malformed entry is reported and dropped, so one unreadable record
    does not hide the reachable/unreachable answer for the records beside it.
    """
    pointer = "/requirements/{0}/surface".format(index)
    declared = requirement.get("surface")
    if declared is None:
        blockers.append(SurfaceBlocker(
            SurfaceObligation.SURFACE_DECLARED, pointer,
            "requirement {0} declares no surface; a requirement states the "
            "repository paths its behaviour lives in, so a lane that cannot "
            "write them is refused before a run starts rather than after a "
            "node has spent its retry budget".format(
                requirement.get("requirement_id", "at index {0}".format(index)))))
        return []
    if not isinstance(declared, list) or not declared:
        blockers.append(SurfaceBlocker(
            SurfaceObligation.SURFACE_DECLARED, pointer,
            "requirement {0} declares a surface that is not a non-empty "
            "list".format(requirement.get("requirement_id",
                                          "at index {0}".format(index)))))
        return []
    entries: List[Tuple[int, str, str]] = []
    for position, entry in enumerate(declared):
        entry_pointer = "{0}/{1}".format(pointer, position)
        if not isinstance(entry, dict):
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_DECLARED, entry_pointer,
                "a surface entry is a {path, mutation} object"))
            continue
        path = entry.get("path")
        if not _is_declarable(path):
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_DECLARED, entry_pointer + "/path",
                "{0!r} is not a relative repository path".format(path)))
            continue
        mutation = entry.get("mutation")
        if mutation not in MUTATIONS:
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_DECLARED,
                entry_pointer + "/mutation",
                "{0!r} is not one of {1}".format(
                    mutation, ", ".join(MUTATIONS))))
            continue
        entries.append((position, _normalized_path(path), mutation))
    return entries


def validate_contract_surface(ir: Mapping[str, Any]
                              ) -> Tuple[SurfaceBlocker, ...]:
    """Refuse a `plan-contract.v1` IR whose lane cannot satisfy its contract.

    The question is asked of the whole plan rather than of one lane: a path is
    reachable when it lies in the lane's own declared outputs, in the outputs
    of a lane in its `depends_on` closure, or among the hash-pinned source
    artifacts the plan declares and changes nowhere. Which of the three a
    requirement means is the requirement's own `mutation`, not an inference,
    so a `written` path is never satisfied by a sibling's output and an
    `unmodified` path is never satisfied by one either.

    Nothing here reads `requirements[].text`, `verifiers[].oracle`,
    `seams[].producer`, `fixtures[].meaning`, or any other free-text field.
    Every input is a declared path, a declared enumerated value, or a declared
    id — §1.2.
    """
    blockers: List[SurfaceBlocker] = []

    requirements = ir.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        blockers.append(SurfaceBlocker(
            SurfaceObligation.SURFACE_DECLARED, "/requirements",
            "the plan declares no requirements, so no lane has a contract to "
            "be checked against; an executable plan states what it must make "
            "true"))
        return tuple(blockers)

    outputs = _lane_outputs(ir)
    every_output: Set[str] = set()
    for paths in outputs.values():
        every_output |= paths
    pinned = _source_paths(ir)

    lanes = ir.get("lanes")
    lane_records = [lane for lane in lanes if isinstance(lane, dict)] \
        if isinstance(lanes, list) else []
    depends: Dict[str, Sequence[str]] = {}
    lanes_by_requirement: Dict[str, List[str]] = {}
    for lane in lane_records:
        lane_id = lane.get("lane_id")
        if not isinstance(lane_id, str) or not lane_id:
            continue
        needs = lane.get("depends_on")
        depends[lane_id] = [need for need in needs
                            if isinstance(need, str)] \
            if isinstance(needs, list) else []
        bound = lane.get("requirement_ids")
        if not isinstance(bound, list):
            continue
        for requirement_id in bound:
            if isinstance(requirement_id, str) and requirement_id:
                lanes_by_requirement.setdefault(requirement_id, []).append(
                    lane_id)

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_DECLARED,
                "/requirements/{0}".format(index),
                "a requirement is an object"))
            continue
        requirement_id = requirement.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_DECLARED,
                "/requirements/{0}/requirement_id".format(index),
                "a requirement declares an id, which is how a lane binds to "
                "it"))
            continue
        entries = _requirement_surface(index, requirement, blockers)
        owners = lanes_by_requirement.get(requirement_id, [])
        if not owners:
            blockers.append(SurfaceBlocker(
                SurfaceObligation.SURFACE_REACHABLE,
                "/requirements/{0}/requirement_id".format(index),
                "{0} is declared by no lane, so no node in this plan can "
                "satisfy it".format(requirement_id)))
            continue
        for lane_id in owners:
            own = outputs.get(lane_id, set())
            upstream: Set[str] = set()
            for upstream_lane in _depends_closure(lane_id, depends):
                upstream |= outputs.get(upstream_lane, set())
            for position, path, mutation in entries:
                pointer = "/requirements/{0}/surface/{1}/path".format(
                    index, position)
                if mutation == "written":
                    if path in own:
                        continue
                    holder = sorted(other for other, paths in outputs.items()
                                    if path in paths)
                    elsewhere = (
                        "it is declared as an output of " + ", ".join(holder)
                        if holder else
                        "no lane in this plan declares it as an output")
                    blockers.append(SurfaceBlocker(
                        SurfaceObligation.SURFACE_REACHABLE, pointer,
                        "lane {0} must write {1} to satisfy {2}, but {1} is "
                        "not one of that lane's declared outputs ({3}); {4}. "
                        "No attempt at this lane can write it, so every "
                        "attempt either fails the permission-delta check or "
                        "leaves the contract".format(
                            lane_id, path, requirement_id,
                            ", ".join(sorted(own)) or "none", elsewhere)))
                elif mutation == "inherited":
                    if path in upstream:
                        continue
                    blockers.append(SurfaceBlocker(
                        SurfaceObligation.SURFACE_REACHABLE, pointer,
                        "lane {0} inherits {1} for {2}, but no lane in its "
                        "depends_on closure ({3}) produces it".format(
                            lane_id, path, requirement_id,
                            ", ".join(sorted(_depends_closure(lane_id, depends)))
                            or "empty")))
                else:
                    if path not in pinned:
                        blockers.append(SurfaceBlocker(
                            SurfaceObligation.SURFACE_REACHABLE, pointer,
                            "lane {0} asserts unmodified behaviour over {1} "
                            "for {2}, but {1} is not a declared source "
                            "artifact, so nothing pins the bytes the "
                            "assertion is about".format(
                                lane_id, path, requirement_id)))
                    if path in every_output:
                        holder = sorted(other for other, paths in outputs.items()
                                        if path in paths)
                        blockers.append(SurfaceBlocker(
                            SurfaceObligation.SURFACE_REACHABLE, pointer,
                            "lane {0} asserts {1} is unmodified for {2}, but "
                            "{3} declares it as an output".format(
                                lane_id, path, requirement_id,
                                ", ".join(holder))))
    return tuple(blockers)
