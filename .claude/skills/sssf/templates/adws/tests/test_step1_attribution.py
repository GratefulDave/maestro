"""Executable proof of the Step 1 attribution fixes (MAESTRO architecture.md, §12.2).

Scope note: §12.2 Step 1 item 2 describes `Console.phase_id`/`phase_name` as a
single instance slot that any open phase can repoint, and says a lock does
not fix it — only thread-locality does. An executed measurement (see
`test_step1_base_corrections.py::Step1ConsoleAttribution`) narrows that claim:
`PhaseHandle.log` is NOT affected, because it passes `self.phase.phase_id`
straight to the tracer rather than reading the shared slot. The real
misattribution surface is `Console._emit` and everything that calls it on the
console's own account — `note`, `agent_started`, `agent_finished`, `retry`,
`gate_result`, `envelope_summary`, `phase_started`, `phase_ended`. This suite
targets that real surface with actual OS threads, because a test that opens
two phases sequentially in one thread cannot distinguish a shared instance
slot from a thread-local one — both give the "right" answer when nothing is
actually concurrent.

Item 3 (quality.py guessing `run.phases[-1]`) needs no thread to prove: the
defect is that the helpers pick a phase by list position instead of taking
one from the caller, so a single-threaded test that gives them the *wrong*
list position and the *right* explicit phase already discriminates old
behaviour from new.

Run with:  just test        (or: uv run adws/adw_test.py)
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

# This file ships inside adws/tests/, so the package root is its parent's parent.
ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules.console import Console  # noqa: E402
from adw_modules.data_types import EventRecord, Phase, PhaseParams  # noqa: E402
from adw_modules import quality  # noqa: E402


def _phase(seq: int, name: str) -> Phase:
    return Phase(
        phase_id=f"threadrun_{seq:02d}_{name}",
        adw_id="threadrun",
        seq=seq,
        params=PhaseParams(name=name, kind="code", owner="test",
                          description="exercise console attribution under real threads"),
        status="running",
    )


class _RecordingTracer:
    """A tracer double that only records events — no sqlite, no disk.

    Console tests below want real OS threads, and `Tracer`'s sqlite
    connection defaults to `check_same_thread=True` (tracer.py:108), so a
    Tracer built on one thread cannot be handed events from another. That
    default is itself a real concurrency gap in the base — see the report at
    the bottom of this file — but it is not this test's job to fix tracer.py,
    only to prove Console's attribution is correct under real concurrency.
    A thread-safe recorder isolates that proof from the sqlite question.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[EventRecord] = []

    def event(self, record: EventRecord) -> str:
        with self._lock:
            self.events.append(record)
        return f"evt_{len(self.events)}"


class Step1ConsoleThreadLocalAttribution(unittest.TestCase):
    """§12.2 Step 1 item 2, narrowed to `Console._emit`'s real surface."""

    def test_two_threads_holding_open_phases_do_not_cross_attribute_notes(self):
        """Thread A opens a phase, thread B opens a second phase while A's is
        still open, then both threads emit a note. A shared instance slot
        makes A's note carry B's phase_id once B has opened; a thread-local
        slot keeps each thread's notes on its own phase regardless of what
        the other thread does concurrently.
        """
        tracer = _RecordingTracer()
        console = Console(tracer, "threadrun")

        a_opened = threading.Event()
        b_opened = threading.Event()
        phase_a = _phase(1, "node_a")
        phase_b = _phase(2, "node_b")

        def worker_a() -> None:
            console.phase_started(phase_a)
            a_opened.set()
            # Do not proceed until B has repointed the (formerly) shared slot.
            self.assertTrue(b_opened.wait(timeout=5), "thread B never opened its phase")
            console.note("note from thread a")
            console.phase_ended(phase_a, 0.01)

        def worker_b() -> None:
            self.assertTrue(a_opened.wait(timeout=5), "thread A never opened its phase")
            console.phase_started(phase_b)
            b_opened.set()
            console.note("note from thread b")
            console.phase_ended(phase_b, 0.01)

        t_a = threading.Thread(target=worker_a)
        t_b = threading.Thread(target=worker_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)
        self.assertFalse(t_a.is_alive(), "thread A did not finish")
        self.assertFalse(t_b.is_alive(), "thread B did not finish")

        def note_event(marker: str) -> EventRecord:
            matches = [e for e in tracer.events
                      if e.type == "log" and marker in e.payload.get("message", "")]
            self.assertEqual(len(matches), 1, f"expected exactly one note for {marker!r}, "
                                               f"got {[e.payload for e in matches]}")
            return matches[0]

        event_a = note_event("note from thread a")
        event_b = note_event("note from thread b")
        self.assertEqual(event_a.phase_id, phase_a.phase_id,
                         "thread A's note was attributed to thread B's phase")
        self.assertEqual(event_b.phase_id, phase_b.phase_id,
                         "thread B's note was attributed to thread A's phase")

    def test_a_fresh_thread_does_not_inherit_another_threads_open_phase(self):
        """A phase left open on one thread must not leak into a brand-new
        thread that has never opened a phase of its own. Per-thread state
        must start at the class default ("", not inherited), the same as it
        does for the very first thread that ever touches the console.
        """
        tracer = _RecordingTracer()
        console = Console(tracer, "threadrun")

        holder_ready = threading.Event()
        holder_done = threading.Event()
        phase_holder = _phase(1, "node_holder")

        def holder() -> None:
            console.phase_started(phase_holder)
            holder_ready.set()
            holder_done.wait(timeout=5)

        def fresh() -> None:
            self.assertTrue(holder_ready.wait(timeout=5), "holder thread never opened its phase")
            console.note("note from fresh thread")

        t_holder = threading.Thread(target=holder)
        t_fresh = threading.Thread(target=fresh)
        t_holder.start()
        t_holder_ready = holder_ready.wait(timeout=5)
        self.assertTrue(t_holder_ready, "holder thread never signalled readiness")
        t_fresh.start()
        t_fresh.join(timeout=10)
        holder_done.set()
        t_holder.join(timeout=10)

        matches = [e for e in tracer.events
                  if e.type == "log" and "note from fresh thread" in e.payload.get("message", "")]
        self.assertEqual(len(matches), 1, "the fresh thread's note was not recorded")
        self.assertEqual(matches[0].phase_id, "",
                         "a fresh thread inherited another thread's open phase_id")

    def test_single_threaded_behaviour_is_unchanged(self):
        """The thread-local retrofit must not alter the main-thread story:
        phase_started sets the slot, phase_ended clears it, exactly as before.
        """
        tracer = _RecordingTracer()
        console = Console(tracer, "mainthread")
        phase = _phase(1, "solo")

        console.phase_started(phase)
        self.assertEqual(console.phase_id, phase.phase_id)
        self.assertEqual(console.phase_name, phase.params.name)
        console.note("solo note")
        console.phase_ended(phase, 0.01)
        self.assertEqual(console.phase_id, "")
        self.assertEqual(console.phase_name, "")

        solo_note = [e for e in tracer.events
                    if e.type == "log" and "solo note" in e.payload.get("message", "")]
        self.assertEqual(len(solo_note), 1)
        self.assertEqual(solo_note[0].phase_id, phase.phase_id)


class _FakeConsole:
    """A no-op console double: quality.py's tests are about which phase gets
    read, not about what the console prints.
    """

    def note(self, message: str) -> None:
        pass


class _FakeRun:
    """The minimal surface `quality._run`/`_check_dir` read from `run`.

    Deliberately not `adw_modules.runner.Run` — that class is owned by a
    concurrently-edited lane (runner.py/tracer.py) in this same work item,
    and this suite has no business depending on its constructor shape.
    """

    def __init__(self, tmp: Path, phases: list[Phase]) -> None:
        self.adw_id = "qualityrun"
        self.repo_root = tmp
        self.context_handoff_dir = tmp / "context_handoff"
        self.phases = phases
        self.console = _FakeConsole()
        self.tracer = _RecordingTracer()


class Step1QualityExplicitPhase(unittest.TestCase):
    """§12.2 Step 1 item 3 — quality helpers must be given their phase."""

    def test_run_tests_uses_the_passed_phase_not_the_last_one_in_the_list(self):
        """Two phases are on `run.phases`; the *last* one (`run.phases[-1]`)
        is a different node's phase entirely. `run_tests` must key its
        tracer event and its output directory off the phase it was handed,
        not off list position — otherwise a concurrent node's quality check
        is filed under whichever node happened to open its phase last.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            phase_mine = _phase(1, "node_mine")
            phase_last_in_list = _phase(2, "node_someone_elses")
            run = _FakeRun(tmp, phases=[phase_mine, phase_last_in_list])

            result = quality.run_tests(run, phase_mine)

            self.assertEqual(len(result.checks), 1)
            tool_call_events = [e for e in run.tracer.events if e.type == "tool_call"]
            self.assertEqual(len(tool_call_events), 1)
            self.assertEqual(
                tool_call_events[0].phase_id, phase_mine.phase_id,
                "the quality check was attributed to run.phases[-1] instead of the passed phase",
            )
            # _check_dir must key its output path off the passed phase's seq (1),
            # not off run.phases[-1].seq (2).
            expected_dir = run.context_handoff_dir / "quality" / "01_test"
            self.assertTrue(expected_dir.is_dir(),
                            f"expected output dir keyed on the passed phase's seq: {expected_dir}")
            unexpected_dir = run.context_handoff_dir / "quality" / "02_test"
            self.assertFalse(unexpected_dir.exists(),
                             "output dir was keyed on run.phases[-1].seq instead of the passed phase")

    def test_run_quality_uses_the_passed_phase_for_every_block(self):
        """Same property, exercised across all four blocks `run_quality` runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            phase_mine = _phase(7, "node_mine")
            phase_last_in_list = _phase(9, "node_someone_elses")
            run = _FakeRun(tmp, phases=[phase_mine, phase_last_in_list])

            result = quality.run_quality(run, phase_mine)

            self.assertEqual(len(result.checks), 4)
            tool_call_events = [e for e in run.tracer.events if e.type == "tool_call"]
            self.assertEqual(len(tool_call_events), 4)
            for event in tool_call_events:
                self.assertEqual(
                    event.phase_id, phase_mine.phase_id,
                    "a quality block was attributed to run.phases[-1] instead of the passed phase",
                )
            for check in result.checks:
                self.assertTrue(check.output_artifact.startswith(
                    str(run.context_handoff_dir / "quality" / "07_")),
                    f"{check.name}'s output artifact was keyed on the wrong phase seq: "
                    f"{check.output_artifact}",
                )


class ReportedFinding(unittest.TestCase):
    """Not a Step 1 property — a diagnostic that documents a real base defect
    found while writing the tests above, for the report back to the lead.

    `sqlite3.connect(...)` in `Tracer.__init__` (tracer.py:108) does not pass
    `check_same_thread=False`, so a `Tracer` (and therefore a `Run`, since
    `Run.__init__` builds exactly one `Tracer`) cannot have its `.conn` used
    from any thread but the one that constructed it. Two concurrent DAG
    nodes sharing one `Run` — the architecture's own scenario for this
    section — would hit `sqlite3.ProgrammingError: SQLite objects created in
    a thread can only be used in that same thread` the moment the second
    node's thread called `tracer.event(...)`. This test only demonstrates
    the failure exists; fixing it is tracer.py, which this lane does not own.
    """

    def test_a_second_thread_cannot_use_a_tracer_built_on_the_first_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sys.path.insert(0, str(ADWS))
            from adw_modules.tracer import Tracer  # noqa: E402

            tracer = Tracer(tmp / "adw_data" / "sssf.db", tmp / "adw_data" / "events.jsonl")
            tracer.session_start("threadconn", "test")

            errors: list[BaseException] = []

            def other_thread() -> None:
                try:
                    tracer.event(EventRecord(adw_id="threadconn", phase_id="p",
                                             type="log", name="x", payload={}))
                except BaseException as error:  # noqa: BLE001 - capturing for the assertion below
                    errors.append(error)

            t = threading.Thread(target=other_thread)
            t.start()
            t.join(timeout=10)

            self.assertEqual(len(errors), 1,
                             "expected the cross-thread sqlite3 use to raise — if this now "
                             "passes, Tracer must have started passing check_same_thread=False "
                             "and the finding below is stale")
            self.assertIn("SQLite objects created in a thread", str(errors[0]))


if __name__ == "__main__":
    unittest.main()
