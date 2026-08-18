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
  member constructed only on a path that cannot be taken, reads as live.

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

Run:  uv run adw_test.py -k dead_seams
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ADWS = Path(__file__).resolve().parent.parent

#: Symbols with no production writer or caller, each with the reason it is
#: legal. `DEFERRED:` marks a known defect with an owner and an audit row, not
#: an accepted state — deleting the line is part of landing the fix.
ALLOWED: Dict[str, str] = {

    # ── §1.2's detectors ────────────────────────────────────────────────────
    # Their job is to convict a planted violation from a test. Production never
    # calls a detector; a detector production called would be a linter.
    "verification.free_text_reads": "detector, asserted from tests only",
    "retry_policy.find_output_content_comparisons": "detector: convicts a classifier that compares against process output text",
    "retry_policy.find_ungated_git_absence": "detector: convicts a git-absence conclusion drawn without a gate",
    "finalization.find_forbidden_report_fields": "detector: convicts verdict or severity appearing in a reviewer report",
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
    "scheduler_types.exits_for": "§11.3 property table: every block_reason "
                                 "admits an exit, proven from tests",
    "scheduler_types.pane_limit": "§7.2 identity — concurrency IS the pane "
                                  "limit; production enforces it through "
                                  "config.concurrency (scheduler.py:460, :503) "
                                  "and the test asserts the two agree",
    "finalization.graded_cells": "derived view of the matrix, asserted in tests",
    "finalization_window.opened_at_monotonic": "accessor over the private "
                                               "_opened_at_monotonic that "
                                               "production maintains",
    "participant.active_participant_ids": "accessor over the runner's live "
                                          "process map, asserted in tests",
    "scheduler.integration_untested": "derived flag on RunReport, asserted "
                                      "in tests",
    "verification.asserts_repository_wide": "derived predicate over a gate "
                                            "spec, asserted in tests",
    "worktree.is_empty": "derived predicate over InventoryDelta",

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

    # D10. Does not reach the scheduler from maestro.py's SchedulerDeps
    # construction. (D11 stood here too and is fixed: a gate's threshold now
    # travels on `PlanNode.gate_min_cases`, the one integration gate's on
    # `SchedulerDeps.integration_min_cases`, and the per-run scalar this
    # registry named was deleted rather than wired.)

    # D3 / D4 / D5. retry_policy.py, under investigation; do not delete.
    "retry_policy.semantic_budget_exhausted": "DEFERRED D3: §7.5's ceiling, "
                                              "duplicated by the scheduler's "
                                              "inline check and off by one "
                                              "from it. Which is correct is "
                                              "an open ruling.",
    "retry_policy.review_budget_exhausted": "DEFERRED D3: same shape as the "
                                            "semantic ceiling above.",
    "retry_policy.semantic_attempts_at_base": "DEFERRED D4: §7.5's "
                                              "(node_id, base_sha) "
                                              "prompt-mutation scope. Possibly "
                                              "a missing half of the rule "
                                              "rather than a duplicate.",
    "scheduler_types.mutates_prompt": "DEFERRED D4: the other half of the same "
                                      "§7.5 rule; held pending D4's ruling.",
    "retry_policy.classify_with_containment": "DEFERRED D5: §7.5's "
                                              "fail-closed-to-ENVIRONMENTAL "
                                              "containment wrapper. The "
                                              "scheduler calls bare classify.",
    "retry_policy.classify_git_exit": "DEFERRED D9: merge-path git exit "
                                      "classification, no production caller.",

    # D1 / D2 / D7 / D8 / D9.
    "worktree.check_pre_merge": "DEFERRED D1: §8.3 evaluates cleanliness twice "
                                "with two consequences; production runs only "
                                "the convicting one, so adapter residue is "
                                "never reported.",
    "lifecycle.record_result": "DEFERRED D7: §7.7's results table has no "
                               "production writer, yet run status renders "
                               "from it.",
    "lifecycle.audit_results": "DEFERRED D7: reader over the same writerless "
                               "results table.",
    "verification.adjudicate_result": "DEFERRED D7: §7.7's adjudication, never "
                                      "called in production.",
    "scheduler_types.is_retryable": "DEFERRED D8: production decides "
                                    "retryability from RetryClass, never from "
                                    "BlockReason.",
    "worktree.remove_attempt_worktree": "DEFERRED D9: unconfirmed — §8.8 "
                                        "retains BLOCKED nodes' worktrees "
                                        "deliberately; what removes the rest "
                                        "is not yet established.",
    "worktree.with_state": "DEFERRED D9: superseded NodeRecord constructor.",
    "scheduler_types.to_record": "DEFERRED D9: superseded PlanNode converter.",
    "route_receipts.load_public_key": "DEFERRED D9: production derives keys via "
                                      "crypto.seed_to_public_key instead.",

    # Audited instances of this same class that sit on the deliver, workspace,
    # or diagnostics paths rather than on `run start`. Real, recorded, and
    # deferred to a pass that owns those paths.
    "coordinator_store.audit_transitions": "DEFERRED: workspace-path audit "
                                           "reader, no production caller",
    "coordinator_store.list_runs": "DEFERRED: workspace-path query, no "
                                   "production caller",
    "lifecycle.audit_transitions": "DEFERRED: diagnostics reader over the "
                                   "transitions table, no production caller",
    "finalization_window.report_launched": "DEFERRED: finalization-path, no "
                                           "production caller",
    "finalization_window.require_fresh_session_dir": "DEFERRED: "
                                                     "finalization-path, no "
                                                     "production caller",
    "publication.prepare": "DEFERRED: WorkspacePublisher.prepare, publication "
                           "path, no production caller",

    # Config keys whose only writer is a test, so the dataclass default is the
    # production value in every run. Not broken branches — constants wearing a
    # config key's clothes — but recorded so that "configurable" is not
    # mistaken for "configured".
    "ValidationConfig.review_payload_budget_bytes": "default is the production "
                                                    "value; tests vary it to "
                                                    "exercise the budget",

    # The launcher-classification lane's typed failure signal. Its writers land
    # with that lane; `launcher_failure` itself is already wired.
    "FailureSignal.binary_resolved": "DEFERRED: launcher-classification lane",
    "FailureSignal.process_started": "DEFERRED: launcher-classification lane",
    "FailureSignal.code_effect": "DEFERRED: launcher-classification lane",
    "CodeEffect.exit_zero": "DEFERRED: launcher-classification lane",
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
        name = (target.attr if isinstance(target, ast.Attribute)
                else getattr(target, "id", ""))
        if name == "dataclass":
            return True
    return False


def _callee_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


class _Index:
    """Definitions and usages over one file set."""

    def __init__(self) -> None:
        self.fields: Dict[str, Dict[str, str]] = {}
        self.field_order: Dict[str, List[str]] = {}
        self.callables: Dict[str, str] = {}
        self.reads: Dict[str, Set[str]] = {}
        self.writes: Dict[str, Set[str]] = {}
        self.scoped_writes: Dict[Tuple[str, str], Set[str]] = {}
        self.splatted: Set[str] = set()
        self.refs: Dict[str, Set[str]] = {}

    def define(self, path: Path, tree: ast.Module) -> None:
        module = path.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_dataclass(node):
                fields = self.fields.setdefault(node.name, {})
                order: List[str] = []
                for stmt in node.body:
                    if not (isinstance(stmt, ast.AnnAssign)
                            and isinstance(stmt.target, ast.Name)):
                        continue
                    order.append(stmt.target.id)
                    if _has_default_factory(stmt):
                        continue
                    fields[stmt.target.id] = f"{path.name}:{stmt.lineno}"
                self.field_order[node.name] = order
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.callables.setdefault(f"{module}.{node.name}",
                                          f"{path.name}:{node.lineno}")

    def _record_construction(self, owner: str, call: ast.Call, loc: str) -> None:
        order = self.field_order.get(owner)
        if order is None:
            return
        for field, _arg in zip(order, call.args):
            self.scoped_writes.setdefault((owner, field), set()).add(loc)
        for kw in call.keywords:
            if kw.arg:
                self.scoped_writes.setdefault((owner, kw.arg), set()).add(loc)
            else:
                # `Cls(**payload)` writes an unknowable set of fields.
                self.splatted.add(owner)

    def use(self, path: Path, tree: ast.Module) -> None:
        # A classmethod builds its own class as `cls(...)`. Resolve that first,
        # or every field a `parse`/`from_row` constructor writes reads as
        # unwritten — which is most of GateCounts.
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.ClassDef):
                continue
            if outer.name not in self.field_order:
                continue
            for inner in ast.walk(outer):
                if isinstance(inner, ast.Call) and _callee_name(inner) == "cls":
                    self._record_construction(
                        outer.name, inner, f"{path.name}:{inner.lineno}")

        for node in ast.walk(tree):
            loc = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Attribute):
                bucket = (self.writes if isinstance(node.ctx, ast.Store)
                          else self.reads)
                bucket.setdefault(node.attr, set()).add(loc)
                self.refs.setdefault(node.attr, set()).add(loc)
            elif isinstance(node, ast.Name):
                self.refs.setdefault(node.id, set()).add(loc)
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name in self.field_order:
                    # Attribute the write to *that class's* field, never to
                    # every field of that name anywhere: `min_cases=` on a plan
                    # gate is not a write of `SchedulerDeps.min_cases`, and
                    # treating it as one is exactly how an unwired field hides
                    # behind a namesake in another class.
                    self._record_construction(name or "", node, loc)
                else:
                    for kw in node.keywords:
                        if kw.arg:
                            self.writes.setdefault(kw.arg, set()).add(loc)


def _build() -> Tuple[_Index, _Index]:
    prod, test = _Index(), _Index()
    prod_trees = [(p, ast.parse(p.read_text(), filename=str(p)))
                  for p in _production_files()]
    test_trees = [(p, ast.parse(p.read_text(), filename=str(p)))
                  for p in _test_files()]
    for path, tree in prod_trees:
        prod.define(path, tree)
    for path, tree in prod_trees:
        prod.use(path, tree)
    # Tests reuse production definitions; only their usages differ.
    test.fields, test.field_order = prod.fields, prod.field_order
    for path, tree in test_trees:
        test.use(path, tree)
    return prod, test


class DeadSeamTest(unittest.TestCase):

    def test_the_tree_was_actually_found(self) -> None:
        """A mislocated or empty ADWS makes every check below pass vacuously,
        which is the one way this file could lie in the reassuring direction."""
        production = _production_files()
        self.assertGreater(len(production), 40,
                           f"only {len(production)} production files under "
                           f"{ADWS} — the checks below would pass vacuously")
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
                    continue      # unread is B15's problem, not this one
                if cls in prod.splatted:
                    continue      # `Cls(**payload)` writes we cannot see
                if prod.scoped_writes.get((cls, field)):
                    continue
                if prod.writes.get(field):
                    continue      # written through a callee we could not resolve
                test_writes = (test.scoped_writes.get((cls, field), set())
                               | test.writes.get(field, set()))
                offenders.append(
                    f"{key} ({where}) is read in production at "
                    f"{sorted(prod.reads[field])[:3]} and written nowhere in "
                    f"production; {len(test_writes)} test writes")
        self.assertEqual(
            [], offenders,
            "a production reader with no production writer: the branch it "
            "guards can never be taken, so the behaviour behind it silently "
            "never happens. Wire a writer, delete the field and its branch "
            "together, or add it to ALLOWED with the reason it has none.\n"
            + "\n".join(offenders))

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
                continue      # referenced nowhere at all is a different check
            offenders.append(
                f"{qualified} ({where}) has no production reference; "
                f"{len(test.refs[name])} test references")
        self.assertEqual(
            [], offenders,
            "a callable exercised only by tests. Either production should call "
            "it, or it is dead and should go with its tests, or it is "
            "deliberately test-only and belongs in ALLOWED with a reason.\n"
            + "\n".join(offenders))

    def test_every_allowlist_entry_carries_a_reason(self) -> None:
        """An entry with an empty or placeholder reason is a suppression
        wearing the allowlist's clothes, which is the failure this file exists
        to prevent one level up."""
        bad = [key for key, reason in ALLOWED.items()
               if len(reason.strip()) < 12 or reason.strip().upper() in
               {"TODO", "FIXME", "N/A", "UNKNOWN"}]
        self.assertEqual([], bad,
                         f"allowlist entries with no usable reason: {bad}")


if __name__ == "__main__":
    unittest.main()
