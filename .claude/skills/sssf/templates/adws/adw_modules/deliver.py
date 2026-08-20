"""`maestro deliver`: one verb from a source document to shipped, runnable plans.

This module automates the sequence `docs/plan-authoring.md` documents and
invents no new entry format. A source document reaches Maestro exactly one way:
as a hash-pinned `source_artifacts` entry inside a `plan-contract.v1` Plan IR.
There is no converter here and there must never be one.

    source document -> architecture Plan IR (anchor, never executable)
                    -> N brownfield work packages, each with extensions.maestro
                    -> per package: plan gate -> plan review -> plan ship
                    -> per package: run start, in dependency order

Two loops, at two different altitudes, and neither reimplements the other.
The repair loop is ACROSS attempts at authoring one package, bounded at
`MAX_ATTEMPTS`. The run loop is ACROSS packages, one at a time, and stops at
the first run that is not ACCEPTED. Inside a single run, the DAG scheduler
already orders every lane and already bounds each lane's retries; nothing here
touches that. The run loop cannot be parallel either: a run's integration
worktree takes the plan's integration branch, and git gives a branch to one
worktree at a time.

Everything that talks to Herdr, planctl, the plan verbs, or the scheduler is
injected, because the interesting behaviour -- the bounded repair loop, what
counts as a finding, what order the packages run in, and where the run loop
halts -- is pure and must be testable without an agent or a run.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, FrozenSet, Iterable, List, Mapping,
                    Optional, Sequence, Tuple)


#: Three tries per package, then stop and surface what the reviewer said.
#: A fourth attempt has never once been the thing that was missing, and an
#: unbounded loop spends an opus lane on a plan a human needs to look at.
MAX_ATTEMPTS = 3

ARCHITECTURE_KIND = "architecture"
IR_SUFFIX = ".plan.json"
RENDERED_SUFFIX = ".html"
RECEIPT_SUFFIX = ".plan-review.json"

#: A package name is one path component, lowercase, and never a traversal.
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class DeliverError(RuntimeError):
    """`deliver` cannot proceed, and says which obligation stopped it."""


# ── what the authoring lane is told ─────────────────────────────────────────

#: Every rule below is a refusal that has actually fired on a plan authored by
#: hand in this repository. They are stated as obligations rather than advice
#: because the lane's output is judged by `plan validate`, not by a human
#: reading its prose.
AUTHORING_RULES = """\
HARD RULES. Each one below is a refusal `plan validate` or the independent
reviewer has already raised on a hand-authored plan in this repository. A plan
that breaks one does not ship.

1. ONE ENTRY FORMAT. A source document -- audit, interview, architecture deck,
   HTML spec -- never gets an adapter. It is cited as a hash-pinned
   `source_artifacts` entry. Do not write a converter.

2. extensions.maestro MUST EXIST BEFORE RENDER AND REVIEW. The review receipt
   binds to the IR bytes. Adding `extensions.maestro` to an already approved IR
   changes those bytes and the projection refuses with RECEIPT_IR_MISMATCH.
   There is no post-hoc fix; the plan must be re-rendered and re-reviewed.

3. GATE_EXECUTABLE. A gate whose selector resolves ENTIRELY within the plan's
   own declared outputs takes the produced arm: it is expected to collect
   nothing at base, because the files do not exist yet. ANY OTHER selector must
   already collect at least `min_cases` at base. Decide which arm each gate is
   on before you write it, and do not mix produced and pre-existing paths in
   one selector unless the whole selector already collects at base.

4. GATE_CORE_UNSHARED. A node gate and the integration gate may not share the
   triple (runner, cwd, argv). Splitting them with a non-noise `-`-prefixed
   flag such as `--strict-markers` works, because `_selector_groups` drops
   `-`-prefixed tokens when it compares selectors -- the flag changes the argv
   without changing what the gate covers.

5. min_cases IS MEASURED, NEVER GUESSED. Run, for each selector:

       pytest --collect-only -q -o addopts= <selector>

   `-o addopts=` is mandatory. This repository's `pytest.ini` sets `-v`, which
   cancels `-q`, and without it every count comes back 0. Use the measured
   number. On the produced arm, measure against the tests you are declaring the
   lane will write, and state the count you intend the lane to reach.

6. EVIDENCE IS READ FROM THE COMMIT, NOT THE WORKTREE. Observed evidence is
   loaded with `pv.blob_at(repo, commit, path)`. Every cited path must be
   COMMITTED and must actually contain the claim you attribute to it. Before
   citing a path, open the blob and confirm the claim is in it. A real refusal
   on this repository: a plan cited `docs/mdl_master_schema.md` for CAS,
   order_id, and s3_key claims that occur zero times in that file.

7. THIS REPOSITORY IGNORES MARKDOWN. `.gitignore` line 57 is `**/*.md`, so a
   pinned evidence document must be committed with `git add -f <path>`.

8. NEVER DECLARE AN OUTPUT THE LANE DOES NOT PRODUCE. A path that already
   exists at base is a FALSE produced claim: `plan_author.py:99-100` back-fills
   its `base_sha256` and the reviewer raises a finding. Declared outputs are
   files the lane creates.

9. RUNNERS ARE CLOSED. `Gate.runner` is `pytest` or `vitest` and nothing else.
   A shell script, a Makefile target, a `psql` migration, or a `curl` health
   check is refused with `maestro.command`. Verify such work by asserting its
   EFFECT from a countable test. Pass the real argv, never `npm test` or
   `make test`.

10. ONE VERIFIER PER LANE. The projection gives each node exactly one gate; a
    lane binding zero or several verifiers is refused with `maestro.lane_gate`.
    Widen one selector or split the lane.

11. EVERY planctl CALL TAKES `--repo-root .`. Without it, `source_artifacts`
    paths resolve from `.maestro/` instead of the repository root, and any `..`
    escape is refused outright.

12. AN ARCHITECTURE PLAN IS NEVER EXECUTABLE. `plan_kind: architecture` is
    refused with ARCHITECTURE_NOT_EXECUTABLE. It is the anchor a brownfield
    plan pins, never something that runs.

13. `plan author` IS CREATE-ONCE. `write_canonical_plan` raises PLAN_EXISTS.
    Re-shipping a plan requires its `.maestro/plans/<name>/` directory to be
    removed first; `deliver` does that for you, so never author into an
    existing plan directory yourself.
"""


def _envelope_clause(envelope: Path, extra: Sequence[str] = ()) -> str:
    lines = [
        "When you have finished, write this file and then stop:",
        "  " + str(envelope),
        "",
        "It must be a JSON object with at least:",
        '  {"success": true, "summary": "<what you did>"}',
    ]
    if extra:
        lines.append("")
        lines.append("It must also carry:")
        lines.extend("  " + item for item in extra)
    lines.extend([
        "",
        'Use "success": false with the reason in the summary if you could not '
        "finish. Nothing else ends this turn: without the file the attempt is "
        "unverified and is discarded.",
    ])
    return "\n".join(lines)


def architecture_prompt(spec: str, ir_path: Path, envelope: Path) -> str:
    """Ask the authoring lane for the architecture anchor and nothing else."""
    return "\n".join([
        "Author the approved architecture Plan IR for this repository from the "
        "source document below, using the `arch-review` skill.",
        "",
        "  source document: " + spec,
        "  Plan IR to write: " + str(ir_path),
        "",
        "Emit `plan_kind: architecture`. It is an anchor a brownfield plan "
        "pins, so it carries no `extensions.maestro` and never runs. Render it "
        "and obtain its review receipt with planctl, passing `--repo-root .` "
        "to every call.",
        "",
        AUTHORING_RULES,
        "",
        _envelope_clause(envelope, (
            '"architecture_ir": "<path to the Plan IR you wrote>"',
        )),
    ])


def brownfield_prompt(spec: str, architecture_ir: Path, plans_dir: Path,
                      envelope: Path, request: str) -> str:
    """Ask for one executable work package per reviewable unit."""
    return "\n".join([
        "Author the executable work packages for this change, using the "
        "`plan-brownfield` skill.",
        "",
        "  what to build: " + request,
        "  source document: " + spec,
        "  approved architecture IR to pin: " + str(architecture_ir),
        "  where each package's artifacts belong: " + str(plans_dir),
        "",
        "Author ONE `plan_kind: brownfield` package per reviewable unit. For "
        "each package <name> write " + str(plans_dir) + "/<name>" + IR_SUFFIX
        + ", and carry `extensions.maestro` in the IR BEFORE it is rendered or "
        "reviewed. Do not render or review them; `maestro deliver` gates, "
        "reviews, and ships each package itself.",
        "",
        "Keep packages small enough that one reviewer can hold a package in "
        "one window. Declare the dependency edges between packages: a package "
        "that consumes another package's declared output depends on it.",
        "",
        AUTHORING_RULES,
        "",
        _envelope_clause(envelope, (
            '"packages": [{"name": "<package>", "depends_on": ["<package>"]}]'
            "  -- every package you authored, with its edges",
        )),
    ])


def repair_prompt(package: str, ir_path: Path, envelope: Path,
                  findings: Sequence["Finding"], attempt: int,
                  max_attempts: int) -> str:
    """Hand the lane the exact cells that failed, and nothing softer."""
    lines = [
        "The work package `" + package + "` did not ship. Re-author its Plan "
        "IR so that every finding below is answered.",
        "",
        "  Plan IR to re-author: " + str(ir_path),
        "  attempt " + str(attempt) + " of " + str(max_attempts),
        "",
        "Findings, verbatim:",
        "",
    ]
    for index, finding in enumerate(findings, start=1):
        lines.append("  " + str(index) + ". [" + finding.source + "] "
                     + finding.code)
        if finding.pointer:
            lines.append("     at: " + finding.pointer)
        if finding.message:
            lines.append("     " + finding.message)
    if not findings:
        lines.append("  (the step failed without emitting a typed finding; "
                     "read the step log named in the failure payload)")
    lines.extend([
        "",
        "Repairing a plan means re-authoring the IR, re-rendering, "
        "re-validating, and obtaining a FRESH receipt. NEVER edit an approved "
        "IR in place: the receipt binds to the IR bytes, so an in-place edit "
        "invalidates it. `deliver` re-gates and re-reviews after this turn, so "
        "write the IR only.",
        "",
        AUTHORING_RULES,
        "",
        _envelope_clause(envelope, (
            '"package": "' + package + '"',
        )),
    ])
    return "\n".join(lines)


# ── findings ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    """One reason a package did not ship, in the form the lane can act on."""

    source: str
    code: str
    pointer: str = ""
    message: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"source": self.source, "code": self.code,
                "pointer": self.pointer, "message": self.message}


def _diagnostic_findings(source: str, text: str) -> Tuple[Finding, ...]:
    """planctl writes its diagnostics as JSON into the step's captured stdout."""
    found: List[Finding] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get("diagnostics") or ():
            if isinstance(item, str):
                found.append(Finding(source, "planctl", message=item))
            elif isinstance(item, dict):
                found.append(Finding(
                    source, str(item.get("code") or "planctl"),
                    str(item.get("pointer") or ""),
                    str(item.get("message") or "")))
    return tuple(found)


def step_findings(source: str, payloads: Sequence[Mapping[str, Any]]
                  ) -> Tuple[Finding, ...]:
    """Every typed finding a failing plan verb emitted, in emission order.

    Three shapes reach here and all three are real: `plan validate`'s typed
    blockers, planctl's diagnostics nested in a step failure's captured stdout,
    and a bare refusal that carries only an outcome and a detail. A refusal
    with no typed cell is still a finding -- dropping it would hand the
    authoring lane an empty list and ask it to guess.
    """
    found: List[Finding] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for row in payload.get("blockers") or ():
            if not isinstance(row, Mapping):
                continue
            found.append(Finding(
                source,
                str(row.get("obligation") or row.get("code") or "BLOCKER"),
                str(row.get("pointer") or ""),
                str(row.get("message") or "")))
        found.extend(_diagnostic_findings(source, str(payload.get("stdout") or "")))
        outcome = str(payload.get("outcome") or "")
        if outcome.endswith("_FAILED") or outcome.startswith("PLAN_CONTRACT_"):
            detail = str(payload.get("detail") or payload.get("stderr") or "")
            step = str(payload.get("step") or "")
            if not any(item.code == outcome for item in found):
                found.append(Finding(source, outcome, step, detail.strip()))
    return tuple(found)


def report_findings(report: Optional[Mapping[str, Any]]) -> Tuple[Finding, ...]:
    """The reviewer's `finding` cells. A `clear` cell is not a finding."""
    if not isinstance(report, Mapping):
        return ()
    found: List[Finding] = []
    for cell in report.get("cells") or ():
        if not isinstance(cell, Mapping):
            continue
        if str(cell.get("status")) != "finding":
            continue
        found.append(Finding(
            "finalize", str(cell.get("check_id") or "check"),
            str(cell.get("object_id") or ""), str(cell.get("message") or "")))
    return tuple(found)


# ── dependency order ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Package:
    name: str
    depends_on: Tuple[str, ...] = ()


def declared_packages(envelope: Mapping[str, Any]) -> Tuple[Package, ...]:
    """The packages the brownfield lane says it authored, validated."""
    rows = envelope.get("packages")
    if not isinstance(rows, list) or not rows:
        raise DeliverError("NO_PACKAGES_DECLARED")
    packages: List[Package] = []
    seen = set()
    for row in rows:
        if isinstance(row, str):
            row = {"name": row}
        if not isinstance(row, Mapping):
            raise DeliverError("PACKAGE_MALFORMED")
        name = row.get("name")
        if not isinstance(name, str) or _PACKAGE_NAME.fullmatch(name) is None:
            raise DeliverError("PACKAGE_NAME_INVALID:{}".format(name))
        if name in seen:
            raise DeliverError("PACKAGE_DUPLICATE:{}".format(name))
        seen.add(name)
        edges = row.get("depends_on") or ()
        if isinstance(edges, str) or not isinstance(edges, Iterable):
            raise DeliverError("PACKAGE_EDGES_INVALID:{}".format(name))
        packages.append(Package(name, tuple(
            str(item) for item in edges if isinstance(item, str))))
    known = {package.name for package in packages}
    for package in packages:
        unknown = [edge for edge in package.depends_on if edge not in known]
        if unknown:
            raise DeliverError("PACKAGE_EDGE_UNKNOWN:{}:{}".format(
                package.name, ",".join(sorted(unknown))))
    return tuple(packages)


def derived_edges(irs: Mapping[str, Mapping[str, Any]]
                  ) -> Dict[str, FrozenSet[str]]:
    """Edges read out of the IR bytes: who consumes whose declared output.

    A lane's declared dependency order is a claim; this is the part of it that
    is checkable. If package B pins a path that package A declares as an
    output, B cannot start before A, whatever B's envelope said.
    """
    produced: Dict[str, str] = {}
    for name, ir in irs.items():
        extension = ((ir.get("extensions") or {}).get("maestro") or {})
        for outputs in (extension.get("outputs") or {}).values():
            for path in outputs or ():
                if isinstance(path, str):
                    produced.setdefault(path, name)
    edges: Dict[str, FrozenSet[str]] = {}
    for name, ir in irs.items():
        upstream = set()
        for source in ir.get("source_artifacts") or ():
            if not isinstance(source, Mapping):
                continue
            owner = produced.get(str(source.get("path") or ""))
            if owner is not None and owner != name:
                upstream.add(owner)
        edges[name] = frozenset(upstream)
    return edges


def order_packages(packages: Sequence[Package],
                   derived: Optional[Mapping[str, FrozenSet[str]]] = None
                   ) -> Tuple[str, ...]:
    """Dependency order, declared edges unioned with the derived ones.

    Ties break on name, so the printed command list is the same on every run
    for the same plans -- an operator comparing two `deliver` outputs is
    comparing the plans, not the iteration order of a set.
    """
    names = [package.name for package in packages]
    needs = {
        package.name: set(package.depends_on)
        | set((derived or {}).get(package.name, ()))
        for package in packages
    }
    ordered: List[str] = []
    placed: set = set()
    remaining = sorted(names)
    while remaining:
        ready = [name for name in remaining if needs[name] <= placed]
        if not ready:
            raise DeliverError("PACKAGE_CYCLE:{}".format(",".join(remaining)))
        for name in ready:
            ordered.append(name)
            placed.add(name)
        remaining = [name for name in remaining if name not in placed]
    return tuple(ordered)


def run_commands(order: Sequence[str]) -> Tuple[str, ...]:
    """The equivalent commands, for a report an operator has to reproduce."""
    return tuple("maestro run start " + name for name in order)


def ready_packages(order: Sequence[str], shipped: Iterable[str],
                   needs: Mapping[str, FrozenSet[str]]) -> Tuple[str, ...]:
    """Shipped packages whose whole dependency closure also shipped.

    A shipped package sitting behind a blocked one is not runnable, so it does
    not appear in the printed commands. Saying otherwise would hand an operator
    a command that fails on a missing upstream.
    """
    done = set(shipped)
    ready: List[str] = []
    reachable: set = set()
    for name in order:
        if name in done and set(needs.get(name, ())) <= reachable:
            ready.append(name)
            reachable.add(name)
    return tuple(ready)


# ── the authoring lane's turn ───────────────────────────────────────────────

@dataclass(frozen=True)
class AuthorLane:
    """The `author:` configuration block: an opus lane on an admitted route."""

    route: str
    model: str
    effort: str
    profile: Optional[str]
    author_timeout_s: float
    turn_timeout_s: float
    poll_interval_s: float


@dataclass(frozen=True)
class PackageOutcome:
    name: str
    shipped: bool
    attempts: int
    findings: Tuple[Finding, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {"package": self.name, "shipped": self.shipped,
                "attempts": self.attempts,
                "findings": [item.as_dict() for item in self.findings]}


#: `author_turn(kind, prompt, envelope_path) -> Mapping` runs one lane turn and
#: returns the envelope the lane wrote. It raises DeliverError if the lane
#: produced no usable envelope.
AuthorTurn = Callable[[str, str, Path], Mapping[str, Any]]

#: `plan_step(verb, name) -> (status, payloads)` runs `maestro plan <verb>
#: <name>` in process and returns its exit status with every JSON object it
#: printed.
PlanStep = Callable[[str, str], Tuple[int, Sequence[Mapping[str, Any]]]]

#: `reviewer_report(name) -> Mapping | None` reads the reviewer's report for a
#: plan that finalized, so a FAIL verdict yields its finding cells.
ReviewerReport = Callable[[str], Optional[Mapping[str, Any]]]

#: `run_start(name) -> (status, payloads)` runs `maestro run start <name>`.
RunStart = Callable[[str], Tuple[int, Sequence[Mapping[str, Any]]]]

#: `accepted_run(name) -> str | None` names an already ACCEPTED run for the
#: package's current plan bytes, so re-running `deliver` does not redo it.
AcceptedRun = Callable[[str], Optional[str]]

#: `blocked_lanes(run_id) -> rows` reads each stopped lane out of the lifecycle
#: store: which lane, in which state, on which attempt, and why.
BlockedLanes = Callable[[str], Sequence[Mapping[str, Any]]]

#: `release_run(name) -> Sequence[str]` frees whatever a previous run of this
#: package still holds -- principally its integration worktree -- and reports
#: what it released.
ReleaseRun = Callable[[str], Sequence[str]]

#: `shipped(name) -> bool` is true when the package's current plan bytes
#: already carry a PASS finalization receipt.
Shipped = Callable[[str], bool]


@dataclass(frozen=True)
class RunResult:
    """One package's run. ACCEPTED is the only outcome that lets the next
    package start."""

    package: str
    outcome: str
    run_id: str = ""
    merged: Tuple[str, ...] = ()
    lanes: Tuple[Dict[str, Any], ...] = ()
    released: Tuple[str, ...] = ()
    skipped: bool = False

    @property
    def accepted(self) -> bool:
        return self.outcome == "ACCEPTED"

    def as_dict(self) -> Dict[str, Any]:
        return {"package": self.package, "outcome": self.outcome,
                "run_id": self.run_id, "merged": list(self.merged),
                "lanes": [dict(row) for row in self.lanes],
                "released": list(self.released), "skipped": self.skipped}


def run_report(payloads: Sequence[Mapping[str, Any]]) -> Tuple[str, str, Tuple[str, ...]]:
    """`run start` exits 0 on BLOCKED too; the outcome is in its payload.

    Same trap as `plan finalize`: a zero exit status means the scheduler
    reached quiescence and said so, not that the run was accepted.
    """
    for payload in payloads:
        if isinstance(payload, Mapping) and payload.get("outcome"):
            merged = payload.get("merged") or ()
            return (str(payload["outcome"]), str(payload.get("run_id") or ""),
                    tuple(str(item) for item in merged))
    return ("NO_RUN_REPORT", "", ())


@dataclass
class Delivery:
    """The whole verb, with every side effect injected.

    The loop below is the reason this class exists: gate, review, and ship each
    fail differently, a FAIL verdict is terminal for the plan bytes rather than
    a step failure, and the repair turn has to be handed the specific cells
    that failed. That is worth testing without an agent, a pane, or planctl.
    """

    spec: str
    repo: Path
    plans_dir: Path
    envelope_dir: Path
    author_turn: AuthorTurn
    plan_step: PlanStep
    reviewer_report: ReviewerReport
    request: str = ""
    max_attempts: int = MAX_ATTEMPTS
    remove_plan_dir: Optional[Callable[[str], None]] = None
    #: Absent means "author and ship, then report the commands" -- the shape
    #: this verb had before it was asked to run to the end. Present means the
    #: verb runs each package itself, one after another.
    run_start: Optional[RunStart] = None
    accepted_run: Optional[AcceptedRun] = None
    blocked_lanes: Optional[BlockedLanes] = None
    release_run: Optional[ReleaseRun] = None
    shipped: Optional[Shipped] = None
    ledger_path: Optional[Path] = None
    notes: List[str] = field(default_factory=list)

    # -- step 1 ---------------------------------------------------------------
    def architecture(self) -> Tuple[Path, bool]:
        """The approved architecture anchor: reuse one, or author it."""
        existing = self._approved_architecture()
        if existing is not None:
            self.notes.append("reused approved architecture IR " + str(existing))
            return existing, False
        stem = Path(self.spec).stem
        target = self.plans_dir / (stem + IR_SUFFIX)
        envelope = self._envelope_path("architecture")
        payload = self.author_turn(
            "architecture",
            architecture_prompt(self.spec, target, envelope), envelope)
        written = payload.get("architecture_ir")
        path = self._resolve_declared(written, target)
        if not path.is_file():
            raise DeliverError("ARCHITECTURE_IR_MISSING:{}".format(path))
        if _plan_kind(path) != ARCHITECTURE_KIND:
            raise DeliverError("ARCHITECTURE_KIND_WRONG:{}".format(path))
        receipt = path.with_name(
            path.name[:-len(IR_SUFFIX)] + RECEIPT_SUFFIX)
        if not _receipt_passes(receipt, path):
            raise DeliverError("ARCHITECTURE_RECEIPT_MISMATCH:{}".format(path))
        return path, True

    def _resolve_declared(self, written: Any, fallback: Path) -> Path:
        """A lane-declared path, held inside plans_dir or refused.

        The lane names the file it wrote; it does not get to name a file
        outside the directory `deliver` gates. A path that escapes is a
        refusal, not a path that is quietly rewritten.
        """
        if not isinstance(written, str) or not written:
            return fallback
        candidate = Path(written)
        resolved = (candidate if candidate.is_absolute()
                    else (self.repo / candidate)).resolve()
        root = self.plans_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise DeliverError("DECLARED_PATH_OUTSIDE_PLANS_DIR:{}".format(written))
        return resolved

    def _approved_architecture(self) -> Optional[Path]:
        """An architecture IR already carrying a PASS receipt, if one exists."""
        if not self.plans_dir.is_dir():
            return None
        for candidate in sorted(self.plans_dir.glob("*" + IR_SUFFIX)):
            if _plan_kind(candidate) != ARCHITECTURE_KIND:
                continue
            receipt = candidate.with_name(
                candidate.name[:-len(IR_SUFFIX)] + RECEIPT_SUFFIX)
            if _receipt_passes(receipt, candidate):
                return candidate
        return None

    # -- step 2 ---------------------------------------------------------------
    def packages(self, architecture_ir: Path) -> Tuple[Package, ...]:
        remembered = self._remembered_packages()
        if remembered is not None:
            self.notes.append(
                "reused the package set recorded for " + self.spec
                + "; no brownfield authoring turn was spent")
            return remembered
        envelope = self._envelope_path("brownfield")
        payload = self.author_turn(
            "brownfield",
            brownfield_prompt(self.spec, architecture_ir, self.plans_dir,
                              envelope, self.request or self.spec),
            envelope)
        packages = declared_packages(payload)
        for package in packages:
            ir = self.plans_dir / (package.name + IR_SUFFIX)
            if not ir.is_file():
                raise DeliverError("PACKAGE_IR_MISSING:{}".format(ir))
            if _plan_kind(ir) == ARCHITECTURE_KIND:
                raise DeliverError(
                    "ARCHITECTURE_NOT_EXECUTABLE:{}".format(package.name))
        self._remember_packages(packages)
        return packages

    def _remembered_packages(self) -> Optional[Tuple[Package, ...]]:
        """The package set a previous `deliver` recorded for this document.

        Resumability starts here. Without it, re-running `deliver` after a
        halt spends an opus turn re-deriving a package set that already
        exists on disk, and the second answer need not match the first.
        """
        if self.ledger_path is None or not self.ledger_path.is_file():
            return None
        try:
            payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("spec") != self.spec:
            return None
        try:
            packages = declared_packages(payload)
        except DeliverError:
            return None
        for package in packages:
            if not (self.plans_dir / (package.name + IR_SUFFIX)).is_file():
                # The recorded set no longer matches the tree, so it is not a
                # resumption point. Re-derive rather than half-trust it.
                return None
        return packages

    def _remember_packages(self, packages: Sequence[Package]) -> None:
        if self.ledger_path is None:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps({
            "spec": self.spec,
            "packages": [{"name": package.name,
                          "depends_on": list(package.depends_on)}
                         for package in packages],
        }, sort_keys=True), encoding="utf-8")

    # -- step 3 ---------------------------------------------------------------
    def ship(self, package: Package) -> PackageOutcome:
        """gate -> review -> ship, re-authoring on failure, bounded."""
        if self.shipped is not None and self.shipped(package.name):
            # Its current bytes already carry a PASS finalization receipt.
            # Re-shipping would delete the plan directory and re-review bytes
            # that are already approved, which is how a resumed `deliver`
            # would undo the work it is resuming.
            self.notes.append(package.name + " was already shipped")
            return PackageOutcome(package.name, True, 0)
        findings: Tuple[Finding, ...] = ()
        for attempt in range(1, self.max_attempts + 1):
            self._clear_plan_directory(package.name)
            findings = self._one_attempt(package.name)
            if not findings:
                return PackageOutcome(package.name, True, attempt)
            if attempt == self.max_attempts:
                break
            envelope = self._envelope_path("repair-" + package.name
                                           + "-" + str(attempt))
            self.author_turn(
                "repair",
                repair_prompt(package.name,
                              self.plans_dir / (package.name + IR_SUFFIX),
                              envelope, findings, attempt + 1,
                              self.max_attempts),
                envelope)
        return PackageOutcome(package.name, False, self.max_attempts, findings)

    def _one_attempt(self, name: str) -> Tuple[Finding, ...]:
        """One gate/review/ship pass. Empty means the package shipped."""
        for verb in ("gate", "review", "ship"):
            status, payloads = self.plan_step(verb, name)
            if status != 0:
                found = step_findings(verb, payloads)
                return found or (Finding(verb, "STEP_FAILED",
                                         message="exit status "
                                                 + str(status)),)
            if verb == "ship":
                verdict = _shipped_verdict(payloads)
                if verdict == "FAIL":
                    found = report_findings(self.reviewer_report(name))
                    return found or (Finding(
                        "finalize", "REVIEWER_FAIL",
                        message="the reviewer returned FAIL and the report "
                                "carried no readable cell"),)
        return ()

    def _clear_plan_directory(self, name: str) -> None:
        """`plan author` is create-once, so a re-ship starts from no plan."""
        if self.remove_plan_dir is not None:
            self.remove_plan_dir(name)

    def _envelope_path(self, label: str) -> Path:
        return self.envelope_dir / ("deliver-" + label + ".envelope.json")

    # -- the whole verb -------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        architecture_ir, authored = self.architecture()
        packages = self.packages(architecture_ir)
        irs = {package.name: _load_ir(self.plans_dir / (package.name + IR_SUFFIX))
               for package in packages}
        derived = derived_edges(irs)
        order = order_packages(packages, derived)
        needs = {
            package.name: frozenset(set(package.depends_on)
                                    | set(derived.get(package.name, ())))
            for package in packages
        }
        outcomes: List[PackageOutcome] = []
        by_name = {package.name: package for package in packages}
        blocked_closure: set = set()
        for name in order:
            if needs[name] & blocked_closure:
                # An upstream package never shipped, so this one cannot be
                # gated against it. Report it as blocked rather than spending
                # three opus attempts on a plan whose input does not exist.
                outcomes.append(PackageOutcome(
                    name, False, 0,
                    (Finding("deliver", "UPSTREAM_BLOCKED",
                             message="depends on " + ", ".join(
                                 sorted(needs[name] & blocked_closure))),)))
                blocked_closure.add(name)
                continue
            outcome = self.ship(by_name[name])
            outcomes.append(outcome)
            if not outcome.shipped:
                blocked_closure.add(name)
        shipped = [item.name for item in outcomes if item.shipped]
        ready = ready_packages(order, shipped, needs)
        blocked = [item for item in outcomes if not item.shipped]
        payload = {
            "spec": self.spec,
            "architecture_ir": str(architecture_ir),
            "architecture_authored": authored,
            "order": list(order),
            "packages": [item.as_dict() for item in outcomes],
            "ready": list(ready),
            "run_commands": list(run_commands(ready)),
            "runs": [],
            "notes": list(self.notes),
        }
        if blocked or self.run_start is None:
            payload["outcome"] = (
                "DELIVER_BLOCKED" if blocked else "DELIVERED_NOT_RUN")
            payload["notes"] = list(self.notes)
            return payload
        runs, halted = self.run_all(ready)
        payload["runs"] = [item.as_dict() for item in runs]
        payload["outcome"] = "DELIVER_HALTED" if halted else "DELIVERED"
        if halted is not None:
            payload["halted_on"] = halted
        payload["notes"] = list(self.notes)
        return payload

    # -- step 4: run each package, in order, one at a time --------------------
    def run_all(self, order: Sequence[str]
                ) -> Tuple[Tuple[RunResult, ...], Optional[Dict[str, Any]]]:
        """Start each package's run in dependency order and stop at the first
        that is not ACCEPTED.

        Sequential on purpose, and not a scheduler. Inside one plan the DAG
        scheduler already runs every lane in dependency order at
        `execution.concurrency` and already bounds each lane's retries; this
        loop is strictly ACROSS plans. It also cannot be parallel: a run's
        integration worktree takes the plan's integration branch, git gives a
        branch to one worktree at a time, and a second concurrent run refuses
        with INTEGRATION_BRANCH_CHECKED_OUT.
        """
        results: List[RunResult] = []
        for name in order:
            existing = (self.accepted_run(name)
                        if self.accepted_run is not None else None)
            if existing:
                self.notes.append(
                    name + " was already accepted by run " + existing)
                results.append(RunResult(name, "ACCEPTED", existing,
                                         skipped=True))
                continue
            released = tuple(self.release_run(name)
                             if self.release_run is not None else ())
            status, payloads = self.run_start(name)
            outcome, run_id, merged = run_report(payloads)
            lanes = tuple(
                dict(row) for row in
                (self.blocked_lanes(run_id)
                 if self.blocked_lanes is not None and run_id else ()))
            result = RunResult(name, outcome, run_id, merged, lanes, released)
            results.append(result)
            if status != 0 or not result.accepted:
                return tuple(results), {
                    "package": name,
                    "outcome": outcome,
                    "status": status,
                    "run_id": run_id,
                    "lanes": [dict(row) for row in lanes],
                    "not_started": [item for item in order
                                    if item not in
                                    {row.package for row in results}],
                }
        return tuple(results), None


# ── small readers ───────────────────────────────────────────────────────────

def _load_ir(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliverError("IR_UNREADABLE:{}".format(path)) from exc
    if not isinstance(payload, dict):
        raise DeliverError("IR_NOT_OBJECT:{}".format(path))
    return payload


def _plan_kind(path: Path) -> str:
    try:
        return str(_load_ir(path).get("plan_kind") or "")
    except DeliverError:
        return ""


def _receipt_passes(path: Path, ir_path: Path) -> bool:
    """True only for a PASS receipt bound to these exact IR bytes."""
    if not Path(path).is_file():
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        ir_bytes = Path(ir_path).read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("verdict") != "PASS":
        return False
    return payload.get("ir_sha256") == hashlib.sha256(ir_bytes).hexdigest()


def _shipped_verdict(payloads: Sequence[Mapping[str, Any]]) -> str:
    """`plan finalize` exits 0 on FAIL too; the verdict is in its payload.

    This is the trap the loop exists for. A FAIL verdict is terminal for those
    plan bytes -- rerunning `plan ship` on them can never pass -- so it has to
    be read as a failed attempt even though every step returned 0.
    """
    for payload in payloads:
        if isinstance(payload, Mapping) and payload.get("verdict"):
            return str(payload["verdict"])
    return ""
