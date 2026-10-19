"""An envelope-shape refusal never ends the attend verb on a correctable answer.

The operator's first live answer carried a valid revision, a valid rationale,
and one `runtime_defects` entry with a `detail` and no `lane`. The loop
refused the whole envelope with `ATTEND_RUNTIME_DEFECT_INVALID: runtime_defects[0]
names no lane` and the run exited, discarding the revision. Pinned here:

- in a one-lane batch, a defect entry naming no lane names that lane;
- a revision answering every parked lane survives a malformed or misplaced
  `runtime_defects`: a well-formed defect on an answered lane files its issue
  and the revision still moves the lane;
- every envelope-shape check -- runtime_defects, revision_path, rationales,
  the reviewer's verdict and findings -- is run before the agent's pane
  closes, and its exact refusal is asked back once in the same session;
- the operator's schema says `lane` is required.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

import maestro
from adw_modules import attend as att
from adw_modules import scheduler_types as st

from tests.test_attend_amendments import (
    LANE_FAULT,
    NO_PROGRESS,
    _Harness,
    _policy,
    _rationale,
)

DETAIL = "every reviewed run ends in SUITE_CRASHED after pytest's summary"


class _Answers(_Harness):
    """The harness, with the operator's envelope written by the case."""

    def __init__(self, tmp: Path, *, envelope, **kw: Any) -> None:
        super().__init__(tmp, **kw)
        self.envelope = envelope

    def dispatch(self, request: att.OperatorRequest) -> Mapping[str, Any]:
        self.dispatched.append(request)
        return self.envelope(request)


def _revision(request: att.OperatorRequest, **extra: Any) -> dict:
    Path(request.revision_out_path).write_text("{}", encoding="utf-8")
    return {
        "revision_path": request.revision_out_path,
        "rationales": [_rationale(lane.lane_id) for lane in request.lanes],
        **extra,
    }


def _request(tmp: Path, *parked: tuple[str, str]) -> att.OperatorRequest:
    return _Harness(tmp, parked=dict(parked), policy=_policy()).request_for(
        parked, SimpleNamespace(plan_revision=1, lanes=()), 1, (), ""
    )


class TheLiveEnvelope(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_a_valid_revision_survives_a_lane_less_defect_entry(self) -> None:
        """The shape that ended the run: the revision is applied, not refused."""
        harness = _Answers(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            envelope=lambda request: _revision(
                request, runtime_defects=[{"detail": DETAIL}]
            ),
        )
        said: list[str] = []
        outcome = harness.attend(say=lambda lane, message: said.append(message))
        self.assertIs(outcome.status, st.RunStatus.COMPLETE)
        self.assertEqual([item.lane_id for item in outcome.applied], ["lane-a-build"])
        self.assertEqual(outcome.escalated, ())
        # Defaulted to the one lane, which is a NO_PROGRESS park: reported.
        self.assertTrue(
            any(
                "runtime_defects ignored" in message
                and "lane-a-build is not a parked LANE_FAULT lane" in message
                for message in said
            ),
            said,
        )

    def test_a_lane_less_defect_in_a_one_lane_batch_names_that_lane(self) -> None:
        filed: list[tuple[str, str]] = []
        harness = _Answers(
            self.tmp,
            parked={"lane-b-build": LANE_FAULT},
            policy=_policy(),
            envelope=lambda request: {"runtime_defects": [{"detail": DETAIL}]},
            file_issue=lambda title, body: filed.append((title, body)) or {
                "status": att.ISSUE_CREATED, "url": "https://example.test/9"
            },
        )
        outcome = harness.attend()
        self.assertEqual([item.lane_id for item in outcome.escalated], ["lane-b-build"])
        self.assertEqual(outcome.escalated[0].detail, DETAIL)
        self.assertEqual(len(filed), 1)
        self.assertEqual(harness.applied, [])

    def test_a_defect_on_a_lane_the_revision_answers_files_and_still_amends(
        self,
    ) -> None:
        filed: list[tuple[str, str]] = []
        harness = _Answers(
            self.tmp,
            parked={"lane-b-build": LANE_FAULT},
            policy=_policy(),
            revision_lanes=("lane-b-build",),
            envelope=lambda request: _revision(
                request,
                runtime_defects=[{"lane": "lane-b-build", "detail": DETAIL}],
            ),
            file_issue=lambda title, body: filed.append((title, body)) or {
                "status": att.ISSUE_CREATED, "url": "https://example.test/9"
            },
        )
        outcome = harness.attend()
        self.assertEqual(len(filed), 1)
        self.assertIn(DETAIL, filed[0][1])
        self.assertEqual(outcome.escalated, ())
        self.assertEqual([item.lane_id for item in outcome.applied], ["lane-b-build"])
        self.assertEqual(harness.held, [(), ()])


class ReadOperatorEnvelope(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.request = _request(
            self.tmp, ("lane-a-build", NO_PROGRESS), ("lane-b-build", LANE_FAULT)
        )

    def test_a_lane_less_defect_in_a_multi_lane_batch_is_asked_back(self) -> None:
        envelope = _revision(self.request, runtime_defects=[{"detail": DETAIL}])
        with self.assertRaises(att.AttendRefused) as caught:
            att.read_operator_envelope(envelope, self.request, strict=True)
        self.assertEqual(caught.exception.code, att.DEFECT_INVALID)
        self.assertEqual(caught.exception.detail, "runtime_defects[0] names no lane")
        # After the one correction turn, the revision still stands.
        answer = att.read_operator_envelope(envelope, self.request, strict=False)
        self.assertEqual(answer.revision, Path(self.request.revision_out_path))
        self.assertEqual(answer.lane_ids, ("lane-a-build", "lane-b-build"))
        self.assertEqual(answer.ignored, "runtime_defects[0] names no lane")

    def test_with_no_revision_a_malformed_defect_is_still_refused(self) -> None:
        envelope = {"runtime_defects": [{"detail": DETAIL}]}
        with self.assertRaises(att.AttendRefused) as caught:
            att.read_operator_envelope(envelope, self.request, strict=False)
        self.assertEqual(caught.exception.code, att.DEFECT_INVALID)

    def test_a_declared_defect_holds_its_lane_and_the_rest_is_revised(self) -> None:
        Path(self.request.revision_out_path).write_text("{}", encoding="utf-8")
        envelope = {
            "revision_path": self.request.revision_out_path,
            "rationales": [_rationale("lane-a-build")],
            "runtime_defects": [{"lane": "lane-b-build", "detail": DETAIL}],
        }
        answer = att.read_operator_envelope(envelope, self.request, strict=True)
        self.assertEqual(answer.held, (("lane-b-build", DETAIL),))
        self.assertEqual(answer.lane_ids, ("lane-a-build",))
        self.assertEqual(answer.reported, ())


def _actor_ctx() -> SimpleNamespace:
    return SimpleNamespace(lane=SimpleNamespace(lane_id="lane-a-build"))


class SameSessionCorrection(unittest.TestCase):
    """The harness asks the agent that wrote a refused envelope once more."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        self.launches: list[Mapping[str, Any]] = []
        self.closed = 0

    def _dispatch(self, method: str, request: Any, answers: list) -> Mapping[str, Any]:
        def launch(ctx, role, cwd, extra, prepare_cwd, provision=True):
            prepare_cwd(cwd)
            self.launches.append(dict(extra))
            answer = answers.pop(0)
            return (answer(extra) if callable(answer) else answer), None, cwd

        def close(*_a, **_kw):
            self.closed += 1

        with (
            mock.patch.object(self.actor, "_role_dir", return_value=self.tmp / "role", create=True),
            mock.patch.object(self.actor, "_launch", side_effect=launch, create=True),
            mock.patch.object(self.actor, "_attend_checkout", create=True),
            mock.patch.object(self.actor, "_bind_checkout", create=True),
            mock.patch.object(self.actor, "_role_key", create=True),
            mock.patch.object(self.actor, "_close_attend_pane", side_effect=close, create=True),
        ):
            return getattr(self.actor, method)(_actor_ctx(), request)

    def _operator_request(self) -> att.OperatorRequest:
        (self.tmp / "current.ir.json").write_text("{}", encoding="utf-8")
        return _request(
            self.tmp, ("lane-a-build", NO_PROGRESS), ("lane-b-build", LANE_FAULT)
        )

    def test_a_refused_operator_envelope_is_asked_back_with_the_exact_error(
        self,
    ) -> None:
        request = self._operator_request()
        good = {
            "revision_path": request.revision_out_path,
            "rationales": [_rationale(lane.lane_id) for lane in request.lanes],
        }
        # Only the first turn writes the revision; the correction fixes the
        # envelope alone.
        payload = self._dispatch(
            "attend_operator",
            request,
            [lambda extra: _revision(request, runtime_defects=[{"detail": DETAIL}]), good],
        )
        self.assertEqual(len(self.launches), 2)
        self.assertNotIn("envelope_error", self.launches[0])
        self.assertEqual(
            self.launches[1]["envelope_error"],
            "ATTEND_RUNTIME_DEFECT_INVALID: runtime_defects[0] names no lane",
        )
        self.assertEqual(payload, good)
        # The revision the first turn wrote is not deleted by the correction.
        self.assertTrue(Path(request.revision_out_path).is_file())
        # One pane, closed once, after the correction turn.
        self.assertEqual(self.closed, 1)

    def test_every_operator_shape_refusal_is_asked_back(self) -> None:
        request = self._operator_request()
        cases = (
            ({"rationales": []}, "ATTEND_OPERATOR_NO_REVISION: envelope names no revision_path"),
            (
                {"revision_path": request.revision_out_path, "rationales": []},
                "ATTEND_OPERATOR_NO_REVISION: revision file was not written",
            ),
        )
        for envelope, error in cases:
            self.launches.clear()
            Path(request.revision_out_path).unlink(missing_ok=True)
            self._dispatch("attend_operator", request, [envelope, envelope])
            self.assertEqual(self.launches[1]["envelope_error"], error)
        # A rationale gap: the revision exists, one lane is owed a rationale.
        self.launches.clear()
        def partial(extra):
            envelope = _revision(request)
            envelope["rationales"] = envelope["rationales"][:1]
            return envelope

        self._dispatch("attend_operator", request, [partial, partial])
        self.assertEqual(
            self.launches[1]["envelope_error"],
            "ATTEND_OPERATOR_NO_REVISION: no rationale for parked lane lane-b-build",
        )

    def test_a_valid_operator_envelope_is_not_asked_again(self) -> None:
        request = self._operator_request()
        self._dispatch("attend_operator", request, [lambda extra: _revision(request)])
        self.assertEqual(len(self.launches), 1)

    def _review_request(self) -> att.ReviewRequest:
        revision = self.tmp / "r2.ir.json"
        revision.write_text('{"rev": 2}', encoding="utf-8")
        return att.ReviewRequest(
            run_id="run-1", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=_request(self.tmp, ("lane-a-build", NO_PROGRESS)).lanes,
            current_ir_path=str(revision), revision_ir_path=str(revision),
            question_surface="{}", rationales=(),
            findings_out_path=str(self.tmp / "out" / "findings.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-a-build",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )

    def test_a_refused_reviewer_verdict_is_asked_back_with_the_exact_error(
        self,
    ) -> None:
        request = self._review_request()

        def passes(extra):
            out = Path(extra["findings_out_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"findings": []}), encoding="utf-8")
            return {"verdict": "PASS", "findings": []}

        payload = self._dispatch(
            "attend_reviewer", request, [{"verdict": "MAYBE", "findings": []}, passes]
        )
        self.assertEqual(
            self.launches[1]["envelope_error"], "ATTEND_REVIEW_INVALID: verdict is 'MAYBE'"
        )
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(self.closed, 1)

    def test_a_pass_with_no_findings_file_is_asked_back(self) -> None:
        request = self._review_request()
        answer = {"verdict": "PASS", "findings": []}
        self._dispatch("attend_reviewer", request, [answer, answer])
        self.assertIn("ATTEND_REVIEW_INVALID: review findings", self.launches[1]["envelope_error"])


class OperatorSchema(unittest.TestCase):
    def test_runtime_defects_lane_is_required(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        entry = actor._schema("operator")["runtime_defects"][0]
        self.assertTrue(entry["lane"].startswith("<required"), entry)

    def test_the_role_contract_states_the_defect_shape(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.target = type("T", (), {"target_repository_root": holder.name})()
        text = actor._materialize_role_instructions(
            Path(holder.name), "operator", "claude"
        ).read_text()
        self.assertIn('{"lane": "<parked LANE_FAULT lane id>"', text)
        self.assertIn("`lane` is required", text)
        self.assertIn("`envelope_error`", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
