"""§19 M49 at the one settle site it did not reach: a review rejection.

`_lane_refusal_repeats` and `IDENTICAL_REFUSAL_LIMIT` stop a node that
produces the identical typed refusal twice, and both verification settle
sites consult them. `_publish_and_review_candidate` did not. What it kept
instead was `self._review_convergence`, a list of finding *counts* per node
whose only reader put it in the run report — a measurement of non-convergence
that nothing acted on.

So a reviewer answering byte-identically every round sent the lane back to
its builder with a repair prompt assembled from findings that had already
failed, until `review_ceiling` ran out and the node blocked
`REVIEW_BUDGET_EXHAUSTED` — a reason that sends the operator to grant more
attempts, which is exactly what was not the problem. This is the shape M49
convicts, and the tests-node repair loop going live (`82d46d3`) is what made
`lane-wp6-tests` able to walk into it.

What each case settles:

  P1  two byte-identical rejections stop the lane where six were available,
      and the reason names the convergence failure rather than a budget
  P2  the block payload carries the typed refusal, its full text, and the
      count of consecutive occurrences — what an operator decides from
  P3  the control: a reviewer whose findings actually change is untouched and
      keeps its whole budget. Without this, "stop on the second rejection"
      would be indistinguishable from "collapse every honest repair loop to
      two attempts", which is the hazard §7.5 names when it refuses identity
      to a coarse refusal
  P4  the sha-bound half of a finding is not in the identity. `review_objects`
      names the diff object `diff:<output_sha>`, so the `object_id` of every
      `diff.*` finding — which is most of the blocking ones — differs by
      construction on every round. Keyed on the raw record, the check could
      never fire on the findings that matter
  P5  the grade is compared by its declared value. The rejection hands the
      scheduler the enum; the same row read back from the ledger is its JSON
      string. Two spellings of one grade make every comparison across that
      boundary unequal, and the check never fires
  P6  `_review_convergence` is gone. The report field it fed is projected
      from `candidate_reviews`, which already held the fact — one
      representation, not two kept in step by copying

P1–P4 and P6 drive the production `Scheduler` through the real review stage:
the real applicability matrix, the real report verification, the real graded
derivation, the real signed receipt. Only the reviewer's answers are
scripted, and no `continue_node` fake accepts what production would refuse —
the repair seam here writes a genuinely different tree on every round,
because a repair that wrote the same bytes would end the loop as an empty
delta (#113) and prove nothing about review convergence.
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path
from typing import Dict

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_graded_findings import ScriptedReviewer  # noqa: E402
from test_review_scope_bounding import (  # noqa: E402
    LAZY_MESSAGE, LAZY_RATIONALE, TheReviewLaneFixture)


CHECK = "diff.implements_the_stated_instruction"


class TurningReviewer(ScriptedReviewer):
    """Answers the same check, saying something different every round.

    The honest repair loop: the reviewer is still refusing, but it is refusing
    *about a different thing*, which is what a builder making progress
    produces. Nothing here may stop that lane early.
    """

    def report_for(self, matrix):
        self.findings = {
            CHECK: (
                "error",
                "{0} (round {1})".format(LAZY_MESSAGE, len(self.reports) + 1),
                LAZY_RATIONALE,
                "in_scope",
            )
        }
        return super().report_for(matrix)


class TheRepeatedRejectionFixture(TheReviewLaneFixture):
    """One reviewed lane with room to loop, and a repair that really repairs.

    Both ceilings are lifted to 6 so that neither budget can be what stops the
    lane; whatever stops it, stops it on its own merits. `continue_node`
    writes fresh bytes per round so each repair publishes a genuinely new
    candidate — the fixture default rewrites the same content, which the empty
    delta guard ends after one round and which would hide the ceiling this
    case is about.
    """

    def config(self, **kw):
        kw.setdefault("review_ceiling", 6)
        kw.setdefault("semantic_ceiling", 6)
        return super().config(**kw)

    def setUp(self) -> None:
        super().setUp()
        self.deltas = itertools.count(1)
        self.repairs = []  # one entry per repair prompt actually dispatched

    def repair(self, attempt, node, record, prompt, rejected_sha, generation, _cancel):
        self.repairs.append(prompt)
        (attempt.path / "build.py").write_text("ok-{0}\n".format(next(self.deltas)))
        return sch.RepairExecution(
            execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
            acknowledged_rejected_sha=rejected_sha,
            builder_generation=generation,
        )

    def run_lane(self, reviewer: ScriptedReviewer) -> Dict[str, Path]:
        ledgers: Dict[str, Path] = {}
        self.written["build"] = {"build.py": "ok\n"}
        self.scheduler = self.schedule(
            [self.agent("build")],
            deps=self.deps(
                continue_node=self.repair,
                review_attempt=self._review_stage(reviewer, ledgers),
            ),
        )
        self.report = self.scheduler.run()
        return ledgers

    def reviews(self):
        return self.store.candidate_reviews("run1", "build::review", limit=100)

    def block_detail(self):
        rows = [
            row
            for row in self.store.audit_transitions("run1", "build")
            if str(row.get("reason", "")).startswith("blocked:")
        ]
        self.assertEqual(1, len(rows))
        return rows[0]["detail"]

    def identical(self) -> ScriptedReviewer:
        return ScriptedReviewer({CHECK: ("error", LAZY_MESSAGE, LAZY_RATIONALE,
                                         "in_scope")})


# ── P1/P2: the stop, and what it tells the operator ─────────────────────────


class TheIdenticalRejectionStopsTheLaneTest(TheRepeatedRejectionFixture):
    """The defect, executed: a reviewer that says the same thing twice."""

    def test_the_second_identical_rejection_stops_instead_of_repairing(self):
        """Two rounds against a ceiling of six, and the truer reason.

        `REVIEW_BUDGET_EXHAUSTED` would be a false statement about this lane:
        no budget ran out, and granting more attempts — the escape that reason
        names — buys six more rounds of the same refusal.
        """
        self.run_lane(self.identical())

        node = self.store.get_node("run1", "build")
        self.assertEqual(st.NodeState.BLOCKED.value, self.states()["build"])
        self.assertIs(st.BlockReason.SEMANTIC_REFUSAL_REPEATED, node.block_reason)
        self.assertEqual(2, len(self.reviews()))
        self.assertNotIn("build", self.report.merged)

    def test_the_stopped_round_is_never_dispatched(self):
        """Stopping means the unchanged prompt is never sent, not that it is
        sent and then regretted. The first rejection is information and buys
        one repair; the second is proof, and buys nothing."""
        self.run_lane(self.identical())

        self.assertEqual(1, len(self.repairs))


class TheBlockNamesWhatRepeatedTest(TheRepeatedRejectionFixture):
    """§7.5's payload obligation, on this path: the operator's decision here is
    "change the plan or the tree" against "grant an attempt anyway", and both
    need the refusal itself, not a count."""

    def test_the_payload_carries_the_refusal_its_text_and_the_count(self):
        self.run_lane(self.identical())
        detail = self.block_detail()

        self.assertEqual("REVIEW_REJECTED", detail["refusal_code"])
        self.assertEqual(rp.IDENTICAL_REFUSAL_LIMIT, detail["identical_refusals"])
        self.assertIn(CHECK, detail["repeated_refusal"])
        self.assertIn(LAZY_MESSAGE, detail["repeated_refusal"])

    def test_the_payload_keeps_what_the_budget_block_already_carried(self):
        """The findings and the loop that failed are not dropped in exchange:
        the same record answers "which loop" and "what did it say"."""
        self.run_lane(self.identical())
        detail = self.block_detail()

        self.assertIs(
            st.BlockReason.SEMANTIC_REFUSAL_REPEATED,
            self.store.get_node("run1", "build").block_reason,
        )
        self.assertEqual(
            st.LaneRetryClass.REVIEW_REJECTION.value, detail["retry_class"]
        )
        self.assertTrue(detail["findings"])
        self.assertEqual(self.reviews()[-1].candidate_sha, detail["candidate_sha"])


# ── P3: the control — an honest loop keeps every attempt it was given ───────


class TheChangingRejectionKeepsItsBudgetTest(TheRepeatedRejectionFixture):
    """Without this the change is indistinguishable from "stop every review
    loop after two rounds", which would be a worse defect than the one it
    fixes."""

    def test_a_reviewer_whose_findings_change_runs_to_its_ceiling(self):
        self.run_lane(TurningReviewer({CHECK: ("error", LAZY_MESSAGE,
                                               LAZY_RATIONALE, "in_scope")}))

        node = self.store.get_node("run1", "build")
        self.assertEqual(6, len(self.reviews()))
        self.assertIs(st.BlockReason.REVIEW_BUDGET_EXHAUSTED, node.block_reason)
        self.assertIsNot(st.BlockReason.SEMANTIC_REFUSAL_REPEATED, node.block_reason)


# ── P4/P5: the two ways the identity silently never matches ─────────────────


class TheIdentityIgnoresWhatChangesByConstructionTest(TheRepeatedRejectionFixture):
    """Both halves of this were observed while building the check, and each one
    alone leaves it permanently inert while every test above still passes for
    the wrong reason."""

    def test_the_sha_bound_object_id_does_not_defeat_the_identity(self):
        """`review_objects` names the diff object `diff:<output_sha>`. Two
        rejections that said the identical thing therefore carry different
        `object_id`s, because the sha is the thing that changed."""
        self.run_lane(self.identical())
        first, second = self.reviews()

        self.assertNotEqual(first.candidate_sha, second.candidate_sha)
        self.assertTrue(
            any(first.candidate_sha in str(f.get("object_id", ""))
                for f in first.findings),
            "the sha-bound object id this case exists for is not in the record",
        )
        self.assertNotEqual(first.findings, second.findings)
        self.assertEqual(
            sch._review_refusal(first.findings, first.candidate_sha),
            sch._review_refusal(second.findings, second.candidate_sha),
        )

    def test_the_grade_is_compared_by_its_declared_value(self):
        """The rejection hands the scheduler the enum; the ledger hands the
        same row back as its JSON string. One grade, two spellings, and every
        comparison across that boundary is unequal."""
        sha = "a" * 40
        as_written = {"check_id": CHECK, "object_id": "file:build.py",
                      "grade": cr.FindingGrade.ERROR, "message": LAZY_MESSAGE}
        as_read = dict(as_written, grade=cr.FindingGrade.ERROR.value)

        # The two spellings, since the enum compares equal to its value and
        # hides the difference everywhere except where the renderer reads it.
        self.assertNotEqual(str(as_written["grade"]), str(as_read["grade"]))
        self.assertEqual(
            sch._review_refusal((as_written,), sha),
            sch._review_refusal((as_read,), sha),
        )

    def test_a_refusal_with_no_findings_is_still_an_identity(self):
        """`refusal_repetition` refuses an identity claim from a surface that
        stamped no code, so the code is stamped unconditionally: a rejection is
        a typed adjudication whether or not the graded cells came back."""
        self.assertEqual(
            "REVIEW_REJECTED", sch._review_refusal((), "a" * 40).refusal_code
        )


# ── P6: one representation of one fact ──────────────────────────────────────


class TheConvergenceSeriesIsProjectedNotAccumulatedTest(TheRepeatedRejectionFixture):
    """`_review_convergence` appended on every rejection, `project` rebuilt it
    from the store on resume, and the report read it. Two representations of
    what `candidate_reviews` already holds, agreeing only by copying."""

    def test_the_report_series_is_the_durable_projection(self):
        self.run_lane(TurningReviewer({CHECK: ("error", LAZY_MESSAGE,
                                               LAZY_RATIONALE, "in_scope")}))

        projected = rp.review_convergence_from_reviews(
            self.store.candidate_reviews("run1", limit=10_000)
        )
        self.assertEqual(
            {node_id: tuple(counts) for node_id, counts in projected.items()},
            self.report.review_convergence,
        )
        self.assertEqual(6, len(self.report.review_convergence["build"]))

    def test_the_scheduler_keeps_no_second_copy_of_it(self):
        """B15 in the other direction: the in-memory duplicate is not kept
        beside the projection "for the report", it is gone."""
        self.run_lane(self.identical())

        self.assertNotIn("_review_convergence", vars(self.scheduler))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
