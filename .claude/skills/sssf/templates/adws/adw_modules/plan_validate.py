"""The fourteen deterministic obligations, and the typed blockers (§6.4).

`maestro plan validate` runs every obligation and emits exactly one outcome
(§11.1). `FINALIZATION_ELIGIBLE` carries the canonical digest and publishes
nothing. `AUTHORING_BLOCKED` carries typed blockers with JSON pointers into
the plan, launches no reviewer, and publishes nothing — the blocked result
has no digest field to publish, so "publishes nothing" is a shape rather
than a promise.

Thirteen of the fourteen are computed from the stored bytes and git objects
alone — the last two of the fourteen need neither, being pure functions over
the parsed plan. The two facts that
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

Two more obligations are gate *scope* rather than gate executability, and
they were checks a prose reviewer used to be asked (§3.6 B12's rubric,
`gate.selector_is_scoped_to_this_node` and
`gate.selector_covers_the_merged_surface`). A reviewer answering them was
answering a question a pure function over `Gate.argv` and the nodes'
declared `outputs` decides exactly, so they are refusals here instead:

* **`GATE_SELECTOR_NODE_SCOPED`** — a node's gate selector may name that
  node's own outputs, or paths no node in the plan claims; it may not reach
  into a *sibling's* declared output. The literal rubric question was
  stricter — selector paths a subset of this node's outputs — and would
  refuse plans `GATE_EXECUTABLE` deliberately admits, where a gate names a
  test file that already exists at base and no node owns. This is the weaker
  form, and it still convicts the defect the rubric was written for: a node
  accepting on a lane it does not build.
* **`INTEGRATION_GATE_COVERS_LANES`** — the integration gate is the plan's
  one whole-suite gate (§6.2, §8.8), so a selector naming *no* path covers
  the merged surface by construction and passes. A selector naming paths
  must contain every node gate's selector paths under posix prefix
  containment; a node gate whose own selector names no path is covered only
  by the whole-suite case, because there is no path to contain. Each lane's
  own gate selector is the definition of that lane's test surface: deciding
  which of a node's `outputs` is a *test* output would need a naming
  heuristic, which is the judgment this promotion exists to remove.

Blockers are collected rather than raised one at a time: §11.1 emits typed
blockers, plural, and a fail-fast validator makes an author fix fourteen
plans instead of one. The single exception is a plan that does not parse, where
nothing downstream has a model to run against.
"""

from __future__ import annotations

import hashlib
import posixpath
import subprocess
from dataclasses import dataclass, field
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
from . import runner_resolution as rr
from . import tests_chain as tc


class Obligation(str, Enum):
    """The fourteen from §6.4, plus the tests/build pair.

    The last two of the original fourteen arrived from the plan-review rubric
    rather than from §6.4's original list: they were the only two of its
    eleven checks that a pure function over the stored plan decides with no
    judgment about prose, so they are stated here as refusals instead of
    being asked of a reviewer.

    `TESTS_BUILD_PAIRED` is the fifteenth: a `tests` node is not an agent
    node, and without this check a tests node with no dependent, or a build
    node that still owns the test files, would parse.
    """

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
    GATE_SELECTOR_NODE_SCOPED = "GATE_SELECTOR_NODE_SCOPED"
    INTEGRATION_GATE_COVERS_LANES = "INTEGRATION_GATE_COVERS_LANES"
    TESTS_BUILD_PAIRED = "TESTS_BUILD_PAIRED"


#: Named as a tuple as well as an enum so the count is checkable rather than
#: a sentence in a docstring.
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
    Obligation.GATE_SELECTOR_NODE_SCOPED,
    Obligation.INTEGRATION_GATE_COVERS_LANES,
    Obligation.TESTS_BUILD_PAIRED,
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


#: The collection flags, without a binary — the table used to carry one.
#:
#: `COLLECT_ARGV["pytest"]` began `("pytest", …)`, so the interpreter that
#: enumerated a gate was whatever `PATH` exposed to the shell that started the
#: verb. On the machine this was measured on that was pytest 8.4.0 from
#: homebrew, which cannot import the target repository's `conftest.py` and
#: reports every gate as zero collected cases. The binary now comes from
#: `runner_resolution.ResolvedRunner`, and this module re-exports the flags
#: from there so there is exactly one table rather than two that can disagree.
COLLECT_ARGS = rr.COLLECT_ARGS


@dataclass(frozen=True)
class SubprocessCollector:
    """The real collector: asks the runner to enumerate, never to execute.

    It is a seam rather than a hard dependency because collection is the one
    obligation that must touch the environment, and §6.4 puts environment
    facts outside eligibility. A missing runner raises rather than blocks.

    The runner is **resolved**, never inherited. `resolver` is the single
    producer `runner_resolution.resolve`, memoised per `(runner, cwd)` so the
    fourteen obligations pay the capability probe once for a plan rather than
    once per gate. A resolution that refuses becomes `CollectorUnavailable` —
    which is a reuse, not a new type: this module's own docstring already
    states that a collector which cannot run is "an operational refusal with
    no identity consequence, rather than being recorded as a blocker that
    would make a missing binary look like a defect in the authored bytes",
    and a runner that cannot import the repository is exactly that.
    """

    timeout_s: float = 120.0
    #: Declared `runners:` values by runner literal, from `maestro.config.yaml`.
    #: Empty means nothing is declared and discovery proposes.
    declared: Mapping[str, str] = field(default_factory=dict)
    env: Optional[Mapping[str, str]] = None
    resolver: Any = staticmethod(rr.resolve)
    _cache: Dict[Tuple[str, str], "rr.ResolvedRunner"] = field(
        default_factory=dict, repr=False, compare=False)

    def runner_for(self, gate: "pm.Gate", tree: Path) -> "rr.ResolvedRunner":
        key = (gate.runner, str(gate.cwd))
        if key not in self._cache:
            try:
                self._cache[key] = self.resolver(
                    gate.runner, Path(tree), gate.cwd,
                    declared=self.declared.get(gate.runner), env=self.env)
            except rr.RunnerUnusable as exc:
                raise CollectorUnavailable(
                    "{0}:{1}".format(rr.RUNNER_UNUSABLE, exc.detail)) from exc
        return self._cache[key]

    def argv_for(self, gate: "pm.Gate", tree: Path) -> Tuple[str, ...]:
        return self.runner_for(gate, tree).collect_argv(gate)

    def collect(self, gate: "pm.Gate", tree: Path) -> int:
        cwd = Path(tree) / gate.cwd
        if not cwd.is_dir():
            raise CollectorUnavailable(
                "the gate's working directory does not exist: {0}".format(cwd))
        try:
            result = subprocess.run(list(self.argv_for(gate, tree)),
                                    cwd=str(cwd),
                                    env=dict(self.env) if self.env is not None
                                    else None,
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
        if paths:
            unproduced = tuple(path for path in paths if path not in produced)
            if not unproduced:
                # The produced arm: a collection count here would have to be
                # zero, because the node exists to create what the selector
                # names.
                return
            if len(unproduced) < len(paths):
                # The mixed arm. `all(...)` used to decide this, so a selector
                # naming one already-merged test file alongside files the run
                # will create missed the produced arm entirely and was
                # collected whole at base — where one absent path makes the
                # runner refuse and report zero for the entire selector.
                # Measured: 11 cases collected from a pre-existing file, 0
                # from the same file plus one not-yet-written path.
                #
                # So collect the unproduced subset alone, and compare it
                # against one, not against `min_cases`. `min_cases` counts
                # what the gate must pass *after* merge, when the produced
                # paths exist; a base-time count over part of the selector is
                # not that quantity, and asserting it is refuses every plan
                # whose gate spans merged and unmerged work.
                restricted = pm.restrict_selector(gate, unproduced)
                collected = collector.collect(restricted, repo)
                if collected < 1:
                    blockers.append(Blocker(
                        Obligation.GATE_EXECUTABLE, pointer + "/argv",
                        "{0}'s selector mixes {1} path(s) the plan produces "
                        "with {2} that exist at base; those {2} collect {3} "
                        "case(s), so the part of the selector that could be "
                        "executable now names nothing. min_cases ({4}) is not "
                        "the comparand for a base-time count over part of a "
                        "selector: {5}".format(
                            label, len(paths) - len(unproduced),
                            len(unproduced), collected, gate.min_cases,
                            ", ".join(unproduced))))
                return
        collected = collector.collect(gate, repo)
        if collected < gate.min_cases:
            blockers.append(Blocker(
                Obligation.GATE_EXECUTABLE, pointer + "/min_cases",
                "{0}'s selector collects {1} case(s) at base, below the "
                "declared min_cases of {2}".format(
                    label, collected, gate.min_cases)))

    for index, node in enumerate(plan.nodes):
        if isinstance(node, (pm.AgentNode, pm.TestsNode)):
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
    by_id = plan.node_by_id()
    for index, node in enumerate(plan.nodes):
        if not isinstance(node, (pm.AgentNode, pm.TestsNode)):
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
            other = by_id[seen[core][1]]
            if _tests_build_may_share_gate(node, other):
                continue
            blockers.append(Blocker(
                Obligation.GATE_CORE_UNSHARED, pointer,
                "{0} and {1} share a gate command core; two agent nodes "
                "accepting on one cloned command is one acceptance, not "
                "two".format(seen[core][1], node.node_id)))
        else:
            seen[core] = (index, node.node_id)
    return blockers


def _tests_build_may_share_gate(left: Any, right: Any) -> bool:
    """A tests node and the one agent that needs it may share a gate core.

    That shared command is the pair's contract: the tests node writes the
    cases, the build node makes them pass. Two tests nodes, or two agents,
    still may not share.
    """
    if isinstance(left, pm.TestsNode) and isinstance(right, pm.AgentNode):
        return left.node_id in right.needs
    if isinstance(right, pm.TestsNode) and isinstance(left, pm.AgentNode):
        return right.node_id in left.needs
    return False


def _path_under(path: str, prefix: str) -> bool:
    """Posix prefix containment over two already-normalized paths.

    `.` is the whole tree, which `posixpath.normpath` produces for an argv
    token of `.` or `./`, and a normalized path never carries the `./` a
    naive `startswith` would need.
    """
    if prefix == ".":
        return True
    return path == prefix or path.startswith(prefix + "/")


def _gate_selector_node_scoped(plan: "pm.Plan") -> List[Blocker]:
    """A node gate may not accept on a sibling lane's declared output.

    The weak form of the rubric's question (see the module docstring): a
    selector path passes if it is one of this node's own normalized outputs,
    or if it is under no *other* node's declared output. A selector naming a
    pre-existing file no node claims stays admitted, which is what keeps
    `GATE_EXECUTABLE`'s all-existing and mixed arms reachable.

    Ownership is resolved in declaration order, so the sibling a message
    names is the same one on every run over the same bytes. A path claimed by
    two nodes is `SINGLE_OUTPUT_OWNER`'s defect, not this one's.
    """
    owners: List[Tuple[str, str]] = []
    claimed: Set[str] = set()
    for node in plan.nodes:
        for path in node.outputs:
            normalized = _norm(path)
            if normalized in claimed:
                continue
            claimed.add(normalized)
            owners.append((normalized, node.node_id))

    blockers: List[Blocker] = []
    by_id = plan.node_by_id()
    for index, node in enumerate(plan.nodes):
        if not isinstance(node, (pm.AgentNode, pm.TestsNode)):
            continue
        own = {_norm(path) for path in node.outputs}
        needed = set(node.needs)
        for path in pm.selector_paths(node.gate):
            if path in own:
                continue
            for owned, owner_id in owners:
                if owner_id == node.node_id:
                    continue
                if (owner_id in needed
                        and isinstance(by_id.get(owner_id), pm.TestsNode)):
                    continue
                if not _path_under(path, owned):
                    continue
                blockers.append(Blocker(
                    Obligation.GATE_SELECTOR_NODE_SCOPED,
                    "/nodes/{0}/gate/argv".format(index),
                    "{0}'s gate selects {1}, which {2} declares as its output "
                    "({3}); a node's gate is scoped to that node's own work "
                    "(§6.2), so accepting on a sibling lane's output is that "
                    "lane's acceptance wearing this node's name".format(
                        node.node_id, path, owner_id, owned)))
                break
    return blockers


def _integration_gate_covers_lanes(plan: "pm.Plan") -> List[Blocker]:
    """The integration gate names the whole surface the plan produces.

    Two arms, and the empty selector is the permissive one. An integration
    gate whose argv names no path falls back to the runner's whole-tree
    collection — which is what the integration gate is for (§6.2, §8.8) — and
    covers every lane by construction.

    Otherwise every agent node's gate selector paths must sit under some
    integration-gate selector path. A node gate that itself names no path is
    covered **only** in the whole-suite case just described: there is no path
    to contain, so a narrowed integration gate cannot be shown to reach it,
    and the message says so rather than passing it silently. Such a gate is
    already `GATE_EXECUTABLE`'s defect; blockers are collected, so it is
    reported by both.
    """
    covers = pm.selector_paths(plan.merge_policy.integration_gate)
    if not covers:
        return []

    pointer = "/merge_policy/integration_gate/argv"
    blockers: List[Blocker] = []
    for node in plan.nodes:
        if not isinstance(node, (pm.AgentNode, pm.TestsNode)):
            continue
        paths = pm.selector_paths(node.gate)
        if not paths:
            blockers.append(Blocker(
                Obligation.INTEGRATION_GATE_COVERS_LANES, pointer,
                "{0}'s gate selector names no path, so nothing states the "
                "surface it produces; only a whole-suite integration gate "
                "covers such a lane, and this one names {1}".format(
                    node.node_id, ", ".join(covers))))
            continue
        uncovered = tuple(
            path for path in paths
            if not any(_path_under(path, prefix) for prefix in covers))
        if uncovered:
            blockers.append(Blocker(
                Obligation.INTEGRATION_GATE_COVERS_LANES, pointer,
                "the integration gate selects {0}, which does not name {1}'s "
                "gate surface: {2}. A gate over a subset of the lanes it must "
                "integrate passes without ever collecting the lanes it "
                "omits (§8.8)".format(
                    ", ".join(covers), node.node_id, ", ".join(uncovered))))
    return blockers

def _tests_build_paired(plan: "pm.Plan") -> List[Blocker]:
    """A tests node is one half of a pair: exactly one agent depends on it,
    that agent does not own the test files, and the tests node's outputs
    are test paths.

    Absent tests nodes, this is a no-op — v1/v2 plans stay valid.
    """
    blockers: List[Blocker] = []
    dependents: Dict[str, List[str]] = {n.node_id: [] for n in plan.nodes
                                        if isinstance(n, pm.TestsNode)}
    if not dependents:
        return blockers
    by_id = plan.node_by_id()
    for node in plan.nodes:
        if not isinstance(node, pm.AgentNode):
            continue
        for needed in node.needs:
            if needed in dependents:
                dependents[needed].append(node.node_id)
    for index, node in enumerate(plan.nodes):
        if not isinstance(node, pm.TestsNode):
            continue
        pointer = "/nodes/{0}".format(index)
        not_tests = [p for p in node.outputs if not tc.is_test_path(p)]
        if not_tests:
            blockers.append(Blocker(
                Obligation.TESTS_BUILD_PAIRED, pointer + "/outputs",
                "{0} is a tests node but declares non-test outputs: {1}"
                .format(node.node_id, ", ".join(not_tests))))
        builders = dependents[node.node_id]
        if len(builders) != 1:
            blockers.append(Blocker(
                Obligation.TESTS_BUILD_PAIRED, pointer,
                "{0} must be needed by exactly one agent (build) node; "
                "dependents: {1}".format(
                    node.node_id,
                    ", ".join(builders) if builders else "(none)")))
            continue
        builder = by_id[builders[0]]
        overlap = sorted(set(_norm(p) for p in node.outputs)
                         & set(_norm(p) for p in builder.outputs))
        if overlap:
            blockers.append(Blocker(
                Obligation.TESTS_BUILD_PAIRED, pointer + "/outputs",
                "{0}'s test files are also outputs of {1}: {2}; the build "
                "node's write scope must exclude the tests node files"
                .format(node.node_id, builder.node_id, ", ".join(overlap))))
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
    blockers.extend(_gate_selector_node_scoped(plan))
    blockers.extend(_integration_gate_covers_lanes(plan))
    blockers.extend(_tests_build_paired(plan))

    if blockers:
        return ValidationResult(Outcome.AUTHORING_BLOCKED, None, tuple(blockers))
    return ValidationResult(Outcome.FINALIZATION_ELIGIBLE,
                            pd.digest_of(stored), ())


# ── contract-IR admission ───────────────────────────────────────────────────
#
# Three obligations over a `plan-contract.v1` IR, over three domains, asked at
# the same moment: can any correct attempt satisfy this lane's contract, does
# any requirement prescribe an effect the plan forbids, and can any fixture
# the gate runs witness what a claim asserts.
#
# ## What the run cost, and why prose could not decide it
#
# Recorded failure, run-0120c32064d144c2aa55c344087e0b0a. Plan
# `cmo-consolidation-l` carried thirteen lanes and lost nodes to two distinct
# shapes of unsatisfiable contract.
#
# **Shape (i) — a lane that cannot write what its requirement needs.**
# `lane-p1-freeze-and-run-log` was required to freeze the legacy writers at a
# high-water mark and prove no code path updates a historical record in place.
# Its declared outputs were one new module and that module's test; the legacy
# writers appeared in the declared outputs of none of the plan's fourteen
# lanes. The builder could not write the file the behaviour needed, §8.3's
# permission delta would have rejected it, and every attempt produced an
# out-of-contract workaround the reviewer correctly rejected.
#
# **Shape (ii) — a requirement that both prohibits and prescribes one effect.**
# `lane-p1-canonical-object-key`'s requirement declares "a pure derivation and
# policy module with injected clients" and "no production migration, object
# mutation, or backfill execution is authorized", and in the same paragraph
# prescribes "verify and hash it first and then server-side copy it to the
# canonical key". Three attempts, all correctly rejected.
#
# Eight of the thirteen catalogued instances are shape (i) and four are shape
# (ii). The two shapes need two predicates because they are over two domains:
# shape (i) is about repository paths, and shape (ii) is about **external**
# effects — a bucket, a table, the network. `_is_declarable` below requires a
# relative repository path, so `case-management-orders/sha256/{first2}/….pdf`
# and `MDLDocumentCatalog` cannot be expressed in a surface entry at all. That
# is a wrong-domain answer, not a gap in cleverness, and `validate_contract_
# surface` catches zero of the four.
#
# Neither shape was visible to any relation the IR already carried. For the
# freeze lane, every typed relation binding a path to it — its outputs, its
# `depends_on` closure, its fixtures, its claim subject, its verifier argv, its
# source artifacts — was *satisfied*. The only statement that the legacy
# writers were in scope was prose, and §1.2 forbids a lifecycle transition
# caused by free text. An admission decision is a lifecycle transition.
#
# ## Migration: a plan authored before these fields is refused
#
# Stated plainly because the alternative is worse. `requirements[].surface`,
# `requirements[].effects`, and `extensions.maestro.prohibited_effects` are
# **required**, so every plan authored before they existed is refused at
# ingress with a blocker naming the requirement that lacks them, and must be
# re-authored. Both real IRs are in that state today: `cmo-consolidation-l`
# refuses with fourteen `SURFACE_DECLARED` blockers and `cmo-consolidation-l-r2`
# with thirteen.
#
# Making them optional-and-checked-when-present was considered and refused on
# §3.6 B8: a field added later is optional forever, and an optional write
# surface would be declared by exactly the plans that already had one. There is
# no upgrade function and no default, for the same reason §6.3 gives for the
# plan schema: a default here is an answer nobody authored.
#
# ## What this does not catch, at the same volume as what it does
#
# * A false declaration. A surface listing only reachable paths, or an effect
#   declared `planned` by a requirement whose module executes, is admitted.
#   Both are now a single typed cell an author had to write, which is the
#   plan-contract reviewer's to falsify (§3.6 B12) — a structural check cannot
#   honestly claim more.
# * Ordering contradictions. `req-p1-canonical-object-key` also contains one —
#   verify *then* copy, in a module described as pure — and only the effect
#   class is seen here, never the sequence.
# * Effects outside the closed five. A filesystem write outside the repository,
#   a queue, an email. Adding a member is cheap; guessing at one now is B15.
# * A requirement whose oracle demands a different artifact from its own text.
#   One of the thirteen is that shape and neither predicate here sees it.


class AdmissionObligation(str, Enum):
    """The seven obligations over a `plan-contract.v1` IR, at admission.

    Named `Admission*` rather than `Surface*` because the set now spans three
    domains. A class called `Surface…` carrying `EFFECT_AUTHORIZED` is how
    "the fourteen" stops being a checkable count and becomes a sentence in a
    docstring — the failure `OBLIGATIONS` above carries a comment about.
    """

    #: The IR states a surface at all, in the declared shape.
    SURFACE_DECLARED = "SURFACE_DECLARED"
    #: Declared paths and declared outputs contain each other.
    SURFACE_REACHABLE = "SURFACE_REACHABLE"
    #: Every prohibited effect has exactly one declared disposition, in every
    #: requirement, and the plan states what each prohibition means.
    EFFECT_DECLARED = "EFFECT_DECLARED"
    #: No requirement performs an effect the plan prohibits.
    EFFECT_AUTHORIZED = "EFFECT_AUTHORIZED"
    #: Every planned effect is executed by something in this plan.
    EFFECT_DISCHARGED = "EFFECT_DISCHARGED"
    #: Requirements bound to one lane agree about that lane's effects.
    EFFECT_CONSISTENT = "EFFECT_CONSISTENT"
    #: Every claim states the boundary its behaviour crosses and the store its
    #: state lives in, and no claim asserts what only a second invocation
    #: could observe over a store that dies with the first.
    CLAIM_UNWITNESSABLE = "CLAIM_UNWITNESSABLE"


#: Named as a tuple as well as an enum so the count is checkable rather than a
#: sentence, and asserted in `tests/test_plan_admission.py`. The predecessor of
#: this tuple had no reader anywhere in the tree, which is the B15 defect this
#: module's own docstring cites — arriving on its first commit.
ADMISSION_OBLIGATIONS: Tuple[AdmissionObligation, ...] = (
    AdmissionObligation.SURFACE_DECLARED,
    AdmissionObligation.SURFACE_REACHABLE,
    AdmissionObligation.EFFECT_DECLARED,
    AdmissionObligation.EFFECT_AUTHORIZED,
    AdmissionObligation.EFFECT_DISCHARGED,
    AdmissionObligation.EFFECT_CONSISTENT,
    AdmissionObligation.CLAIM_UNWITNESSABLE,
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
#:   the plan changes nowhere. It must be a declared `source_artifacts` entry
#:   **carrying a sha256**, and it must not appear in any lane's outputs — a
#:   path some lane rewrites is not unmodified, whoever says otherwise.
#:
#: A fourth member was proposed and refused. `creates` buys nothing: "written
#: and absent at base" is already distinguished downstream, where
#: `plan_author.fill_git_facts` fills `base_sha256` from the git object and
#: `_evidence_typed_against_git` blocks a produced path present at base that
#: declares none. `deletes` is an object-store fact and lives in `EFFECTS`.
#: `fake_only` is a disposition, not a mutation: it is a statement about an
#: external effect, and putting it here would make a path field carry a
#: non-path fact.
MUTATIONS: Tuple[str, ...] = ("written", "inherited", "unmodified")


#: The closed set of external effects a plan can prohibit.
#:
#: Named after the **act** rather than the mechanism, deliberately. An earlier
#: draft named them `s3_object_write`, `network_retrieval`, and so on — but the
#: source document prohibits acts, so a lane could use the mechanism without
#: committing the act and no author could falsify the cell. `source_backfill`
#: is retrieving an artifact from a provider because no local copy exists;
#: reading an API to compare output is not that, and under a mechanism name it
#: would have been indistinguishable.
EFFECTS: Tuple[str, ...] = (
    #: Writing an object into the canonical namespace. A report artifact
    #: written elsewhere is not this.
    "canonical_object_write",
    #: Deleting, moving, or replacing an object at a source or canonical
    #: locator.
    "source_object_delete",
    #: Writing a projection table in the document store.
    "catalog_projection_write",
    #: Retrieving an artifact from a provider source because no local copy
    #: exists. Reading an API to compare output is not this.
    "source_backfill",
    #: Applying a migration. Authoring a revision file is not this.
    "migration_execution",
)


#: What a requirement says its lane does about one effect.
#:
#: * ``performed`` — the lane's code executes the effect against a real client.
#: * ``planned`` — the lane emits a typed record *describing* the effect and
#:   executes nothing. Something else in the plan has to execute it, which is
#:   what `EFFECT_DISCHARGED` checks; without that reader `planned` would be a
#:   member that changes no decision.
#: * ``fake_only`` — the lane's code executes the effect, and every invocation
#:   the gate exercises reaches an injected fake.
#: * ``none`` — the lane does not perform, plan, or fake this effect.
#:
#: `none` earns its place by measurement rather than by symmetry. With three
#: values, an author forced to declare all five effects had no way to say "not
#: applicable" and reached for `planned`: in the first IR authored against this
#: field, `planned` was truthful in 3 of 51 entries. The field had stopped
#: discriminating on first use. `none` narrows `planned` to something
#: falsifiable.
DISPOSITIONS: Tuple[str, ...] = ("performed", "planned", "fake_only", "none")

#: The dispositions that execute an effect, and so discharge a plan for it.
_EXECUTING_DISPOSITIONS: frozenset = frozenset({"performed", "fake_only"})


#: The boundary a claim's behaviour crosses — what a fixture would have to span
#: to observe the claim at all.
#:
#: * ``in_process`` — everything one invocation of the verifier can observe.
#:   In-process ordering, a "second run" driven inside one pytest process, and
#:   any state one interpreter holds from start to finish are all this.
#: * ``cross_invocation`` — behaviour only a *second* invocation can observe:
#:   survival across a restart or a process death, cross-process ordering, a
#:   cursor read back by something that did not write it.
#:
#: Two members and not three. `cross_process` was proposed and refused: under
#: this lattice it decides exactly what `cross_invocation` decides, and a
#: member that changes no decision is §3.6 B15 — the defect this module's own
#: docstring cites — arriving on its first commit.
WITNESS_SCOPES: Tuple[str, ...] = ("in_process", "cross_invocation")


#: Where the state a claim is about lives while the verifier runs.
#:
#: * ``none`` — the claim is about no stored state at all. A pure function's
#:   return value.
#: * ``in_memory`` — one interpreter's own objects: a dict, an in-memory
#:   SQLite session, a fake injected for the duration of one test.
#: * ``tmp_path`` — a filesystem location the run creates and can read back.
#: * ``repository`` — a tracked path in the worktree.
#: * ``external`` — a store outside the run entirely: a real database, a
#:   bucket, a queue.
#:
#: The vocabulary names *where the bytes are*, not what writes them, for the
#: reason `EFFECTS` names acts rather than mechanisms: a mechanism name lets a
#: lane use the mechanism without committing to the fact the cell asserts, and
#: the author's declaration stops being falsifiable.
WITNESS_STORES: Tuple[str, ...] = ("none", "in_memory", "tmp_path",
                                   "repository", "external")


#: The stores that outlive one invocation, and so can be read by a second one.
#: A claim scoped `cross_invocation` over anything else asserts something no
#: fixture the gate runs can witness: no correct attempt produces the evidence,
#: and the reviewer is asked to judge what the gate never shows it.
_WITNESSING_STORES: frozenset = frozenset({"tmp_path", "repository",
                                           "external"})


@dataclass(frozen=True)
class AdmissionBlocker:
    """One typed blocker with a JSON pointer into the authored IR.

    Deliberately not `Blocker`: those fourteen are obligations over
    `maestro-plan.v1` stored bytes and their count is asserted as fourteen.
    These are obligations over the contract IR, which is a different document
    with different pointers, and merging the two enums would make "the
    fourteen" a sentence again rather than a checkable count.
    """

    obligation: AdmissionObligation
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


def _is_digest(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


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


def _pinned_paths(ir: Mapping[str, Any]) -> Set[str]:
    """Source-artifact paths that actually pin their bytes.

    An entry without a sha256 is not counted. The `unmodified` blocker below
    says the point of the arm is that something pins the bytes the assertion
    is about, and an unpinned entry defeats exactly that — so admitting one
    would make the check's own stated purpose false.
    """
    sources = ir.get("source_artifacts")
    if not isinstance(sources, list):
        return set()
    paths: Set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        if _is_declarable(source.get("path")) and _is_digest(source.get("sha256")):
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


def _lane_graph(ir: Mapping[str, Any]
                ) -> Tuple[Dict[str, Sequence[str]], Dict[str, List[str]],
                           Dict[str, List[str]]]:
    """`(depends_on, lanes-by-requirement, requirements-by-lane)`.

    One walk of `lanes[]`, because both predicates need the same three maps and
    two walks would be two chances to disagree about which lane binds what.
    """
    lanes = ir.get("lanes")
    records = [lane for lane in lanes if isinstance(lane, dict)] \
        if isinstance(lanes, list) else []
    depends: Dict[str, Sequence[str]] = {}
    lanes_by_requirement: Dict[str, List[str]] = {}
    requirements_by_lane: Dict[str, List[str]] = {}
    for lane in records:
        lane_id = lane.get("lane_id")
        if not isinstance(lane_id, str) or not lane_id:
            continue
        needs = lane.get("depends_on")
        depends[lane_id] = [need for need in needs if isinstance(need, str)] \
            if isinstance(needs, list) else []
        requirements_by_lane.setdefault(lane_id, [])
        bound = lane.get("requirement_ids")
        if not isinstance(bound, list):
            continue
        for requirement_id in bound:
            if isinstance(requirement_id, str) and requirement_id:
                lanes_by_requirement.setdefault(requirement_id, []).append(lane_id)
                requirements_by_lane[lane_id].append(requirement_id)
    return depends, lanes_by_requirement, requirements_by_lane


def _requirement_surface(index: int, requirement: Mapping[str, Any],
                         blockers: List[AdmissionBlocker]
                         ) -> List[Tuple[int, str, str]]:
    """The requirement's surface as `(entry index, path, mutation)` triples.

    Every malformed entry is reported and dropped, so one unreadable record
    does not hide the reachable/unreachable answer for the records beside it.
    """
    pointer = "/requirements/{0}/surface".format(index)
    label = requirement.get("requirement_id", "at index {0}".format(index))
    declared = requirement.get("surface")
    if declared is None:
        blockers.append(AdmissionBlocker(
            AdmissionObligation.SURFACE_DECLARED, pointer,
            "requirement {0} declares no surface; a requirement states the "
            "repository paths its behaviour lives in, so a lane that cannot "
            "write them is refused before a run starts rather than after a "
            "node has spent its retry budget".format(label)))
        return []
    if not isinstance(declared, list) or not declared:
        blockers.append(AdmissionBlocker(
            AdmissionObligation.SURFACE_DECLARED, pointer,
            "requirement {0} declares a surface that is not a non-empty "
            "list".format(label)))
        return []
    entries: List[Tuple[int, str, str]] = []
    for position, entry in enumerate(declared):
        entry_pointer = "{0}/{1}".format(pointer, position)
        if not isinstance(entry, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_DECLARED, entry_pointer,
                "a surface entry is a {path, mutation} object"))
            continue
        path = entry.get("path")
        if not _is_declarable(path):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_DECLARED, entry_pointer + "/path",
                "{0!r} is not a relative repository path".format(path)))
            continue
        mutation = entry.get("mutation")
        if mutation not in MUTATIONS:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_DECLARED,
                entry_pointer + "/mutation",
                "{0!r} is not one of {1}".format(
                    mutation, ", ".join(MUTATIONS))))
            continue
        entries.append((position, _normalized_path(path), mutation))
    return entries


def validate_contract_surface(ir: Mapping[str, Any]
                              ) -> Tuple[AdmissionBlocker, ...]:
    """Refuse a `plan-contract.v1` IR whose lane cannot satisfy its contract.

    The question is asked of the whole plan rather than of one lane: a path is
    reachable when it lies in the lane's own declared outputs, in the outputs
    of a lane in its `depends_on` closure, or among the hash-pinned source
    artifacts the plan declares and changes nowhere. Which of the three a
    requirement means is the requirement's own `mutation`, not an inference,
    so a `written` path is never satisfied by a sibling's output and an
    `unmodified` path is never satisfied by one either.

    **Containment runs both ways, and the converse arm is what makes the check
    a satisfiability check rather than a consistency one.** With forward
    containment alone, an author who writes each requirement's surface as
    exactly the outputs already chosen for its lane passes trivially: the
    freeze lane's surface would have been its own module and its own test, and
    the run would have burned identically. Requiring every declared output to
    be claimed `written` by some requirement the lane binds means
    under-declaration now needs a visibly unowned output, which the author has
    to look at.

    Nothing here reads `requirements[].text`, `verifiers[].oracle`,
    `seams[].producer`, `fixtures[].meaning`, or any other free-text field.
    Every input is a declared path, a declared enumerated value, or a declared
    id — §1.2.
    """
    blockers: List[AdmissionBlocker] = []

    requirements = ir.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        blockers.append(AdmissionBlocker(
            AdmissionObligation.SURFACE_DECLARED, "/requirements",
            "the plan declares no requirements, so no lane has a contract to "
            "be checked against; an executable plan states what it must make "
            "true"))
        return tuple(blockers)

    outputs = _lane_outputs(ir)
    every_output: Set[str] = set()
    for paths in outputs.values():
        every_output |= paths
    pinned = _pinned_paths(ir)
    depends, lanes_by_requirement, requirements_by_lane = _lane_graph(ir)

    #: `(lane_id, path)` pairs some bound requirement claims as `written`, for
    #: the converse arm below.
    claimed: Set[Tuple[str, str]] = set()

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_DECLARED,
                "/requirements/{0}".format(index),
                "a requirement is an object"))
            continue
        requirement_id = requirement.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_DECLARED,
                "/requirements/{0}/requirement_id".format(index),
                "a requirement declares an id, which is how a lane binds to "
                "it"))
            continue
        entries = _requirement_surface(index, requirement, blockers)
        owners = lanes_by_requirement.get(requirement_id, [])
        if not owners:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_REACHABLE,
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
                    claimed.add((lane_id, path))
                    if path in own:
                        continue
                    holder = sorted(other for other, paths in outputs.items()
                                    if path in paths)
                    elsewhere = (
                        "it is declared as an output of " + ", ".join(holder)
                        if holder else
                        "no lane in this plan declares it as an output")
                    blockers.append(AdmissionBlocker(
                        AdmissionObligation.SURFACE_REACHABLE, pointer,
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
                    blockers.append(AdmissionBlocker(
                        AdmissionObligation.SURFACE_REACHABLE, pointer,
                        "lane {0} inherits {1} for {2}, but no lane in its "
                        "depends_on closure ({3}) produces it".format(
                            lane_id, path, requirement_id,
                            ", ".join(sorted(_depends_closure(lane_id, depends)))
                            or "empty")))
                else:
                    if path not in pinned:
                        blockers.append(AdmissionBlocker(
                            AdmissionObligation.SURFACE_REACHABLE, pointer,
                            "lane {0} asserts unmodified behaviour over {1} "
                            "for {2}, but {1} is not a declared source "
                            "artifact carrying a sha256, so nothing pins the "
                            "bytes the assertion is about".format(
                                lane_id, path, requirement_id)))
                    if path in every_output:
                        holder = sorted(other for other, paths in outputs.items()
                                        if path in paths)
                        blockers.append(AdmissionBlocker(
                            AdmissionObligation.SURFACE_REACHABLE, pointer,
                            "lane {0} asserts {1} is unmodified for {2}, but "
                            "{3} declares it as an output".format(
                                lane_id, path, requirement_id,
                                ", ".join(holder))))

    # The converse arm. A declared output no bound requirement claims is a
    # lane doing work its own contract does not describe, which is exactly the
    # under-declaration that made the forward arm passable by an author who
    # copied the outputs into the surface.
    for lane_id in sorted(outputs):
        bound = requirements_by_lane.get(lane_id, [])
        for path in sorted(outputs[lane_id]):
            if (lane_id, path) in claimed:
                continue
            blockers.append(AdmissionBlocker(
                AdmissionObligation.SURFACE_REACHABLE,
                "/extensions/maestro/outputs/{0}".format(lane_id),
                "lane {0} declares the output {1}, which no requirement it "
                "binds ({2}) claims as written; a path the lane writes and no "
                "requirement accounts for is work outside the contract the "
                "reviewer will judge it against".format(
                    lane_id, path,
                    ", ".join(bound) if bound else "none")))
    return tuple(blockers)


# ── the second domain: effects a plan forbids ───────────────────────────────

def _prohibited_effects(ir: Mapping[str, Any],
                        blockers: List[AdmissionBlocker]
                        ) -> "Dict[str, str]":
    """`{effect: meaning}` from `extensions.maestro.prohibited_effects`.

    Each entry is `{effect, meaning}` rather than a bare effect name, and
    `meaning` is transcribed from the plan's source document in that
    document's own terms. It has three readers and would not exist without
    them: this presence check, which refuses a plan that leaves it blank; the
    `EFFECT_AUTHORIZED` refusal below, which quotes it so an author is told
    which prohibited act they declared rather than only which enum member; and
    the node contract the reviewer is handed. Without that last one the plan
    reviewer and the node reviewer would resolve an effect name from two
    different documents, which is this same failure one level down.
    """
    extensions = ir.get("extensions")
    maestro = extensions.get("maestro") if isinstance(extensions, dict) else None
    declared = maestro.get("prohibited_effects") if isinstance(maestro, dict) \
        else None
    pointer = "/extensions/maestro/prohibited_effects"
    if declared is None:
        blockers.append(AdmissionBlocker(
            AdmissionObligation.EFFECT_DECLARED, pointer,
            "the plan declares no prohibited_effects; a plan states, once and "
            "before any lane exists, which external acts it forbids, so a "
            "requirement prescribing one is refused before a run starts"))
        return {}
    if not isinstance(declared, list):
        blockers.append(AdmissionBlocker(
            AdmissionObligation.EFFECT_DECLARED, pointer,
            "prohibited_effects is a list of {effect, meaning} objects"))
        return {}
    prohibited: Dict[str, str] = {}
    for index, entry in enumerate(declared):
        entry_pointer = "{0}/{1}".format(pointer, index)
        if not isinstance(entry, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED, entry_pointer,
                "a prohibited effect is an {effect, meaning} object"))
            continue
        effect = entry.get("effect")
        if effect not in EFFECTS:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/effect",
                "{0!r} is not one of {1}".format(effect, ", ".join(EFFECTS))))
            continue
        if effect in prohibited:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/effect",
                "{0} is prohibited twice; one prohibition means one "
                "act".format(effect)))
            continue
        meaning = entry.get("meaning")
        if not isinstance(meaning, str) or not meaning.strip():
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/meaning",
                "the prohibition of {0} states no meaning; an effect name is "
                "resolved against the plan's source document, and without the "
                "transcribed act the plan reviewer and the node reviewer "
                "resolve it against different documents".format(effect)))
            continue
        prohibited[effect] = meaning.strip()
    return prohibited


def _requirement_effects(index: int, requirement: Mapping[str, Any],
                         prohibited: Mapping[str, str],
                         blockers: List[AdmissionBlocker]
                         ) -> "Dict[str, Tuple[int, str]]":
    """`{effect: (entry index, disposition)}` for one requirement.

    Exhaustive over the prohibited set: every prohibited effect needs exactly
    one entry here. That is what turns the bypass from silent omission into an
    active false declaration in a typed field — an author who performs a
    prohibited act can no longer stay quiet about it, they have to write a
    disposition beside it, and a false one is a single cell the plan-contract
    reviewer can falsify against the code the lane produces.
    """
    label = requirement.get("requirement_id", "at index {0}".format(index))
    pointer = "/requirements/{0}/effects".format(index)
    declared = requirement.get("effects")
    if declared is None:
        blockers.append(AdmissionBlocker(
            AdmissionObligation.EFFECT_DECLARED, pointer,
            "requirement {0} declares no effects; every prohibited effect "
            "needs a disposition in every requirement, so silence about a "
            "prohibited act is not a way to pass".format(label)))
        return {}
    if not isinstance(declared, list):
        blockers.append(AdmissionBlocker(
            AdmissionObligation.EFFECT_DECLARED, pointer,
            "requirement {0} declares effects that are not a list".format(
                label)))
        return {}
    found: Dict[str, Tuple[int, str]] = {}
    for position, entry in enumerate(declared):
        entry_pointer = "{0}/{1}".format(pointer, position)
        if not isinstance(entry, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED, entry_pointer,
                "an effect entry is an {effect, disposition} object"))
            continue
        effect = entry.get("effect")
        if effect not in EFFECTS:
            # An out-of-enum value and an omission are different author
            # errors, and during migration both will be hit constantly, so
            # they are never reported with one sentence.
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/effect",
                "requirement {0} declares the effect {1!r}, which is outside "
                "the closed set {2}; this is a value error, not an "
                "omission".format(label, effect, ", ".join(EFFECTS))))
            continue
        disposition = entry.get("disposition")
        if disposition not in DISPOSITIONS:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/disposition",
                "requirement {0} declares the disposition {1!r} for {2}, "
                "which is outside the closed set {3}; this is a value error, "
                "not an omission".format(
                    label, disposition, effect, ", ".join(DISPOSITIONS))))
            continue
        if effect in found:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED,
                entry_pointer + "/effect",
                "requirement {0} declares {1} twice, as {2} and as {3}; one "
                "effect has one disposition".format(
                    label, effect, found[effect][1], disposition)))
            continue
        found[effect] = (position, disposition)
    for effect in EFFECTS:
        if effect in prohibited and effect not in found:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.EFFECT_DECLARED, pointer,
                "requirement {0} omits a required entry for the prohibited "
                "effect {1} ({2}); declare it performed, planned, fake_only, "
                "or none. This is an omission, not a value error".format(
                    label, effect, prohibited[effect])))
    return found


def validate_effect_authorization(ir: Mapping[str, Any]
                                  ) -> Tuple[AdmissionBlocker, ...]:
    """Refuse a requirement that prescribes an act its own plan forbids.

    Four of the thirteen catalogued instances are this shape, and none of them
    is expressible as a repository path: the contentions are an object write
    into the canonical namespace, a source backfill, and a projection write.
    `validate_contract_surface` catches zero of the four, which is a
    wrong-domain answer rather than a gap in it.

    Three rules over the declared fields, and no others:

    * **`EFFECT_AUTHORIZED`** — a requirement declaring an effect `performed`
      while the plan prohibits it is refused. `planned`, `fake_only`, and
      `none` are admitted under a prohibition: that is the distinction the
      source document itself draws between describing an act, exercising it
      against an injected fake, and not touching it.
    * **`EFFECT_DISCHARGED`** — an effect some requirement declares `planned`,
      that no requirement in the plan declares `performed` or `fake_only`, is
      refused. A planned effect nothing discharges is a decision the plan
      describes and never makes. Measured against the first IR authored with
      this field, this alone refuses two of the five effects with no prose
      read at all.
    * **`EFFECT_CONSISTENT`** — a lane's effects are the union of the
      requirements it binds, so two requirements on one lane declaring
      different dispositions for one effect is refused rather than silently
      merged. A lane cannot both fake and plan the same act.

    The plan-level prohibition is what makes this stronger than a surface
    check. `validate_contract_surface` compares two fields one author writes
    in one sitting from one mental model, so copying outputs into the surface
    passes it; `prohibited_effects` is transcribed once from the source
    document before any lane exists, and the dispositions are written later,
    one per lane. The contradiction is a fact between two fields written at
    two moments about two different things.

    Every input is a declared id or a member of a closed enumeration. No path,
    no prose, no title — §1.2, satisfied more strongly than the surface
    predicate's, which at least reads paths.
    """
    blockers: List[AdmissionBlocker] = []
    prohibited = _prohibited_effects(ir, blockers)

    requirements = ir.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        # The surface predicate already refuses a plan with no requirements,
        # naming it once. Reporting it twice would make one defect look like
        # two in two vocabularies.
        return tuple(blockers)

    _depends, lanes_by_requirement, _by_lane = _lane_graph(ir)
    planned: Dict[str, List[str]] = {}
    planned_pointer: Dict[str, str] = {}
    executed: Set[str] = set()
    #: `(lane_id, effect) -> (requirement_id, disposition)`, first writer wins,
    #: so the conflict below names the pair that disagreed.
    by_lane: Dict[Tuple[str, str], Tuple[str, str]] = {}

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        requirement_id = requirement.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            continue
        found = _requirement_effects(index, requirement, prohibited, blockers)
        for effect, (position, disposition) in sorted(found.items()):
            pointer = "/requirements/{0}/effects/{1}/disposition".format(
                index, position)
            if disposition == "performed" and effect in prohibited:
                lanes = lanes_by_requirement.get(requirement_id, [])
                blockers.append(AdmissionBlocker(
                    AdmissionObligation.EFFECT_AUTHORIZED, pointer,
                    "requirement {0} performs {1}, which this plan prohibits "
                    "({2}); lane {3} would have to both do it and not do it. "
                    "Declare the effect planned, fake_only, or none, or lift "
                    "the plan-wide prohibition".format(
                        requirement_id, effect, prohibited[effect],
                        ", ".join(lanes) if lanes else "no lane binds it")))
            if disposition == "planned":
                planned.setdefault(effect, []).append(requirement_id)
                planned_pointer.setdefault(effect, pointer)
            if disposition in _EXECUTING_DISPOSITIONS:
                executed.add(effect)
            for lane_id in lanes_by_requirement.get(requirement_id, []):
                key = (lane_id, effect)
                if key not in by_lane:
                    by_lane[key] = (requirement_id, disposition)
                    continue
                first_requirement, first_disposition = by_lane[key]
                if first_disposition == disposition:
                    continue
                blockers.append(AdmissionBlocker(
                    AdmissionObligation.EFFECT_CONSISTENT, pointer,
                    "lane {0} binds {1} and {2}, which declare {3} as {4} and "
                    "as {5}; a lane's effects are the union of its "
                    "requirements', and a lane cannot both {4} and {5} one "
                    "act".format(lane_id, first_requirement, requirement_id,
                                 effect, first_disposition, disposition)))

    for effect in sorted(planned):
        if effect in executed:
            continue
        blockers.append(AdmissionBlocker(
            AdmissionObligation.EFFECT_DISCHARGED, planned_pointer[effect],
            "{0} is planned by {1} and executed by nothing in this plan; a "
            "planned effect emits a record describing an act something else "
            "carries out, so an undischarged one is a decision the plan "
            "describes and never makes. Declare a lane that performs or fakes "
            "it, or declare the effect none".format(
                effect, ", ".join(planned[effect]))))
    return tuple(blockers)


def validate_claim_witness(ir: Mapping[str, Any]
                           ) -> Tuple[AdmissionBlocker, ...]:
    """Refuse a claim no fixture the gate runs could witness.

    A claim states what the run has to show. A verifier shows it by running
    one command, once, and reporting what it observed. When the claim is about
    a boundary that command never crosses, no correct attempt produces the
    evidence: the builder writes the behaviour, the gate cannot see it, and
    the reviewer is asked to judge something the gate never showed it. That is
    the same class as an unreachable write surface — a contract no correct
    attempt satisfies — one domain over, so it is refused at the same moment
    rather than discovered after a node has spent its retry budget.

    The decision reads two enumerated cells and nothing else. `witness.scope`
    is the boundary the claim's behaviour crosses; `witness.store` is where
    the state it is about lives while the verifier runs. A claim scoped
    `cross_invocation` over a store that dies with the invocation is the
    refusal; every `in_process` claim is admitted under every store, because
    an author who says the behaviour is within one invocation has said
    something a single command can check.

    Nothing here reads `requirements[].text`, `claims[].object`,
    `verifiers[].oracle`, `seams[].contract`, or any other free-text field. A
    requirement whose prose says "survives a restart" while its claim declares
    `in_process` is admitted, and a claim whose prose says nothing about
    persistence is refused when its declared cells say it crosses an
    invocation over an in-memory store. An admission decision is a lifecycle
    transition and §1.2 forbids one caused by free text; whether the declared
    cells are *true* is the plan-contract reviewer's to falsify (§3.6 B12),
    exactly as it is for `surface` and `effects`.

    A `claims` key that is absent or not a list yields nothing here. The
    authoring schema requires the array and `planctl validate` refuses a
    malformed one in its own vocabulary; reporting the same defect twice makes
    one plan error look like two.
    """
    blockers: List[AdmissionBlocker] = []

    claims = ir.get("claims")
    if not isinstance(claims, list):
        return ()

    for index, claim in enumerate(claims):
        pointer = "/claims/{0}/witness".format(index)
        if not isinstance(claim, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE,
                "/claims/{0}".format(index),
                "a claim is an object carrying a {scope, store} witness"))
            continue
        label = claim.get("claim_id")
        if not isinstance(label, str) or not label:
            label = "at index {0}".format(index)

        declared = claim.get("witness")
        if declared is None:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE, pointer,
                "claim {0} declares no witness; a claim states the boundary "
                "its behaviour crosses and the store its state lives in, so a "
                "gate that cannot observe what the claim asserts is refused "
                "before a run starts rather than after a node has spent its "
                "retry budget. Declare {{\"scope\": one of {1}, \"store\": "
                "one of {2}}}".format(
                    label, ", ".join(WITNESS_SCOPES),
                    ", ".join(WITNESS_STORES))))
            continue
        if not isinstance(declared, dict):
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE, pointer,
                "claim {0} declares a witness that is not a {{scope, store}} "
                "object".format(label)))
            continue

        scope = declared.get("scope")
        store = declared.get("store")
        well_formed = True
        if scope not in WITNESS_SCOPES:
            well_formed = False
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE, pointer + "/scope",
                "claim {0} declares scope {1!r}, which is not one of "
                "{2}".format(label, scope, ", ".join(WITNESS_SCOPES))))
        if store not in WITNESS_STORES:
            well_formed = False
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE, pointer + "/store",
                "claim {0} declares store {1!r}, which is not one of "
                "{2}".format(label, store, ", ".join(WITNESS_STORES))))
        if not well_formed:
            continue

        if scope == "cross_invocation" and store not in _WITNESSING_STORES:
            blockers.append(AdmissionBlocker(
                AdmissionObligation.CLAIM_UNWITNESSABLE, pointer,
                "claim {0} is scoped cross_invocation over store {1}, which "
                "does not outlive the invocation that creates it, so no "
                "fixture this plan's gate can run observes the second "
                "invocation the claim is about and no correct attempt "
                "produces the evidence. A witnessing store is one of {2}: "
                "give the claim a store the run can read back, or scope it "
                "in_process and state the within-invocation behaviour the "
                "gate actually shows".format(
                    label, store,
                    ", ".join(sorted(_WITNESSING_STORES)))))

    return tuple(blockers)


def validate_admission(ir: Mapping[str, Any]) -> Tuple[AdmissionBlocker, ...]:
    """All three admission predicates, in one pass, collecting every blocker.

    Not short-circuiting, for §11.1's reason: an author sent back twice for
    two admission defects in one document is the fail-fast validator this
    design already refuses. The three are over three domains — repository
    paths, external acts, and the boundary a gate can observe — so a document
    carrying one of each is refused once, naming all three.
    """
    return (tuple(validate_contract_surface(ir))
            + tuple(validate_effect_authorization(ir))
            + tuple(validate_claim_witness(ir)))
