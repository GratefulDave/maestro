"""A revise turn edits the artifact its review judged, not a bare base.

Run 2acd2f5f, `lane-device-tests`: `_writing_tests` built the tester's context
with LANE_PLAN and TEST_REVIEW only, so `write_tests` found no TEST_DRAFT to
lay back down and the revise turn started from the integration head. The
turn-13 draft was the accepted suite +35/-7; the turn-14 draft was the same
suite +120 with every turn-13 edit gone, and the tester said so: "The checkout
was reset to the accepted suite, so my earlier round's restores are gone."

`test_role_tree_matches_dispatch` covered the actor's restore with a context
it built by hand, holding a TEST_DRAFT the scheduler never supplied, so it was
green the whole time. These cases start at the scheduler's stage method and
read the bytes the real actor's checkout held at dispatch.

The siblings: a CODE_REVISE context carried the CODE_REVIEW but not the
BUILDER_OUTPUT it judged, and a BASE_INVALIDATION context carried neither. A builder checkout that survives holds the candidate
anyway; one an amendment removed (`close_run_panes`) came back as the bare
base.
"""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from tests.test_code_revise_across_amendment import RUN, _Ledger
from tests.test_role_tree_matches_dispatch import (
    _ROUND_ONE,
    _SUITE,
    _Bench,
    _commit_suite,
    _git,
    _lane,
)

PLAN_ID = "1" * 64
DRAFT_ID = "3" * 64
NEWER_DRAFT_ID = "4" * 64
REVIEW_ID = "5" * 64
SUITE_ID = "2" * 64
BUILDER_ID = "6" * 64
CODE_REVIEW_ID = "7" * 64


class _Dispatched(Exception):
    """Raised once the role returns: everything after it is out of scope."""


def _scheduler(
    bench: _Bench, ledger: _Ledger, outputs: tuple[str, ...] = (_SUITE,)
) -> sch.FactoryScheduler:
    scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    scheduler.run_id = RUN
    scheduler.store = ledger
    scheduler.target = bench.target
    scheduler.actor = bench.actor
    lane = dataclasses.replace(
        _lane(),
        declared_outputs=outputs,
        lane_projection_digest=st.lane_projection_digest(
            _lane().spec_digest, (), outputs, lane_kind=None
        ),
    )
    row = {"plan_revision": 1, "plan_digest": "ef" * 32}
    scheduler._common = lambda lane_id: (row, lane)
    scheduler._integration_head = lambda: bench.head
    scheduler._plan_artifact_ref = lambda row_arg: "plan:x"

    def say(lane_id: str, message: str, detail: str = "") -> None:
        del lane_id, detail
        if message.endswith(" returned a draft") or message.endswith(
            " returned a candidate"
        ):
            raise _Dispatched

    scheduler._say = say
    return scheduler


def _checkout(bench: _Bench, role: str) -> Path:
    return bench.state / "worktrees" / RUN / "lane-a" / role / "checkout"


class ATesterReviseTurnFromTheScheduler(unittest.TestCase):
    def _ledger(self, draft_sha: str) -> _Ledger:
        ledger = _Ledger(revision=1)
        lane = "lane-a"
        ledger.stage(lane, st.LaneStage.WRITING_TESTS)
        ledger.add(lane, PLAN_ID, st.ArtifactKind.LANE_PLAN, 1, {})
        ledger.add(
            lane,
            DRAFT_ID,
            st.ArtifactKind.TEST_DRAFT,
            1,
            {
                "candidate_sha": draft_sha,
                "candidate_ref": "refs/x",
                "files": {_SUITE: "00" * 20},
            },
        )
        ledger.add(
            lane,
            REVIEW_ID,
            st.ArtifactKind.TEST_REVIEW,
            1,
            {
                "verdict": "REVISE",
                "findings": [{"summary": "fix it"}],
                "input_artifact_ids": [PLAN_ID, DRAFT_ID],
            },
        )
        return ledger

    def _drive(self, bench: _Bench, ledger: _Ledger) -> None:
        scheduler = _scheduler(bench, ledger)
        scheduler._redraft_baseline = lambda lane, contract: None
        with self.assertRaises(_Dispatched):
            scheduler._writing_tests("lane-a")

    def test_the_revise_turn_dispatches_over_the_judged_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            draft_sha = _commit_suite(bench.product, parent=bench.head, body=_ROUND_ONE)

            self._drive(bench, self._ledger(draft_sha))

            self.assertEqual(bench.launcher.seen, [_ROUND_ONE])
            # Uncommitted on the head, as if the tester had just written it.
            checkout = _checkout(bench, "tester")
            self.assertEqual(_git(checkout, "rev-parse", "HEAD"), bench.head)

    def test_the_draft_is_the_one_the_review_named_not_the_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            draft_sha = _commit_suite(bench.product, parent=bench.head, body=_ROUND_ONE)
            ledger = self._ledger(draft_sha)
            newer = _commit_suite(bench.product, parent=bench.head, body="// unjudged\n")
            ledger.add(
                "lane-a",
                NEWER_DRAFT_ID,
                st.ArtifactKind.TEST_DRAFT,
                1,
                {"candidate_sha": newer, "files": {_SUITE: "11" * 20}},
            )

            self._drive(bench, ledger)

            self.assertEqual(bench.launcher.seen, [_ROUND_ONE])

    def test_a_first_turn_has_no_draft_to_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            ledger = _Ledger(revision=1)
            ledger.stage("lane-a", st.LaneStage.WRITING_TESTS)
            ledger.add("lane-a", PLAN_ID, st.ArtifactKind.LANE_PLAN, 1, {})

            self._drive(bench, ledger)

            self.assertEqual(bench.launcher.seen, [None])


class ABuilderReviseTurnFromTheScheduler(unittest.TestCase):
    def _ledger(self, bench: _Bench, candidate: str) -> _Ledger:
        ledger = _Ledger(revision=1)
        lane = "lane-a"
        ledger.stage(lane, st.LaneStage.BUILDING)
        ledger.add(lane, PLAN_ID, st.ArtifactKind.LANE_PLAN, 1, {})
        ledger.add(
            lane,
            BUILDER_ID,
            st.ArtifactKind.BUILDER_OUTPUT,
            1,
            {
                "builder_base_sha": bench.head,
                "candidate_sha": candidate,
                "tree_delta": [
                    {"status": "A", "old_path": _SUITE, "new_path": _SUITE},
                    {"status": "D", "old_path": "a.txt", "new_path": "a.txt"},
                ],
            },
        )
        ledger.add(
            lane,
            CODE_REVIEW_ID,
            st.ArtifactKind.CODE_REVIEW,
            1,
            {
                "verdict": "REVISE",
                "findings": [{"summary": "fix it"}],
                "input_artifact_ids": [PLAN_ID, SUITE_ID, BUILDER_ID],
            },
        )
        return ledger

    def _drive(self, bench: _Bench, ledger: _Ledger) -> None:
        # The builder declared both, so deleting `a.txt` is its to make.
        scheduler = _scheduler(bench, ledger, ("a.txt", _SUITE))
        suite = SimpleNamespace(
            artifact_id=SUITE_ID, payload={"test_suite_digest": "test-suite.v1:x"}
        )
        scheduler._accepted_suite_for = lambda lane: suite
        scheduler._probe_budget_exhausted = lambda lane_id: False
        scheduler._builder_base = lambda lane, suite_arg: bench.head
        scheduler._dep_receipts = lambda needs: []
        scheduler._accumulated_probes = lambda lane_id: ((), (), (), "")
        scheduler._own_test_paths = lambda lane, suite_arg: ()
        scheduler._probe_sources = lambda *args: None
        scheduler._bound_surface = lambda lane, suite_arg: None
        with self.assertRaises(_Dispatched):
            scheduler._building("lane-a")

    def _candidate(self, bench: _Bench) -> str:
        """The builder's last candidate: the suite written, `a.txt` deleted."""
        work = Path(bench.product)
        _git(work, "checkout", "-q", "--detach", bench.head)
        (work / _SUITE).parent.mkdir(parents=True, exist_ok=True)
        (work / _SUITE).write_text(_ROUND_ONE, encoding="utf-8")
        _git(work, "add", _SUITE)
        _git(work, "rm", "-q", "a.txt")
        _git(work, "commit", "-q", "-m", "declared outputs")
        sha = _git(work, "rev-parse", "HEAD")
        _git(work, "checkout", "-q", "main")
        return sha

    def test_a_fresh_checkout_gets_the_judged_candidate_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            candidate = self._candidate(bench)

            self._drive(bench, self._ledger(bench, candidate))

            self.assertEqual(bench.launcher.seen, [_ROUND_ONE])
            checkout = _checkout(bench, "builder")
            # Committed by `build` as the new candidate: same tree as the
            # judged one, deletion included.
            self.assertEqual(
                _git(checkout, "rev-parse", "HEAD^{tree}"),
                _git(bench.product, "rev-parse", candidate + "^{tree}"),
            )

    def test_a_base_invalidation_rebuilds_from_the_invalidated_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            candidate = self._candidate(bench)
            ledger = self._ledger(bench, candidate)
            passing = "8" * 64
            ledger.add(
                "lane-a", passing, st.ArtifactKind.CODE_REVIEW, 1, {"verdict": "PASS"}
            )
            ledger.add(
                "lane-a",
                "9" * 64,
                st.ArtifactKind.BASE_INVALIDATION,
                1,
                {
                    "stale_builder_output_artifact_id": BUILDER_ID,
                    "stale_code_review_artifact_id": passing,
                },
            )

            self._drive(bench, ledger)

            self.assertEqual(bench.launcher.seen, [_ROUND_ONE])

    def test_a_surviving_checkout_is_left_as_the_builder_left_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            candidate = self._candidate(bench)
            checkout = _checkout(bench, "builder")
            checkout.parent.mkdir(parents=True)
            subprocess.check_call(
                ["git", "-C", str(bench.product), "worktree", "add", "-q",
                 "--detach", str(checkout), bench.head]
            )
            later = "// the builder's own later edit\n"
            (checkout / _SUITE).parent.mkdir(parents=True, exist_ok=True)
            (checkout / _SUITE).write_text(later, encoding="utf-8")
            _git(checkout, "add", _SUITE)
            _git(checkout, "commit", "-q", "-m", "declared outputs")

            self._drive(bench, self._ledger(bench, candidate))

            self.assertEqual(bench.launcher.seen, [later])


if __name__ == "__main__":
    unittest.main()
