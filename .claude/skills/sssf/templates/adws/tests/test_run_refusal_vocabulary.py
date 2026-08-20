"""A run verb's `outcome` is a vocabulary, never a Python class name.

`_run_start` and `_run_resume` both ended in

    return _refusal(type(exc).__name__.upper(), str(exc))

so the operator-visible `outcome` field was whatever class the implementation
happened to raise: `FILENOTFOUNDERROR`, `VALUEERROR`, `OSERROR`,
`LIFECYCLEERROR`. None of those is declared anywhere, none can be branched on,
and each changes when an implementation detail changes what it raises. §1.2's
rule is about lifecycle transitions, but the same reasoning governs the refusal
surface: a fact a caller must act on travels as a typed field, not as prose,
and a name nobody declared is not typed.

Three things this file settles.

  the class     -- the `__name__.upper()` shape had four operator-visible
                   instances: `_run_start`, `_run_resume`, and `main`'s two
                   last-chance arms. All four are gone, and this file convicts
                   any return of the shape at each of them.
  the instance  -- `run start` against a digest with no receipt is
                   `RUN_RECEIPT_ABSENT`, and it says which of the two causes
                   fired as a typed field, because a set-aside digest
                   (§3.6 B10, §19 M16) and a never-finalized one need
                   different things done about them.
  the smuggling -- `RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE` and
                   `RUN_RECEIPT_NOT_PASS` already were declared names, carried
                   through `ValueError` messages, so they arrived as `detail`
                   prose under an `outcome` of `VALUEERROR`. §19 M16 quotes
                   `RUN_RECEIPT_NOT_PASS` as the outcome; it was never printed
                   as one. It is now.

Real resources: a real `ReceiptStore` with real Ed25519 signing, the real
`finalize`, the real `set_aside`, and the real CLI handlers.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402

from adw_modules import finalization as fin  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402

from test_finalization import (  # noqa: E402
    DIGEST, clean_report, make_store, matrix_for)
from test_receipt_set_aside import (  # noqa: E402
    INVOKER, REASON, Review, failing_report, finalize)


def _emit(call):
    """One handler invocation, and the single JSON object it printed."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = call()
    return code, json.loads(output.getvalue())


class TheAbsentReceiptNamesItsCauseTest(unittest.TestCase):
    """`_receipt_absent` against a real store, both causes."""

    def test_a_never_finalized_digest_is_named_as_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            refusal = maestro._receipt_absent(store, DIGEST)

        self.assertEqual("RUN_RECEIPT_ABSENT", refusal.outcome)
        self.assertEqual({"cause": "NEVER_FINALIZED", "set_aside_count": 0},
                         refusal.fields)

    def test_a_set_aside_digest_is_distinguished_from_it(self):
        """The store *can* tell them apart, so the refusal must. B10's escape
        frees the live slot deliberately — the absence is the admission — and
        an operator who reads `no receipt` there will re-ship a plan that only
        needed `plan finalize`."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            refusal = maestro._receipt_absent(store, DIGEST)

        self.assertEqual("RUN_RECEIPT_ABSENT", refusal.outcome)
        self.assertEqual({"cause": "SET_ASIDE", "set_aside_count": 1},
                         refusal.fields)

    def test_the_escape_count_is_the_number_of_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-1"))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-2"),
                     clock=lambda: 1_760_000_500.0)
            store.set_aside(DIGEST, invoked_by=INVOKER, reason="and again")

            refusal = maestro._receipt_absent(store, DIGEST)

        self.assertEqual(2, refusal.fields["set_aside_count"])

    def test_an_unreadable_escape_trail_does_not_invent_a_set_aside(self):
        """§19.2's defaulting failure, refused: a read that failed is not
        evidence of an escape. The receipt is absent either way, so the honest
        answer is the one that claims less."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            with mock.patch.object(fin.ReceiptStore, "set_aside_records",
                                   side_effect=OSError("store unreadable")):
                refusal = maestro._receipt_absent(store, DIGEST)

        self.assertEqual("NEVER_FINALIZED", refusal.fields["cause"])


class TheRunPathRefusalsAreNamedTest(unittest.TestCase):
    """`_load_runnable_plan`'s three refusals, through the real receipt store.

    Only `pv.validate_plan` is stubbed, and it is a different subject: these
    tests ask what the *receipt* arm says, so the plan validator is held
    eligible rather than reimplemented.
    """

    def _arguments(self, tmp, store, seed):
        plan_file = tmp / "plan.json"
        plan_file.write_bytes(b"{}\n")
        return argparse.Namespace(
            plan_file=str(plan_file), digest=DIGEST, repo=str(tmp / "repo"),
            receipt_dir=str(store.root), data_dir=str(tmp / "sssf-data"),
            verify_key=[rc.seed_to_public_key(seed).hex()], runners=None)

    @contextlib.contextmanager
    def _eligible(self, eligible=True, digest=DIGEST):
        validation = mock.Mock(eligible=eligible, digest=digest)
        with mock.patch.object(maestro.pv, "validate_plan",
                               return_value=validation), \
                mock.patch.object(maestro, "_plan_collector",
                                  return_value=None):
            yield

    def test_a_never_finalized_digest_refuses_run_receipt_absent(self):
        """The reported instance: this printed `FILENOTFOUNDERROR`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _repo, _data, seed = make_store(root)
            args = self._arguments(root, store, seed)
            with self._eligible():
                with self.assertRaises(maestro._RunRefused) as caught:
                    maestro._load_runnable_plan(args)

        self.assertEqual("RUN_RECEIPT_ABSENT", caught.exception.outcome)
        self.assertEqual("NEVER_FINALIZED", caught.exception.fields["cause"])

    def test_a_set_aside_digest_refuses_with_its_own_cause(self):
        """The reason the instance became common: `plan set-aside` sends a
        digest down this path deliberately."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _repo, _data, seed = make_store(root)
            finalize(store, Review(report=failing_report()))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            args = self._arguments(root, store, seed)
            with self._eligible():
                with self.assertRaises(maestro._RunRefused) as caught:
                    maestro._load_runnable_plan(args)

        self.assertEqual("RUN_RECEIPT_ABSENT", caught.exception.outcome)
        self.assertEqual("SET_ASIDE", caught.exception.fields["cause"])

    def test_a_fail_receipt_refuses_under_the_name_it_always_had(self):
        """§19 M16 quotes `RUN_RECEIPT_NOT_PASS` as the outcome. It was a
        `ValueError` message under an outcome of `VALUEERROR`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _repo, _data, seed = make_store(root)
            finalize(store, Review(report=failing_report()))
            args = self._arguments(root, store, seed)
            with self._eligible():
                with self.assertRaises(maestro._RunRefused) as caught:
                    maestro._load_runnable_plan(args)

        self.assertEqual("RUN_RECEIPT_NOT_PASS", caught.exception.outcome)
        self.assertEqual({}, caught.exception.fields)

    def test_an_ineligible_plan_refuses_under_the_name_it_always_had(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _repo, _data, seed = make_store(root)
            args = self._arguments(root, store, seed)
            with self._eligible(eligible=False):
                with self.assertRaises(maestro._RunRefused) as caught:
                    maestro._load_runnable_plan(args)

        self.assertEqual("RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE",
                         caught.exception.outcome)

    def test_a_pass_receipt_is_not_refused(self):
        """The positive control §13.4 requires beside every conviction: the
        refusals above must not be what this path always does."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _repo, _data, seed = make_store(root)
            finalize(store, Review(report=clean_report(matrix_for())))
            args = self._arguments(root, store, seed)
            with self._eligible(), mock.patch.object(
                    maestro.plan_model, "parse_bytes",
                    return_value="the-plan") as parsed:
                self.assertEqual("the-plan",
                                 maestro._load_runnable_plan(args))
        self.assertEqual(1, parsed.call_count)


class TheRunVerbsPrintTheTypedRefusalTest(unittest.TestCase):
    """The handler end: what an operator actually reads."""

    def _args(self):
        return argparse.Namespace(plan_file="plan.json", run_id="run-1",
                                  selector=None)

    def _drive(self, handler, exc):
        with mock.patch.object(maestro, "_execute_run", side_effect=exc):
            return _emit(lambda: handler(self._args()))

    def test_run_start_prints_the_outcome_and_its_typed_cause(self):
        code, payload = self._drive(
            maestro._run_start,
            maestro._RunRefused("RUN_RECEIPT_ABSENT", "no receipt",
                                cause="SET_ASIDE", set_aside_count=1))

        self.assertEqual(3, code)
        self.assertEqual("RUN_RECEIPT_ABSENT", payload["outcome"])
        self.assertEqual("SET_ASIDE", payload["cause"])
        self.assertEqual(1, payload["set_aside_count"])

    def test_run_resume_prints_it_too(self):
        """The two handlers were byte-identical and must not drift."""
        code, payload = self._drive(
            maestro._run_resume,
            maestro._RunRefused("RUN_RECEIPT_ABSENT", "no receipt",
                                cause="NEVER_FINALIZED", set_aside_count=0))

        self.assertEqual(3, code)
        self.assertEqual("RUN_RECEIPT_ABSENT", payload["outcome"])
        self.assertEqual("NEVER_FINALIZED", payload["cause"])

    def test_the_cross_run_node_budget_prints_its_nodes_as_a_field(self):
        """The newest name in the vocabulary, and the shape it travels in.

        An operator tool deciding which nodes to re-author has to read the
        node ids, the cumulative counts and the runs that hold them. All three
        are a list of typed records, because the refusal is about a *set* of
        nodes and a sentence naming them would put the branching fact back
        into prose (§1.2).
        """
        code, payload = self._drive(
            maestro._run_start,
            maestro._RunRefused(
                "NODE_BUDGET_EXHAUSTED_ACROSS_RUNS",
                "lane-x has spent its review budget",
                plan_digest="c" * 64, semantic_ceiling=3,
                nodes=[{"node_id": "lane-x",
                        "cumulative_semantic_attempts": 7,
                        "effective_ceiling": 3,
                        "run_ids": ["run-a", "run-b"]}]))

        self.assertEqual(3, code)
        self.assertEqual("NODE_BUDGET_EXHAUSTED_ACROSS_RUNS",
                         payload["outcome"])
        self.assertEqual(3, payload["semantic_ceiling"])
        self.assertEqual("c" * 64, payload["plan_digest"])
        self.assertEqual([{"node_id": "lane-x",
                           "cumulative_semantic_attempts": 7,
                           "effective_ceiling": 3,
                           "run_ids": ["run-a", "run-b"]}],
                         payload["nodes"])

    def test_run_resume_prints_the_cross_run_budget_too(self):
        """§19 M6: a guard whose refusal only one verb can print is a guard
        only one verb has."""
        code, payload = self._drive(
            maestro._run_resume,
            maestro._RunRefused(
                "NODE_BUDGET_EXHAUSTED_ACROSS_RUNS", "spent",
                plan_digest="c" * 64, semantic_ceiling=3, nodes=[]))

        self.assertEqual(3, code)
        self.assertEqual("NODE_BUDGET_EXHAUSTED_ACROSS_RUNS",
                         payload["outcome"])

    def test_an_unknown_allowed_node_has_its_own_outcome(self):
        """§3.6 B10's escape can be typed wrong, and a misspelled node id
        admits nothing and refuses nothing. It is a different mistake from a
        spent budget and gets a different name."""
        code, payload = self._drive(
            maestro._run_start,
            maestro._RunRefused(
                "ALLOW_EXHAUSTED_NODE_UNKNOWN", "no such node",
                unknown_node_ids=["lane-typo"],
                plan_node_ids=["lane-x", "lane-y"]))

        self.assertEqual(3, code)
        self.assertEqual("ALLOW_EXHAUSTED_NODE_UNKNOWN", payload["outcome"])
        self.assertEqual(["lane-typo"], payload["unknown_node_ids"])

    def test_a_refusal_with_no_extra_facts_stays_two_fields(self):
        code, payload = self._drive(
            maestro._run_start,
            maestro._RunRefused("RUN_RECEIPT_NOT_PASS", "the receipt FAILs"))

        self.assertEqual(3, code)
        self.assertEqual({"detail", "outcome"}, set(payload))
        self.assertEqual("RUN_RECEIPT_NOT_PASS", payload["outcome"])


    def test_a_quiescence_failure_carries_its_declared_code(self):
        """The other pseudo-outcome the shape was producing.
        `HarnessQuiescenceError`'s message *is* a declared code, so under the
        untyped arm the typed fact travelled as prose and the class name
        travelled as the field a caller branches on."""
        code, payload = self._drive(
            maestro._run_start,
            maestro.launcher.HarnessQuiescenceError(
                "HERDR_QUIESCENCE_UNPROVEN:handle-token"))

        self.assertEqual(3, code)
        self.assertEqual("RUN_QUIESCENCE_UNPROVEN", payload["outcome"])
        self.assertEqual("HERDR_QUIESCENCE_UNPROVEN",
                         payload["quiescence_code"])
        self.assertIn("handle-token", payload["detail"])

    def test_the_resume_verb_carries_it_too(self):
        code, payload = self._drive(
            maestro._run_resume,
            maestro.launcher.HarnessQuiescenceError(
                "HARNESS_CONTEXT_QUIESCENCE_UNPROVEN"))

        self.assertEqual(3, code)
        self.assertEqual("RUN_QUIESCENCE_UNPROVEN", payload["outcome"])
        self.assertEqual("HARNESS_CONTEXT_QUIESCENCE_UNPROVEN",
                         payload["quiescence_code"])


class NoRunRefusalIsAClassNameTest(unittest.TestCase):
    """The class, not the instance. Every exception type the old untyped arm
    listed must now leave under a declared name, with the class name demoted to
    the `detail` prose where a diagnosis belongs."""

    CLASS_NAMES = ("FILENOTFOUNDERROR", "VALUEERROR", "OSERROR",
                   "RUNTIMEERROR", "LIFECYCLEERROR", "CALLEDPROCESSERROR",
                   "PERMISSIONERROR")

    EXCEPTIONS = (
        FileNotFoundError("no receipt for aaaa"),
        ValueError("RUN_RECEIPT_NOT_PASS"),
        OSError("disk is gone"),
        RuntimeError("the scheduler exploded"),
        lc.LifecycleError("the ledger disagrees"),
        PermissionError("not yours"),
    )

    def test_run_start_never_prints_a_class_name_as_an_outcome(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exception=type(exc).__name__):
                with mock.patch.object(maestro, "_execute_run",
                                       side_effect=exc):
                    code, payload = _emit(lambda: maestro._run_start(
                        argparse.Namespace(plan_file="plan.json",
                                           run_id="run-1", selector=None)))
                self.assertEqual(3, code)
                self.assertEqual("RUN_EXECUTION_FAILED", payload["outcome"])
                self.assertNotIn(payload["outcome"], self.CLASS_NAMES)
                self.assertIn(type(exc).__name__, payload["detail"])

    def test_run_resume_never_prints_a_class_name_as_an_outcome(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exception=type(exc).__name__):
                with mock.patch.object(maestro, "_execute_run",
                                       side_effect=exc):
                    code, payload = _emit(lambda: maestro._run_resume(
                        argparse.Namespace(plan_file="plan.json",
                                           run_id="run-1", selector=None)))
                self.assertEqual(3, code)
                self.assertEqual("RUN_EXECUTION_FAILED", payload["outcome"])
                self.assertIn(type(exc).__name__, payload["detail"])

    def test_mains_last_chance_net_never_prints_one_either(self):
        """The other two instances of the shape. `main`'s workspace branch has
        said `INTERNAL_ERROR` here since it was written; the branch beside it
        printed the class, so the same condition reached an operator as
        `DATABASEERROR` or `OSERROR` depending on which library raised."""
        for exc in (sqlite3.DatabaseError("file is not a database"),
                    OSError("disk is gone"),
                    ValueError("nonsense"),
                    lc.LifecycleError("the ledger disagrees")):
            with self.subTest(exception=type(exc).__name__):
                code, payload = _emit(lambda: maestro._internal_error(exc))
                self.assertEqual(2, code)
                self.assertEqual("MAESTRO_INTERNAL_ERROR", payload["outcome"])
                self.assertNotIn(payload["outcome"], self.CLASS_NAMES)
                self.assertIn(type(exc).__name__, payload["detail"])

    @staticmethod
    def _upcased_class_names(source):
        """Every `<something>.__name__.upper()` the module actually evaluates.

        Parsed rather than grepped, so the prose that explains the defect —
        this file's docstring, and the two comments in `maestro.py` that record
        why the arms went away — is not mistaken for the defect. A grep here
        would convict the explanation and then be deleted for being noisy,
        which is how a detector stops detecting.
        """
        found = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "upper"):
                continue
            inner = func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "__name__":
                found.append(func.lineno)
        return found

    def test_the_shape_is_gone_from_the_runtime(self):
        """§19.2's cheaper detector, applied: once a defect is characterised as
        a shape, search the runtime for the shape rather than waiting for its
        next instance. Four sites carried it; none may return."""
        source = (ADWS / "maestro.py").read_text(encoding="utf-8")
        self.assertEqual([], self._upcased_class_names(source))

    def test_the_detector_convicts_a_planted_instance(self):
        """§13.4: a search returning zero proves nothing until it has been
        shown to return non-zero on a planted violation."""
        planted = "def f(exc):\n    return _refusal(type(exc).__name__.upper(), str(exc))\n"
        self.assertEqual([2], self._upcased_class_names(planted))


if __name__ == "__main__":
    unittest.main()
