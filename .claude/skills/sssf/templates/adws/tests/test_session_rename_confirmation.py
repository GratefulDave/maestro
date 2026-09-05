"""The rename confirmation reaches the pane wrapped, and used to be unmatchable.

Run 98fa094e published its integration ref and then refused its own cleanup:

    {"detail": "SESSION_RENAME_UNCONFIRMED:w1EE:p3: HerdrCallError:
      LAUNCH_REFUSED:{\"error\":{\"code\":\"timeout\",\"message\":\"timed out
      waiting for output match\"},\"id\":\"cli:pane:wait-output\"}",
     "outcome": "CLEANUP_REFUSED"}

The rename had worked. The confirmation was in the pane. It was in the pane on
four lines, because the composer word-wraps its own output -- a real newline
after `to` -- and the terminal then hard-wraps the session name mid-token.
Measured against that pane with `herdr pane wait-output --match`, in all three
of Herdr's snapshot sources: `Session renamed to` matched, `code-reviewer".`
matched, `Session renamed to "FDAdb` did not.

`--source recent-unwrapped` is not the fix, and this file pins that: it rejoins
*terminal* wrapping, and the break after `to` is a newline the renderer emitted
into its own output. Every session name Maestro produces carries a 64-hex
repository fingerprint, so every one of them is long enough to wrap, so a
contiguous match could never confirm any rename this module can ask for. The
run had to publish before anyone reached the code that says so.

The fixture below is the real pane's bytes.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch  # noqa: E402
from herdr_fake import COMPOSER_COLUMNS, FakeHerdr, _as_composer_renders  # noqa: E402

#: `herdr pane read w1EE:p3 --source recent`, verbatim, from the pane that
#: refused run 98fa094e's cleanup.
OBSERVED_PANE_TEXT = (
    'Session renamed to\n'
    '"FDAdb-d89af85d951e2aec923434e0c4449df21ee0d37b8b0237\n'
    'a8fad44cdfcc37d326-98fa-lane-wp8-store-build-code-rev\n'
    'iewer".\n'
    "\U0001f527 tools:12  \U0001f4c4 read:5\n"
)

OBSERVED_SESSION_NAME = (
    "FDAdb-d89af85d951e2aec923434e0c4449df21ee0d37b8b0237"
    "a8fad44cdfcc37d326-98fa-lane-wp8-store-build-code-reviewer"
)


class TheObservedPaneIsRecognised(unittest.TestCase):
    def test_the_contiguous_sentence_is_not_in_the_pane(self) -> None:
        # This is the whole defect. `herdr pane wait-output --match` was asked
        # for exactly this string and timed out while looking at this text.
        self.assertNotIn(
            lch.session_rename_confirmation(OBSERVED_SESSION_NAME),
            OBSERVED_PANE_TEXT,
        )

    def test_the_confirmation_is_recognised_anyway(self) -> None:
        self.assertTrue(
            lch.session_rename_confirmed(
                OBSERVED_PANE_TEXT, OBSERVED_SESSION_NAME
            )
        )

    def test_the_break_is_the_renderers_own_newline(self) -> None:
        # Unwrapping the terminal cannot rejoin `to` and `"FDAdb`: they are on
        # two lines because the composer put them there, not the pane width.
        first, rest = OBSERVED_PANE_TEXT.split("\n", 1)
        self.assertEqual(first, "Session renamed to")
        self.assertLess(len(first), COMPOSER_COLUMNS)
        self.assertTrue(rest.startswith('"FDAdb'))

    def test_a_different_session_name_is_not_confirmed(self) -> None:
        # Removing whitespace must not make the comparison agree with a pane
        # that confirmed some other session.
        self.assertFalse(
            lch.session_rename_confirmed(
                OBSERVED_PANE_TEXT, OBSERVED_SESSION_NAME + "-builder"
            )
        )

    def test_an_empty_pane_is_not_confirmed(self) -> None:
        self.assertFalse(
            lch.session_rename_confirmed("", OBSERVED_SESSION_NAME)
        )

    def test_a_name_broken_the_same_way_by_the_fake(self) -> None:
        # The fake must render the sentence the way the composer does, or no
        # test in the suite can fail while the comparison is wrong.
        rendered = _as_composer_renders(
            lch.session_rename_confirmation(OBSERVED_SESSION_NAME)
        )
        self.assertNotIn(
            lch.session_rename_confirmation(OBSERVED_SESSION_NAME), rendered
        )
        self.assertTrue(
            lch.session_rename_confirmed(rendered, OBSERVED_SESSION_NAME)
        )
        self.assertEqual(rendered.split("\n")[0], "Session renamed to")


def _launcher() -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher.herdr_path = Path("herdr")
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._cleaned_absent = set()
    return launcher


def _handle(pane_id: str, cwd: Path) -> lch.LaunchHandle:
    return lch.LaunchHandle(
        correlation_token="maestro-token",
        pane_id=pane_id,
        agent_name="maestro-token",
        launched_cwd=cwd,
        workspace_id="w1",
        environment={},
    )


class TheRenameIsConfirmedByReadingThePane(unittest.TestCase):
    """`_confirm_session_rename` against a fake whose composer wraps."""

    def _bound(self) -> tuple[FakeHerdr, lch.HerdrLauncher, lch.LaunchHandle]:
        herdr = FakeHerdr()
        launcher = _launcher()
        launcher._herdr = herdr  # type: ignore[method-assign]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pane = herdr._new_pane("w1", "w1:t1", tmp)
        return herdr, launcher, _handle(str(pane["pane_id"]), Path(tmp))

    def test_a_wrapped_confirmation_is_accepted(self) -> None:
        herdr, launcher, handle = self._bound()
        launcher._confirm_session_rename(
            handle, OBSERVED_SESSION_NAME, timeout_s=1.0
        )
        sent = [call for call in herdr.calls if call[:2] == ("pane", "send-text")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3], "/rename " + OBSERVED_SESSION_NAME)

    def test_it_never_asks_herdr_to_match_the_string(self) -> None:
        # The verb that could not do this job is not called at all any more.
        herdr, launcher, handle = self._bound()
        launcher._confirm_session_rename(
            handle, OBSERVED_SESSION_NAME, timeout_s=1.0
        )
        self.assertFalse(
            any(call[:2] == ("pane", "wait-output") for call in herdr.calls)
        )

    def test_a_composer_that_never_confirms_still_refuses(self) -> None:
        herdr, launcher, handle = self._bound()
        herdr.rename_confirms = False
        with self.assertRaises(lch.LaunchRefused) as raised:
            launcher._confirm_session_rename(
                handle, OBSERVED_SESSION_NAME, timeout_s=1.0
            )
        self.assertEqual(
            raised.exception.refusal, lch.LaunchRefusal.SESSION_RENAME_UNCONFIRMED
        )
        self.assertTrue(raised.exception.pane_created)

    def test_an_already_renamed_pane_is_not_renamed_again(self) -> None:
        herdr, launcher, handle = self._bound()
        launcher._confirm_session_rename(
            handle, OBSERVED_SESSION_NAME, timeout_s=1.0
        )
        before = len(herdr.calls)
        launcher._confirm_session_rename(
            handle, OBSERVED_SESSION_NAME, timeout_s=1.0
        )
        self.assertFalse(
            any(
                call[:2] == ("pane", "send-text")
                for call in herdr.calls[before:]
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
