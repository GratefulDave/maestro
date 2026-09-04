"""The envelope is declared by a rename, never by writing into place.

Two readers open the agent's envelope: `HerdrStageActor._await_envelope` polls
it, and `HerdrLauncher._declared_result` reads the same path to decide whether
the turn declared. Both can catch a file mid-write, and the launcher turns one
unlucky read into a permanent `EXITED/ENVELOPE_UNPARSED` verdict.

There is no state in which a reader can know the write finished. `EXITED` does
not mean the process died -- role sessions persist across turns -- and the pane
signals lied three times before (see `launcher.poll`). Only the writer knows, so
the writer declares by renaming a complete `.part` file into place. These cases
pin that protocol:

* the turn instruction and the role contract both tell the agent to rename;
* a `.part` file alone is not an envelope, so a reader never sees a partial one;
* a genuinely corrupt envelope still refuses, so this widens nothing.

Observed on run a33d5e9b4a404f5889785cb1c9ca5f6f: the builder's envelope read
`{"changed": false}` on disk, complete and valid, while the run had already
refused `STAGE_PAYLOAD_INVALID` from reading it a fraction of a second earlier.
"""

from __future__ import annotations

import json
import sys
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
    """Polls GONE, optionally finishing the write first (the real race)."""

    def __init__(self, on_poll=None) -> None:
        self._on_poll = on_poll

    def poll(self, handle):
        del handle
        if self._on_poll is not None:
            self._on_poll()
        return types.SimpleNamespace(state=lch.PollState.GONE)


def _actor(launcher) -> M.HerdrStageActor:
    actor = M.HerdrStageActor.__new__(M.HerdrStageActor)
    actor.launcher = launcher
    actor.step = None
    return actor


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
            with self.assertRaises(M.FactoryRefused) as caught:
                M.HerdrStageActor._await_envelope(
                    _actor(_Launcher()), _Handle(tmp), envelope, "builder", "lane-x"
                )
            # Missing, not invalid: nothing was ever declared at that path.
            self.assertIn("STAGE_PAYLOAD_MISSING", str(caught.exception))

    def test_a_rename_that_lands_before_the_final_read_is_honoured(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            part = tmp / "envelope-1.json.part"
            envelope.write_text('{"changed": fal')  # an in-place partial write

            def finish_the_write() -> None:
                part.write_text('{"changed": false}')
                part.replace(envelope)

            out = M.HerdrStageActor._await_envelope(
                _actor(_Launcher(finish_the_write)),
                _Handle(tmp),
                envelope,
                "builder",
                "lane-x",
            )
            self.assertEqual(out, {"changed": False})

    def test_a_genuinely_corrupt_envelope_still_refuses(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            envelope.write_text("not json at all")
            with self.assertRaises(M.FactoryRefused) as caught:
                M.HerdrStageActor._await_envelope(
                    _actor(_Launcher()), _Handle(tmp), envelope, "builder", "lane-x"
                )
            self.assertIn("STAGE_PAYLOAD_INVALID", str(caught.exception))

    def test_a_reviewer_envelope_without_a_verdict_still_refuses(self):
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            envelope = tmp / "envelope-1.json"
            envelope.write_text(json.dumps({"findings": []}))
            with self.assertRaises(M.FactoryRefused) as caught:
                M.HerdrStageActor._await_envelope(
                    _actor(_Launcher()),
                    _Handle(tmp),
                    envelope,
                    "code-reviewer",
                    "lane-x",
                )
            self.assertIn("STAGE_PAYLOAD_INVALID", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
