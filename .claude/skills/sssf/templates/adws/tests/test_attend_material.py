"""What an attended amendment leaves behind for a human: an issue and a file.

A material amendment changes something a reader of the plan relied on -- a
lane's outputs, dependencies or kind, a build lane's declared interface, a
gating obligation or one of its worked examples, or a lane that was already
merged. Whether one happened is computed from the two compiled plans and the
`PLAN_AMENDMENT`'s own resets, never from what the author says it did, so
every classifier case below compiles real plans with the real compiler.

The deviations file is rendered from ledger rows, so these cases write real
rows into a real `ArtifactStore` and render twice.
"""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import maestro
from adw_modules import attend as att
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.plan_compiler import compile_plan
from adw_modules.plan_model import SCHEMA_VERSION
from tests.test_artifact_store import RUN_ID, make_binding, make_lane, make_plan


_INTERFACE = {
    "kind": "callable",
    "module": "src/b.py",
    "name": "build_contract",
    "signature": {
        "parameters": [{"name": "record", "type": "Mapping"}],
        "returns": "dict",
    },
    "errors": ["ValueError"],
    "consumed_by": {"deferred_to": "a later lane wires build_contract in"},
}
_EXPECT = {"input": {"provenance": {"source": "a"}}, "expect": {"recorded": True}}
_EXPECT_2 = {"input": {"provenance": {"source": "b"}}, "expect": {"recorded": True}}
_REFUSES = {
    "input": {"provenance": {}},
    "refuses": {"error": "ValueError", "message": "provenance is empty"},
}
_RESTRICTION = {
    "polarity": "negative",
    "has_exception_ids": False,
    "has_preconditions": False,
    "external_store": False,
}


def _obligation(claim_id: str, examples: list) -> dict:
    return {
        "criterion": "{0} (negative): a record without provenance is refused".format(
            claim_id
        ),
        "gating": True,
        "observation_seam": "build_contract is the public export a case calls",
        "decided_by": examples,
        "restriction": _RESTRICTION,
    }


def _document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "lanes": [
            {
                "id": "lane-t",
                "lane_kind": "tests",
                "needs": [],
                "outputs": ["tests/test_b.py"],
                "spec": {"intent": "tests"},
                "acceptance": [
                    _obligation("claim-x", [_EXPECT, _EXPECT_2, _REFUSES]),
                    _obligation("claim-y", [_EXPECT, _REFUSES]),
                ],
            },
            {
                "id": "lane-b",
                "lane_kind": "build",
                "needs": ["lane-t"],
                "outputs": ["src/b.py"],
                "spec": {"intent": "build", "interface": [_INTERFACE]},
                "acceptance": ["lane-b produces src/b.py"],
            },
            {
                "id": "lane-c",
                "needs": [],
                "outputs": ["docs/c.md"],
                "spec": {"intent": "docs"},
                "acceptance": ["lane-c produces docs/c.md"],
            },
        ],
    }


def _compile(document: dict, revision: int) -> st.CompiledPlan:
    return compile_plan(
        json.dumps(document, sort_keys=True).encode("utf-8"), plan_revision=revision
    )


def _lane(document: dict, lane_id: str) -> dict:
    return next(lane for lane in document["lanes"] if lane["id"] == lane_id)


BEFORE = _compile(_document(), 1)


def _classify(edit, resets=()) -> att.MaterialChange:
    document = copy.deepcopy(_document())
    edit(document)
    return att.classify_material(BEFORE, _compile(document, 2), resets)


class MaterialClassifier(unittest.TestCase):
    """A6: each condition alone is material; a spec-text edit is not."""

    def test_a_spec_text_only_change_is_not_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-b")["spec"]["intent"] = "build the contract"

        result = _classify(edit)
        self.assertFalse(result.material, result.hits)
        self.assertEqual(result.hits, ())

    def test_changed_declared_outputs_are_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-c")["outputs"] = ["docs/c.md", "docs/d.md"]

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-c: declared outputs changed",))

    def test_changed_needs_are_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-c")["needs"] = ["lane-t"]

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-c: needs changed",))

    def test_a_changed_lane_kind_is_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-c")["lane_kind"] = "tests"

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-c: lane_kind changed",))

    def test_a_changed_build_interface_is_material(self) -> None:
        def edit(document: dict) -> None:
            interface = _lane(document, "lane-b")["spec"]["interface"][0]
            interface["signature"]["returns"] = "list"

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-b: spec.interface changed",))

    def test_a_removed_gating_obligation_is_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-t")["acceptance"] = [
                _obligation("claim-x", [_EXPECT, _EXPECT_2, _REFUSES])
            ]

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-t: gating obligation claim-y removed",))
        self.assertIn("lane-t: claim-y", result.obligations_touched)

    def test_a_lost_decided_by_example_is_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-t")["acceptance"][0] = _obligation(
                "claim-x", [_EXPECT, _REFUSES]
            )

        result = _classify(edit)
        self.assertTrue(result.material)
        self.assertEqual(
            result.hits,
            ("lane-t: gating obligation claim-x lost 1 decided_by example(s)",),
        )

    def test_an_added_example_is_touched_but_not_material(self) -> None:
        extra = {"input": {"provenance": {"source": "c"}}, "expect": {"recorded": True}}

        def edit(document: dict) -> None:
            _lane(document, "lane-t")["acceptance"][1] = _obligation(
                "claim-y", [_EXPECT, extra, _REFUSES]
            )

        result = _classify(edit)
        self.assertFalse(result.material, result.hits)
        self.assertEqual(result.obligations_touched, ("lane-t: claim-y",))

    def test_a_merged_lane_reset_is_material(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-b")["spec"]["intent"] = "build the contract"

        resets = (
            {"lane_id": "lane-b", "from_stage": "MERGED", "to_stage": "PLANNED"},
            {"lane_id": "lane-c", "from_stage": "MERGED", "to_stage": "MERGED"},
        )
        result = _classify(edit, resets)
        self.assertTrue(result.material)
        self.assertEqual(result.hits, ("lane-b: MERGED lane reset to PLANNED",))


def _gh(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class IssueFiling(unittest.TestCase):
    """A7. `gh` is stubbed: what is asserted is the argv Maestro hands it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.repo = Path(holder.name)
        self.title = att.issue_title("run-1", 3)

    def test_a_new_material_change_creates_exactly_one_issue(self) -> None:
        calls: list = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            if argv[1:3] == ["issue", "list"]:
                return _gh(stdout="[]")
            return _gh(stdout="https://example.invalid/issues/7\n")

        with mock.patch.object(maestro.subprocess, "run", side_effect=run):
            result = maestro._attend_file_issue(self.repo, self.title, "body")
        self.assertEqual(
            result, {"status": att.ISSUE_CREATED, "url": "https://example.invalid/issues/7"}
        )
        self.assertEqual(
            [argv for argv, _cwd in calls],
            [
                [
                    "gh", "issue", "list", "--state", "open",
                    "--search", '"{0}" in:title'.format(self.title),
                    "--json", "title,url",
                ],
                ["gh", "issue", "create", "--title", self.title, "--body", "body"],
            ],
        )
        self.assertEqual({cwd for _argv, cwd in calls}, {str(self.repo)})

    def test_an_open_issue_with_the_title_files_no_second_one(self) -> None:
        listed = json.dumps(
            [
                {"title": self.title + " (older)", "url": "u-0"},
                {"title": self.title, "url": "u-1"},
            ]
        )
        calls: list = []

        def run(argv, **kwargs):
            calls.append(argv)
            return _gh(stdout=listed)

        with mock.patch.object(maestro.subprocess, "run", side_effect=run):
            result = maestro._attend_file_issue(self.repo, self.title, "body")
        self.assertEqual(result, {"status": att.ISSUE_EXISTS, "url": "u-1"})
        self.assertFalse(any(argv[1:3] == ["issue", "create"] for argv in calls))

    def test_a_gh_failure_is_reported_and_not_raised(self) -> None:
        with mock.patch.object(
            maestro.subprocess,
            "run",
            return_value=_gh(returncode=1, stderr="gh: not logged in\nmore"),
        ):
            result = maestro._attend_file_issue(self.repo, self.title, "body")
        self.assertEqual(result, {"status": att.ISSUE_FAILED, "detail": "gh: not logged in"})

    def test_a_missing_gh_is_reported_and_not_raised(self) -> None:
        with mock.patch.object(
            maestro.subprocess, "run", side_effect=FileNotFoundError("gh")
        ):
            result = maestro._attend_file_issue(self.repo, self.title, "body")
        self.assertEqual(result["status"], att.ISSUE_FAILED)


class _Record:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id


class _LedgerStore:
    """A real `ArtifactStore` whose lane reads are scripted for one park.

    Records go into the real ledger, so the deviations file is rendered from
    real rows; only the park the scheduler would have left is scripted.
    """

    def __init__(self, store: ArtifactStore, before: st.CompiledPlan) -> None:
        self.store = store
        self.lanes = before.lanes
        self.parked = {"lane-b": st.WaitReason.NO_PROGRESS.value}
        self.amendments: dict = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def active_projection(self, run_id: str):
        return self.lanes

    def lane_stage(self, run_id: str, lane_id: str) -> st.LaneStage:
        if lane_id in self.parked:
            return st.LaneStage.WAITING_FOR_USER
        return st.LaneStage.MERGED

    def latest_lane_artifact_payload(self, run_id, lane_id, kind):
        if kind is not st.ArtifactKind.USER_WAIT or lane_id not in self.parked:
            return None
        return {"wait_reason": self.parked[lane_id]}


class AttendLeavesIssueAndDeviations(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.ledger = ArtifactStore(self.tmp / "lifecycle.sqlite3")
        self.addCleanup(self.ledger.close)
        self.ledger.create_run(RUN_ID, make_plan(make_lane("A")), make_binding())

    def _attend(self, edit, issue_result: dict, apply_refusal=None) -> tuple:
        document = copy.deepcopy(_document())
        edit(document)
        after = _compile(document, 2)
        store = _LedgerStore(self.ledger, BEFORE)
        filed: list = []
        revision = self.tmp / "operator" / "r2.ir.json"
        revision.parent.mkdir(parents=True, exist_ok=True)
        findings = self.tmp / "reviewer" / "round1.json"
        findings.parent.mkdir(parents=True, exist_ok=True)
        passes = []

        def run_scheduler(plan, held=()):
            passes.append(plan.plan_revision)
            return st.RunStatus.WAITING if store.parked else st.RunStatus.COMPLETE

        def request_for(parked, plan, review_round, reviewer_findings, previous):
            return att.OperatorRequest(
                run_id=RUN_ID, plan_revision=1, next_plan_revision=2,
                lanes=tuple(
                    att.LaneEvidence(
                        lane_id=lane_id, wait_reason=reason, stage="WAITING_FOR_USER",
                        round_number=3, public_contract={}, reviews=(),
                        failure_output=(), lane_gates="", suite_files={},
                    )
                    for lane_id, reason in parked
                ),
                ir_path=str(self.tmp / "current.ir.json"),
                revision_out_path=str(revision), amendment_rules="rules",
                allowed_lane_ids=att.batch_scope(plan.lanes, parked),
                review_round=review_round,
            )

        def dispatch(request):
            revision.write_text("{}", encoding="utf-8")
            return {
                "revision_path": str(revision),
                "rationales": [
                    {
                        "lane": "lane-b", "round": 3,
                        "failing_cases_summary": "a case fails",
                        "contract_gap": "the interface returned the wrong shape",
                        "edit_path": "/lanes/1/spec/interface",
                        "edit_text": "returns list",
                    }
                ],
            }

        def review_request_for(request, written, rationales, review_round, digest):
            return att.ReviewRequest(
                run_id=RUN_ID, plan_revision=1, next_plan_revision=2,
                review_round=review_round, lanes=request.lanes,
                current_ir_path=request.ir_path, revision_ir_path=str(written),
                question_surface="{}", rationales=tuple(rationales),
                findings_out_path=str(findings), amendment_rules="rules",
                allowed_lane_ids=request.allowed_lane_ids, revision_sha256=digest,
            )

        def review(request):
            findings.write_text(json.dumps({"findings": []}), encoding="utf-8")
            return {"verdict": "PASS", "findings": []}

        def apply(plan):
            if apply_refusal is not None:
                raise apply_refusal
            store.parked = {}
            store.lanes = plan.lanes
            # A real PLAN_AMENDMENT needs the scheduler's whole amend path;
            # what the loop reads back from it is its `resets`, read here
            # from the run artifacts it would have written.
            return _Record("amendment-1")

        def file_issue(title, body):
            filed.append((title, body))
            return issue_result

        deviations = self.tmp / "plans" / "{0}.deviations.md".format(RUN_ID)
        outcome = att.attend_run(
            store=store, run_id=RUN_ID,
            policy=att.AttendPolicy(
                max_amendments_per_lane=1,
                reviewer_id="attend-reviewer:omp:profile-b",
                reviewer_vendor="vendor-b",
            ),
            compiled=BEFORE, session_id="session-1",
            run_scheduler=run_scheduler, request_for=request_for,
            dispatch=dispatch, review_request_for=review_request_for,
            review=review, project=lambda path, number, found: after,
            apply_amendment=apply, file_issue=file_issue,
            deviations_path=deviations,
        )
        return outcome, filed, deviations, passes

    def _interface_edit(self, document: dict) -> None:
        _lane(document, "lane-b")["spec"]["interface"][0]["signature"]["returns"] = "list"

    def test_an_apply_refused_after_pass_still_shows_in_deviations(self) -> None:
        """F4: the file is rendered in a `finally` around the batch."""
        deviations = self.tmp / "plans" / "{0}.deviations.md".format(RUN_ID)
        with self.assertRaises(RuntimeError):
            self._attend(
                self._interface_edit, {}, apply_refusal=RuntimeError("amend refused")
            )
        text = deviations.read_text()
        self.assertIn("## Revision r2 (session session-1)", text)
        self.assertIn("- amendment: none applied", text)
        self.assertIn("- round 1: PASS by attend-reviewer:omp:profile-b", text)

    def test_a_material_amendment_files_one_issue(self) -> None:
        outcome, filed, deviations, passes = self._attend(
            self._interface_edit, {"status": att.ISSUE_CREATED, "url": "u-7"}
        )
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(passes, [1, 2])
        self.assertEqual(len(filed), 1)
        title, body = filed[0]
        self.assertEqual(title, "Attended amendment r2 on run {0}: material change".format(RUN_ID))
        self.assertIn("lane-b: spec.interface changed", body)
        self.assertIn(str(deviations), body)

    def test_a_spec_text_amendment_files_nothing(self) -> None:
        def edit(document: dict) -> None:
            _lane(document, "lane-b")["spec"]["intent"] = "build the contract"

        _outcome, filed, deviations, _passes = self._attend(edit, {})
        self.assertEqual(filed, [])
        self.assertIn("- material: no", deviations.read_text())

    def test_the_deviations_file_is_rendered_from_the_ledger_byte_for_byte(self) -> None:
        """A8."""
        _outcome, _filed, deviations, _passes = self._attend(
            self._interface_edit, {"status": att.ISSUE_FAILED, "detail": "gh: not logged in"}
        )
        written = deviations.read_bytes()
        self.assertEqual(written, att.render_deviations(self.ledger, RUN_ID).encode("utf-8"))
        self.assertEqual(
            att.render_deviations(self.ledger, RUN_ID),
            att.render_deviations(self.ledger, RUN_ID),
        )
        text = written.decode("utf-8")
        self.assertIn("## Revision r2 (session session-1)", text)
        self.assertIn("  - lane-b: NO_PROGRESS", text)
        self.assertIn("- material: yes", text)
        self.assertIn("  - lane-b: spec.interface changed", text)
        # A failed `gh` is recorded, and the amendment it reports on stands.
        self.assertIn("- issue: FAILED gh: not logged in", text)
        self.assertIn(
            "- round 1: PASS by attend-reviewer:omp:profile-b (vendor-b)", text
        )
        self.assertIn("contract_gap: the interface returned the wrong shape", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
