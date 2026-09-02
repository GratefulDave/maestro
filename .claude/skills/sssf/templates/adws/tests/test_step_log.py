"""The step stream survives the terminal it was printed to.

`FactoryConsole.step` reports what a stage is doing while it runs, but only to
a terminal. A dashboard in another process had nothing to read, so a stage that
takes minutes was indistinguishable from a dead scheduler from outside the CLI.

These cases pin the durable mirror:

  * a step lands on disk as one compact JSON object per line, read back from a
    real file rather than a mocked `open`;
  * the file is created 0600, appended to, and never truncated;
  * one record is one `os.write`, so a tailing reader never sees half a line;
  * a disk that cannot be written is a lost report, never a failed lane;
  * the run is bounded by a `run opened` and a `run finished` line;
  * the console still prints exactly what it printed before.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import step_log  # noqa: E402

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$")


class _Recorder:
    """Stand-in for FactoryConsole: records instead of printing."""

    def __init__(self, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self.raises = raises

    def opened(self, action, run_id, repository, main_ref, lanes) -> None:
        self.calls.append(("opened", action, run_id, str(repository), main_ref))

    def stage_started(self, lane_id, stage) -> None:
        self.calls.append(("stage_started", lane_id, stage))

    def stage_completed(self, lane_id, previous, current) -> None:
        self.calls.append(("stage_completed", lane_id, previous, current))

    def step(self, lane_id, message, detail="") -> None:
        self.calls.append(("step", lane_id, message, detail))
        if self.raises:
            raise RuntimeError("the terminal went away")

    def finished(self, run_id, status) -> None:
        self.calls.append(("finished", run_id, status))


def _lines(root: Path) -> list[dict]:
    text = (root / step_log.STEPS_FILENAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _raw(root: Path) -> str:
    return (root / step_log.STEPS_FILENAME).read_text(encoding="utf-8")


class ARecordLandsOnDisk(unittest.TestCase):
    def test_the_line_has_the_declared_shape(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = step_log.StepLog(root, "f50638ab")
            log.append("lane-wp7-build", "sealed suite FAILED", "12 executed")
            (record,) = _lines(root)
            self.assertEqual(record["run_id"], "f50638ab")
            self.assertEqual(record["lane_id"], "lane-wp7-build")
            self.assertEqual(record["message"], "sealed suite FAILED")
            self.assertEqual(record["detail"], "12 executed")
            self.assertTrue(TS.match(record["ts"]), record["ts"])
            # The same instant the ledger would have stamped: tz-aware, ms.
            parsed = datetime.fromisoformat(record["ts"])
            self.assertIsNotNone(parsed.tzinfo)

    def test_detail_is_present_and_empty_when_there_is_none(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            step_log.StepLog(root, "r1").append("lane-a", "asking builder")
            (record,) = _lines(root)
            self.assertIn("detail", record)
            self.assertEqual(record["detail"], "")

    def test_the_json_is_compact_and_newline_terminated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = step_log.StepLog(root, "r1")
            log.append("lane-a", "one", "d")
            raw = _raw(root)
            self.assertTrue(raw.endswith("\n"))
            self.assertNotIn(", ", raw)
            self.assertNotIn('": ', raw)

    def test_the_file_lives_beside_the_ledger(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = step_log.StepLog(root, "r1")
            self.assertEqual(log.path, root / "steps.jsonl")

    def test_it_is_created_0600_on_first_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / step_log.STEPS_FILENAME
            self.assertFalse(path.exists())
            step_log.StepLog(root, "r1").append("lane-a", "one")
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_appends_never_truncate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = step_log.StepLog(root, "r1")
            first.append("lane-a", "one")
            first.append("lane-b", "two")
            # A second run object opening the same file must not lose the first.
            step_log.StepLog(root, "r2").append("lane-c", "three")
            records = _lines(root)
            self.assertEqual(
                [r["message"] for r in records], ["one", "two", "three"]
            )
            self.assertEqual([r["run_id"] for r in records], ["r1", "r1", "r2"])

    def test_one_record_is_one_write(self):
        """A tailing reader must never observe half a line."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = os.write
            payloads: list[bytes] = []

            def spy(fd, data):
                payloads.append(data)
                return real(fd, data)

            with mock.patch.object(os, "write", spy):
                step_log.StepLog(root, "r1").append("lane-a", "one", "d")
            self.assertEqual(len(payloads), 1)
            self.assertTrue(payloads[0].endswith(b"\n"))
            self.assertEqual(payloads[0].decode("utf-8"), _raw(root))

    def test_the_file_is_opened_for_append(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = os.open
            flags: list[int] = []

            def spy(path, flag, *rest):
                flags.append(flag)
                return real(path, flag, *rest)

            with mock.patch.object(os, "open", spy):
                step_log.StepLog(root, "r1").append("lane-a", "one")
            self.assertTrue(all(f & os.O_APPEND for f in flags), flags)
            self.assertFalse(any(f & os.O_TRUNC for f in flags), flags)


class ABadDiskCannotFailALane(unittest.TestCase):
    def test_a_missing_directory_is_swallowed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "never-created"
            step_log.StepLog(root, "r1").append("lane-a", "one")
            self.assertFalse(root.exists())

    def test_an_unwritable_directory_is_swallowed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "locked"
            root.mkdir(mode=0o500)
            try:
                step_log.StepLog(root, "r1").append("lane-a", "one")
                self.assertFalse((root / step_log.STEPS_FILENAME).exists())
            finally:
                root.chmod(0o700)

    def test_a_path_that_is_a_directory_is_swallowed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / step_log.STEPS_FILENAME).mkdir()
            step_log.StepLog(root, "r1").append("lane-a", "one")

    def test_a_reporter_whose_disk_fails_cannot_fail_the_lane(self):
        """The disk equivalent of a reporter that raises.

        Same seam as `StepsAreReported.test_a_reporter_that_raises_cannot_fail_
        the_lane`: the scheduler reports through `_say`, and a report is never
        the thing that ends a run.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "never-created"
            console = _Recorder()
            reporter = step_log.RunReporter("r1", root, console=console)
            scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
            scheduler.step = reporter.step
            scheduler._say("lane-a", "asking builder for a candidate")
            self.assertEqual(
                console.calls,
                [("step", "lane-a", "asking builder for a candidate", "")],
            )
            self.assertFalse(root.exists())


class TheReporterDrivesBoth(unittest.TestCase):
    def test_a_step_prints_and_is_written(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            console = _Recorder()
            reporter = step_log.RunReporter("r1", root, console=console)
            reporter.step("lane-a", "sealed suite passed", "12 executed")
            self.assertEqual(
                console.calls,
                [("step", "lane-a", "sealed suite passed", "12 executed")],
            )
            (record,) = _lines(root)
            self.assertEqual(record["message"], "sealed suite passed")
            self.assertEqual(record["detail"], "12 executed")

    def test_a_console_that_raises_still_leaves_the_record(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            reporter = step_log.RunReporter("r1", root, console=_Recorder(raises=True))
            with self.assertRaises(RuntimeError):
                reporter.step("lane-a", "one")
            (record,) = _lines(root)
            self.assertEqual(record["message"], "one")

    def test_the_run_is_bounded_by_opened_and_finished(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            console = _Recorder()
            reporter = step_log.RunReporter("r1", root, console=console)
            reporter.opened("start", "r1", root, "refs/heads/main", ["lane-a"])
            reporter.step("lane-a", "asking builder for a candidate")
            reporter.finished("r1", SimpleNamespace(value="COMPLETE"))
            records = _lines(root)
            self.assertEqual(
                [(r["lane_id"], r["message"], r["detail"]) for r in records],
                [
                    ("-", "run opened", "start"),
                    ("lane-a", "asking builder for a candidate", ""),
                    ("-", "run finished", "COMPLETE"),
                ],
            )
            self.assertEqual({r["run_id"] for r in records}, {"r1"})

    def test_the_console_surface_is_unchanged(self):
        with TemporaryDirectory() as tmp:
            console = _Recorder()
            reporter = step_log.RunReporter("r1", Path(tmp), console=console)
            reporter.opened("resume", "r1", Path(tmp), "refs/heads/main", [])
            reporter.stage_started("lane-a", "BUILDING")
            reporter.stage_completed("lane-a", "BUILDING", "REVIEWING_CODE")
            reporter.finished("r1", SimpleNamespace(value="BLOCKED"))
            self.assertEqual(
                [call[0] for call in console.calls],
                ["opened", "stage_started", "stage_completed", "finished"],
            )

    def test_the_default_console_is_the_factory_console(self):
        from adw_modules import factory_console as fconsole

        with TemporaryDirectory() as tmp:
            reporter = step_log.RunReporter("r1", Path(tmp))
            self.assertIsInstance(reporter._console, fconsole.FactoryConsole)


class TheCliWiresIt(unittest.TestCase):
    def test_the_run_verbs_report_through_the_run_reporter(self):
        import maestro  # noqa: F401 - imported for the wiring check below

        source = (ADWS / "maestro.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("step_log.RunReporter(run_id, runtime.path)"), 3
        )
        self.assertNotIn("fconsole.FactoryConsole()", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
