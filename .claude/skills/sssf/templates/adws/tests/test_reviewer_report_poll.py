"""§6.5 — a reviewer's report is read when it is finished, not when it parses.

Recorded failure, 2026-08-18, `plan ship cmo-consolidation-l`. The finalization
window's completion signal is `poll_report()` returning any payload at all. The
poller returned the first thing that parsed, and the reviewer was still writing:
the window converted a draft carrying a handful of cells into a completed
outcome, `verify_report` refused it with `CELL_SET: missing=[...]` naming almost
the whole matrix, and the complete 136-cell report — `missing=0 invented=0`
against the same matrix — was on disk moments later.

The cost of that ordering is the whole point. `ReportRejected` is terminal for
the plan's bytes and writes no receipt, so a read race did not delay a verdict,
it *became* one: a plan the reviewer went on to clear was refused because the
poll landed mid-write. A report that never completes inside the window now
stalls it instead, and a stall is explicitly "a fact about the machine or the
route, never a verdict about the plan", which permits a rerun.

The predicate is structural per §1.2: it compares the `pair_count` the reviewer
declared against the number of cells present. It reads no message, no status,
and no other prose the reviewer produced.

Both reviewer windows — `plan finalize` and the node code review — poll through
the same production function, so these cases cover both by construction rather
than by two copies that can drift apart.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro                                   # noqa: E402
from adw_modules import finalization as fin      # noqa: E402


def _cells(count: int):
    return [{"check_id": "check.{}".format(index), "object_id": "obj",
             "status": "clear", "message": ""} for index in range(count)]


def _report(declared: int, present: int):
    return {"plan_digest": "d" * 64, "pair_count": declared,
            "cells": _cells(present)}


class ReportCompleteness(unittest.TestCase):
    """`finalization.report_is_complete`, including what it convicts."""

    def test_a_report_carrying_every_cell_it_declares_is_complete(self):
        self.assertTrue(fin.report_is_complete(_report(136, 136)))

    def test_a_report_still_being_written_is_not_complete(self):
        # The observed shape: the echo is right, the cells are still arriving.
        self.assertFalse(fin.report_is_complete(_report(136, 3)))

    def test_a_report_with_no_cells_yet_is_not_complete(self):
        self.assertFalse(fin.report_is_complete(_report(136, 0)))

    def test_a_report_that_declares_nothing_is_not_complete(self):
        self.assertFalse(fin.report_is_complete({"cells": _cells(2)}))
        self.assertFalse(fin.report_is_complete({"pair_count": 2}))

    def test_a_non_mapping_payload_is_not_complete(self):
        for payload in (None, [], "report", 3):
            self.assertFalse(fin.report_is_complete(payload))

    def test_a_boolean_count_is_not_a_count(self):
        # `True == 1` in Python, so a bool would silently admit a one-cell
        # draft as a complete report.
        self.assertFalse(fin.report_is_complete(
            {"pair_count": True, "cells": _cells(1)}))

    def test_extra_cells_are_not_admitted_as_complete_either(self):
        # More cells than declared is not a finished report, it is a report
        # that disagrees with its own canary. `verify_report` convicts it as
        # an invented cell; admitting it here would be admitting a payload
        # this predicate cannot vouch for.
        self.assertFalse(fin.report_is_complete(_report(2, 3)))


class ProductionPoller(unittest.TestCase):
    """`maestro._poll_reviewer_report`, the reader both windows call."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.report = Path(self._tmp.name) / "report.json"

    def test_an_absent_report_polls_as_not_ready(self):
        self.assertIsNone(maestro._poll_reviewer_report(self.report))

    def test_a_partially_flushed_report_polls_as_not_ready(self):
        # Bytes that do not parse yet.
        self.report.write_text('{"plan_digest": "d", "pair_c',
                               encoding="utf-8")
        self.assertIsNone(maestro._poll_reviewer_report(self.report))

    def test_a_parseable_unfinished_report_polls_as_not_ready(self):
        # The defect: this used to be returned, and returning it completed
        # the window on the reviewer's draft.
        self.report.write_text(json.dumps(_report(136, 3)), encoding="utf-8")
        self.assertIsNone(maestro._poll_reviewer_report(self.report))

    def test_the_finished_report_polls_as_ready_and_is_returned_whole(self):
        payload = _report(136, 136)
        self.report.write_text(json.dumps(payload), encoding="utf-8")
        polled = maestro._poll_reviewer_report(self.report)
        self.assertEqual(polled, payload)

    def test_the_recorded_sequence_completes_only_on_the_finished_report(self):
        """The 2026-08-18 sequence, in order, through the real reader.

        Nothing is stubbed: the file is written the way a reviewer writes it,
        and the poller is asked at each stage what the window would have seen.
        """
        seen = []
        for present in (0, 3, 87, 136):
            self.report.write_text(json.dumps(_report(136, present)),
                                   encoding="utf-8")
            seen.append(maestro._poll_reviewer_report(self.report) is not None)
        self.assertEqual(seen, [False, False, False, True])

    def test_the_draft_would_have_been_refused_by_verify_report(self):
        """Why 'not ready' rather than 'reject': the draft is a CELL_SET FAIL.

        This asserts the consequence the poller now prevents, so the test
        fails if someone reinstates the old reader — the draft reaches
        `verify_report` and the plan is condemned on cells the reviewer had
        not written yet.
        """
        kind = fin.ObjectKind.DIFF
        rubric = fin.Rubric(version="test.v1", checks=(
            fin.RubricCheck(check_id="check.0", question="q0",
                            applies_to=(kind,), severity=fin.Severity.BLOCKING),
            fin.RubricCheck(check_id="check.1", question="q1",
                            applies_to=(kind,), severity=fin.Severity.BLOCKING),
        ))
        matrix = fin.compute_matrix(
            rubric, "d" * 64,
            (fin.ReviewObject(object_id="obj", kind=kind),))
        draft = {"plan_digest": "d" * 64, "pair_count": matrix.pair_count,
                 "cells": [{"check_id": "check.0", "object_id": "obj",
                            "status": "clear", "message": ""}]}
        self.assertFalse(fin.report_is_complete(draft))
        with self.assertRaises(fin.ReportRejected) as caught:
            fin.verify_report(matrix, fin.ReviewerReport.model_validate(draft))
        self.assertIs(caught.exception.reason, fin.RejectionReason.CELL_SET)


class StaleReportClearing(unittest.TestCase):
    """`maestro._clear_stale_reviewer_report`, run before every launch.

    A report on disk ends the next reviewer's window on its first poll, and
    the receipt then names a session that did not write those bytes.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.report = Path(self._tmp.name) / "report.json"

    def test_a_previous_reviewers_report_does_not_survive_into_a_new_launch(self):
        self.report.write_text(json.dumps(_report(136, 136)), encoding="utf-8")
        maestro._clear_stale_reviewer_report(self.report)
        self.assertFalse(self.report.exists())
        # The consequence: the next poll reports the window still open,
        # rather than completing on the old reviewer's bytes.
        self.assertIsNone(maestro._poll_reviewer_report(self.report))

    def test_clearing_an_absent_report_is_not_an_error(self):
        maestro._clear_stale_reviewer_report(self.report)
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
