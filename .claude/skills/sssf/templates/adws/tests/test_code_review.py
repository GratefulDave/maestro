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

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
import pydantic  # noqa: E402

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
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
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    data_dir = tmp / "sssf-data"
    data_dir.mkdir(exist_ok=True)
    seed = rc.generate_seed()
    return fin.ReceiptStore(
        tmp / "receipts",
        repo_paths=(repo,),
        data_dir=data_dir,
        verify_keys=[rc.seed_to_public_key(seed)],
        signing_seed=seed,
    )


def cell(check_id, object_id, status, severity, message="", canary=None):
    return fin.DerivedCell(
        check_id=check_id,
        object_id=object_id,
        status=status,
        severity=severity,
        message=message,
        canary=canary,
    )


def graded(
    check_id,
    object_id,
    status,
    severity,
    message="",
    canary=None,
    grade=None,
    rationale="because",
):
    """A derived cell with A9's second axis on it.

    `rationale` is defaulted here and nowhere in production: the invariant
    under test is about a *missing* reason, so every case that is not about
    that one must supply one, and repeating the same string in fifteen
    constructors would bury the cases that deliberately omit it.
    """
    return cr.GradedCell(
        check_id=check_id,
        object_id=object_id,
        status=status,
        severity=severity,
        grade=grade,
        message=message,
        rationale=rationale,
        canary=canary,
    )


def a_node(node_id="build"):
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=0,
        outputs=(f"{node_id}.py",),
        instruction=f"Implement {node_id} as the plan declares.",
        gate_command=("pytest",),
        gate_selector=f"tests/{node_id}",
    )


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
        return cr.GradedVerdict(verdict=verdict, cells=cells, reject_at=reject_at)

    def _fail_with_finding(self):
        return self._verdict(
            fin.Verdict.FAIL,
            graded(
                "diff.introduces_no_obvious_defect",
                "diff:abc",
                fin.CellStatus.FINDING,
                fin.Severity.BLOCKING,
                "inverted condition at line 42",
                grade=cr.FindingGrade.ERROR,
            ),
        )

    def test_acquits_a_fail_that_carries_a_located_finding(self):
        cr.require_located_findings(self._fail_with_finding())

    def test_acquits_a_clean_pass(self):
        cr.require_located_findings(
            self._verdict(
                fin.Verdict.PASS,
                graded(
                    "diff.introduces_no_obvious_defect",
                    "diff:abc",
                    fin.CellStatus.CLEAR,
                    fin.Severity.BLOCKING,
                ),
            )
        )

    def test_convicts_a_contentless_fail(self):
        """The exact B8 shape: a status word with nothing behind it."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded(
                "diff.introduces_no_obvious_defect",
                "diff:abc",
                fin.CellStatus.CLEAR,
                fin.Severity.BLOCKING,
            ),
        )
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_finding_with_no_message(self):
        """A finding that names no place is not located, so it is not a
        finding — it cannot be handed to a builder as retry guidance."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded(
                "diff.introduces_no_obvious_defect",
                "diff:abc",
                fin.CellStatus.FINDING,
                fin.Severity.BLOCKING,
                "   ",
                grade=cr.FindingGrade.ERROR,
            ),
        )
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_rejecting_finding_with_no_reason_for_its_grade(self):
        """A9's addition to the same invariant: the grade decides the merge, so
        a grade nobody justified is the contentless verdict one level down."""
        planted = self._verdict(
            fin.Verdict.FAIL,
            graded(
                "diff.introduces_no_obvious_defect",
                "diff:abc",
                fin.CellStatus.FINDING,
                fin.Severity.BLOCKING,
                "inverted condition at line 42",
                grade=cr.FindingGrade.ERROR,
                rationale="  ",
            ),
        )
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_convicts_a_pass_that_carries_a_rejecting_finding(self):
        """The inverted shape, which is the one that would silently merge."""
        planted = self._verdict(
            fin.Verdict.PASS,
            graded(
                "diff.introduces_no_obvious_defect",
                "diff:abc",
                fin.CellStatus.FINDING,
                fin.Severity.BLOCKING,
                "a real problem",
                grade=cr.FindingGrade.ERROR,
            ),
        )
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_an_advisory_finding_does_not_force_a_fail(self):
        cr.require_located_findings(
            self._verdict(
                fin.Verdict.PASS,
                graded(
                    "diff.is_coherent_with_its_surroundings",
                    "diff:abc",
                    fin.CellStatus.FINDING,
                    fin.Severity.ADVISORY,
                    "naming drifts from the module",
                    grade=cr.FindingGrade.WARNING,
                ),
            )
        )

    def test_a_sub_threshold_blocking_finding_does_not_force_a_fail(self):
        """A9, as an invariant rather than as a verdict: the whole point of
        the grade is that a true finding on a blocking check can be recorded
        instead of ending the lane."""
        cr.require_located_findings(
            self._verdict(
                fin.Verdict.PASS,
                graded(
                    "diff.introduces_no_obvious_defect",
                    "diff:abc",
                    fin.CellStatus.FINDING,
                    fin.Severity.BLOCKING,
                    "a pre-existing robustness gap",
                    grade=cr.FindingGrade.WARNING,
                ),
            )
        )

    def test_the_known_bad_canary_never_forces_a_fail(self):
        """The known-bad control is answered `finding` by construction, so
        counting it would fail every diff ever reviewed."""
        cr.require_located_findings(
            self._verdict(
                fin.Verdict.PASS,
                graded(
                    fin.CANARY_CHECK_ID,
                    fin.CANARY_KNOWN_BAD_OBJECT,
                    fin.CellStatus.FINDING,
                    fin.Severity.ADVISORY,
                    "control",
                    canary=fin.CanaryKind.KNOWN_BAD,
                    grade=cr.FindingGrade.ERROR,
                ),
            )
        )


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
            subject_digest="a" * 64,
            run_id="run1",
            node_id="build",
            node_kind="agent",
            instruction="Add the parser.",
            declared_outputs=["parser.py"],
            gate_command=["pytest"],
            gate_selector="tests/parser",
            base_sha=BASE_SHA,
            output_sha=OUTPUT_SHA,
            diff="--- a\n+++ b\n",
            matrix=[{"check_id": "c", "object_id": "o"}],
            pair_count=1,
            report_path="/tmp/report.json",
            rubric=[{"check_id": "c", "question": "is it right?"}],
        )
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
        self._complete(
            node_kind="code", gate_command=[], gate_selector=""
        ).require_complete()

    def test_the_contract_forbids_smuggled_fields(self):
        with self.assertRaises(pydantic.ValidationError):
            self._complete(verdict="PASS")

    def test_the_rendered_prompt_carries_every_declared_part(self):
        """B9 is about what actually reaches the reviewer, so the assertion is
        on the rendered bytes rather than on the model's fields."""
        text = self._complete().render()
        for expected in (
            "Add the parser.",
            "parser.py",
            "pytest",
            "tests/parser",
            BASE_SHA,
            OUTPUT_SHA,
            "is it right?",
        ):
            self.assertIn(expected, text)


# ── B10: identical bytes replay a verdict, never earn a second opinion ──────


class ReplayTests(unittest.TestCase):
    def test_the_digest_ignores_the_attempt_number(self):
        """The whole B10 guard. Including `attempt_no` would mint a fresh
        identity for an empty resubmission, which is exactly how a FAIL became
        a PASS on a byte-identical commit."""
        first = cr.review_digest(
            run_id="r",
            node_id="n",
            base_sha=BASE_SHA,
            output_sha=OUTPUT_SHA,
            rubric_version="v1",
        )
        second = cr.review_digest(
            run_id="r",
            node_id="n",
            base_sha=BASE_SHA,
            output_sha=OUTPUT_SHA,
            rubric_version="v1",
        )
        self.assertEqual(first, second)

    def test_the_digest_changes_with_the_output(self):
        self.assertNotEqual(
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha=BASE_SHA,
                output_sha=OUTPUT_SHA,
                rubric_version="v1",
            ),
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha=BASE_SHA,
                output_sha="3" * 40,
                rubric_version="v1",
            ),
        )

    def test_the_digest_changes_with_the_base(self):
        """The same tree over a different base is different evidence: the
        surrounding code moved, so 'no unrelated change' has a new answer."""
        self.assertNotEqual(
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha=BASE_SHA,
                output_sha=OUTPUT_SHA,
                rubric_version="v1",
            ),
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha="9" * 40,
                output_sha=OUTPUT_SHA,
                rubric_version="v1",
            ),
        )

    def test_the_digest_changes_with_the_rubric(self):
        self.assertNotEqual(
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha=BASE_SHA,
                output_sha=OUTPUT_SHA,
                rubric_version="v1",
            ),
            cr.review_digest(
                run_id="r",
                node_id="n",
                base_sha=BASE_SHA,
                output_sha=OUTPUT_SHA,
                rubric_version="v2",
            ),
        )

    def test_a_stored_fail_replays_without_launching_a_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            digest = "b" * 64
            store.write(
                fin.Receipt(
                    plan_digest=digest,
                    rubric_version=cr.CODE_RUBRIC.version,
                    verdict=fin.Verdict.FAIL,
                    cells=(
                        cell(
                            "diff.introduces_no_obvious_defect",
                            "diff:abc",
                            fin.CellStatus.FINDING,
                            fin.Severity.BLOCKING,
                            "off by one",
                        ),
                    ),
                    reviewer=fin.ReviewerIdentity(
                        route="omp", model="m", session_id="s"
                    ),
                    created_at_epoch=1.0,
                )
            )

            def must_not_launch(_matrix):
                raise AssertionError(
                    "a byte-identical subject launched a second reviewer"
                )

            outcome = cr.review_attempt(
                subject_digest=digest,
                handoff=HandoffContractTests()._complete(subject_digest=digest),
                objects=cr.review_objects(("a.py",), OUTPUT_SHA),
                rubric=cr.CODE_RUBRIC,
                store=store,
                window_factory=must_not_launch,
                occupancy_reader=lambda _s: 0.1,
            )

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
            return self.status_calls.pop(0) if self.status_calls else "idle"

        base = dict(
            config=fw.FinalizationConfig(
                finalization_timeout_s=600.0,
                turn_timeout_s=300.0,
                poll_interval_s=0.01,
                quiescence_confirm_s=60.0,
            ),
            time_source=lambda: self.now[0],
            launch=lambda: fw.ReviewerSession(
                route="omp", model="m", session_id="pane1"
            ),
            poll_report=lambda: report,
            record_reviewer_session=lambda _s: None,
            kill=lambda _s: None,
            actor_status=next_status,
            transcript_record_count=lambda _s: 0,
        )
        base.update(kw)
        return fw.FinalizationWindow(**base)

    def test_idle_held_after_working_with_no_report_is_a_stall(self):
        window = self._window(["working", "idle"])
        window.open()
        window.report_launched(pid=None)
        self.assertIsNone(window.poll())  # observed working
        self.assertIsNone(window.poll())  # idle: confirmation starts
        self.now[0] += 61.0
        outcome = window.poll()  # still idle, nothing written
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
        window = self._window(
            ["working", "idle"],
            report={"plan_digest": "x", "pair_count": 0, "cells": []},
        )
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
# `check_occupancy`'s, the derived verdict by `grade_verdict`'s, and the signed
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
    """A `Scheduler` reduced to the durable same-session budget seam."""

    def __init__(self, cfg, attempts=(), granted=0, lane_spends=()):
        self.run_id = "run1"
        self.config = cfg
        self.lane_spends = list(lane_spends)

        def spend_lane_retry(_run_id, _node_id, retry_class, **_kwargs):
            self.lane_spends.append(SimpleNamespace(retry_class=retry_class))
            return SimpleNamespace(created=True)

        self.deps = SimpleNamespace(
            store=SimpleNamespace(
                attempts_for=lambda run_id, node_id: tuple(attempts),
                get_node=lambda run_id, node_id: SimpleNamespace(
                    granted_extra_attempts=granted
                ),
                lane_retry_spends=lambda run_id, node_id, limit: tuple(
                    self.lane_spends
                ),
                spend_lane_retry=spend_lane_retry,
            )
        )


class ReviewBudgetTests(unittest.TestCase):
    @staticmethod
    def _spend_review(probe):
        return sch.Scheduler._lane_retry(
            probe,
            SimpleNamespace(node_id="n"),
            st.LaneRetryClass.REVIEW_REJECTION,
            candidate_sha=BASE_SHA,
            detail={"reason": "rejected"},
        )

    def test_a_semantic_failure_never_spends_review_budget(self):
        """Attempt failures and candidate rejections use separate ledgers."""
        attempts = [
            st.AttemptRecord(
                run_id="run1",
                node_id="n",
                attempt_no=1,
                base_sha=BASE_SHA,
                state=st.NodeState.PENDING,
                retry_class=st.RetryClass.SEMANTIC,
                extra={},
            ),
        ]
        config = st.SchedulerConfig(
            concurrency=1,
            node_timeout_s=1.0,
            turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0,
            backstop_t_s=100.0,
            semantic_ceiling=3,
            review_ceiling=3,
        )
        probe = CeilingProbe(config, attempts=attempts)
        self.assertTrue(self._spend_review(probe))
        self.assertEqual(rp.semantic_attempts_total(attempts, "n"), 1)
        self.assertEqual(
            [spend.retry_class for spend in probe.lane_spends],
            [st.LaneRetryClass.REVIEW_REJECTION],
        )

    def test_the_ceiling_admits_exactly_its_count(self):
        """The third rejection is reviewed, then exhausts a ceiling of three."""
        cfg = st.SchedulerConfig(
            concurrency=1,
            node_timeout_s=1.0,
            turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0,
            backstop_t_s=100.0,
            semantic_ceiling=3,
            review_ceiling=3,
        )
        prior = [SimpleNamespace(retry_class=st.LaneRetryClass.REVIEW_REJECTION)]
        self.assertTrue(self._spend_review(CeilingProbe(cfg, lane_spends=prior)))
        prior = [
            SimpleNamespace(retry_class=st.LaneRetryClass.REVIEW_REJECTION)
            for _ in range(2)
        ]
        self.assertFalse(self._spend_review(CeilingProbe(cfg, lane_spends=prior)))

    def test_a_forced_grant_reopens_an_exhausted_budget(self):
        """A grant permits repair after the rejection that exhausted the base budget."""
        cfg = st.SchedulerConfig(
            concurrency=1,
            node_timeout_s=1.0,
            turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0,
            backstop_t_s=100.0,
            semantic_ceiling=3,
            review_ceiling=3,
        )
        prior = [
            SimpleNamespace(retry_class=st.LaneRetryClass.REVIEW_REJECTION)
            for _ in range(2)
        ]
        self.assertFalse(
            self._spend_review(CeilingProbe(cfg, granted=0, lane_spends=prior))
        )
        self.assertTrue(
            self._spend_review(CeilingProbe(cfg, granted=1, lane_spends=prior))
        )

    def test_a_semantic_spend_never_spends_the_review_ceiling(self):
        """Same-session budget classes are counted independently."""
        cfg = st.SchedulerConfig(
            concurrency=1,
            node_timeout_s=1.0,
            turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0,
            backstop_t_s=100.0,
            semantic_ceiling=3,
            review_ceiling=2,
        )
        semantic = [
            SimpleNamespace(retry_class=st.LaneRetryClass.SEMANTIC) for _ in range(5)
        ]
        self.assertTrue(self._spend_review(CeilingProbe(cfg, lane_spends=semantic)))

    def test_a_zero_ceiling_is_refused_as_a_setting(self):
        with self.assertRaises(ValueError):
            st.SchedulerConfig(
                concurrency=1,
                node_timeout_s=1.0,
                turn_timeout_s=1.0,
                final_acceptance_timeout_s=1.0,
                backstop_t_s=100.0,
                semantic_ceiling=3,
                review_ceiling=0,
            )


# ── the stage in the scheduler, over a real repository ──────────────────────


class PersistentRepairArtifactTests(unittest.TestCase):
    """The builder can acknowledge only the candidate/generation it received."""

    def test_acknowledgement_is_strictly_bound_to_one_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ack = root / "repair-acknowledgement.json"
            rejected = "a" * 40
            prompt = maestro._repair_prompt_text(
                "Exact findings, gate output, and unresolved criteria.",
                rejected,
                3,
                ack,
            )
            self.assertIn(rejected, prompt)
            self.assertIn(
                "Exact findings, gate output, and unresolved criteria.", prompt
            )
            ack.write_text(
                json.dumps(
                    {
                        "kind": "repair_acknowledgement",
                        "rejected_candidate_sha": rejected,
                        "builder_generation": 3,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(maestro._read_repair_acknowledgement(ack, rejected, 3))
            ack.write_text(
                json.dumps(
                    {
                        "kind": "repair_acknowledgement",
                        "rejected_candidate_sha": rejected,
                        "builder_generation": 3,
                        "late": True,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(maestro._read_repair_acknowledgement(ack, rejected, 3))


class FakeReview:
    """A scripted reviewer. Records every subject it was asked about, so a
    test can assert the stage did *not* run as easily as that it did."""

    def __init__(self, verdicts, raises=None):
        self.verdicts = list(verdicts)
        self.raises = raises
        self.subjects = []

    def __call__(
        self,
        attempt,
        node,
        record,
        base_sha,
        output_sha,
        _resume_existing_dispatch=False,
    ):
        self.subjects.append((node.node_id, base_sha, output_sha))
        if self.raises is not None:
            raise self.raises
        passed = self.verdicts.pop(0) if self.verdicts else True
        digest = cr.review_digest(
            run_id="run1",
            node_id=node.node_id,
            base_sha=base_sha,
            output_sha=output_sha,
            rubric_version=cr.CODE_RUBRIC.version,
        )
        findings = (
            ()
            if passed
            else (
                graded(
                    "diff.gate_is_passed_on_the_merits",
                    f"diff:{output_sha}",
                    fin.CellStatus.FINDING,
                    fin.Severity.BLOCKING,
                    "the gate passes because the value is hardcoded",
                    grade=cr.FindingGrade.ERROR,
                    rationale="the behaviour the gate witnesses is absent",
                ),
            )
        )
        return cr.ReviewOutcome(
            subject_digest=digest,
            verdict=fin.Verdict.PASS if passed else fin.Verdict.FAIL,
            receipt=fin.Receipt(
                plan_digest=digest,
                rubric_version=cr.CODE_RUBRIC.version,
                verdict=fin.Verdict.PASS if passed else fin.Verdict.FAIL,
                # The receipt's frozen schema carries severity and no grade,
                # so the two shapes are built separately rather than one being
                # passed where the other belongs.
                cells=tuple(
                    cell(c.check_id, c.object_id, c.status, c.severity, c.message)
                    for c in findings
                ),
                reviewer=fin.ReviewerIdentity(
                    route="omp", model="m", session_id="pane"
                ),
                created_at_epoch=1.0,
            ),
            replayed=False,
            findings=findings,
        )


class LegacyInlineReviewRuntimeTests(unittest.TestCase):
    """Runtime migration trusts only a digest-bound signed receipt."""

    def test_receipt_proven_legacy_review_becomes_the_derived_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(
                ("git", "init", "-q", str(repo)), check=True, capture_output=True
            )
            for text in ("base", "candidate"):
                (repo / "artifact.txt").write_text(text, encoding="utf-8")
                subprocess.run(
                    (
                        "git",
                        "-C",
                        str(repo),
                        "-c",
                        "user.name=test",
                        "-c",
                        "user.email=test@example.invalid",
                        "add",
                        "artifact.txt",
                    ),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    (
                        "git",
                        "-C",
                        str(repo),
                        "-c",
                        "user.name=test",
                        "-c",
                        "user.email=test@example.invalid",
                        "commit",
                        "-qm",
                        text,
                    ),
                    check=True,
                    capture_output=True,
                )
            base_sha = subprocess.run(
                ("git", "-C", str(repo), "rev-parse", "HEAD^"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            output_sha = subprocess.run(
                ("git", "-C", str(repo), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            data_dir = root / "data"
            data_dir.mkdir()
            seed = rc.generate_seed()
            receipt_store = fin.ReceiptStore(
                root / "receipts",
                repo_paths=(repo,),
                data_dir=data_dir,
                verify_keys=(rc.seed_to_public_key(seed),),
                signing_seed=seed,
            )
            digest = cr.review_digest(
                run_id="run-1",
                node_id="build",
                base_sha=base_sha,
                output_sha=output_sha,
                rubric_version=cr.CODE_RUBRIC.version,
            )
            receipt_store.write(
                fin.Receipt(
                    plan_digest=digest,
                    rubric_version=cr.CODE_RUBRIC.version,
                    verdict=fin.Verdict.PASS,
                    cells=(),
                    reviewer=fin.ReviewerIdentity(
                        route="omp", model="reviewer", session_id="pane"
                    ),
                    created_at_epoch=1.0,
                )
            )
            store = lc.LifecycleStore(root / "lifecycle.db")
            try:
                node = a_node()
                store.create_run("run-1", "plan", [node])
                store.ensure_derived_review_node(
                    "run-1", "build", depth=1, downstream_needs=()
                )
                store.start_attempt("run-1", "build", base_sha=base_sha)
                # This is the pre-candidate-ledger durable state being resumed.
                store.conn.execute(
                    "UPDATE node_lifecycle SET output_sha=?"
                    " WHERE run_id=? AND node_id=?",
                    (output_sha, "run-1", "build"),
                )
                args = SimpleNamespace(
                    run_id="run-1",
                    repo=str(repo),
                    data_dir=str(data_dir),
                    review_receipt_dir=str(receipt_store.root),
                    verify_key=[rc.seed_to_public_key(seed).hex()],
                )
                migrated = maestro._migrate_legacy_inline_reviews(args, store, [node])
                self.assertEqual(len(migrated), 1)
                self.assertTrue(migrated[0].migrated)
                self.assertEqual(migrated[0].reviews[-1].verdict, st.ReviewVerdict.PASS)
                self.assertEqual(
                    store.candidate_reviews("run-1", "build::review")[0].candidate_sha,
                    output_sha,
                )
                self.assertEqual(
                    store.candidate_reviews("run-1", "build::review")[0].review_digest,
                    digest,
                )
            finally:
                store.close()

    def test_operator_reset_build_does_not_remigrate_superseded_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(
                ("git", "init", "-q", str(repo)), check=True, capture_output=True
            )
            data_dir = root / "data"
            data_dir.mkdir()
            receipts = root / "receipts"
            receipts.mkdir()
            store = lc.LifecycleStore(root / "lifecycle.db")
            try:
                node = a_node()
                store.create_run("run-1", "plan", [node])
                store.ensure_derived_review_node(
                    "run-1", "build", depth=1, downstream_needs=()
                )
                attempt = store.start_attempt("run-1", "build", base_sha=BASE_SHA)
                store.conn.execute(
                    "UPDATE attempts SET extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        json.dumps({"review_output_sha": OUTPUT_SHA}),
                        "run-1",
                        "build",
                        attempt,
                    ),
                )
                store.conn.execute(
                    "UPDATE attempts SET state='VERIFIED'"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    ("run-1", "build", attempt),
                )
                store.conn.execute(
                    "UPDATE node_lifecycle SET state=?, output_sha=NULL,"
                    " block_reason=?, pending_cause=NULL, retry_spend_floor=?"
                    " WHERE run_id=? AND node_id=?",
                    (st.NodeState.PENDING.value, None, attempt, "run-1", "build"),
                )
                replacement = store.start_attempt("run-1", "build", base_sha=BASE_SHA)
                store.conn.execute(
                    "UPDATE node_lifecycle SET state=?, block_reason=?"
                    " WHERE run_id=? AND node_id=?",
                    (
                        st.NodeState.BLOCKED.value,
                        "QUIESCENCE_UNPROVEN",
                        "run-1",
                        "build",
                    ),
                )
                args = SimpleNamespace(
                    run_id="run-1",
                    repo=str(repo),
                    data_dir=str(data_dir),
                    review_receipt_dir=str(receipts),
                    verify_key=[],
                )

                migrated = maestro._migrate_legacy_inline_reviews(args, store, [node])

                self.assertEqual(migrated, ())
                self.assertEqual(store.legacy_review_migrations("run-1"), ())
                self.assertEqual(store.lane_candidates("run-1", "build"), ())
            finally:
                store.close()


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

    def test_late_recovery_reopens_blocked_lane_for_review(self):
        review = FakeReview([True])
        node = self.agent("build")
        scheduler = self.schedule([node], deps=self.deps(review_attempt=review))
        scheduler.project()
        base = wt.integration_head(self.repo, "integration/run1")
        attempt_no = self.store.start_attempt("run1", "build", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "build",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "build", attempt_no, baseline, attempt.ignored_at_base
        )
        (attempt.path / "build.py").write_text("late\n")
        self.assertTrue(
            self.store.set_lane_phase("run1", "build", st.LanePhase.BUILDING)
        )
        self.assertTrue(
            self.store.set_lane_phase(
                "run1", "build", st.LanePhase.BLOCKED, expected=st.LanePhase.BUILDING
            )
        )
        self.store.mark_blocked("run1", "build", st.BlockReason.QUIESCENCE_UNPROVEN)
        self.store.declare_outcome("run1")
        self.store.resume_run("run1")
        self.store.prepare_late_envelope_recovery("run1", "build", attempt_no)
        self.assertIs(
            self.store.get_node("run1", "build").lane_phase, st.LanePhase.BUILDING
        )

        recovered = mock.Mock(
            return_value=sch.NodeExecution(
                envelope_parsed=True, envelope_payload={"success": True}, exit_code=0
            )
        )
        report = self.schedule(
            [node],
            deps=self.deps(
                review_attempt=review,
                run_node=mock.Mock(side_effect=AssertionError("relaunch")),
                recover_node=recovered,
            ),
        ).run()

        recovered.assert_called_once()
        self.assertEqual(self.store.get_node("run1", "build").attempt_no, attempt_no)
        self.assertEqual(len(review.subjects), 1)
        self.assertIs(self.store.get_node("run1", "build").state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_late_recovery_reuses_already_sealed_output_commit(self):
        review = FakeReview([True])
        node = self.agent("build")
        scheduler = self.schedule([node], deps=self.deps(review_attempt=review))
        scheduler.project()
        base = wt.integration_head(self.repo, "integration/run1")
        attempt_no = self.store.start_attempt("run1", "build", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "build",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "build", attempt_no, baseline, attempt.ignored_at_base
        )
        (attempt.path / "build.py").write_text("sealed\n")
        after = wt.inventory(attempt.path)
        measured = wt.delta(baseline, after)
        output_sha = wt.commit_measured_delta(
            attempt, measured, after, "build attempt 1"
        )
        self.store.record_sealed_output("run1", "build", attempt_no, output_sha)
        self.assertEqual(
            self.store.attempt_sealed_output("run1", "build", attempt_no), output_sha
        )
        published = self.store.publish_candidate(
            "run1",
            "build",
            output_sha,
            parent_candidate_sha=None,
            builder_generation=attempt_no,
            repo_path=self.repo,
        )
        self.assertTrue(published.created)
        self.assertTrue(
            self.store.set_lane_phase("run1", "build", st.LanePhase.BUILDING)
        )
        self.assertTrue(
            self.store.set_lane_phase(
                "run1", "build", st.LanePhase.BLOCKED, expected=st.LanePhase.BUILDING
            )
        )
        self.store.mark_blocked("run1", "build", st.BlockReason.QUIESCENCE_UNPROVEN)
        self.store.declare_outcome("run1")
        self.store.resume_run("run1")
        self.store.prepare_late_envelope_recovery("run1", "build", attempt_no)

        recovered = mock.Mock(
            return_value=sch.NodeExecution(
                envelope_parsed=True, envelope_payload={"success": True}, exit_code=0
            )
        )
        report = self.schedule(
            [node],
            deps=self.deps(
                review_attempt=review,
                run_node=mock.Mock(side_effect=AssertionError("relaunch")),
                recover_node=recovered,
            ),
        ).run()

        recovered.assert_called_once()
        self.assertEqual(self.store.get_node("run1", "build").attempt_no, attempt_no)
        self.assertEqual(review.subjects[0][2], output_sha)
        self.assertIs(self.store.get_node("run1", "build").state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_the_stage_never_runs_when_the_gate_already_failed(self):
        """A red post-gate is repaired before any reviewer turn is spent."""
        review = FakeReview([True])
        self.gate_script[("build", "post")] = [red(), green()]
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule(
            [self.agent("build")], deps=self.deps(review_attempt=review)
        ).run()

        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)
        self.assertEqual(len(review.subjects), 1)
        self.assertEqual(self.store.get_node("run1", "build").attempt_no, 1)

    def test_the_stage_never_runs_when_the_pre_gate_is_not_falsifiable(self):
        review = FakeReview([True])
        self.gate_script[("build", "pre")] = [green()]
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule(
            [self.agent("build")], deps=self.deps(review_attempt=review)
        ).run()

        self.assertEqual(self.states()["build"], st.NodeState.BLOCKED.value)
        self.assertEqual(review.subjects, [])

    def test_a_stalled_reviewer_is_environmental_and_spends_no_review_budget(self):
        """A wedged reviewer says nothing about the code. Charging it to the
        review ceiling would let a broken herdr consume a node's attempts."""
        review = FakeReview(
            [],
            raises=cr.ReviewStalled(
                fw.ReviewerSession(route="omp", model="m", session_id="pane"),
                fw.FinalizationSignal.ACTOR_ABANDONED,
                12.0,
            ),
        )
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule(
            [self.agent("build")], deps=self.deps(review_attempt=review)
        ).run()

        attempts = self.store.attempts_for("run1", "build")
        spends = tuple(
            spend
            for spend in self.store.lane_retry_spends("run1", "build", limit=100)
            if spend.retry_class is st.LaneRetryClass.REVIEW_REJECTION
        )
        self.assertEqual(spends, ())
        self.assertTrue(
            any(a.retry_class is st.RetryClass.ENVIRONMENTAL for a in attempts)
        )
        # And no findings were invented for a review that never happened: the
        # prompt is never mutated, on any of the retries the stall earns.
        self.assertTrue(all(p is None for p in self.prompts["build"]))

    def test_the_review_subject_is_the_diff_against_the_integration_head(self):
        review = FakeReview([True])
        self.written["build"] = {"build.py": "ok\n"}
        head = wt.integration_head(self.repo, "integration/run1")
        self.schedule(
            [self.agent("build")], deps=self.deps(review_attempt=review)
        ).run()

        node_id, base_sha, output_sha = review.subjects[0]
        self.assertEqual(node_id, "build")
        self.assertEqual(base_sha, head)
        self.assertNotEqual(output_sha, head)

    def test_a_code_node_is_not_projected_as_a_reviewable_build(self):
        """Only build nodes own a derived review node.

        A code node remains an authored deterministic DAG step; projecting a
        reviewer for it would create a second review convention beside the
        build-lane lifecycle.
        """
        review = FakeReview([True])
        self.schedule(
            [self.code("fmt", outputs=())], deps=self.deps(review_attempt=review)
        ).run()

        self.assertEqual(review.subjects, [])


# ── the objects the matrix ranges over ──────────────────────────────────────


class ReviewObjectTests(unittest.TestCase):
    def test_every_changed_file_becomes_a_located_object(self):
        """Per-file objects are what make a finding located: a secret found in
        `a.py` names `a.py`, not 'somewhere in the diff'."""
        objects = cr.review_objects(("a.py", "b/c.py"), OUTPUT_SHA)
        self.assertEqual(
            [o.object_id for o in objects],
            [f"diff:{OUTPUT_SHA}", "file:a.py", "file:b/c.py"],
        )

    def test_the_matrix_covers_the_diff_and_every_file(self):
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "c" * 64, cr.review_objects(("a.py",), OUTPUT_SHA)
        )
        graded = {(c.check_id, c.object_id) for c in matrix.graded_cells}
        self.assertIn(
            ("diff.implements_the_stated_instruction", f"diff:{OUTPUT_SHA}"), graded
        )
        self.assertIn(("file.no_secret_or_credential_introduced", "file:a.py"), graded)
        # Both controls are present, and they are excluded from grading.
        self.assertEqual(len(matrix.canary_cells), 2)

    def test_a_rubric_emits_no_cell_for_a_kind_it_does_not_declare(self):
        """`compute_matrix` ranges over `applies_to` and nothing else.

        This used to be stated as "the code rubric emits no *plan* cells",
        because two rubrics shared `ObjectKind` and a leak between them would
        have been a real defect. Plan finalization dispatches no reviewer now
        and its five object kinds were deleted with it, so the property is
        stated over a rubric that declares one kind and is asked about the
        other -- the same guarantee, without a kind that no longer exists.
        """
        one_kind = fin.Rubric(
            version="fixture.v1",
            checks=(
                fin.RubricCheck(
                    check_id="file.only",
                    question="q",
                    applies_to=(fin.ObjectKind.CHANGED_FILE,),
                    severity=fin.Severity.BLOCKING,
                ),
            ),
        )
        matrix = fin.compute_matrix(
            one_kind,
            "c" * 64,
            (fin.ReviewObject(object_id="diff:abc", kind=fin.ObjectKind.DIFF),),
        )
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
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            )
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
                run_id="run-1",
                repo=str(repo),
                review_root=str(review_root),
                review_receipt_dir=str(review_root / "receipts"),
                data_dir=str(data_dir),
                verify_key=[rc.seed_to_public_key(seed).hex()],
                signing_seed=seed.hex(),
                reviewer_route="omp",
                reviewer_model="openai-codex/gpt-5.6-sol",
                reviewer_effort="high",
                reviewer_profile="openai-performance",
                reviewer_vendor="openai",
                execution_vendor="anthropic",
                review_timeout_s=60.0,
                reviewer_turn_timeout_s=30.0,
                reviewer_poll_interval_s=0.1,
                review_reject_grade=cr.DEFAULT_REJECT_GRADE,
            )

            captured = {}

            class FakeRunner:
                def __init__(self):
                    self.launched = []
                    self.resubmitted = []
                    self.cancelled = []

                def launch(self, spec):
                    self.launched.append(spec)
                    captured["spec"] = spec
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="w1:p2",
                        agent_name="reviewer",
                        launched_cwd=spec.worktree,
                        environment=spec.environment,
                    )

                def resubmit(self, handle, prompt_path, **kwargs):
                    self.resubmitted.append((handle, prompt_path, kwargs))
                    return handle

                def cancel(self, handle, deadline):
                    self.cancelled.append(handle)

                def agent_status(self, handle):
                    return "working"

            class CapturedWindow:
                def __init__(self, **kwargs):
                    self.launch = kwargs["launch"]
                    self.poll_report = kwargs["poll_report"]

            def stub_review_attempt(*, window_factory, **_kwargs):
                window = window_factory(None)
                window.launch()
                window.poll_report()
                return "reviewed"

            # B13's preflight resolves the model through omp's merged
            # catalog. Stubbing the catalog keeps this test about the launch
            # environment instead of about whichever models this machine has
            # registered.
            with (
                mock.patch.object(
                    maestro.agent_pi,
                    "catalog",
                    lambda: (("openai-codex", "gpt-5.6-sol", 400_000),),
                ),
                mock.patch.object(
                    maestro.agent_pi, "context_window", return_value=400000
                ),
                mock.patch.object(
                    maestro.finalization_window, "FinalizationWindow", CapturedWindow
                ),
                mock.patch.object(
                    maestro.code_review, "review_attempt", stub_review_attempt
                ),
            ):
                runner = FakeRunner()
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, runner)
                )
                review(None, a_node(), None, base_sha, output_sha)
                (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(repo), "add", "a.py"],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-qm", "output-two"],
                    check=True,
                    capture_output=True,
                )
                output_two = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                review(None, a_node(), None, base_sha, output_two)
                # A resumed durable dispatch owns its retained turn even if
                # the report is absent or still a parseable draft. It polls;
                # it does not prompt the same reviewer a second time.
                review(
                    None,
                    a_node(),
                    None,
                    base_sha,
                    output_two,
                    resume_existing_dispatch=True,
                )
                digest = cr.review_digest(
                    run_id="run-1",
                    node_id="build",
                    base_sha=base_sha,
                    output_sha=output_two,
                    rubric_version=cr.CODE_RUBRIC.version,
                )
                report = review_root / digest / "report.json"
                report.write_text("{}", encoding="utf-8")
                review(
                    None,
                    a_node(),
                    None,
                    base_sha,
                    output_two,
                    resume_existing_dispatch=True,
                )
                self.assertEqual(report.read_text(encoding="utf-8"), "{}")

            spec = captured["spec"]
            # The refusal this closes is computed from exactly these keys, so
            # the assertion is that `pane_env_flags` does not refuse.
            pane_env = _pane_env(lch.pane_env_flags(spec.environment))
            self.assertEqual(set(pane_env), set(lch.SCRATCH_ENV_KEYS))
            # The reviewer runs at the repository rather than in an attempt
            # worktree, so its byproducts must land under the run's own review
            # root — never in the repo, and never in some attempt's scratch.
            for key, value in pane_env.items():
                path = (
                    value.split("cache_dir=", 1)[-1]
                    if key == "PYTEST_ADDOPTS"
                    else value
                )
                self.assertTrue(Path(path).is_relative_to(review_root), key)
                self.assertFalse(Path(path).is_relative_to(repo), key)
            self.assertEqual(spec.worktree, Path(str(repo)))
            self.assertEqual(len(runner.launched), 1)
            self.assertEqual(len(runner.resubmitted), 1)
            self.assertEqual(runner.cancelled, [])
            self.assertEqual(
                runner.launched[0].session_dir, review_root / "build" / "session"
            )
            review.close("build")
            self.assertEqual(runner.cancelled, [runner.resubmitted[0][0]])

    def test_resume_replaces_only_an_absent_reviewer_and_adopts_a_complete_report(self):
        import argparse

        from adw_modules import launcher as lch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, base_sha, output_sha = self._repo_with_two_commits(root)
            data_dir = root / "sssf-data"
            data_dir.mkdir()
            seed = rc.generate_seed()
            review_root = root / "state" / "review"
            args = argparse.Namespace(
                run_id="run-1",
                repo=str(repo),
                review_root=str(review_root),
                review_receipt_dir=str(review_root / "receipts"),
                data_dir=str(data_dir),
                verify_key=[rc.seed_to_public_key(seed).hex()],
                signing_seed=seed.hex(),
                reviewer_route="omp",
                reviewer_model="openai-codex/gpt-5.6-sol",
                reviewer_effort="high",
                reviewer_profile="openai-performance",
                reviewer_vendor="openai",
                execution_vendor="anthropic",
                review_timeout_s=60.0,
                reviewer_turn_timeout_s=30.0,
                reviewer_poll_interval_s=0.1,
                review_reject_grade=cr.DEFAULT_REJECT_GRADE,
            )
            store = lc.LifecycleStore(root / "lifecycle.sqlite3")
            self.addCleanup(store.close)
            store.create_run("run-1", "digest", [a_node()])
            store.register_actor_session(
                "run-1",
                "build",
                "reviewer",
                generation=1,
                pane_id="old-pane",
                session_path=str(root / "old.jsonl"),
                correlation_token="old-token",
            )
            digest = cr.review_digest(
                run_id="run-1",
                node_id="build",
                base_sha=base_sha,
                output_sha=output_sha,
                rubric_version=cr.CODE_RUBRIC.version,
            )
            report = review_root / digest / "report.json"
            report.parent.mkdir(parents=True)
            draft = {
                "plan_digest": "d" * 64,
                "pair_count": 2,
                "cells": [
                    {
                        "check_id": "one",
                        "object_id": "diff",
                        "status": "clear",
                        "message": "",
                    }
                ],
            }
            report.write_text(json.dumps(draft), encoding="utf-8")

            class FakeRunner:
                def __init__(self, adoption):
                    self.adoption = adoption
                    self.adopted = []
                    self.retired = []
                    self.launched = []
                    self.resubmitted = []
                    self.actorless_closed = []

                def adopt(self, persisted):
                    self.adopted.append(persisted)
                    if self.adoption == "absent":
                        raise lch.HandleAbsent("HANDLE_ABSENT")
                    if self.adoption == "stale-placement":
                        raise lch.HandleAdoptionRefused("WORKSPACE_ID_MISMATCH")
                    return lch.LaunchHandle(
                        correlation_token=persisted.correlation_token,
                        pane_id=persisted.pane_id,
                        agent_name="reviewer",
                        launched_cwd=persisted.launched_cwd,
                        transcript_path=persisted.transcript_path,
                    )

                def retire_for_replacement(self, persisted, _deadline):
                    if self.adoption != "stale-placement":
                        raise AssertionError("only stale placement may be retired")
                    self.retired.append(persisted)

                def close_actorless_pane(self, persisted):
                    if self.adoption != "absent":
                        raise AssertionError("only an actorless pane may be closed")
                    self.actorless_closed.append(persisted)

                def launch(self, spec):
                    self.launched.append(spec)
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="replacement-pane",
                        agent_name="reviewer",
                        launched_cwd=spec.worktree,
                        transcript_path=root / "replacement.jsonl",
                        environment=spec.environment,
                    )

                def resubmit(self, handle, prompt_path, **kwargs):
                    self.resubmitted.append((handle, prompt_path, kwargs))

                def agent_status(self, _handle):
                    return "working"

                def cancel(self, _handle, _deadline):
                    raise AssertionError("replacement must remain active")

            class CapturedWindow:
                def __init__(self, **kwargs):
                    self.launch = kwargs["launch"]
                    self.poll_report = kwargs["poll_report"]

            def run_window(*, window_factory, **_kwargs):
                window = window_factory(None)
                window.launch()
                return window.poll_report()

            patches = (
                mock.patch.object(
                    maestro.agent_pi,
                    "catalog",
                    lambda: (("openai-codex", "gpt-5.6-sol", 400_000),),
                ),
                mock.patch.object(
                    maestro.agent_pi, "context_window", return_value=400_000
                ),
                mock.patch.object(
                    maestro.finalization_window, "FinalizationWindow", CapturedWindow
                ),
                mock.patch.object(maestro.code_review, "review_attempt", run_window),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                replacement = FakeRunner("absent")
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, replacement), store
                )
                self.assertIsNone(
                    review(
                        None,
                        a_node(),
                        SimpleNamespace(attempt_no=1),
                        base_sha,
                        output_sha,
                        resume_existing_dispatch=True,
                    )
                )
                self.assertEqual(len(replacement.launched), 1)
                self.assertEqual(replacement.resubmitted, [])
                self.assertEqual(len(replacement.actorless_closed), 1)

                stale = FakeRunner("stale-placement")
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, stale), store
                )
                self.assertIsNone(
                    review(
                        None,
                        a_node(),
                        SimpleNamespace(attempt_no=1),
                        base_sha,
                        output_sha,
                        resume_existing_dispatch=True,
                    )
                )
                self.assertEqual(len(stale.retired), 1)
                self.assertEqual(len(stale.launched), 1)
                self.assertEqual(stale.resubmitted, [])

                complete = dict(draft, pair_count=1)
                report.write_text(json.dumps(complete), encoding="utf-8")
                adopted = FakeRunner("present")
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, adopted), store
                )
                self.assertEqual(
                    review(
                        None,
                        a_node(),
                        SimpleNamespace(attempt_no=1),
                        base_sha,
                        output_sha,
                        resume_existing_dispatch=True,
                    ),
                    complete,
                )
                self.assertEqual(adopted.launched, [])
                self.assertEqual(adopted.resubmitted, [])

    def test_semantic_repair_recovers_builder_without_a_review_handoff(self):
        """A post-gate semantic repair has a candidate SHA but no rejection."""
        from adw_modules import launcher as lch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            integration = root / "integration"
            scratch = root / "scratch"
            repo.mkdir()
            integration.mkdir()
            scratch.mkdir()
            args = SimpleNamespace(
                plan_file=str(root / "plan.json"),
                db=str(root / "state.sqlite3"),
                run_id="run-1",
                integration_path=str(integration),
                repo=str(repo),
                data_dir=str(root / "data"),
                receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(scratch),
                digest="d" * 64,
                agent_route="omp",
                agent_model="model",
                agent_effort="high",
                agent_profile="profile",
                concurrency=None,
                restrict_actor_tools=False,
            )
            node = SimpleNamespace(node_id="build", kind=st.NodeKind.AGENT, needs=())
            plan = SimpleNamespace(
                title="plan",
                agent_nodes=(node,),
                tests_nodes=(),
                merge_policy=SimpleNamespace(
                    integration_branch="main",
                    integration_gate=SimpleNamespace(min_cases=1),
                ),
                to_plan_nodes=lambda: (),
            )
            captured = {}
            store = mock.Mock()
            old_session = SimpleNamespace(
                generation=1,
                pane_id="old-pane",
                session_path=str(root / "old.jsonl"),
                correlation_token="run-1-build-builder-g1",
            )
            new_session = SimpleNamespace(
                generation=2,
                pane_id="replacement-pane",
                session_path=str(root / "replacement.jsonl"),
                correlation_token="run-1-build-builder-g2",
            )
            store.current_actor_session.return_value = old_session
            store.repair_handoff.return_value = None
            store.recover_actor_session.return_value = SimpleNamespace(
                recovered=True, session=new_session
            )

            class FakeRunner:
                def __init__(self):
                    self.launched = []

                def adopt(self, _persisted):
                    raise lch.HandleAbsent("HANDLE_ABSENT")

                def launch(self, spec):
                    self.launched.append(spec)
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="replacement-pane",
                        agent_name="builder",
                        launched_cwd=spec.worktree,
                        transcript_path=root / "replacement.jsonl",
                        environment=spec.environment,
                    )

                def cancel(self, _handle, _deadline):
                    raise AssertionError("recovery must retain its replacement")

                def provision(self, _worktree):
                    return None

            class NoProgress:
                def __init__(self, *_args, **_kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            class CapturingScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    captured["deps"] = deps

                def project(self):
                    return None

                def run(self):
                    return SimpleNamespace(
                        outcome=SimpleNamespace(value="ACCEPTED"),
                        merged=(),
                        blocked=(),
                        review_findings={},
                    )

            execution = SimpleNamespace(ok=True)
            runner = FakeRunner()
            with (
                mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
                ),
                mock.patch.object(maestro, "_load_runnable_plan", return_value=plan),
                mock.patch.object(maestro, "_refuse_cross_run_node_budget"),
                mock.patch.object(maestro, "_validate_run_paths"),
                mock.patch.object(maestro, "_resolve_run_runners", return_value={}),
                mock.patch.object(maestro, "_runtime_launcher", return_value=runner),
                mock.patch.object(
                    maestro, "_refuse_base_commit_divergence", return_value=None
                ),
                mock.patch.object(
                    maestro, "_refuse_uncommittable_outputs", return_value=None
                ),
                mock.patch.object(maestro, "_RunProgress", NoProgress),
                mock.patch.object(maestro.lc, "LifecycleStore", return_value=store),
                mock.patch.object(maestro.scheduler, "Scheduler", CapturingScheduler),
                mock.patch.object(
                    maestro, "_poll_agent_execution", return_value=execution
                ),
                mock.patch.object(maestro, "_route_context_window", return_value=None),
                mock.patch.object(maestro.worktree, "launch_env", return_value={}),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(maestro._execute_run(args, resuming=False), 0)
                    attempt = SimpleNamespace(path=repo, scratch=scratch, repo=repo)
                    repaired = captured["deps"].continue_node(
                        attempt,
                        a_node(),
                        SimpleNamespace(attempt_no=1),
                        "repair the gate failure",
                        "a" * 40,
                        1,
                        lambda: False,
                    )
            self.assertIs(repaired.execution, execution)
            store.recover_actor_session.assert_called_once()
            store.recover_builder_handoff.assert_not_called()
            store.mark_handoff_submitted.assert_not_called()
            self.assertEqual(len(runner.launched), 1)

    def test_closed_reviewer_generation_is_not_reused(self):
        import argparse

        from adw_modules import launcher as lch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, base_sha, output_sha = self._repo_with_two_commits(root)
            data_dir = root / "sssf-data"
            data_dir.mkdir()
            seed = rc.generate_seed()
            review_root = root / "state" / "review"
            args = argparse.Namespace(
                run_id="run-1",
                repo=str(repo),
                review_root=str(review_root),
                review_receipt_dir=str(review_root / "receipts"),
                data_dir=str(data_dir),
                verify_key=[rc.seed_to_public_key(seed).hex()],
                signing_seed=seed.hex(),
                reviewer_route="omp",
                reviewer_model="openai-codex/gpt-5.6-sol",
                reviewer_effort="high",
                reviewer_profile="openai-performance",
                reviewer_vendor="openai",
                execution_vendor="anthropic",
                review_timeout_s=60.0,
                reviewer_turn_timeout_s=30.0,
                reviewer_poll_interval_s=0.1,
                review_reject_grade=cr.DEFAULT_REJECT_GRADE,
            )
            store = lc.LifecycleStore(root / "lifecycle.sqlite3")
            store.create_run("run-1", "digest", [a_node()])
            store.register_actor_session(
                "run-1",
                "build",
                "reviewer",
                generation=1,
                pane_id="old-pane",
                session_path="/old/session",
                correlation_token="old-token",
            )
            store.close_actor_session("run-1", "build", "reviewer", generation=1)
            launched = []

            class FakeRunner:
                def launch(self, spec):
                    launched.append(spec)
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token,
                        pane_id="new-pane",
                        agent_name="reviewer",
                        launched_cwd=spec.worktree,
                        transcript_path=root / "new-session.jsonl",
                        environment=spec.environment,
                    )

                def agent_status(self, _handle):
                    return "working"

            class CapturedWindow:
                def __init__(self, **kwargs):
                    self.launch = kwargs["launch"]

            def stub_review_attempt(*, window_factory, **_kwargs):
                window_factory(None).launch()
                return "reviewed"

            with (
                mock.patch.object(
                    maestro.agent_pi,
                    "catalog",
                    lambda: (("openai-codex", "gpt-5.6-sol", 400_000),),
                ),
                mock.patch.object(
                    maestro.agent_pi, "context_window", return_value=400_000
                ),
                mock.patch.object(
                    maestro.finalization_window, "FinalizationWindow", CapturedWindow
                ),
                mock.patch.object(
                    maestro.code_review, "review_attempt", stub_review_attempt
                ),
            ):
                review = maestro._code_review_runner(
                    args, cast(lch.HerdrLauncher, FakeRunner()), store
                )
                review(
                    None, a_node(), SimpleNamespace(attempt_no=1), base_sha, output_sha
                )

            self.assertEqual(launched[0].attempt_no, 2)
            sessions = store.actor_sessions("run-1", "build", actor_role="reviewer")
            self.assertEqual(
                [(session.generation, session.state) for session in sessions],
                [(1, st.ActorSessionState.CLOSED), (2, st.ActorSessionState.ACTIVE)],
            )
            store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
