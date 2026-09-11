"""A REVISE finding must quote the contract the reviewer was handed.

`violated_requirement` was required and unread. On FDAdb run d246ae95,
lane-geo-corpus-tests TEST_REVIEW seq 35 named no clause of the public
contract and REVISEd anyway; the lane parked NO_PROGRESS after nine
amendments. Seq 29's finding is a paraphrase of the same contract, not a
substring -- the matcher does not loosen for it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "finding_cites_contract"
PUBLIC_CONTRACT = json.loads((FIXTURES / "public_contract.json").read_text())
REVIEW_29 = json.loads((FIXTURES / "test_review_29.json").read_text())
REVIEW_35 = json.loads((FIXTURES / "test_review_35.json").read_text())

CONTRACT_CLAUSE = "negative amounts are refused"
PUBLIC = {
    "acceptance_criteria": [CONTRACT_CLAUSE],
    "declared_outputs": ["services/api/app.py"],
}
PARAPHRASE_29 = (
    "The latest public lane acceptance expressly limits wording tests to "
    "S1-S5 exact substring presence and the specified occurrence count, "
    "and forbids additional semantic wording assertions."
)
PROBE_35 = (
    "The public corpus contract withholds private routes while allowing "
    "robots policy alongside. A grouped prohibition does not advertise "
    "its second object. Reviewer obligations require satisfiable legal "
    "positive controls as well as rejection across the prohibited "
    "ordering family; no public contract restricts policy to one route "
    "per clause."
)


def _digest(seed: str) -> str:
    return st.digest_bytes(seed.encode())


def _finding(requirement: str, **extra: str) -> dict[str, str]:
    row = {
        "implementation_area": "services/api/app.py",
        "observed_behavior": "negative amounts are accepted",
        "required_behavior": "reject amounts below zero",
        "violated_requirement": requirement,
    }
    row.update(extra)
    return row


def _cite_error(requirement: str) -> str:
    return "REVISE finding does not cite the contract: {0}".format(requirement[:80])


def _bind(findings, contract_text: str):
    return st.bind_findings_to_contract(
        findings,
        contract_text=contract_text,
        harness_keys=sch.HARNESS_VIOLATED_REQUIREMENTS,
    )


class BindFindingsToContract(unittest.TestCase):
    def test_a_verbatim_quote_passes(self) -> None:
        bound = _bind((_finding(CONTRACT_CLAUSE),), CONTRACT_CLAUSE)
        self.assertEqual(bound[0]["violated_requirement"], CONTRACT_CLAUSE)

    def test_whitespace_quotes_and_case_are_normalized(self) -> None:
        quoted = '  `"Negative   amounts ARE refused"`  '
        bound = _bind((_finding(quoted),), CONTRACT_CLAUSE)
        self.assertEqual(bound[0]["violated_requirement"], quoted)

    def test_a_short_quote_is_refused(self) -> None:
        short = "x" * 11
        with self.assertRaises(st.CanonicalIdentityError) as raised:
            _bind((_finding(short),), short * 2)
        self.assertIn(_cite_error(short), str(raised.exception))

    def test_seq_29_paraphrase_does_not_match_the_contract(self) -> None:
        """Astra paraphrased. Do not loosen the matcher to accept this."""
        contract = sch._public_contract_text(PUBLIC_CONTRACT)
        for finding in REVIEW_29["findings"]:
            with self.subTest(requirement=finding["violated_requirement"][:40]):
                with self.assertRaises(st.CanonicalIdentityError) as raised:
                    _bind((finding,), contract)
                self.assertIn(
                    _cite_error(finding["violated_requirement"]),
                    str(raised.exception),
                )
        self.assertTrue(
            REVIEW_29["findings"][1]["violated_requirement"].startswith(
                PARAPHRASE_29
            )
        )

    def test_seq_35_probe_does_not_match_the_contract(self) -> None:
        contract = sch._public_contract_text(PUBLIC_CONTRACT)
        finding = REVIEW_35["findings"][0]
        self.assertEqual(finding["violated_requirement"], PROBE_35)
        with self.assertRaises(st.CanonicalIdentityError) as raised:
            _bind((finding,), contract)
        self.assertIn(_cite_error(PROBE_35), str(raised.exception))

    def test_a_harness_key_passes_without_quoting_the_contract(self) -> None:
        for key in (
            "gate collection",
            "gate.required_cases",
            "gate.min_cases",
            cr._RUNNER_REVISE["violated_requirement"],
            cr._COLLECTION_REVISE["violated_requirement"],
            cr._INTEGRATION_GATE_REVISE["violated_requirement"],
        ):
            with self.subTest(key=key):
                bound = _bind((_finding(key),), "")
                self.assertEqual(bound[0]["violated_requirement"], key)

    def test_a_non_spec_axis_passes_without_quoting_the_contract(self) -> None:
        finding = _finding("possible Duplicated Code", axis=st.FINDING_AXIS_STANDARDS)
        bound = _bind((finding,), "")
        self.assertEqual(bound[0]["violated_requirement"], "possible Duplicated Code")

    def test_a_spec_axis_still_must_quote(self) -> None:
        finding = _finding("not in the contract at all here", axis=st.FINDING_AXIS_SPEC)
        with self.assertRaises(st.CanonicalIdentityError):
            _bind((finding,), CONTRACT_CLAUSE)

    def test_private_keys_are_still_refused(self) -> None:
        finding = _finding(CONTRACT_CLAUSE)
        finding["fixture"] = "secret"
        bound = _bind((finding,), CONTRACT_CLAUSE)
        self.assertEqual(bound[0]["fixture"], "secret")
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings((finding,))
        with self.assertRaises(st.CanonicalIdentityError) as raised:
            st._reject_private_keys({"fixture": "secret"})
        self.assertIn("private field refused", str(raised.exception))


class RubricNamesTheCheck(unittest.TestCase):
    def test_test_reviewer_and_code_reviewer_are_told(self) -> None:
        sentence = (
            "The violated_requirement field is checked mechanically and "
            "must quote the public contract verbatim"
        )
        self.assertIn(sentence, maestro.TEST_CRAFT_REVIEWER_QUESTION)
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("checked mechanically"), 2)


class _EmptyStore:
    class _Conn:
        def execute(self, *_args):
            return iter(())

    def __init__(self):
        self.conn = self._Conn()

    def lane_stage(self, _run_id, _lane_id):
        return st.LaneStage.BUILDING


class _ScriptedReviewer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[str] = []

    def review_tests(self, ctx):
        del ctx
        self.seen.append("tests")
        return self.replies.pop(0)

    def review_code(self, ctx):
        del ctx
        self.seen.append("code")
        return self.replies.pop(0)

    def review_integration(self, ctx, lanes, integration):
        del ctx, lanes, integration
        self.seen.append("final")
        return self.replies.pop(0)


def _green_measurement():
    return sch.cr.SealedMeasurement(
        summary={
            "errored": 0,
            "executed": 1,
            "failed": 0,
            "passed": 1,
            "skipped": 0,
        },
        runner_failed=False,
        collection_broken=False,
        min_cases=1,
        run={},
        files={},
        vault=Path("/state/vault"),
    )


def _drive_reviewing_tests(actor):
    scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run1"
    scheduler.store = _EmptyStore()
    scheduler.runtime = SimpleNamespace(path=Path("/state"))
    scheduler.actor = actor
    lane = SimpleNamespace(
        lane_id="lane-a",
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        public_acceptance=(CONTRACT_CLAUSE,),
        declared_outputs=("tests/test_refund.py",),
        lane_kind=st.LANE_KIND_TESTS,
        needs=(),
    )
    row = {"plan_revision": 1, "plan_digest": _digest("plan")}
    plan = SimpleNamespace(artifact_id="plan-1")
    draft = SimpleNamespace(artifact_id="draft-1", payload={"public_contract": PUBLIC})
    scheduler._common = lambda lane_id: (row, lane)
    scheduler._plan_artifact_ref = lambda row_arg: "plan:ref"
    completed: list[object] = []
    blocked: list[str] = []
    scheduler._block_if_stalled = lambda lane_id: blocked.append(lane_id)

    def latest(store, run_id, lane_id, kind, verdict=None):
        del store, run_id, lane_id, verdict
        if kind is st.ArtifactKind.LANE_PLAN:
            return plan
        return draft

    with mock.patch.object(sch, "_latest", side_effect=latest), mock.patch.object(
        scheduler, "_measure_draft_gate", return_value=(None, None)
    ), mock.patch.object(
        sch, "_record_as_lane_artifact", side_effect=lambda art, lane_arg: art
    ), mock.patch.object(
        sch.tc, "draft_private_tokens", return_value=()
    ), mock.patch.object(
        sch.tc,
        "review_test_draft",
        side_effect=lambda **kwargs: SimpleNamespace(verdict=kwargs["verdict"]),
    ), mock.patch.object(
        sch, "_with_input_artifact_ids", side_effect=lambda art, ids: art
    ), mock.patch.object(
        sch, "_complete", side_effect=lambda store, ctx, art: completed.append(art)
    ):
        scheduler._reviewing_tests("lane-a")
    return completed, blocked


def _drive_reviewing_code(actor):
    scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run1"
    scheduler.store = _EmptyStore()
    scheduler._provision_argv = ()
    scheduler._provision_timeout_s = 1800.0
    scheduler.runtime = SimpleNamespace(path=Path("/state"))
    scheduler.target = SimpleNamespace(target_repository_root="/repo")
    scheduler.actor = actor
    lane = SimpleNamespace(
        lane_id="lane-a",
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        public_acceptance=(CONTRACT_CLAUSE,),
        declared_outputs=("services/api/app.py",),
        lane_kind=st.LANE_KIND_BUILD,
        needs=(),
    )
    row = {"plan_revision": 1, "plan_digest": _digest("plan")}
    artifact = SimpleNamespace(
        artifact_id="art-1",
        payload={
            "builder_base_sha": "1" * 40,
            "candidate_ref": st.candidate_ref("run1", "lane-a", _digest("b")),
            "candidate_sha": "2" * 40,
            "sealed_digest": "3" * 64,
        },
    )
    scheduler._common = lambda lane_id: (row, lane)
    scheduler._sealed_for = lambda lane_arg: artifact
    scheduler._plan_artifact_ref = lambda row_arg: "plan:ref"
    scheduler._sealed_suite_gate = lambda lane_arg: None
    completed: list[object] = []
    blocked: list[str] = []
    scheduler._block_if_stalled = lambda lane_id: blocked.append(lane_id)
    with mock.patch.object(sch, "_latest", return_value=artifact), mock.patch.object(
        sch, "_record_as_lane_artifact", return_value=None
    ), mock.patch.object(
        sch, "_with_input_artifact_ids", side_effect=lambda art, ids: art
    ), mock.patch.object(
        sch, "_complete", side_effect=lambda store, ctx, art: completed.append(art)
    ), mock.patch.object(
        sch.cr, "measure_candidate", return_value=_green_measurement()
    ), mock.patch.object(
        sch.cr,
        "review_builder_output",
        side_effect=lambda **kwargs: SimpleNamespace(verdict=kwargs["verdict"]),
    ):
        scheduler._reviewing_code("lane-a")
    return completed, blocked


def _drive_final_review(actor):
    spec_digest = st.digest_canonical({"spec": "lane-a"})
    outputs = ("lane-a.py",)
    lane = st.LaneProjection(
        lane_id="lane-a",
        needs=(),
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=st.lane_projection_digest(
            spec_digest, (), outputs, lane_kind=st.LANE_KIND_BUILD
        ),
        public_acceptance=(CONTRACT_CLAUSE,),
        lane_kind=st.LANE_KIND_BUILD,
    )
    recorded: dict[str, object] = {}

    class Store:
        def active_final_review_fingerprint(self, run_id, integration):
            del run_id, integration
            return "b" * 64

        def active_projection(self, run_id):
            del run_id
            return (lane,)

        def complete_final_review(
            self, run_id, review_fingerprint, integration, observed, artifact, affected
        ):
            del run_id, review_fingerprint, integration, observed
            recorded["payload"] = artifact.payload
            recorded["affected"] = tuple(affected)

    class Locks:
        def acquire(self, level):
            del level

        def release(self):
            return None

    class Git:
        def rev_parse(self, ref):
            del ref
            return "c" * 40

    class Target:
        target_repository_root = "/nonexistent"

        def git(self):
            return Git()

    scheduler = object.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run-1"
    scheduler.store = Store()
    scheduler.locks = Locks()
    scheduler.target = Target()
    scheduler.actor = actor
    scheduler._compiled = None
    scheduler._integration_head = lambda: "a" * 40
    scheduler._plan_artifact_ref = lambda row: "plan.v1"
    scheduler._failed_run_gates = lambda lanes, h, f: ()
    real_run_row = sch.run_row
    sch.run_row = lambda store, run_id: {  # type: ignore[assignment]
        "plan_revision": 1,
        "plan_digest": "d" * 64,
        "target_main_ref": "refs/heads/main",
    }
    try:
        sch.FactoryScheduler._final_review(scheduler)
    finally:
        sch.run_row = real_run_row  # type: ignore[assignment]
    return recorded


class ReviewKindSites(unittest.TestCase):
    def test_a_rejected_test_review_reasks_and_does_not_park(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
                (st.ReviewerVerdict.PASS, ()),
            ]
        )
        completed, blocked = _drive_reviewing_tests(actor)
        self.assertEqual(actor.seen, ["tests", "tests"])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(blocked, [])

    def test_a_rejected_code_review_reasks_and_does_not_park(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
                (st.ReviewerVerdict.PASS, ()),
            ]
        )
        completed, blocked = _drive_reviewing_code(actor)
        self.assertEqual(actor.seen, ["code", "code"])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(blocked, [])

    def test_a_rejected_final_review_reasks_and_does_not_park(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),), ("lane-a",)),
                (st.ReviewerVerdict.PASS, (), ()),
            ]
        )
        recorded = _drive_final_review(actor)
        self.assertEqual(actor.seen, ["final", "final"])
        self.assertEqual(recorded["payload"]["verdict"], st.ReviewerVerdict.PASS.value)
        self.assertEqual(recorded["affected"], ())

    def test_a_twice_uncited_test_review_refuses_typed(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
            ]
        )
        with self.assertRaises(sch.ReviewFindingUncited) as raised:
            _drive_reviewing_tests(actor)
        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "REVIEW_FINDING_UNCITED")
        self.assertTrue(
            message.startswith("REVIEW_FINDING_UNCITED:lane-a:test-reviewer:")
        )
        self.assertIn(PROBE_35[:120], message)

    def test_a_twice_uncited_code_review_refuses_typed(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),)),
            ]
        )
        with self.assertRaises(sch.ReviewFindingUncited) as raised:
            _drive_reviewing_code(actor)
        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "REVIEW_FINDING_UNCITED")
        self.assertTrue(
            message.startswith("REVIEW_FINDING_UNCITED:lane-a:code-reviewer:")
        )
        self.assertIn(PROBE_35[:120], message)

    def test_a_twice_uncited_final_review_refuses_typed(self) -> None:
        actor = _ScriptedReviewer(
            [
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),), ("lane-a",)),
                (st.ReviewerVerdict.REVISE, (_finding(PROBE_35),), ("lane-a",)),
            ]
        )
        with self.assertRaises(sch.ReviewFindingUncited) as raised:
            _drive_final_review(actor)
        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "REVIEW_FINDING_UNCITED")
        self.assertTrue(
            message.startswith("REVIEW_FINDING_UNCITED:RUN:integration-reviewer:")
        )
        self.assertIn(PROBE_35[:120], message)


    def test_a_cited_test_review_is_not_reasked(self) -> None:
        actor = _ScriptedReviewer(
            [(st.ReviewerVerdict.REVISE, (_finding(CONTRACT_CLAUSE),))]
        )
        completed, blocked = _drive_reviewing_tests(actor)
        self.assertEqual(actor.seen, ["tests"])
        self.assertEqual(len(completed), 1)
        self.assertEqual(blocked, ["lane-a"])


if __name__ == "__main__":
    unittest.main()
