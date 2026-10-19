"""Every failing accepted-suite case is ruled on, so a suite defect is never luck.

Run 2acd2f5f, lane-spend-build: the accepted suite executed 160 cases and one
failed, `test_p1_spend_ledger_15`, because it asserts a bare "c2" never appears
in a ledger whose settle hash contained "c2" by chance. The builder said so in
its reply, which nothing read. The code reviewer's REVISE carried three
candidate findings and none about that case, so the suite reopen (#333), which
fires only on a finding attributed to the accepted suite, never fired, and each
builder round was sent back against a test only a hash collision could pass.

These cases pin the repair:

  * the harness hands the reviewer the runner's own ids of every failing case,
    and a REVISE must name each in some finding's `test_id`; one unnamed id is
    asked back once, and a second miss parks `ReviewFailingTestUnattributed`;
  * a builder may claim `suite_defects` `[{test_id, reason}]`; the claim is
    recorded on BUILDER_OUTPUT, reaches the code reviewer's prompt, and must be
    ruled on the same way;
  * a failing case attributed to `accepted_suite` by `test_id` reopens the tests
    lane at once (see `test_suite_reopen`), with no `<path>:<line>` needed.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import code_review as cr
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from tests import test_reviewer_sees_suite_result as seen

import maestro

FAILING = "tests/test_spend.py::test_p1_spend_ledger_15"


def _finding(**extra):
    return dict(seen.LOCATED, **extra)


class FailingCaseIds(unittest.TestCase):
    def test_pytest_outcome_and_summary_lines_name_each_case_once(self):
        output = "\n".join(
            [
                "tests/test_spend.py::test_ok PASSED                       [ 33%]",
                FAILING + " FAILED                    [ 66%]",
                "tests/test_spend.py::test_fixture ERROR                  [100%]",
                "/x/tests/test_spend.py:40: AssertionError: assert 'c2' not in ...",
                "FAILED " + FAILING + " - AssertionError: assert 'c2' not in ...",
                "ERROR tests/test_spend.py::test_fixture - RuntimeError",
                "==== 1 failed, 1 passed, 1 error in 0.20s ====",
            ]
        )
        self.assertEqual(
            cr.failing_case_ids(output),
            (FAILING, "tests/test_spend.py::test_fixture"),
        )

    def test_vitest_fail_header_names_the_case_without_its_duration(self):
        output = "\n".join(
            [
                " \x1b[31mFAIL\x1b[39m  tests/a.test.ts > ledger > hides clip ids 12ms",
                " ✓ tests/a.test.ts > ledger > settles",
                " FAIL  tests/a.test.ts [ tests/a.test.ts ]",
            ]
        )
        self.assertEqual(
            cr.failing_case_ids(output), ("tests/a.test.ts > ledger > hides clip ids",)
        )

    def test_a_green_run_names_nothing(self):
        self.assertEqual(
            cr.failing_case_ids("tests/a.py::test_x PASSED\n1 passed in 0.1s"), ()
        )


class FailingCasesMustBeAttributed(unittest.TestCase):
    def _drive(self, replies, *, failing=(FAILING,), claims=(), green=False):
        actor = seen._RecordingActor(replies)
        scheduler, artifact = seen._scheduler(actor)
        if claims:
            artifact.payload["suite_defects"] = [dict(item) for item in claims]
        measurement = seen._measurement(failing_test_ids=tuple(failing))
        if green:
            measurement = seen._measurement(
                runner_failed=False,
                failing_test_ids=(),
                summary={"errored": 0, "executed": 2, "failed": 0, "passed": 2, "skipped": 0},
            )
        parked = mock.patch.object(
            scheduler,
            "_park_lane_fault",
            side_effect=lambda ctx, kind, detail, cause: (_ for _ in ()).throw(
                AssertionError("parked " + kind + " " + detail)
            ),
        )
        with parked as park:
            try:
                _measure, review = seen._drive(scheduler, artifact, measurement)
            except AssertionError as exc:
                if not str(exc).startswith("parked "):
                    raise
                return actor, None, park
        return actor, review, park

    def test_a_revise_that_skips_a_failing_case_is_asked_back_once(self):
        named = _finding(test_id=FAILING, attribution=st.FINDING_ATTRIBUTION_SUITE)
        actor, review, park = self._drive(
            [
                (st.ReviewerVerdict.REVISE, (_finding(),)),
                (st.ReviewerVerdict.REVISE, (_finding(), named)),
            ]
        )
        self.assertEqual(len(actor.seen), 2)
        self.assertEqual(actor.seen[0].failing_test_ids, (FAILING,))
        self.assertEqual(actor.seen[0].unattributed_tests, ())
        self.assertEqual(actor.seen[1].unattributed_tests, (FAILING,))
        self.assertEqual(review.call_args.kwargs["findings"], (_finding(), named))
        park.assert_not_called()

    def test_a_second_miss_parks_the_lane_typed(self):
        actor, review, park = self._drive([(st.ReviewerVerdict.REVISE, (_finding(),))])
        self.assertIsNone(review)
        self.assertEqual(len(actor.seen), 2)
        self.assertEqual(park.call_args.args[1], "ReviewFailingTestUnattributed")
        self.assertIn(FAILING, park.call_args.args[2])

    def test_a_revise_that_names_every_failing_case_is_not_asked_back(self):
        named = _finding(test_id=FAILING)
        actor, review, park = self._drive([(st.ReviewerVerdict.REVISE, (named,))])
        self.assertEqual(len(actor.seen), 1)
        park.assert_not_called()

    def test_a_builder_claim_reaches_the_reviewer_and_must_be_ruled_on(self):
        claim = {"test_id": "tests/test_spend.py::test_p1_spend_ledger_9", "reason": "x"}
        ruled = _finding(test_id=claim["test_id"], attribution=st.FINDING_ATTRIBUTION_CANDIDATE)
        actor, review, park = self._drive(
            [
                (st.ReviewerVerdict.REVISE, (_finding(test_id=FAILING),)),
                (st.ReviewerVerdict.REVISE, (_finding(test_id=FAILING), ruled)),
            ],
            claims=(claim,),
        )
        self.assertEqual(actor.seen[0].suite_defect_claims, (claim,))
        self.assertEqual(actor.seen[1].unattributed_tests, (claim["test_id"],))
        self.assertEqual(review.call_args.kwargs["suite_defect_claims"], (claim,))
        park.assert_not_called()

    def test_a_pass_over_a_green_suite_owes_no_ruling(self):
        claim = {"test_id": "tests/test_spend.py::test_p1_spend_ledger_9", "reason": "x"}
        actor, review, park = self._drive(
            [(st.ReviewerVerdict.PASS, ())], claims=(claim,), green=True
        )
        self.assertEqual(len(actor.seen), 1)
        park.assert_not_called()


class SuiteDefectClaims(unittest.TestCase):
    def test_claims_are_typed_or_refused(self):
        self.assertEqual(st.require_suite_defect_claims(None), ())
        self.assertEqual(
            st.require_suite_defect_claims([{"test_id": "a::b", "reason": "r"}]),
            ({"reason": "r", "test_id": "a::b"},),
        )
        for bad in ("a::b", [{"test_id": "a::b"}], [{"test_id": " ", "reason": "r"}]):
            with self.assertRaises(st.CanonicalIdentityError):
                st.require_suite_defect_claims(bad)

    def test_a_finding_test_id_survives_normalization(self):
        (row,) = st.require_revise_findings([_finding(test_id=FAILING)])
        self.assertEqual(row["test_id"], FAILING)
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings([_finding(test_id="")])


class TheClaimReachesTheReviewerPrompt(unittest.TestCase):
    """`review_code` assembles extra; `_prompt` renders it. Both are production."""

    def test_review_code_carries_failing_ids_and_claims_into_the_prompt(self):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        actor._roles = {}
        claim = {"test_id": FAILING, "reason": "the settle hash contains c2 by chance"}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(
                lane_id="lane-a",
                lane_kind=st.LANE_KIND_BUILD,
                declared_outputs=("services/api/app.py",),
            ),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.REVIEWING_CODE,
            candidate_sha="2" * 40,
            public_contract={},
            protected_test_paths=("tests/test_spend.py",),
            test_suite_digest="d",
            suite_result_summary={
                "errored": 0, "executed": 160, "failed": 1, "passed": 159, "skipped": 0
            },
            suite_findings_required=False,
            failing_test_ids=(FAILING,),
            suite_defect_claims=(claim,),
            unattributed_tests=(),
        )
        captured = {}

        def launch(ctx_arg, role, cwd, extra, **_kw):
            captured.update(extra)
            return {"verdict": "PASS", "findings": []}, None, Path("/tmp/cwd")

        with mock.patch.multiple(
            actor,
            _base_sha=mock.Mock(return_value="1" * 40),
            _role_key=mock.Mock(return_value=("k",)),
            _prepare=mock.Mock(return_value=(Path("/tmp/a"), Path("/tmp/cwd"))),
            _launch=launch,
            _with_fixtures=mock.Mock(return_value=None),
            _bind_checkout=mock.Mock(return_value=Path("/tmp/cwd")),
            _stash_probes=mock.Mock(),
        ):
            maestro.HerdrStageActor.review_code(actor, ctx)
        self.assertEqual(captured["failing_test_ids"], [FAILING])
        self.assertEqual(captured["suite_defect_claims"], [claim])
        text = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "code-reviewer",
            Path("/tmp/envelope.json"),
            Path("/tmp/cwd"),
            captured,
        )["instructions"]
        self.assertIn("must rule on every case in failing_test_ids", text)
        self.assertIn(FAILING + ": the settle hash contains c2 by chance", text)

    def test_the_builder_schema_offers_suite_defects(self):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        schema = maestro.HerdrStageActor._schema(actor, "builder")
        self.assertEqual(set(schema["suite_defects"][0]), {"test_id", "reason"})
        reviewer = maestro.HerdrStageActor._schema(actor, "code-reviewer")
        self.assertIn("test_id", reviewer["findings"][0])


if __name__ == "__main__":
    unittest.main()
