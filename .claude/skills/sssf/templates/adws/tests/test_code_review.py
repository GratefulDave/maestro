"""Executable proof of the code-review gate: no lane merges on tests alone.

Before this stage existed a run merged a build lane's diff on gate results
only. The gap is not hypothetical — it is B12 in the failure inventory, where
the integrator path could declare `verified` on the strength of its own
acceptance re-run and the review declaration was a flag nothing read.

Every named invariant below is tested in **both** directions, per §13.4's rule
that a detector which has only ever returned "clean" has proven nothing: each
convicts a planted violation and acquits the real article. That rule is also
B15's lesson in miniature — Strav's gates 1–10 survived a rewrite as *fields*
with zero readers, and the tests that would have caught it were deleted by the
cutover commit itself.

Grouped by what each settles:

  B8   FAIL is unrepresentable without a located blocking finding
  B9   the reviewer's input is a declared, validated contract
  B10  byte-identical bytes replay a stored verdict, never a second opinion
  B12  no actor reviews its own output
  B13  the handoff is size-checked before dispatch, and fails closed
  B14  quiescence after liveness, without relying on a wall clock
  §7.3 the review-node predicate, which shares no clause with an agent node's
  §7.5 the review ceiling, bounded and disjoint from the semantic one
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import pydantic  # noqa: E402

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import verification as vf  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_scheduler import SchedulerFixture, green, red  # noqa: E402


BASE_SHA = "1" * 40
OUTPUT_SHA = "2" * 40


def make_store(tmp: Path):
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    data_dir = tmp / "sssf-data"
    data_dir.mkdir(exist_ok=True)
    seed = rc.generate_seed()
    return fin.ReceiptStore(tmp / "receipts", repo_paths=(repo,),
                            data_dir=data_dir,
                            verify_keys=[rc.seed_to_public_key(seed)],
                            signing_seed=seed)


def cell(check_id, object_id, status, severity, message="", canary=None):
    return fin.DerivedCell(check_id=check_id, object_id=object_id,
                           status=status, severity=severity, message=message,
                           canary=canary)


def graded(check_id, object_id, status, severity, message="", canary=None,
           grade=None, rationale="because"):
    """A derived cell with A9's second axis on it.

    `rationale` is defaulted here and nowhere in production: the invariant
    under test is about a *missing* reason, so every case that is not about
    that one must supply one, and repeating the same string in fifteen
    constructors would bury the cases that deliberately omit it.
    """
    return cr.GradedCell(check_id=check_id, object_id=object_id, status=status,
                         severity=severity, grade=grade, message=message,
                         rationale=rationale, canary=canary)


def a_node(node_id="build"):
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
                       outputs=(f"{node_id}.py",),
                       instruction=f"Implement {node_id} as the plan declares.",
                       gate_command=("pytest",),
                       gate_selector=f"tests/{node_id}")


# ── B8: FAIL is unrepresentable without a located blocking finding ──────────

class LocatedFindingsTests(unittest.TestCase):
    """B8's invariant, from v1 rather than retrofitted.

    Strav could add the `findings` *field* after the fact but not the
    invariant, because a field added later is optional forever. So the
    detector is tested against planted violations here, before any receipt for
    a subject can exist.

    Since §3.6 A9's grading landed, "blocking" alone no longer decides: a
    finding rejects when its check is BLOCKING **and** the reviewer graded it
    at or above the installation's threshold. The invariant is otherwise
    unchanged, and `tests/test_graded_findings.py` owns the grading itself.
    """

    def _verdict(self, verdict, *cells, reject_at=cr.FindingGrade.ERROR):
        return cr.GradedVerdict(verdict=verdict, cells=cells,
                                reject_at=reject_at)

    def _fail_with_finding(self):
        return self._verdict(
            fin.Verdict.FAIL,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.BLOCKING,
                   "inverted condition at line 42",
                   grade=cr.FindingGrade.ERROR))

    def test_acquits_a_fail_that_carries_a_located_finding(self):
        cr.require_located_findings(self._fail_with_finding())

    def test_acquits_a_clean_pass(self):
        cr.require_located_findings(self._verdict(
            fin.Verdict.PASS,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.CLEAR, fin.Severity.BLOCKING)))

    def test_convicts_a_contentless_fail(self):
        """The exact B8 shape: a status word with nothing behind it."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.CLEAR, fin.Severity.BLOCKING))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_finding_with_no_message(self):
        """A finding that names no place is not located, so it is not a
        finding — it cannot be handed to a builder as retry guidance."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.BLOCKING, "   ",
                   grade=cr.FindingGrade.ERROR))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_rejecting_finding_with_no_reason_for_its_grade(self):
        """A9's addition to the same invariant: the grade decides the merge, so
        a grade nobody justified is the contentless verdict one level down."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.BLOCKING,
                   "inverted condition at line 42",
                   grade=cr.FindingGrade.ERROR, rationale="  "))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_pass_that_carries_a_rejecting_finding(self):
        """The inverted shape, which is the one that would silently merge."""
        planted = self._verdict(
            fin.Verdict.PASS,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.BLOCKING,
                   "a real problem", grade=cr.FindingGrade.ERROR))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_an_advisory_finding_does_not_force_a_fail(self):
        cr.require_located_findings(self._verdict(
            fin.Verdict.PASS,
            graded("diff.is_coherent_with_its_surroundings", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.ADVISORY,
                   "naming drifts from the module",
                   grade=cr.FindingGrade.WARNING)))

    def test_a_sub_threshold_blocking_finding_does_not_force_a_fail(self):
        """A9, as an invariant rather than as a verdict: the whole point of
        the grade is that a true finding on a blocking check can be recorded
        instead of ending the lane."""
        cr.require_located_findings(self._verdict(
            fin.Verdict.PASS,
            graded("diff.introduces_no_obvious_defect", "diff:abc",
                   fin.CellStatus.FINDING, fin.Severity.BLOCKING,
                   "a pre-existing robustness gap",
                   grade=cr.FindingGrade.WARNING)))

    def test_the_known_bad_canary_never_forces_a_fail(self):
        """The known-bad control is answered `finding` by construction, so
        counting it would fail every diff ever reviewed."""
        cr.require_located_findings(self._verdict(
            fin.Verdict.PASS,
            graded(fin.CANARY_CHECK_ID, fin.CANARY_KNOWN_BAD_OBJECT,
                   fin.CellStatus.FINDING, fin.Severity.ADVISORY, "control",
                   canary=fin.CanaryKind.KNOWN_BAD,
                   grade=cr.FindingGrade.ERROR)))


# ── B12: no actor reviews its own output ────────────────────────────────────

class CrossVendorTests(unittest.TestCase):

    def test_acquits_distinct_vendors(self):
        cr.require_distinct_vendor("xai", "openai")

    def test_convicts_the_same_vendor(self):
        with self.assertRaises(cr.SelfJudgeRefused):
            cr.require_distinct_vendor("openai", "openai")

    def test_convicts_case_and_whitespace_variants(self):
        """Otherwise `OpenAI ` and `openai` read as two vendors."""
        with self.assertRaises(cr.SelfJudgeRefused):
            cr.require_distinct_vendor(" OpenAI ", "openai")

    def test_convicts_an_unnamed_vendor(self):
        """Fail closed: a vendor nobody declared cannot be shown to differ."""
        for builder, reviewer in (("", "openai"), ("xai", ""), ("", "")):
            with self.subTest(builder=builder, reviewer=reviewer):
                with self.assertRaises(cr.SelfJudgeRefused):
                    cr.require_distinct_vendor(builder, reviewer)


# ── B13: size-check before dispatch, fail closed ────────────────────────────

class HandoffPreflightTests(unittest.TestCase):

    def test_acquits_a_handoff_that_fits(self):
        self.assertGreater(cr.preflight_handoff("x" * 900, 200_000), 0)

    def test_convicts_a_handoff_that_overflows(self):
        """B13's shape: 710,673 bytes at a 272K window."""
        with self.assertRaises(cr.HandoffTooLarge):
            cr.preflight_handoff("x" * 710_673, 272_000)

    def test_convicts_an_unknown_window(self):
        """An unmeasured window is not a passing one — the same rule
        `check_occupancy` applies to a NULL occupancy row."""
        for window in (None, 0):
            with self.subTest(window=window):
                with self.assertRaises(cr.HandoffTooLarge):
                    cr.preflight_handoff("small", window)

    def test_the_budget_is_a_fraction_not_the_whole_window(self):
        """A handoff filling the window leaves no room to answer in, and
        would be refused after the fact by the occupancy gate anyway."""
        window = 10_000
        just_over_half = "x" * int(window * cr.BYTES_PER_TOKEN * 0.6)
        with self.assertRaises(cr.HandoffTooLarge):
            cr.preflight_handoff(just_over_half, window)


# ── B9: the reviewer's input is a declared, validated contract ──────────────

class HandoffContractTests(unittest.TestCase):

    def _complete(self, **kw):
        # Annotated because the literal is heterogeneous: without it every
        # field of the unpack is inferred as the union of all value types and
        # each typed constructor parameter is reported as mismatched.
        base: Dict[str, Any] = dict(
            subject_digest="a" * 64, run_id="run1", node_id="build",
            node_kind="agent", instruction="Add the parser.",
            declared_outputs=["parser.py"], gate_command=["pytest"],
            gate_selector="tests/parser", base_sha=BASE_SHA,
            output_sha=OUTPUT_SHA, diff="--- a\n+++ b\n",
            matrix=[{"check_id": "c", "object_id": "o"}], pair_count=1,
            report_path="/tmp/report.json",
            rubric=[{"check_id": "c", "question": "is it right?"}])
        base.update(kw)
        return cr.ReviewHandoff(**base)

    def test_acquits_a_complete_contract(self):
        self._complete().require_complete()

    def test_convicts_the_409_byte_starved_handoff(self):
        """B9's literal shape: identifiers present, everything load-bearing
        empty. A reviewer given this cannot produce a meaningful verdict, so
        its PASS is worthless and the launch must not happen."""
        with self.assertRaises(cr.HandoffIncomplete):
            self._complete(instruction="   ").require_complete()

    def test_convicts_a_contract_with_no_cells(self):
        with self.assertRaises(cr.HandoffIncomplete):
            self._complete(matrix=[], pair_count=0).require_complete()

    def test_convicts_a_contract_with_no_questions(self):
        """A cell id without its question is not something a reviewer can
        answer."""
        with self.assertRaises(cr.HandoffIncomplete):
            self._complete(rubric=[]).require_complete()

    def test_convicts_an_inconsistent_pair_count(self):
        """The fabrication canary must at least be self-consistent, or the
        reviewer is asked to echo a number the contract itself contradicts."""
        with self.assertRaises(cr.HandoffIncomplete):
            self._complete(pair_count=7).require_complete()

    def test_convicts_an_agent_node_with_no_acceptance_contract(self):
        with self.assertRaises(cr.HandoffIncomplete):
            self._complete(gate_command=[]).require_complete()

    def test_a_code_node_may_have_no_gate(self):
        """A code node's acceptance is its exit code, so demanding a gate of
        every kind would refuse the composition §6.7 recommends."""
        self._complete(node_kind="code", gate_command=[],
                       gate_selector="").require_complete()

    def test_the_contract_forbids_smuggled_fields(self):
        with self.assertRaises(pydantic.ValidationError):
            self._complete(verdict="PASS")

    def test_the_rendered_prompt_carries_every_declared_part(self):
        """B9 is about what actually reaches the reviewer, so the assertion is
        on the rendered bytes rather than on the model's fields."""
        text = self._complete().render()
        for expected in ("Add the parser.", "parser.py", "pytest",
                         "tests/parser", BASE_SHA, OUTPUT_SHA,
                         "is it right?"):
            self.assertIn(expected, text)


# ── B10: identical bytes replay a verdict, never earn a second opinion ──────

class ReplayTests(unittest.TestCase):

    def test_the_digest_ignores_the_attempt_number(self):
        """The whole B10 guard. Including `attempt_no` would mint a fresh
        identity for an empty resubmission, which is exactly how a FAIL became
        a PASS on a byte-identical commit."""
        first = cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                                 output_sha=OUTPUT_SHA, rubric_version="v1")
        second = cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                                  output_sha=OUTPUT_SHA, rubric_version="v1")
        self.assertEqual(first, second)

    def test_the_digest_changes_with_the_output(self):
        self.assertNotEqual(
            cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                             output_sha=OUTPUT_SHA, rubric_version="v1"),
            cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                             output_sha="3" * 40, rubric_version="v1"))

    def test_the_digest_changes_with_the_base(self):
        """The same tree over a different base is different evidence: the
        surrounding code moved, so 'no unrelated change' has a new answer."""
        self.assertNotEqual(
            cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                             output_sha=OUTPUT_SHA, rubric_version="v1"),
            cr.review_digest(run_id="r", node_id="n", base_sha="9" * 40,
                             output_sha=OUTPUT_SHA, rubric_version="v1"))

    def test_the_digest_changes_with_the_rubric(self):
        self.assertNotEqual(
            cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                             output_sha=OUTPUT_SHA, rubric_version="v1"),
            cr.review_digest(run_id="r", node_id="n", base_sha=BASE_SHA,
                             output_sha=OUTPUT_SHA, rubric_version="v2"))

    def test_a_stored_fail_replays_without_launching_a_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            digest = "b" * 64
            store.write(fin.Receipt(
                plan_digest=digest, rubric_version=cr.CODE_RUBRIC.version,
                verdict=fin.Verdict.FAIL,
                cells=(cell("diff.introduces_no_obvious_defect", "diff:abc",
                            fin.CellStatus.FINDING, fin.Severity.BLOCKING,
                            "off by one"),),
                reviewer=fin.ReviewerIdentity(route="omp", model="m",
                                              session_id="s"),
                created_at_epoch=1.0))

            def must_not_launch(_matrix):
                raise AssertionError(
                    "a byte-identical subject launched a second reviewer")

            outcome = cr.review_attempt(
                subject_digest=digest,
                handoff=HandoffContractTests()._complete(subject_digest=digest),
                objects=cr.review_objects(("a.py",), OUTPUT_SHA),
                rubric=cr.CODE_RUBRIC, store=store,
                window_factory=must_not_launch,
                occupancy_reader=lambda _s: 0.1)

        self.assertTrue(outcome.replayed)
        self.assertIs(outcome.verdict, fin.Verdict.FAIL)
        self.assertFalse(outcome.passed)
        # The replayed FAIL still carries its findings, so the recycled attempt
        # gets the same guidance the first rejection produced.
        self.assertIn("off by one", outcome.findings_text())


# ── B14: quiescence after liveness, no reliance on a wall clock ─────────────

class ActorAbandonedTests(unittest.TestCase):
    """B14: 'the reviewer went idle at its prompt having written nothing' and
    the verb waited 22 minutes with no output. The fix is not a shorter
    timeout — a legitimate large review is slow, and a wall clock bounds
    honest work. It is noticing that the thing stopped."""

    def _window(self, statuses, report=None, **kw):
        self.status_calls = list(statuses)
        #: Injected so the confirmation interval can be crossed without
        #: sleeping. `idle` no longer convicts on one sample, so a test that
        #: wants the conviction has to hold the status *and* move the clock.
        self.now = [0.0]

        def next_status(_session):
            return (self.status_calls.pop(0) if self.status_calls
                    else "idle")

        base = dict(
            config=fw.FinalizationConfig(finalization_timeout_s=600.0,
                                         turn_timeout_s=300.0,
                                         poll_interval_s=0.01,
                                         quiescence_confirm_s=60.0),
            time_source=lambda: self.now[0],
            launch=lambda: fw.ReviewerSession(route="omp", model="m",
                                              session_id="pane1"),
            poll_report=lambda: report,
            record_reviewer_session=lambda _s: None,
            kill=lambda _s: None,
            actor_status=next_status,
            transcript_record_count=lambda _s: 0)
        base.update(kw)
        return fw.FinalizationWindow(**base)

    def test_idle_held_after_working_with_no_report_is_a_stall(self):
        window = self._window(["working", "idle"])
        window.open()
        window.report_launched(pid=None)
        self.assertIsNone(window.poll())           # observed working
        self.assertIsNone(window.poll())           # idle: confirmation starts
        self.now[0] += 61.0
        outcome = window.poll()                    # still idle, nothing written
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome.completed)
        self.assertIs(outcome.signal, fw.FinalizationSignal.ACTOR_ABANDONED)
        self.assertTrue(outcome.observed_working)

    def test_one_idle_sample_after_working_is_not_yet_a_stall(self):
        """A pane reads `idle` between turns and while blocked inside a tool
        call alike. One sample is not evidence that anything stopped."""
        window = self._window(["working", "idle"])
        window.open()
        window.report_launched(pid=None)
        self.assertIsNone(window.poll())
        self.assertIsNone(window.poll())
        self.now[0] += 59.0
        self.assertIsNone(window.poll())

    def test_idle_before_ever_working_is_not_a_stall(self):
        """`launch()` returns while the agent still sits at a fresh prompt, and
        `idle` is a *live* status. Convicting here would kill every reviewer
        before it started — which is why the signal has to be armed."""
        window = self._window(["idle", "idle", "idle"])
        window.open()
        window.report_launched(pid=None)
        for _ in range(3):
            self.assertIsNone(window.poll())

    def test_a_written_report_beats_an_idle_actor(self):
        """Precedence, in the same direction the launcher's `poll` uses: what
        the reviewer wrote outranks what the pane reports."""
        window = self._window(["working", "idle"],
                              report={"plan_digest": "x", "pair_count": 0,
                                      "cells": []})
        window.open()
        window.report_launched(pid=None)
        outcome = window.poll()
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome.completed)

    def test_an_unreadable_status_never_convicts(self):
        """A missing observation is not evidence of a stall — herdr hiccups
        must not kill healthy reviewers."""
        window = self._window(["working", None, None])
        window.open()
        window.report_launched(pid=None)
        self.assertIsNone(window.poll())
        self.assertIsNone(window.poll())
        self.assertIsNone(window.poll())

    def test_a_window_without_a_status_reader_is_unchanged(self):
        """Plan finalization passes no reader; its behaviour must not move."""
        window = self._window([], actor_status=None)
        window.open()
        window.report_launched(pid=None)
        self.assertIsNone(window.poll())


# ── §7.3's review-node predicate ────────────────────────────────────────────
#
# `ReviewNodePredicateTests` stood here and exercised `vf.verify_review_node`,
# which production never called. Both are gone: the five clauses are enforced
# along the review path itself and are covered by the tests over that path —
# report parsing and the matrix by `verify_report`'s tests, occupancy by
# `check_occupancy`'s, the derived verdict by `derive_verdict`'s, and the signed
# receipt by the `ReceiptStore` signature tests. Keeping a second predicate
# green proved only that the copy nobody ran still worked.


# ── B11: review is a kind, and never an authored one ────────────────────────

class ReviewKindTests(unittest.TestCase):

    def test_review_is_a_node_kind(self):
        self.assertEqual(st.NodeKind.REVIEW.value, "review")

    def test_a_plan_may_not_author_a_review_node(self):
        """Derived by the scheduler per build attempt. An authored one would
        make 'every merged lane was reviewed' depend on the author writing it."""
        with self.assertRaises(ValueError) as caught:
            st.PlanNode(node_id="r", kind=st.NodeKind.REVIEW, depth=0)
        self.assertIn("derived by the scheduler", str(caught.exception))


# ── §7.5 the review budget, counted durably and kept separate ───────────────

class CeilingProbe:
    """A `Scheduler` reduced to exactly what the two ceiling methods read.

    The production methods are invoked unbound against this, so what runs is
    the rule the scheduler runs — not a second copy of it in a test. They
    touch three things and nothing else: `run_id`, `config`, and
    `deps.store.attempts_for`.

    They are the only enforcers of §7.5's two ceilings.
    `retry_policy.review_budget_exhausted` and `semantic_budget_exhausted`
    stated the same rules from the outside, had no production caller, and
    disagreed with these by one — they counted only the rows that already
    existed, while the scheduler counts the attempt that is failing right now,
    whose row is written by the very call the decision gates. Testing the
    unused pair proved nothing about a run, so they were deleted and these
    tests re-pointed at what enforces the rule.
    """

    def __init__(self, cfg, attempts):
        self.run_id = "run1"
        self.config = cfg
        self.deps = SimpleNamespace(
            store=SimpleNamespace(
                attempts_for=lambda run_id, node_id: tuple(attempts)))


class ReviewBudgetTests(unittest.TestCase):

    def _attempt(self, node_id, no, rejected):
        return st.AttemptRecord(
            run_id="run1", node_id=node_id, attempt_no=no, base_sha=BASE_SHA,
            state=st.NodeState.PENDING,
            extra={rp.REVIEW_REJECTED_KEY: True} if rejected else {})

    def test_counts_only_review_rejected_rows(self):
        attempts = [self._attempt("n", 1, True), self._attempt("n", 2, False),
                    self._attempt("n", 3, True)]
        self.assertEqual(rp.review_attempts_total(attempts, "n"), 2)

    def test_a_semantic_failure_never_spends_review_budget(self):
        """The two ceilings bound different things. A shared counter would let
        a node that burned its attempts on red gates merge unreviewed."""
        attempts = [
            st.AttemptRecord(run_id="run1", node_id="n", attempt_no=1,
                             base_sha=BASE_SHA, state=st.NodeState.PENDING,
                             retry_class=st.RetryClass.SEMANTIC, extra={}),
        ]
        self.assertEqual(rp.review_attempts_total(attempts, "n"), 0)
        self.assertEqual(rp.semantic_attempts_total(attempts, "n"), 1)

    def test_the_ceiling_admits_exactly_its_count(self):
        """`review_ceiling` attempts total, the in-flight one included.

        The scheduler decides while the failing attempt still holds RUNNING
        and before its marker row is written, so at a ceiling of 3 it stops
        the node on the third rejection — two stored markers plus this one —
        rather than admitting a fourth.
        """
        cfg = st.SchedulerConfig(
            concurrency=1, node_timeout_s=1.0, turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0, backstop_t_s=100.0,
            semantic_ceiling=3, review_ceiling=3)
        rows = [self._attempt("n", i, True) for i in range(1)]
        self.assertFalse(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 0))
        rows.append(self._attempt("n", 1, True))
        self.assertTrue(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 0))

    def test_a_forced_grant_reopens_an_exhausted_budget(self):
        """B10's missing operator escape: a flaky FAIL would otherwise strand
        the producer, because identical bytes replay the stored verdict."""
        cfg = st.SchedulerConfig(
            concurrency=1, node_timeout_s=1.0, turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0, backstop_t_s=100.0,
            semantic_ceiling=3, review_ceiling=3)
        rows = [self._attempt("n", i, True) for i in range(2)]
        self.assertTrue(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 0))
        self.assertFalse(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 1))

    def test_a_semantic_row_never_spends_the_review_ceiling(self):
        """The disjointness, asserted against the live enforcer rather than
        only against the counting helper: a node that burned every attempt on
        red gates must still arrive at review with its full allowance."""
        cfg = st.SchedulerConfig(
            concurrency=1, node_timeout_s=1.0, turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0, backstop_t_s=100.0,
            semantic_ceiling=3, review_ceiling=2)
        rows = [
            st.AttemptRecord(run_id="run1", node_id="n", attempt_no=i,
                             base_sha=BASE_SHA, state=st.NodeState.PENDING,
                             retry_class=st.RetryClass.SEMANTIC, extra={})
            for i in range(5)]
        self.assertFalse(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 0))

    def test_a_zero_ceiling_is_refused_as_a_setting(self):
        with self.assertRaises(ValueError):
            st.SchedulerConfig(
                concurrency=1, node_timeout_s=1.0, turn_timeout_s=1.0,
                final_acceptance_timeout_s=1.0, backstop_t_s=100.0,
                semantic_ceiling=3, review_ceiling=0)


# ── the stage in the scheduler, over a real repository ──────────────────────

class FakeReview:
    """A scripted reviewer. Records every subject it was asked about, so a
    test can assert the stage did *not* run as easily as that it did."""

    def __init__(self, verdicts, raises=None):
        self.verdicts = list(verdicts)
        self.raises = raises
        self.subjects = []

    def __call__(self, attempt, node, record, base_sha, output_sha):
        self.subjects.append((node.node_id, base_sha, output_sha))
        if self.raises is not None:
            raise self.raises
        passed = self.verdicts.pop(0) if self.verdicts else True
        digest = cr.review_digest(run_id="run1", node_id=node.node_id,
                                  base_sha=base_sha, output_sha=output_sha,
                                  rubric_version=cr.CODE_RUBRIC.version)
        findings = () if passed else (
            graded("diff.gate_is_passed_on_the_merits",
                   f"diff:{output_sha}", fin.CellStatus.FINDING,
                   fin.Severity.BLOCKING,
                   "the gate passes because the value is hardcoded",
                   grade=cr.FindingGrade.ERROR,
                   rationale="the behaviour the gate witnesses is absent"),)
        return cr.ReviewOutcome(
            subject_digest=digest,
            verdict=fin.Verdict.PASS if passed else fin.Verdict.FAIL,
            receipt=fin.Receipt(
                plan_digest=digest, rubric_version=cr.CODE_RUBRIC.version,
                verdict=fin.Verdict.PASS if passed else fin.Verdict.FAIL,
                # The receipt's frozen schema carries severity and no grade,
                # so the two shapes are built separately rather than one being
                # passed where the other belongs.
                cells=tuple(cell(c.check_id, c.object_id, c.status, c.severity,
                                 c.message) for c in findings),
                reviewer=fin.ReviewerIdentity(route="omp", model="m",
                                              session_id="pane"),
                created_at_epoch=1.0),
            replayed=False, findings=findings)


class ReviewStageTests(SchedulerFixture):

    def config(self, **kw):
        kw.setdefault("review_ceiling", 3)
        return super().config(**kw)

    def test_a_reviewed_pass_merges_exactly_as_before(self):
        review = FakeReview([True])
        node = self.agent("build")
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([node], deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)
        self.assertEqual(len(review.subjects), 1)

    def test_the_stage_runs_before_the_merge_not_after(self):
        """The review must see the diff while the node can still be recycled.
        A node the reviewer rejected must never have reached MERGED."""
        review = FakeReview([False, False, False])
        self.written["build"] = {"build.py": "hardcoded\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        self.assertNotEqual(self.states()["build"], st.NodeState.MERGED.value)

    def test_a_rejection_recycles_the_attempt_with_the_findings(self):
        """The findings are the whole justification for spending another
        attempt: a retry that repeats the original request would produce the
        same diff and be rejected for the same reason."""
        review = FakeReview([False, True])
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)
        prompts = self.prompts["build"]
        self.assertEqual(len(prompts), 2)
        self.assertIsNone(prompts[0])
        self.assertIn("hardcoded", prompts[1])
        self.assertIn("code review", prompts[1].lower())
        self.assertIn("diff.gate_is_passed_on_the_merits", prompts[1])

    def test_three_rejections_block_the_lane_and_surface_the_findings(self):
        review = FakeReview([False, False, False])
        self.written["build"] = {"build.py": "ok\n"}
        report = self.schedule(
            [self.agent("build")],
            deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.BLOCKED.value)
        self.assertEqual(
            self.store.get_node("run1", "build").block_reason,
            st.BlockReason.REVIEW_BUDGET_EXHAUSTED)
        self.assertEqual(len(review.subjects), 3)
        # A bare budget-exhausted reason names the rule that fired and nothing
        # an operator can act on, so the findings ride the report.
        self.assertIn("build", report.review_findings)
        self.assertIn("hardcoded", report.review_findings["build"])
        # And how many findings each rejected attempt drew. Flat at 1 across
        # all three: the reviewer was not converging, which is what says
        # raising `review_ceiling` for this node would have bought nothing.
        self.assertEqual(report.review_convergence["build"], (1, 1, 1))

    def test_resume_reloads_review_convergence_from_attempt_rows(self):
        """The series survives the process that observed it.

        Rebuilt from the same review-rejected rows the budget is counted
        from, so a run finished by a second process reports the whole run's
        convergence rather than its own slice of it.
        """
        review = FakeReview([False, False, False])
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule(
            [self.agent("build")],
            deps=self.deps(review_attempt=review)).run()
        rebuilt = rp.review_convergence_from_attempts(
            self.store.attempts_for("run1"))
        self.assertEqual(rebuilt["build"], [1, 1, 1])
        resumed = self.schedule([self.agent("build")])
        resumed.project()
        self.assertEqual(resumed._review_convergence["build"], [1, 1, 1])

    def test_the_ceiling_is_configurable_and_respected(self):
        review = FakeReview([False, False])
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      config=self.config(review_ceiling=2),
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.BLOCKED.value)
        self.assertEqual(len(review.subjects), 2)

    def test_the_stage_never_runs_when_the_gate_already_failed(self):
        """A red post-gate ends the attempt before the review, so a reviewer
        turn is never spent on code that has not met its stated contract."""
        review = FakeReview([True])
        self.gate_script[("build", "post")] = [red(), green()]
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)
        # Two attempts ran; only the one whose gate went green was reviewed.
        self.assertEqual(len(review.subjects), 1)

    def test_the_stage_never_runs_when_the_pre_gate_is_not_falsifiable(self):
        review = FakeReview([True])
        self.gate_script[("build", "pre")] = [green()]
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.BLOCKED.value)
        self.assertEqual(review.subjects, [])

    def test_a_stalled_reviewer_is_environmental_and_spends_no_review_budget(self):
        """A wedged reviewer says nothing about the code. Charging it to the
        review ceiling would let a broken herdr consume a node's attempts."""
        review = FakeReview([], raises=cr.ReviewStalled(
            fw.ReviewerSession(route="omp", model="m", session_id="pane"),
            fw.FinalizationSignal.ACTOR_ABANDONED, 12.0))
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        attempts = self.store.attempts_for("run1", "build")
        self.assertEqual(rp.review_attempts_total(attempts, "build"), 0)
        self.assertTrue(any(a.retry_class is st.RetryClass.ENVIRONMENTAL
                            for a in attempts))
        # And no findings were invented for a review that never happened: the
        # prompt is never mutated, on any of the retries the stall earns.
        self.assertTrue(all(p is None for p in self.prompts["build"]))

    def test_the_review_subject_is_the_diff_against_the_integration_head(self):
        review = FakeReview([True])
        self.written["build"] = {"build.py": "ok\n"}
        head = wt.integration_head(self.repo, "integration/run1")
        self.schedule([self.agent("build")],
                      deps=self.deps(review_attempt=review)).run()

        node_id, base_sha, output_sha = review.subjects[0]
        self.assertEqual(node_id, "build")
        self.assertEqual(base_sha, head)
        self.assertNotEqual(output_sha, head)

    def test_a_code_node_is_reviewed_too(self):
        """A code node's diff merges on an exit code alone otherwise, which is
        the same unreviewed-merge gap one kind over."""
        review = FakeReview([True])
        self.schedule([self.code("fmt", outputs=())],
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(len(review.subjects), 1)

    def test_without_the_stage_the_run_behaves_exactly_as_before(self):
        """The stage is optional and its absence is a stated limit, not a
        silent one — this pins the old behaviour so a regression is visible."""
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")], deps=self.deps()).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)

    def test_the_two_ceilings_do_not_share_a_budget(self):
        """A node that spends semantic attempts on red gates still gets its
        full review allowance, which is why the counters are separate."""
        review = FakeReview([False, True])
        self.gate_script[("build", "post")] = [red(), green(), green()]
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      config=self.config(semantic_ceiling=3, review_ceiling=3),
                      deps=self.deps(review_attempt=review)).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)
        attempts = self.store.attempts_for("run1", "build")
        self.assertEqual(rp.semantic_attempts_total(attempts, "build"), 1)
        self.assertEqual(rp.review_attempts_total(attempts, "build"), 1)


# ── the objects the matrix ranges over ──────────────────────────────────────

class ReviewObjectTests(unittest.TestCase):

    def test_every_changed_file_becomes_a_located_object(self):
        """Per-file objects are what make a finding located: a secret found in
        `a.py` names `a.py`, not 'somewhere in the diff'."""
        objects = cr.review_objects(("a.py", "b/c.py"), OUTPUT_SHA)
        self.assertEqual(
            [o.object_id for o in objects],
            [f"diff:{OUTPUT_SHA}", "file:a.py", "file:b/c.py"])

    def test_the_matrix_covers_the_diff_and_every_file(self):
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "c" * 64, cr.review_objects(("a.py",), OUTPUT_SHA))
        graded = {(c.check_id, c.object_id) for c in matrix.graded_cells}
        self.assertIn(("diff.implements_the_stated_instruction",
                       f"diff:{OUTPUT_SHA}"), graded)
        self.assertIn(("file.no_secret_or_credential_introduced",
                       "file:a.py"), graded)
        # Both controls are present, and they are excluded from grading.
        self.assertEqual(len(matrix.canary_cells), 2)

    def test_the_code_rubric_emits_no_plan_cells(self):
        """The two review families share an enum and must not share cells."""
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "c" * 64,
            (fin.ReviewObject(object_id="plan", kind=fin.ObjectKind.PLAN),))
        self.assertEqual(matrix.graded_cells, ())


# ── §8.3: the reviewer's pane carries the redirection too ───────────────────

def _pane_env(flags: "tuple[str, ...]") -> "dict[str, str]":
    """What `--env KEY=VALUE` flags will actually put in the pane's shell."""
    pane_env: "dict[str, str]" = {}
    for index, token in enumerate(flags):
        if token == "--env":
            key, _, value = flags[index + 1].partition("=")
            pane_env[key] = value
    return pane_env



class ReviewerLaunchEnvironmentTests(unittest.TestCase):
    """The reviewer is an agent in a pane, so §8.3 binds its launch as well.

    `LaunchSpec.environment` defaults to an empty mapping, and this launch was
    the one construction that never passed one. `pane_env_flags` refuses a launch
    whose redirection is incomplete — deliberately, since a silently dropped
    redirect convicts an agent for a harness defect — so the omission did not
    degrade quietly: it refused with all seven variables named and discarded a
    verified attempt's 61 turns of work at the review stage.
    """

    def _repo_with_two_commits(self, root: Path) -> "tuple[Path, str, str]":
        repo = root / "repo"
        repo.mkdir()

        def git(*args: str) -> str:
            result = subprocess.run(["git", "-C", str(repo), *args],
                                    capture_output=True, text=True, check=True)
            return result.stdout.strip()

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-qm", "base")
        base_sha = git("rev-parse", "HEAD")
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-qm", "output")
        return repo, base_sha, git("rev-parse", "HEAD")

    def test_the_reviewer_launch_carries_every_scratch_redirection(self):
        import argparse
        from unittest import mock

        import maestro
        from adw_modules import launcher as lch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, base_sha, output_sha = self._repo_with_two_commits(root)
            data_dir = root / "sssf-data"
            data_dir.mkdir()
            seed = rc.generate_seed()
            review_root = root / "state" / "review"
            args = argparse.Namespace(
                run_id="run-1", repo=str(repo),
                review_root=str(review_root),
                review_receipt_dir=str(review_root / "receipts"),
                data_dir=str(data_dir),
                verify_key=[rc.seed_to_public_key(seed).hex()],
                signing_seed=seed.hex(),
                reviewer_route="omp", reviewer_model="openai-codex/gpt-5.6-sol",
                reviewer_effort="high", reviewer_profile="openai-performance",
                reviewer_vendor="openai", execution_vendor="anthropic",
                review_timeout_s=60.0, reviewer_turn_timeout_s=30.0,
                reviewer_poll_interval_s=0.1,
                review_reject_grade=cr.DEFAULT_REJECT_GRADE)

            captured = {}

            class FakeRunner:
                def launch(self, spec):
                    captured["spec"] = spec
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="w1:p2", agent_name="reviewer",
                        launched_cwd=spec.worktree,
                        environment=spec.environment)

                def cancel(self, handle, deadline):
                    return None

                def agent_status(self, handle):
                    return "working"

            class CapturedWindow:
                def __init__(self, **kwargs):
                    self.launch = kwargs["launch"]

            def stub_review_attempt(*, window_factory, **_kwargs):
                window_factory(None).launch()
                return "reviewed"

            # B13's preflight resolves the model through omp's merged
            # catalog. Stubbing the catalog keeps this test about the launch
            # environment instead of about whichever models this machine has
            # registered.
            with mock.patch.object(
                    maestro.agent_pi, "catalog",
                    lambda: (("openai-codex", "gpt-5.6-sol", 400_000),)), \
                    mock.patch.object(maestro.agent_pi, "context_window",
                                      return_value=400000), \
                    mock.patch.object(maestro.finalization_window,
                                      "FinalizationWindow", CapturedWindow), \
                    mock.patch.object(maestro.code_review, "review_attempt",
                                      stub_review_attempt):
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, FakeRunner()))
                review(None, a_node(), None, base_sha, output_sha)

            spec = captured["spec"]
            # The refusal this closes is computed from exactly these keys, so
            # the assertion is that `pane_env_flags` does not refuse.
            pane_env = _pane_env(lch.pane_env_flags(spec.environment))
            self.assertEqual(set(pane_env), set(lch.SCRATCH_ENV_KEYS))
            # The reviewer runs at the repository rather than in an attempt
            # worktree, so its byproducts must land under the run's own review
            # root — never in the repo, and never in some attempt's scratch.
            for key, value in pane_env.items():
                path = (value.split("cache_dir=", 1)[-1]
                        if key == "PYTEST_ADDOPTS" else value)
                self.assertTrue(Path(path).is_relative_to(review_root), key)
                self.assertFalse(Path(path).is_relative_to(repo), key)
            self.assertEqual(spec.worktree, Path(str(repo)))

    def test_the_plan_finalize_reviewer_launch_carries_every_redirection(self):
        """The same omission, at the reviewer `plan finalize` builds.

        It allocates its own launcher rather than sharing the node runner's, so
        it is a second construction of the same `LaunchSpec` and failed the same
        way — the refusal simply waits until the verb is exercised.
        """
        import argparse
        from unittest import mock

        import maestro
        from adw_modules import launcher as lch
        from adw_modules import route_receipts as rr

        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = rr.load_public_key(fixtures / "route_receipts.pub")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            state = root / "state"
            session_dir = state / "finalize-session"
            args = argparse.Namespace(
                herdr="/bin/true", omp="/bin/true", claude="/bin/true",
                repo=str(repo), reviewer_route="omp", reviewer_model="m",
                reviewer_effort="high", reviewer_profile="p",
                reviewer_session_dir=str(session_dir),
                reviewer_report_file=str(state / "review" / "report.json"),
                route_verify_key=[key.hex()],
                route_receipt=["omp={}".format(fixtures / "omp.json")],
                finalization_timeout_s=60, reviewer_turn_timeout_s=20,
                reviewer_poll_interval_s=1)

            captured = {}

            class FakeLauncher:
                def __init__(self, **_kwargs):
                    pass

                def launch(self, spec):
                    captured["spec"] = spec
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="w1:p2", agent_name="reviewer",
                        launched_cwd=spec.worktree,
                        environment=spec.environment)

                def cancel(self, handle, deadline):
                    return None

            windows = []

            class CapturedWindow:
                def __init__(self, **kwargs):
                    self.launch = kwargs["launch"]
                    windows.append(self)

            matrix = fin.compute_matrix(
                cr.CODE_RUBRIC, "c" * 64,
                cr.review_objects(("a.py",), OUTPUT_SHA))
            with mock.patch.object(maestro.launcher, "HerdrLauncher",
                                   FakeLauncher), \
                    mock.patch.object(maestro.agent_pi, "catalog",
                                      lambda: (("stub", "m", 400_000),)), \
                    mock.patch.object(maestro.finalization_window,
                                      "FinalizationWindow", CapturedWindow):
                maestro._reviewer_window_factory(args)(matrix)
                # Inside the patch: the launch now resolves the reviewer's
                # context window from the catalog to put it on the spec (B13 at
                # the launcher chokepoint), so the stub has to still be in
                # place when the spec is built, not only when it is assembled.
                windows[0].launch()

            spec = captured["spec"]
            pane_env = _pane_env(lch.pane_env_flags(spec.environment))
            self.assertEqual(set(pane_env), set(lch.SCRATCH_ENV_KEYS))
            # A sibling of the session directory the operator named, so it
            # lands wherever that reviewer's own state does — never in the
            # repository under review.
            for key_name, value in pane_env.items():
                path = (value.split("cache_dir=", 1)[-1]
                        if key_name == "PYTEST_ADDOPTS" else value)
                self.assertTrue(Path(path).is_relative_to(
                    session_dir.with_name(session_dir.name + ".scratch")),
                    key_name)
                self.assertFalse(Path(path).is_relative_to(repo), key_name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
