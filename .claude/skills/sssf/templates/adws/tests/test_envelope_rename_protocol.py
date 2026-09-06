"""The envelope is declared by a rename, never by writing into place.

Two readers open the agent's envelope: `HerdrStageActor._await_envelope` polls
it, and `HerdrLauncher._declared_result` reads the same path to decide whether
the turn declared. Both can catch a file mid-write, and the launcher turns one
unlucky read into a permanent `EXITED/ENVELOPE_UNPARSED` verdict.

There is no state in which a reader can know the write finished. Only the
writer knows, so the writer declares by renaming a complete `.part` file into
place. These cases pin that protocol:

* the turn instruction and the role contract both tell the agent to rename;
* a `.part` file alone is not an envelope, so a reader never sees a partial one;
* a corrupt or verdict-less envelope is not a declaration either, so the
  protocol widens nothing.

What none of them do any more is refuse. `_await_envelope` used to answer the
partial-write race by consulting `poll` and raising `STAGE_PAYLOAD_INVALID` or
`STAGE_PAYLOAD_MISSING` when the pane looked dead -- which is how run
`a33d5e9b4a404f5889785cb1c9ca5f6f` refused `STAGE_PAYLOAD_INVALID` while a
complete, valid `{"changed": false}` sat on disk, written a fraction of a
second after the read. That branch is gone: nothing but the envelope ends the
wait, so an unfinished, unparsed, or unqualified file simply is not a
declaration yet, and the writer gets to finish. See
`test_absence_is_not_a_verdict`.
"""

from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro as M  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402


class _Handle:
    def __init__(self, cwd: Path) -> None:
        self.launched_cwd = str(cwd)


class _Launcher:
    """Polls GONE -- the reading `_await_envelope` no longer takes."""

    def poll(self, handle):
        del handle
        return types.SimpleNamespace(state=lch.PollState.GONE)


def _actor(launcher) -> M.HerdrStageActor:
    actor = M.HerdrStageActor.__new__(M.HerdrStageActor)
    actor.launcher = launcher
    actor.step = None
    return actor


def _await_in_thread(tmp: Path, envelope: Path, role: str):
    box: dict = {}

    def run() -> None:
        try:
            box["payload"] = M.HerdrStageActor._await_envelope(
                _actor(_Launcher()), _Handle(tmp), envelope, role, "lane-x"
            )
        except BaseException as exc:  # pragma: no cover - failure is the point
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return box, thread


class EnvelopeRenameProtocolTests(unittest.TestCase):
    def test_the_turn_instruction_tells_the_agent_to_rename_into_place(self):
        source = (ADWS / "maestro.py").read_text(encoding="utf-8")
        self.assertIn("Create UTF-8 JSON at {0}.part", source)
        self.assertIn("then rename it to {0}", source)
        self.assertIn("never write into {0} directly", source)
        self.assertIn(
            "envelope to its `.part` sibling and rename it into place",
            source,
        )

    def test_a_part_file_alone_is_not_read_as_the_envelope(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            (tmp / "envelope-1.json.part").write_text('{"changed": true}')
            box, thread = _await_in_thread(tmp, envelope, "builder")
            thread.join(timeout=1.0)
            # Nothing was declared at that path, so nothing is returned and
            # nothing is refused.
            self.assertTrue(thread.is_alive())
            self.assertEqual(box, {})

    def test_a_rename_that_lands_after_a_partial_read_is_honoured(self):
        """The a33d5e9b race, with the refusal removed rather than timed.

        An in-place partial write is on disk when the wait starts. The writer
        then finishes properly, by rename. The wait must return that value --
        previously it could refuse first and never see it.
        """
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            part = tmp / "envelope-1.json.part"
            envelope.write_text('{"changed": fal')  # an in-place partial write

            box, thread = _await_in_thread(tmp, envelope, "builder")
            thread.join(timeout=0.5)
            self.assertTrue(thread.is_alive())

            part.write_text('{"changed": false}')
            part.replace(envelope)

            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(box.get("payload"), {"changed": False})

    def test_a_corrupt_envelope_is_not_a_declaration(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            envelope.write_text("not json at all")
            box, thread = _await_in_thread(tmp, envelope, "builder")
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive())
            self.assertEqual(box, {})

    def test_a_reviewer_envelope_without_a_verdict_is_not_a_declaration(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            envelope.write_text(json.dumps({"findings": []}))
            box, thread = _await_in_thread(tmp, envelope, "code-reviewer")
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive())
            self.assertEqual(box, {})


if __name__ == "__main__":
    unittest.main()
