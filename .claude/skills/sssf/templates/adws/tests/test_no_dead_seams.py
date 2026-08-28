"""Every production reader has a production writer — B15's mirror.

`MAESTRO_architecture.md` Family B item B15 makes a check field with zero
*readers* a build failure. This file enforces the other direction, which is the
one that has been reaching production: a typed field, dataclass attribute, enum
member, or callable that production READS or BRANCHES ON but never WRITES or
CALLS. The value is structurally unreachable, so the behaviour the branch guards
silently never happens — and because the branch exists and the suite constructs
the value by hand, every test stays green while production takes the fallback
path forever. Three consecutive production runs failed on instances of this
before anything in the suite noticed, because nothing in the suite was looking.

A deliberately test-only symbol is legal. What is not legal is for it to be
test-only *silently*: it must appear in `ALLOWED` with the reason it has no
production writer. That allowlist is the entire mechanism. It converts "nobody
noticed this had no writer" into "somebody wrote down why it has none", and an
entry marked DEFERRED is a work item with a name, not a suppression — landing
its fix means deleting its line.

**What this cannot catch.** A green run here is not a proof that no dead seam
exists, and reading it as one would recreate the false confidence it was written
to remove:

* **Dynamic writers.** `setattr`, `Cls(**payload)` splats, `object.__setattr__`,
  and deserialization into a model (`model_validate`, row factories) are
  invisible to it. A class built by splat is skipped outright rather than
  guessed at, so a genuine defect inside one hides.
* **Writers inside unreachable branches.** A writer that exists but sits in code
  nothing reaches still counts as a writer here. `LaunchHandle.process_group`
  is that shape and needed a human to see it.
* **Semantic deadness.** A field written only with its own default, or an enum
  member constructed only on a path that cannot be taken, still reads as live.
  A constructor argument whose *every* production site is a no-op lambda used
  to sit in this bucket too — the lambda counted as a writer — and is now a
  separate check. A named no-op (`def _drop(...): return None`) is not.

Each of those needs a reader, not a sweep. The complementary check — driving a
real node attempt through the production `run_node` adapter rather than a
re-implementation — catches a different slice and is worth having as well; it is
not a substitute for this and this is not a substitute for it.

**Why this resolves the callee class, and grep cannot.** A field hides behind a
same-named keyword argument on an unrelated class: `min_cases=` passed to a plan
gate is not a write of `SchedulerDeps.min_cases`, but to any analysis keyed on
the bare name — this file's own first draft, and every grep anyone would write —
it looks like one. `SchedulerDeps.min_cases` had no production writer for exactly
as long as that masking held, while the plan's per-gate value reached the agent's
prompt and the adjudicator read a default of 1. Attributing every write to the
class the callee resolves to is the whole reason this check sees what a text
search cannot.

**And which class the callee resolves to is a question, not a given.** Two
production modules may define a dataclass by the same name. Keying field order
on the bare name alone lets whichever definition is parsed last silently
replace the other's, and then every construction of *either* class is zipped
against the wrong field order. That degrades in both directions at once:
`deliver.Finding(source, code, pointer, message)` read as unwritten in `source`
and `pointer`, because `plan_amendment.Finding`'s three-field order had
displaced it and the fourth argument fell off the end of the zip -- while
`plan_amendment.Finding.node_id` simultaneously read as *written* by sixteen
call sites, seven of which were `deliver.py`'s and wrote no such field. The
loud half was a false positive; the quiet half is a false negative, and it is
the half that would have hidden the next real seam. Resolution is therefore
module-local first -- which is what Python itself does with a bare name -- and
a name that genuinely collides must be declared in `SHARED_DATACLASS_NAMES`
with the reason, so the ambiguity is written down rather than rediscovered.

The residual, stated: `scoped_writes` stays keyed on the *bare* class name,
because `ALLOWED` and the converse check below are written that way. Two
same-named classes that also share a *field* name would still cross-attribute
that one field. `SHARED_DATACLASS_NAMES` carries the reason each collision is
tolerated, and that reason is where a shared field name has to be argued.

Run:  uv run adw_test.py -k dead_seams
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ADWS = Path(__file__).resolve().parent.parent

#: Symbols with no production writer or caller, each with the reason it is
#: legal. `DEFERRED:` marks a known defect with an owner and an audit row, not
#: an accepted state — deleting the line is part of landing the fix.
ALLOWED: Dict[str, str] = {
    # ── the hidden-tests containment prerequisite ───────────────────────────
    # DEFERRED, and deferred with a named successor rather than suppressed:
    # these are the landed half of `test_visibility: hidden`. The half that
    # calls them -- the composed evaluation tree, the absence/provenance/
    # coverage conjuncts, the sanitised repair handoff -- is not built, so
    # `maestro-plan.v5` is deliberately absent from
    # `maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS` and no run can reach a hidden
    # node. Containment was landed first because `docs/hidden-tests-design.md`
    # §9's verdict is that the feature is not worth building until it is
    # proven, and proving it is regression 7. Delete these five lines in the
    # change that makes v5 runnable; a caller arrives with it.
    "hidden_vault.attempt_repository": "DEFERRED: hidden-tests step 2 is the caller; v5 is unrunnable until then",
    "hidden_vault.blob_id_in": "DEFERRED: hidden-tests step 2 is the caller; v5 is unrunnable until then",
    "hidden_vault.object_is_absent": "DEFERRED: containment assertion; hidden-tests step 2 is the caller",
    "hidden_vault.unreachable_from": "DEFERRED: containment assertion; hidden-tests step 2 is the caller",
    # ── plan amendment: the store half, landed ahead of its wiring ───────────
    # DEFERRED with a named successor. These are the durable half of amending a
    # run's plan without discarding merged work: retained plan bytes, the
    # lineage, and the guarded `dag_nodes` writer. The caller is the maestro
    # side — `record_plan_version` at `run start`, `current_plan` in
    # `_resume_run_selection`, and a `run amend` verb running
    # `plan_amendment.classify` before `amend_run_plan`.
    #
    # They are landed first and unwired on purpose: the `dag_nodes` write is
    # §19 M42's shape and was the part worth proving before anything could
    # reach it. Nothing behaves differently while these have no caller, which
    # is what makes the half-landing safe rather than the half-built path the
    # brief warned against. Delete these four lines with the wiring.
    # `record_plan_version`, `current_plan` and `amend_run_plan` were listed
    # here while the store half was landed ahead of its wiring. All three now
    # have production callers -- `Scheduler.project`, `_resolve_resume_target`
    # and `_run_amend` -- and this check convicted the stale entries the moment
    # they did, which is the allowlist working rather than a nuisance.
    #
    # ── §1.2's detectors ────────────────────────────────────────────────────
    # Their job is to convict a planted violation from a test. Production never
    # calls a detector; a detector production called would be a linter.
    "verification.free_text_reads": "detector, asserted from tests only",
    "retry_policy.find_output_content_comparisons": "detector: convicts a classifier that compares against process output text",
    "retry_policy.find_ungated_git_absence": "detector: convicts a git-absence conclusion drawn without a gate",
    "finalization.find_forbidden_report_fields": "detector: convicts verdict or severity appearing in a reviewer report (node review's -- it is the only report a reviewer writes now)",
    "permissions.argv_denies_delegation": "detector: convicts a launch argv that leaves an actor able to delegate the work its signature attests to (B12/§1.2). Production removes the capability rather than reading it back — `route_capability_argv` is the production half — so this exists to assert over the real argv builders and over a planted violation",
    "enforcement.scan_real_tree": "detector: scans the installed tree for §13.4 fixture violations",
    "enforcement.assert_installed_bytes": "detector: proves the installed bytes match the reviewed tree (RC8)",
    "enforcement.assert_verbs": "detector: proves the CLI exposes the verbs the docs claim",
    "enforcement.assert_workspace_verbs": "detector: the workspace half of the verb-surface proof",
    "maestro.parser_verbs": "detector: enumerates the argparse surface for the verb-surface proof",
    # ── the offline golden scenario's fake adapter (§13.1) ──────────────────
    "launcher.complete": "FakeLauncher, the golden scenario's test double",
    # ── property tables and derived accessors ───────────────────────────────
    # A named view over state production already maintains, existing so a test
    # can state a property about it. Nothing branches on these in production,
    # so nothing is unreachable behind them.
    "participant.active_participant_ids": "accessor over the runner's live "
    "process map, asserted in tests",
    "worktree.is_empty": "derived predicate over InventoryDelta",
    "lifecycle.audit_orphans": "accessor over recorded unreachable panes, asserted in tests",
    "publication.prepare": "retained as the two-phase test seam for the "
    "prepare-to-push window, not deferred work",
    # ── DEFERRED — audited, owner assigned, fix not yet landed ──────────────
    # Rows D1-D11 of the dead-seam audit. Each is a real instance of this
    # class; none is accepted as correct.
    # D6. Honestly unreachable rather than mistakenly unwired, and the one
    # entry here that is NOT deferred work: §8.3 states herdr's recorded
    # surface exposes no pid and no process group, and §16.3 item 17 makes
    # adopting one conditional on a receipt that does not exist. Deleting the
    # field would delete the seam that receipt is meant to fill.
    "LaunchHandle.process_group": "§8.3 + §16.3 item 17: no group exists to "
    "record for an agent node until the quiesce "
    "receipt lands. Deliberate, not deferred.",
    # The sibling field, and a different reason: this one HAS a production
    # writer -- `HerdrLauncher.launch` sets it after the agent starts (and,
    # for direct Claude, after its separate prompt submission) -- but does so
    # through `object.__setattr__`, which this module's own header lists among
    # the dynamic writers it cannot see. Allowlisting it here says
    # "the sweep cannot see this write", not "there is no write", and the
    # difference is checked by a reader rather than asserted: the write is
    # driven end to end through the real launch path in
    # `test_agent_liveness_pid.py::LaunchRecordsTheLivenessPidTests`, which
    # fails if `launch` stops populating it or stops calling herdr at all.
    # It is NOT written as a constructor keyword, because the value cannot
    # exist at construction time -- before the agent starts, the pane's
    # foreground group is still its own shell -- and passing
    # `liveness_pid=None` there to satisfy this sweep would be exactly the
    # "field written only with its own default" that the header names as
    # semantic deadness.
    "LaunchHandle.liveness_pid": "written by HerdrLauncher.launch via "
    "object.__setattr__, which this sweep "
    "documents as invisible; covered by "
    "test_agent_liveness_pid.py",
    # D10. Does not reach the scheduler from maestro.py's SchedulerDeps
    # construction. (D11 stood here too and is fixed: a gate's threshold now
    # travels on `PlanNode.gate_min_cases`, the one integration gate's on
    # `SchedulerDeps.integration_min_cases`, and the per-run scalar this
    # registry named was deleted rather than wired.)
    # D3 / D4 / D5. retry_policy.py, under investigation; do not delete.
    # D2 / D7 / D8 / D9. (D1, and the D4, D5, D7 and D9 rows that stood here,
    # were removed when the bidirectional check below convicted them: each had
    # acquired the production caller its entry said it lacked, and the entry
    # had gone on suppressing a check for a defect that no longer existed.)
    "lifecycle.audit_results": "DEFERRED D7: reader over §7.7's results "
    "table, which now has a production writer "
    "(scheduler._record_result) while this reader "
    "still has no production caller. Run status "
    "renders from the table; nothing audits it.",
    "route_receipts.load_public_key": "DEFERRED D9: production derives keys via "
    "crypto.seed_to_public_key instead.",
    # Audited instances of this same class that sit on the deliver, workspace,
    # or diagnostics paths rather than on `run start`. Real, recorded, and
    # deferred to a pass that owns those paths.
    "lifecycle.audit_transitions": "DEFERRED: diagnostics reader over the "
    "transitions table, no production caller",
    # The launcher-classification lane's typed failure signal. Its writers land
    # with that lane; `launcher_failure` itself is already wired.
    "FailureSignal.binary_resolved": "DEFERRED: launcher-classification lane",
    "FailureSignal.process_started": "DEFERRED: launcher-classification lane",
}

#: Production dataclass names defined in more than one module, each with the
#: reason the collision is tolerated. A shared name is not itself a defect --
#: two unrelated domains may each have earned the word -- but it is the one
#: input that makes every other check in this file approximate rather than
#: exact, so it is declared rather than discovered. An entry must say what the
#: two field lists are and why cross-attribution between them is harmless.
SHARED_DATACLASS_NAMES: Dict[str, str] = {
    "Finding": (
        "deliver.Finding (source, code, pointer, message) is a reason a work "
        "package did not ship; plan_amendment.Finding (code, node_id, detail) "
        "is a reason an amendment is refused. Unrelated domains, and each is "
        "constructed only within its own module, so module-local resolution "
        "is exact for both. The residual is the bare-name key on "
        "scoped_writes: the two share the field name `code`, and both write "
        "it at every construction, so neither can mask the other there."
    ),
}


def _production_files() -> List[Path]:
    out: List[Path] = []
    for path in sorted(ADWS.rglob("*.py")):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or ".omc" in parts:
            continue
        out.append(path)
    return out


def _test_files() -> List[Path]:
    return sorted((ADWS / "tests").glob("*.py"))


def _has_default_factory(stmt: ast.AnnAssign) -> bool:
    """A `default_factory` field holds a mutable object mutated in place —
    `threading.Event.set()`, `list.append` — and is correctly never rebound.
    "Never assigned" is not "never written" for these."""
    value = stmt.value
    if not isinstance(value, ast.Call):
        return False
    return any(kw.arg == "default_factory" for kw in value.keywords)


def _is_dataclass(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", "")
        )
        if name == "dataclass":
            return True
    return False


def _callee_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _is_noop_lambda(node: ast.AST) -> bool:
    """`lambda ...: None` — the writer that is not a writer (#99).

    A named function that returns None is a different shape and is not this
    check: the point is the anonymous slot that reads as wired at the call
    site while implementing nothing.
    """
    if not isinstance(node, ast.Lambda):
        return False
    body = node.body
    return isinstance(body, ast.Constant) and body.value is None


def _noop_lambda_offenders(prod: "_Index") -> List[str]:
    """Constructor parameters whose every production pass is a no-op lambda."""
    offenders: List[str] = []
    for (cls, param), writes in sorted(prod.constructor_args.items()):
        key = f"{cls}.{param}"
        if key in ALLOWED or not writes:
            continue
        if all(_is_noop_lambda(value) for _loc, value in writes):
            locs = [loc for loc, _ in writes]
            offenders.append(
                f"{key} is passed a no-op lambda at every production call "
                f"site {locs[:3]}; the seam reads as wired and records "
                "nothing. Wire a real callable, delete the parameter, or "
                "add it to ALLOWED with the reason the no-op is stated."
            )
    return offenders


class _Index:
    """Definitions and usages over one file set."""

    def __init__(self) -> None:
        self.fields: Dict[str, Dict[str, str]] = {}
        #: Field order and `__init__` parameters, keyed by (defining module,
        #: class) rather than by class name alone. Two production modules
        #: define a dataclass named `Finding`; on a bare-name key the second
        #: one parsed replaced the first, and from then on every construction
        #: of either class was zipped against the other's field order. The
        #: module is what disambiguates, because it is what Python itself
        #: uses to resolve a bare `Finding(...)`.
        self.module_field_order: Dict[Tuple[str, str], List[str]] = {}
        self.module_constructors: Dict[Tuple[str, str], List[str]] = {}
        #: Bare class name -> every module that defines a class by it. Used to
        #: resolve a name that is *imported* rather than module-local.
        self.definitions: Dict[str, List[str]] = {}
        self.callables: Dict[str, str] = {}
        self.reads: Dict[str, Set[str]] = {}
        self.writes: Dict[str, Set[str]] = {}
        self.scoped_writes: Dict[Tuple[str, str], Set[str]] = {}
        self.splatted: Set[str] = set()
        self.refs: Dict[str, Set[str]] = {}
        #: Where each name is *called*, as opposed to merely mentioned.
        #:
        #: `refs` counts every appearance of a bare name, which is right for
        #: the forward checks — over-counting there only makes them quieter.
        #: The converse check needs the opposite bias, and `refs` gives it a
        #: false positive immediately: `launcher.py` binds a local named
        #: `complete`, and on `refs` alone that reads as a production call to
        #: `FakeLauncher.complete`, which is the golden scenario's test double.
        #: A call site cannot be a local binding, so this is what the converse
        #: check keys on.
        self.calls: Dict[str, Set[str]] = {}
        #: Production constructor arguments, keyed by (Class, param). The
        #: field check counts any keyword as a writer; this is what lets a
        #: `lambda ...: None` be seen as the absence it actually is.
        self.constructors: Dict[str, List[str]] = {}
        self.constructor_args: Dict[Tuple[str, str], List[Tuple[str, ast.AST]]] = {}

    def define(self, path: Path, tree: ast.Module) -> None:
        module = path.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if _is_dataclass(node):
                    fields = self.fields.setdefault(node.name, {})
                    order: List[str] = []
                    for stmt in node.body:
                        if not (
                            isinstance(stmt, ast.AnnAssign)
                            and isinstance(stmt.target, ast.Name)
                        ):
                            continue
                        order.append(stmt.target.id)
                        if _has_default_factory(stmt):
                            continue
                        fields[stmt.target.id] = f"{path.name}:{stmt.lineno}"
                    self.module_field_order[(module, node.name)] = order
                self.definitions.setdefault(node.name, []).append(module)
                init = next(
                    (
                        stmt
                        for stmt in node.body
                        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and stmt.name == "__init__"
                    ),
                    None,
                )
                if init is not None:
                    params = [arg.arg for arg in init.args.args if arg.arg != "self"]
                else:
                    params = list(
                        self.module_field_order.get((module, node.name), ())
                    )
                if init is not None or params:
                    self.constructors[node.name] = params
                    self.module_constructors[(module, node.name)] = params
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.callables.setdefault(
                    f"{module}.{node.name}", f"{path.name}:{node.lineno}"
                )

    def _shapes_for(
        self, module: str, name: str, table: Dict[Tuple[str, str], List[str]]
    ) -> List[List[str]]:
        """Which class does a bare `Name(...)` call in `module` construct?

        Module-local definition first, which is Python's own answer and is
        exact. Otherwise the name was imported, and the shape is whichever
        module defines it -- unambiguous unless the name collides, in which
        case every candidate is returned and the write is attributed to all of
        them. That over-attributes rather than under-attributes, so a genuine
        seam behind a collided name reads as written; `SHARED_DATACLASS_NAMES`
        is the reader that makes that possibility visible instead of silent.
        """
        local = table.get((module, name))
        if local is not None:
            return [local]
        return [
            table[(mod, name)]
            for mod in self.definitions.get(name, ())
            if (mod, name) in table
        ]

    def _record_construction(
        self, owner: str, call: ast.Call, loc: str, order: Sequence[str]
    ) -> None:
        for field, _arg in zip(order, call.args):
            self.scoped_writes.setdefault((owner, field), set()).add(loc)
        for kw in call.keywords:
            if kw.arg:
                self.scoped_writes.setdefault((owner, kw.arg), set()).add(loc)
            else:
                # `Cls(**payload)` writes an unknowable set of fields.
                self.splatted.add(owner)

    def _record_constructor_args(
        self, owner: str, call: ast.Call, loc: str, params: Sequence[str]
    ) -> None:
        for param, arg in zip(params, call.args):
            self.constructor_args.setdefault((owner, param), []).append((loc, arg))
        for kw in call.keywords:
            if kw.arg:
                self.constructor_args.setdefault((owner, kw.arg), []).append(
                    (loc, kw.value)
                )

    def use(self, path: Path, tree: ast.Module) -> None:
        module = path.stem
        # A classmethod builds its own class as `cls(...)`. Resolve that first,
        # or every field a `parse`/`from_row` constructor writes reads as
        # unwritten — which is most of GateCounts.
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.ClassDef):
                continue
            loc = f"{path.name}:{{0}}"
            for inner in ast.walk(outer):
                if not (isinstance(inner, ast.Call) and _callee_name(inner) == "cls"):
                    continue
                site = loc.format(inner.lineno)
                for order in self._shapes_for(
                    module, outer.name, self.module_field_order
                ):
                    self._record_construction(outer.name, inner, site, order)
                for params in self._shapes_for(
                    module, outer.name, self.module_constructors
                ):
                    self._record_constructor_args(outer.name, inner, site, params)

        for node in ast.walk(tree):
            loc = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Attribute):
                bucket = self.writes if isinstance(node.ctx, ast.Store) else self.reads
                bucket.setdefault(node.attr, set()).add(loc)
                self.refs.setdefault(node.attr, set()).add(loc)
            elif isinstance(node, ast.Name):
                self.refs.setdefault(node.id, set()).add(loc)
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name:
                    self.calls.setdefault(name, set()).add(loc)
                # Attribute the write to *that class's* field, never to
                # every field of that name anywhere: `min_cases=` on a plan
                # gate is not a write of `SchedulerDeps.min_cases`, and
                # treating it as one is exactly how an unwired field hides
                # behind a namesake in another class. `_shapes_for` carries
                # that same argument one level up, to the class name itself.
                orders = (
                    self._shapes_for(module, name, self.module_field_order)
                    if name
                    else []
                )
                for order in orders:
                    self._record_construction(name or "", node, loc, order)
                if not orders:
                    for kw in node.keywords:
                        if kw.arg:
                            self.writes.setdefault(kw.arg, set()).add(loc)
                if name:
                    for params in self._shapes_for(
                        module, name, self.module_constructors
                    ):
                        self._record_constructor_args(name, node, loc, params)


def _build() -> Tuple[_Index, _Index]:
    prod, test = _Index(), _Index()
    prod_trees = [
        (p, ast.parse(p.read_text(), filename=str(p))) for p in _production_files()
    ]
    test_trees = [(p, ast.parse(p.read_text(), filename=str(p))) for p in _test_files()]
    for path, tree in prod_trees:
        prod.define(path, tree)
    for path, tree in prod_trees:
        prod.use(path, tree)
    # Tests reuse production definitions; only their usages differ. The
    # module-scoped tables travel with them, or a test-module call site would
    # resolve nothing and every field would read as unwritten by tests.
    test.fields = prod.fields
    test.module_field_order = prod.module_field_order
    test.definitions = prod.definitions
    for path, tree in test_trees:
        test.use(path, tree)
    return prod, test


class DeadSeamTest(unittest.TestCase):
    def test_the_tree_was_actually_found(self) -> None:
        """A mislocated or empty ADWS makes every check below pass vacuously,
        which is the one way this file could lie in the reassuring direction."""
        production = _production_files()
        self.assertGreater(
            len(production),
            40,
            f"only {len(production)} production files under "
            f"{ADWS} — the checks below would pass vacuously",
        )
        self.assertGreater(len(_test_files()), 20, "test files not found")

    def test_every_branched_field_has_a_production_writer(self) -> None:
        prod, test = _build()
        offenders: List[str] = []
        for cls, fields in sorted(prod.fields.items()):
            for field, where in sorted(fields.items()):
                if field.startswith("_"):
                    continue
                key = f"{cls}.{field}"
                if key in ALLOWED:
                    continue
                if not prod.reads.get(field):
                    continue  # unread is B15's problem, not this one
                if cls in prod.splatted:
                    continue  # `Cls(**payload)` writes we cannot see
                if prod.scoped_writes.get((cls, field)):
                    continue
                if prod.writes.get(field):
                    continue  # written through a callee we could not resolve
                test_writes = test.scoped_writes.get(
                    (cls, field), set()
                ) | test.writes.get(field, set())
                offenders.append(
                    f"{key} ({where}) is read in production at "
                    f"{sorted(prod.reads[field])[:3]} and written nowhere in "
                    f"production; {len(test_writes)} test writes"
                )
        self.assertEqual(
            [],
            offenders,
            "a production reader with no production writer: the branch it "
            "guards can never be taken, so the behaviour behind it silently "
            "never happens. Wire a writer, delete the field and its branch "
            "together, or add it to ALLOWED with the reason it has none.\n"
            + "\n".join(offenders),
        )

    def test_every_callable_is_referenced_in_production(self) -> None:
        prod, test = _build()
        offenders: List[str] = []
        for qualified, where in sorted(prod.callables.items()):
            _module, _, name = qualified.partition(".")
            if name.startswith("__") or qualified in ALLOWED:
                continue
            refs = {r for r in prod.refs.get(name, set()) if r != where}
            if refs:
                continue
            if not test.refs.get(name):
                continue  # referenced nowhere at all is a different check
            offenders.append(
                f"{qualified} ({where}) has no production reference; "
                f"{len(test.refs[name])} test references"
            )
        self.assertEqual(
            [],
            offenders,
            "a callable exercised only by tests. Either production should call "
            "it, or it is dead and should go with its tests, or it is "
            "deliberately test-only and belongs in ALLOWED with a reason.\n"
            + "\n".join(offenders),
        )

    def test_no_constructor_parameter_is_only_fed_a_noop_lambda(self) -> None:
        """#99: a `lambda ...: None` is not a writer, even though it is a
        keyword argument the field check counts as one."""
        prod, _test = _build()
        self.assertEqual(
            [],
            _noop_lambda_offenders(prod),
            "a constructor parameter whose every production call site is a "
            "no-op lambda. The field check cannot see this — the lambda is "
            "the writer it counts. Wire a real callable, delete the "
            "parameter, or add it to ALLOWED with the reason the no-op is "
            "stated.\n" + "\n".join(_noop_lambda_offenders(prod)),
        )

    def test_a_noop_lambda_writer_is_now_convicted_and_was_not(self) -> None:
        """§13.4 / §16.3: the extension convicts a planted slot the field
        check acquits, because the lambda counted as a writer."""
        planted = ast.parse(
            "class Window:\n"
            "    def __init__(self, record_reviewer_session):\n"
            "        self._r = record_reviewer_session\n"
            "Window(record_reviewer_session=lambda _s: None)\n"
        )
        idx = _Index()
        idx.define(Path("planted.py"), planted)
        idx.use(Path("planted.py"), planted)
        # The field check keys on dataclass fields. Window is not one, so
        # it is silent — and even if the slot were a field, the lambda
        # would count as a write.
        self.assertNotIn("Window", idx.fields)
        self.assertTrue(
            idx.writes.get("record_reviewer_session"),
            "the old check's write bucket saw the lambda",
        )
        offenders = _noop_lambda_offenders(idx)
        self.assertTrue(
            any("Window.record_reviewer_session" in row for row in offenders),
            "planted no-op lambda was not convicted: " + repr(offenders),
        )
        # Control: a real callable at the same slot is not a dead seam.
        live = ast.parse(
            "class Window:\n"
            "    def __init__(self, record_reviewer_session):\n"
            "        self._r = record_reviewer_session\n"
            "def record(session):\n"
            "    persist(session)\n"
            "Window(record_reviewer_session=record)\n"
        )
        live_idx = _Index()
        live_idx.define(Path("live.py"), live)
        live_idx.use(Path("live.py"), live)
        self.assertEqual([], _noop_lambda_offenders(live_idx))

    def test_a_same_named_dataclass_no_longer_steals_the_other_field_order(
        self,
    ) -> None:
        """The collision that hid two real writers and invented seven fake ones.

        `deliver.Finding(source, code, pointer, message)` and
        `plan_amendment.Finding(code, node_id, detail)` are both production
        dataclasses. On a bare-name key the second parsed replaced the first,
        so every construction in either module was zipped against the other's
        order. Both directions are asserted here, because only one of them
        fails loudly:

        * `w`/`x`/`y`/`z` read as **unwritten** although their own module
          writes all four positionally -- the false positive, which is what
          finally sent someone to look.
        * `p`/`q` read as **written** by a module that has never heard of
          them -- the false negative, which is the half that would have
          acquitted a genuine seam in silence.
        """
        alpha = ast.parse(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Thing:\n"
            "    w: int\n"
            "    x: int\n"
            "    y: int\n"
            "    z: int\n"
            "def build():\n"
            "    return Thing(1, 2, 3, 4)\n"
        )
        beta = ast.parse(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Thing:\n"
            "    p: int\n"
            "    q: int\n"
        )
        idx = _Index()
        # Parse order is the one that used to decide the winner: `beta` is
        # sorted after `alpha`, so beta's two-field order was the survivor.
        idx.define(Path("alpha.py"), alpha)
        idx.define(Path("beta.py"), beta)
        idx.use(Path("alpha.py"), alpha)
        idx.use(Path("beta.py"), beta)

        for field in ("w", "x", "y", "z"):
            self.assertTrue(
                idx.scoped_writes.get(("Thing", field)),
                f"alpha.Thing.{field} is written positionally by alpha and "
                "reads as unwritten: the other module's field order won",
            )
        for field in ("p", "q"):
            self.assertFalse(
                idx.scoped_writes.get(("Thing", field)),
                f"beta.Thing.{field} reads as written, but only alpha "
                "constructs anything and alpha has no such field",
            )

    def test_no_allowlist_entry_has_acquired_a_production_caller(self) -> None:
        """The allowlist read in one direction only, and that is how an entry
        outlives the defect it names.

        The two checks above fail when a test-only symbol is *missing* from
        `ALLOWED`. Neither ever fails when an allowlisted symbol *acquires* a
        production writer or caller, so an entry — and the audit row, register
        line, or architecture item written from it — stays true-sounding long
        after the code stopped agreeing with it. Three entries were in exactly
        that state when this was added: two named symbols the scheduler's
        settle path had begun calling, and their reasons still read "no
        production writer" and "never called in production".

        This is the converse assertion over data the checks above already
        compute. Two limits, stated rather than discovered:

        * It keys on the callee name, so a same-named method on an unrelated
          class reads as a caller. That is the masking this file's docstring
          describes, in the direction that over-reports — which fails loudly
          and sends someone to look, rather than passing quietly.
        * An entry that names a symbol which no longer exists is a different
          staleness and is not caught here. It needs its own reader.
        """
        prod, _test = _build()
        stale: List[str] = []
        for key in sorted(ALLOWED):
            module_or_class, _, name = key.rpartition(".")
            if not name:
                continue
            # A `Cls.field` entry: it claims no production writer.
            # `scoped_writes` and nothing else: it attributes a write to the
            # class the callee resolves to, which is the whole reason this file
            # sees what a text search cannot. `writes` is keyed on the bare
            # name and would report a namesake on an unrelated class.
            writes = prod.scoped_writes.get((module_or_class, name), set())
            if writes:
                stale.append(
                    f"{key} is allowlisted as having no production writer, "
                    f"but production writes it at {sorted(writes)[:3]}"
                )
                continue
            # A `module.callable` entry: it claims no production caller.
            where = prod.callables.get(key)
            if where is None:
                continue
            calls = {call for call in prod.calls.get(name, set()) if call != where}
            if calls:
                stale.append(
                    f"{key} is allowlisted as having no production caller, "
                    f"but production calls it at {sorted(calls)[:3]}"
                )
        self.assertEqual(
            [],
            stale,
            "an allowlist entry that is no longer true. The symbol has "
            "acquired a production writer or caller, so the entry now "
            "suppresses a check for a defect that no longer exists — and "
            "anything written from that entry, in an audit row or an "
            "architecture register, went stale with it. Delete the entry.\n"
            + "\n".join(stale),
        )

    def test_no_shared_dataclass_name_is_undeclared_and_none_is_stale(
        self,
    ) -> None:
        """A class name defined in two modules is what makes this file guess.

        Every check here keys `scoped_writes` on the bare class name, so two
        production dataclasses sharing one name is the single input that turns
        an exact attribution into an approximate one. Module-local resolution
        handles the construction sites; what it cannot handle is the *field*
        names, and it cannot handle a name imported into a third module where
        both definitions are candidates. So a collision is legal and declared,
        never legal and silent.

        Asserted in both directions, because an entry that outlives its
        collision is the same staleness the allowlist check above convicts: a
        reason still on the page for a condition that no longer holds.
        """
        prod, _test = _build()
        by_name: Dict[str, List[str]] = {}
        for module, cls in sorted(prod.module_field_order):
            by_name.setdefault(cls, []).append(module)
        colliding = {
            cls: mods for cls, mods in by_name.items() if len(set(mods)) > 1
        }

        undeclared = [
            f"{cls} is defined as a dataclass in {sorted(set(mods))}; every "
            "write to either is attributed to the same bare-name key"
            for cls, mods in sorted(colliding.items())
            if cls not in SHARED_DATACLASS_NAMES
        ]
        self.assertEqual(
            [],
            undeclared,
            "a production dataclass name defined in more than one module, "
            "with no declaration. Rename one, or add it to "
            "SHARED_DATACLASS_NAMES with the two field lists and the reason "
            "cross-attribution between them is harmless.\n"
            + "\n".join(undeclared),
        )

        stale = [
            f"{cls} is declared in SHARED_DATACLASS_NAMES but is defined in "
            f"{sorted(set(by_name.get(cls, [])))}"
            for cls in sorted(SHARED_DATACLASS_NAMES)
            if cls not in colliding
        ]
        self.assertEqual(
            [],
            stale,
            "a SHARED_DATACLASS_NAMES entry whose collision is gone. The "
            "reason it carries is now describing code that does not exist; "
            "delete the entry.\n" + "\n".join(stale),
        )

        thin = [
            cls
            for cls, reason in SHARED_DATACLASS_NAMES.items()
            if len(reason.strip()) < 12
        ]
        self.assertEqual([], thin, f"declarations with no usable reason: {thin}")

    def test_every_allowlist_entry_carries_a_reason(self) -> None:
        """An entry with an empty or placeholder reason is a suppression
        wearing the allowlist's clothes, which is the failure this file exists
        to prevent one level up."""
        bad = [
            key
            for key, reason in ALLOWED.items()
            if len(reason.strip()) < 12
            or reason.strip().upper() in {"TODO", "FIXME", "N/A", "UNKNOWN"}
        ]
        self.assertEqual([], bad, f"allowlist entries with no usable reason: {bad}")


if __name__ == "__main__":
    unittest.main()
