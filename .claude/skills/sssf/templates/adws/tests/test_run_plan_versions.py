"""Retained plan bytes, the amendment lineage, and the guard on `dag_nodes`.

The dead end: a run identifies its plan by the digest of a *mutable file*, so
`plan ship` amending that file changes its digest and `_resume_run_selection`
refuses the run forever — *"the plan file has changed since the run started."*
On 2026-08-27 the only escape was abandoning a run holding 9 MERGED and 4
ACCEPTED nodes to correct one lane's unsatisfiable gate.

The plan is the only artifact a run depends on that was referenced by content
and not *stored* by content: `candidate_sha`, `output_sha`, `base_sha` and
`accepted_test_sha` are git objects, `review_digest` resolves into the receipt
store at `{digest}.json` — which is exactly why a finalization receipt survives
an edit to the plan it finalised — and `runs.plan_digest` alone resolved through
a file path. Retaining the bytes makes the resume refusal *unnecessary* rather
than relaxed: resume reads the run's own record instead of searching mutable
files, so it still cannot be handed a different plan by accident.

**The class this file mainly exists to guard is §19 M42's.** An amendment has to
rewrite `dag_nodes` rows, and M42 is a projection that rewired `needs_json` on a
run in flight, reopening dependency decisions for nodes already terminal. The
guard here is structural rather than conventional and is asserted twice: by
behaviour, and by a source sweep proving no amendment statement names the column
at all. A guard that depends on a caller having run the classifier first is a
convention; one the statement cannot express is a property.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_lifecycle import make_node, new_store  # noqa: E402

RUN_ID = "run-versions"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def _agent(node_id, depth=0, needs=(), min_cases=2):
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=depth,
        needs=tuple(needs),
        outputs=("src/{0}.py".format(node_id),),
        gate_command=("pytest", "tests/test_{0}.py".format(node_id)),
        gate_selector="tests/test_{0}.py".format(node_id),
        gate_min_cases=min_cases,
        instruction="do the work",
    )


def _run(tmp, node_ids=("a",)):
    store = new_store(Path(tmp))
    store.create_run(RUN_ID, DIGEST_A, [make_node(n, 0) for n in node_ids])
    return store


def _needs_json(store, node_id):
    return store.conn.execute(
        "SELECT needs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
        (RUN_ID, node_id),
    ).fetchone()[0]


def _outputs_json(store, node_id):
    return store.conn.execute(
        "SELECT outputs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
        (RUN_ID, node_id),
    ).fetchone()[0]


class ThePlanBytesAreRetained(unittest.TestCase):
    def test_recorded_bytes_come_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp)
            self.addCleanup(store.close)
            store.record_plan_version(RUN_ID, DIGEST_A, b'{"plan": 1}')

            self.assertEqual(store.current_plan(RUN_ID), (DIGEST_A, b'{"plan": 1}'))
            self.assertEqual(
                [(s, d) for s, d, _ in store.plan_versions(RUN_ID)], [(1, DIGEST_A)]
            )

    def test_re_recording_the_head_is_free(self):
        """A resume re-asserting what it already runs must not append."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp)
            self.addCleanup(store.close)
            first = store.record_plan_version(RUN_ID, DIGEST_A, b"x")

            again = store.record_plan_version(RUN_ID, DIGEST_A, b"x")

            self.assertEqual(first, again)
            self.assertEqual(len(store.plan_versions(RUN_ID)), 1)

    def test_readopting_an_earlier_version_is_refused(self):
        """The lineage is a history, not a set."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp)
            self.addCleanup(store.close)
            store.record_plan_version(RUN_ID, DIGEST_A, b"x")
            store.record_plan_version(RUN_ID, DIGEST_B, b"y")

            with self.assertRaises(lc.LifecycleError) as caught:
                store.record_plan_version(RUN_ID, DIGEST_A, b"x")
            self.assertIn("history, not a set", str(caught.exception))

    def test_a_run_predating_retention_answers_none_rather_than_erroring(self):
        """Absence is a legacy run, not a fault — it keeps the old resolution."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp)
            self.addCleanup(store.close)
            self.assertIsNone(store.current_plan(RUN_ID))

    def test_empty_bytes_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp)
            self.addCleanup(store.close)
            with self.assertRaises(lc.LifecycleError):
                store.record_plan_version(RUN_ID, DIGEST_A, b"")


class SettledRowsCannotBeRewritten(unittest.TestCase):
    """The decision point: can the `dag_nodes` write be made safe?"""

    def _merged(self, store, node_id):
        store.start_attempt(RUN_ID, node_id, base_sha="s1")
        store.mark_verified(RUN_ID, node_id, output_sha="a" * 40)
        store.mark_merged(RUN_ID, node_id)

    def test_amending_a_merged_node_is_refused_by_the_statement_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("merged", "pending"))
            self.addCleanup(store.close)
            self._merged(store, "merged")
            before = _outputs_json(store, "merged")

            with self.assertRaises(lc.IllegalTransition) as caught:
                store.amend_run_plan(
                    RUN_ID, DIGEST_B, b"y",
                    {"merged": replace(_agent("merged"), outputs=("src/other.py",))},
                )

            self.assertIn("MERGED", str(caught.exception))
            self.assertEqual(
                _outputs_json(store, "merged"), before, "the row must be untouched"
            )

    def test_amending_a_running_node_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("running",))
            self.addCleanup(store.close)
            store.start_attempt(RUN_ID, "running", base_sha="s1")

            with self.assertRaises(lc.IllegalTransition):
                store.amend_run_plan(RUN_ID, DIGEST_B, b"y", {"running": _agent("running")})

    def test_amending_an_absent_node_is_refused_rather_than_a_silent_no_op(self):
        """A statement matching nothing must refuse, not quietly succeed."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("a",))
            self.addCleanup(store.close)

            with self.assertRaises(lc.IllegalTransition):
                store.amend_run_plan(RUN_ID, DIGEST_B, b"y", {"ghost": _agent("ghost")})

    def test_the_refusal_leaves_no_plan_version_behind(self):
        """A refused amendment is not half-adopted."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("merged",))
            self.addCleanup(store.close)
            self._merged(store, "merged")

            with self.assertRaises(lc.IllegalTransition):
                store.amend_run_plan(RUN_ID, DIGEST_B, b"y", {"merged": _agent("merged")})

            self.assertEqual(store.plan_versions(RUN_ID), ())
            self.assertIsNone(store.current_plan(RUN_ID))


class TheAmendmentTheRunNeeded(unittest.TestCase):
    """9 merged nodes stand; one blocked lane's gate is corrected."""

    def test_a_pending_node_is_rewritten_and_merged_rows_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("merged-a", "merged-b", "blocked"))
            self.addCleanup(store.close)
            for node_id in ("merged-a", "merged-b"):
                store.start_attempt(RUN_ID, node_id, base_sha="s1")
                store.mark_verified(RUN_ID, node_id, output_sha="a" * 40)
                store.mark_merged(RUN_ID, node_id)
            merged_before = {
                n: (_outputs_json(store, n), _needs_json(store, n))
                for n in ("merged-a", "merged-b")
            }

            seq = store.amend_run_plan(
                RUN_ID,
                DIGEST_B,
                b'{"amended": true}',
                {"blocked": replace(_agent("blocked"), outputs=("src/fixed.py",))},
            )

            self.assertEqual(seq, 1)
            self.assertEqual(store.current_plan(RUN_ID)[0], DIGEST_B)
            self.assertEqual(
                json.loads(_outputs_json(store, "blocked")), ["src/fixed.py"]
            )
            for node_id, before in merged_before.items():
                with self.subTest(node_id=node_id):
                    self.assertEqual(
                        (_outputs_json(store, node_id), _needs_json(store, node_id)),
                        before,
                        "merged evidence must survive the amendment intact",
                    )

    def test_the_amendment_never_touches_an_existing_nodes_needs(self):
        """§19 M42's column, asserted by behaviour as well as by the sweep."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("a", "b"))
            self.addCleanup(store.close)
            store.conn.execute(
                "UPDATE dag_nodes SET needs_json=? WHERE run_id=? AND node_id=?",
                (json.dumps(["a"]), RUN_ID, "b"),
            )
            before = _needs_json(store, "b")

            store.amend_run_plan(
                RUN_ID, DIGEST_B, b"y",
                {"b": replace(_agent("b"), needs=("something-else",))},
            )

            self.assertEqual(
                _needs_json(store, "b"),
                before,
                "an amendment must not be able to rewire the graph",
            )

    def test_a_new_node_is_inserted_pending_and_may_carry_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("a",))
            self.addCleanup(store.close)

            store.amend_run_plan(
                RUN_ID, DIGEST_B, b"y", {}, [_agent("new", depth=1, needs=("a",))]
            )

            self.assertIs(
                store.get_node(RUN_ID, "new").state, st.NodeState.PENDING
            )
            self.assertEqual(json.loads(_needs_json(store, "new")), ["a"])

    def test_the_amendment_is_a_typed_transition(self):
        """§1.2 — the record is a row, not prose in a log."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("a",))
            self.addCleanup(store.close)

            store.amend_run_plan(RUN_ID, DIGEST_B, b"y", {"a": _agent("a")})

            row = store.conn.execute(
                "SELECT reason, actor, detail_json FROM transitions"
                " WHERE run_id=? ORDER BY id DESC LIMIT 1",
                (RUN_ID,),
            ).fetchone()
            self.assertEqual(row[0], "plan-amended")
            self.assertEqual(row[1], "operator")
            detail = json.loads(row[2])
            self.assertEqual(detail["plan_digest"], DIGEST_B)
            self.assertEqual(detail["updated"], ["a"])


class ReleasingAFinishedPath(unittest.TestCase):
    """The one statement that touches a settled row, and its narrowness.

    It sets one column, removes exactly one value from that column's JSON
    array, and matches only an agent-kind row in a settled state. Everything
    unsound about a settled-node outputs change is unreachable from it rather
    than refused by it.
    """

    def _settle(self, store, node_id):
        store.start_attempt(RUN_ID, node_id, base_sha="s1")
        store.mark_verified(RUN_ID, node_id, output_sha="a" * 40)
        store.mark_merged(RUN_ID, node_id)

    def test_a_merged_agent_node_releases_exactly_the_named_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("donor", "live"))
            self.addCleanup(store.close)
            # `make_node` builds code nodes; the release is agent-only, so the
            # donor is made one explicitly rather than relying on a default.
            store.conn.execute(
                "UPDATE dag_nodes SET kind=?, outputs_json=?"
                " WHERE run_id=? AND node_id=?",
                (
                    st.NodeKind.AGENT.value,
                    json.dumps(["src/keep.py", "src/move.py"]),
                    RUN_ID,
                    "donor",
                ),
            )
            self._settle(store, "donor")

            store.amend_run_plan(
                RUN_ID, DIGEST_B, b"y", {},
                transfers=[("src/move.py", "donor", "live")],
            )

            self.assertEqual(
                json.loads(_outputs_json(store, "donor")),
                ["src/keep.py"],
                "exactly the released path leaves; the rest stay",
            )

    def test_a_merged_tests_node_may_not_release(self):
        """Its outputs are still read by `compare_test_bytes` after the merge."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("tests-lane", "live"))
            self.addCleanup(store.close)
            store.conn.execute(
                "UPDATE dag_nodes SET kind=?, outputs_json=?"
                " WHERE run_id=? AND node_id=?",
                (
                    st.NodeKind.TESTS.value,
                    json.dumps(["tests/test_x.py"]),
                    RUN_ID,
                    "tests-lane",
                ),
            )
            self._settle(store, "tests-lane")

            with self.assertRaises(lc.IllegalTransition) as caught:
                store.amend_run_plan(
                    RUN_ID, DIGEST_B, b"y", {},
                    transfers=[("tests/test_x.py", "tests-lane", "live")],
                )
            self.assertIn("not a spent permission", str(caught.exception))

    def test_an_unsettled_donor_may_not_release_through_this_path(self):
        """A live node's outputs change through the ordinary update, not here.

        Keeping the release statement settled-only means it can never become a
        general-purpose outputs writer that happens to allow settled rows.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("pending", "live"))
            self.addCleanup(store.close)

            with self.assertRaises(lc.IllegalTransition):
                store.amend_run_plan(
                    RUN_ID, DIGEST_B, b"y", {},
                    transfers=[("src/pending.py", "pending", "live")],
                )

    def test_the_release_is_recorded_in_the_typed_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _run(tmp, ("donor", "live"))
            self.addCleanup(store.close)
            store.conn.execute(
                "UPDATE dag_nodes SET kind=?, outputs_json=?"
                " WHERE run_id=? AND node_id=?",
                (
                    st.NodeKind.AGENT.value,
                    json.dumps(["src/move.py"]),
                    RUN_ID,
                    "donor",
                ),
            )
            self._settle(store, "donor")

            store.amend_run_plan(
                RUN_ID, DIGEST_B, b"y", {},
                transfers=[("src/move.py", "donor", "live")],
            )

            detail = json.loads(
                store.conn.execute(
                    "SELECT detail_json FROM transitions WHERE run_id=?"
                    " ORDER BY id DESC LIMIT 1",
                    (RUN_ID,),
                ).fetchone()[0]
            )
            self.assertEqual(detail["transfers"], [["src/move.py", "donor", "live"]])


class ResumeSurvivesAPlanShip(unittest.TestCase):
    """The dead end itself, through the real resolver.

    `plan ship` correcting a defect rewrites the installed file, changing its
    digest, and `_resolve_resume_target` then matched no installed plan and
    refused the run forever. The pair below is the whole point: the control
    reproduces that refusal, and the regression shows retained bytes removing
    it — the *same* resolver, the *same* overwritten file, one difference.
    """

    def _layout(self, root, plan_bytes):
        plans = root / "plans" / "epa"
        plans.mkdir(parents=True)
        (plans / "maestro-plan.v1").write_bytes(plan_bytes)
        return {
            "plans_dir": root / "plans",
            "database": root / "lifecycle.db",
            "data_dir": root / "state",
        }

    def _args(self, run_id):
        import argparse

        return argparse.Namespace(run_id=run_id, selector=run_id, digest=None)

    def test_without_retained_bytes_a_shipped_plan_strands_the_run(self):
        """The control. This is the behaviour that cost 9 MERGED nodes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b'{"schema_version": "maestro-plan.v1", "v": 1}'
            config = self._layout(root, original)
            store = lc.LifecycleStore(config["database"])
            digest = maestro.plan_digest.digest_of(original)
            store.create_run(RUN_ID, digest, [make_node("a", 0)])
            store.close()

            (config["plans_dir"] / "epa" / "maestro-plan.v1").write_bytes(
                b'{"schema_version": "maestro-plan.v1", "v": 2}'
            )

            with self.assertRaises(maestro._RunSelectionError) as caught:
                maestro._resolve_resume_target(self._args(RUN_ID), config)
            self.assertIn("plan file has changed", str(caught.exception))

    def test_with_retained_bytes_the_same_ship_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b'{"schema_version": "maestro-plan.v1", "v": 1}'
            config = self._layout(root, original)
            store = lc.LifecycleStore(config["database"])
            digest = maestro.plan_digest.digest_of(original)
            store.create_run(RUN_ID, digest, [make_node("a", 0)])
            store.record_plan_version(RUN_ID, digest, original)
            store.close()

            (config["plans_dir"] / "epa" / "maestro-plan.v1").write_bytes(
                b'{"schema_version": "maestro-plan.v1", "v": 2}'
            )

            run_id, plan_file = maestro._resolve_resume_target(
                self._args(RUN_ID), config
            )

            self.assertEqual(run_id, RUN_ID)
            self.assertEqual(plan_file.read_bytes(), original)
            self.assertEqual(
                maestro.plan_digest.digest_of(plan_file.read_bytes()),
                digest,
                "resume must execute the bytes the run adopted, not the file",
            )

    def test_the_materialised_copy_is_rebuilt_when_it_does_not_match(self):
        """It is a rendering of the durable record, never the record."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = b'{"schema_version": "maestro-plan.v1", "v": 1}'
            config = self._layout(root, original)
            store = lc.LifecycleStore(config["database"])
            digest = maestro.plan_digest.digest_of(original)
            store.create_run(RUN_ID, digest, [make_node("a", 0)])
            store.record_plan_version(RUN_ID, digest, original)
            store.close()

            _run_id, plan_file = maestro._resolve_resume_target(
                self._args(RUN_ID), config
            )
            plan_file.write_bytes(b"corrupted")

            _again, rebuilt = maestro._resolve_resume_target(
                self._args(RUN_ID), config
            )

            self.assertEqual(rebuilt.read_bytes(), original)


class TheVerbsDecisionCoreIsReachable(unittest.TestCase):
    """`run amend`'s body, exercised without the plan apparatus around it.

    The first version of the verb read `args.database`, which nothing binds,
    and would have raised `AttributeError` the first time anyone ran it.
    Nothing caught that because nothing could reach the body — doing so meant
    standing up plan files, receipts and a configured installation. So the
    decision was split out, and these run it against a real store.
    """

    def _store(self, tmp, node_ids=("merged", "pending")):
        store = _run(tmp, node_ids)
        store.record_plan_version(RUN_ID, DIGEST_A, b'{"v": 1}')
        return store

    def _states(self, store, node_ids):
        return {n: store.get_node(RUN_ID, n).state for n in node_ids}

    def test_a_safe_amendment_is_applied_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            db = store.conn.execute("PRAGMA database_list").fetchone()[2]
            store.start_attempt(RUN_ID, "merged", base_sha="s1")
            store.mark_verified(RUN_ID, "merged", output_sha="a" * 40)
            store.mark_merged(RUN_ID, "merged")
            states = self._states(store, ("merged", "pending"))
            current = [_agent("merged"), _agent("pending")]
            amended = [
                _agent("merged"),
                replace(_agent("pending"), gate_min_cases=2, outputs=("src/fix.py",)),
            ]
            store.close()

            code, payload = maestro._adjudicate_amendment(
                db, RUN_ID, states, current, amended, DIGEST_B, b'{"v": 2}'
            )

            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "RUN_PLAN_AMENDED")
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["changed"], ["pending"])

            reopened = lc.LifecycleStore(Path(db))
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.current_plan(RUN_ID)[0], DIGEST_B)
            self.assertEqual(
                json.loads(_outputs_json(reopened, "pending")), ["src/fix.py"]
            )

    def test_a_refused_amendment_returns_three_and_names_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            db = store.conn.execute("PRAGMA database_list").fetchone()[2]
            store.start_attempt(RUN_ID, "merged", base_sha="s1")
            store.mark_verified(RUN_ID, "merged", output_sha="a" * 40)
            store.mark_merged(RUN_ID, "merged")
            states = self._states(store, ("merged", "pending"))
            current = [_agent("merged"), _agent("pending")]
            amended = [
                replace(_agent("merged"), instruction="retconned"),
                _agent("pending"),
            ]
            store.close()

            code, payload = maestro._adjudicate_amendment(
                db, RUN_ID, states, current, amended, DIGEST_B, b'{"v": 2}'
            )

            self.assertEqual(code, 3)
            self.assertEqual(payload["outcome"], "RUN_PLAN_AMENDMENT_REFUSED")
            self.assertEqual(
                [f["code"] for f in payload["refusals"]],
                ["AMEND_SETTLED_NODE_CHANGED"],
            )

            reopened = lc.LifecycleStore(Path(db))
            self.addCleanup(reopened.close)
            self.assertEqual(
                reopened.current_plan(RUN_ID)[0],
                DIGEST_A,
                "a refused amendment must not be adopted",
            )


class TheGuardIsStructuralNotConventional(unittest.TestCase):
    """A reader-without-writer sweep over the amendment statement itself.

    Behaviour tests prove the guard fires for the cases they exercise. This
    proves the *column is unreachable*, which is a different and stronger
    claim: §19 M42 happened because a projection could write `needs_json`, and
    a guard that merely refuses to is one refactor away from not refusing.
    """

    def test_no_amendment_statement_names_needs_json(self):
        import ast

        source = (ADWS / "adw_modules" / "lifecycle.py").read_text()
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "amend_run_plan":
                target = node
        self.assertIsNotNone(
            target, "amend_run_plan is gone; this guard has stopped guarding"
        )
        # The docstring explains the rule and therefore names the column; it is
        # prose, not a statement, so it is excluded rather than allowed to
        # convict the very method it documents.
        docstring = ast.get_docstring(target, clean=False)
        literals = [
            n.value
            for n in ast.walk(target)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value != docstring
        ]
        offenders = [text for text in literals if "needs_json" in text]
        updates = [
            text
            for text in literals
            if "UPDATE dag_nodes" in text and "needs_json" in text
        ]
        self.assertEqual(
            updates, [], "an amendment must never UPDATE needs_json (§19 M42)"
        )
        # An INSERT for a *new* node may name it: nothing was admitted in its
        # absence, so it reopens no decision.
        for text in offenders:
            self.assertIn(
                "INSERT INTO dag_nodes",
                text,
                "needs_json may appear only in the new-node INSERT",
            )


if __name__ == "__main__":
    unittest.main()
