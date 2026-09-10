"""A handle is bound to its terminal, not to the pane id it started in.

Real-binary observation, herdr 0.9.0, 2026-09-10, in a throwaway Space that was
closed afterwards. A pane split off ``w1AT:t1`` was moved with::

    herdr pane move w1AT:p2R --tab w1J3:t1 --split right

and ``move_result`` reported ``previous_pane_id`` ``w1AT:p2R``,
``previous_tab_id`` ``w1AT:t8``, ``previous_workspace_id`` ``w1AT``, and a pane
whose ``pane_id`` was now ``w1J3:p2`` and whose ``tab_id`` and ``workspace_id``
had changed with it. ``terminal_id`` stayed ``term_65b1a58627363b8`` throughout.
A following ``herdr pane get w1AT:p2R`` -- the *stale* id -- exited 0 and
returned the pane under its new id rather than ``pane_not_found``, which is
exactly the shape ``_verified_handle_binding`` refused on FDAdb run d246ae95.

A fake proves only what a field is permitted to be, so both shapes are covered
below: the stale id resolving to the moved pane, and the stale id being gone
(the list fallback). The terminal is the durable key in both.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch


TERMINAL = "term_65b1a58627363b8"


def _launcher() -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher.herdr_path = Path("herdr")
    launcher.omp_path = Path("omp")
    launcher.claude_path = Path("claude")
    launcher.admitted_routes = None  # type: ignore[assignment]
    launcher.provision_argv = ()
    launcher.workspace_label = "product run-1"
    launcher.agent_start_busy_window_s = 0.0
    launcher.quiescence_confirm_s = 0.0
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._tailers = {}
    launcher._quiescent_since = {}
    launcher._proven_absent = {}
    launcher._split_parent_id = None
    launcher._parent_workspace_id = ""
    launcher._invocation_workspace_id = ""
    launcher._workspace_id = ""
    launcher._run_id = "e892fe8df79046ca8ea6504934e912c6"
    launcher._repository_fingerprint = "repo-fdadb"
    launcher._repository_root = Path("/repo/product")
    launcher._tabs = {}
    launcher._role_handles = {}
    launcher._cleaned_absent = set()
    return launcher


class _Herdr:
    """One pane, one agent, and a `relocate` that behaves like herdr's move."""

    def __init__(self, cwd: Path, name: str) -> None:
        self.name = name
        self.pane = {
            "pane_id": "w1HY:p2",
            "tab_id": "w1HY:t1",
            "workspace_id": "w1HY",
            "terminal_id": TERMINAL,
            "cwd": str(cwd),
        }
        #: Ids `pane get` still resolves to the live pane, as herdr does after
        #: a move; emptied to model a stale id that is genuinely gone.
        self.aliases = {"w1HY:p2"}
        self.calls: list[tuple[str, ...]] = []

    def relocate(self, pane_id: str, tab_id: str, *, keep_alias: bool) -> None:
        self.pane["pane_id"] = pane_id
        self.pane["tab_id"] = tab_id
        self.pane["workspace_id"] = tab_id.split(":")[0]
        self.aliases = ({"w1HY:p2", pane_id} if keep_alias else {pane_id})

    def __call__(self, *args: str, **kwargs: object) -> dict:
        del kwargs
        self.calls.append(tuple(args))
        if args[:2] == ("pane", "get"):
            if args[2] not in self.aliases:
                raise lch.HerdrCallError(lch.PANE_NOT_FOUND, "pane_not_found")
            return {"result": {"type": "pane_info", "pane": dict(self.pane)}}
        if args[:2] == ("pane", "list"):
            return {"result": {"type": "pane_list", "panes": [dict(self.pane)]}}
        if args[:2] == ("agent", "get"):
            return {
                "result": {
                    "type": "agent_info",
                    "agent": {
                        "name": self.name,
                        "agent_status": "idle",
                        "pane_id": self.pane["pane_id"],
                        "cwd": self.pane["cwd"],
                    },
                }
            }
        raise AssertionError(args)


def _bound(launcher: lch.HerdrLauncher, cwd: Path) -> lch.LaunchHandle:
    token = lch.role_session_token("run-1", "lane-a", "tester")
    handle = lch.LaunchHandle(
        token,
        "w1HY:p2",
        lch.agent_name_for(token),
        cwd,
        terminal_id=TERMINAL,
        workspace_id="w1HY",
        tab_id="w1HY:t1",
        lane_key="lane-a",
        child_workspace_id="w1HY",
        pane_role="tester",
        lane_label="lane-a",
    )
    launcher._handles[token] = handle
    return handle


class ARelocatedPaneIsReResolved(unittest.TestCase):
    def test_a_cross_space_move_does_not_refuse_the_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=True)

            launcher._verified_handle_binding(handle)

            self.assertEqual(handle.pane_id, "w1FA:p7")
            self.assertEqual(handle.tab_id, "w1FA:t1")
            self.assertEqual(handle.workspace_id, "w1FA")
            self.assertEqual(handle.terminal_id, TERMINAL)

    def test_a_relocation_is_found_when_the_stale_id_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=False)

            launcher._verified_handle_binding(handle)

            self.assertEqual(handle.pane_id, "w1FA:p7")
            self.assertIn(("pane", "list"), [call[:2] for call in herdr.calls])

    def test_the_lane_space_maestro_must_close_is_not_rewritten(self) -> None:
        # An operator dragging a lane pane into their own Space must not make
        # that Space Maestro's to reap. `child_workspace_id` is cleanup
        # ownership, not a display coordinate.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=True)

            launcher._verified_handle_binding(handle)

            self.assertEqual(handle.child_workspace_id, "w1HY")

    def test_a_pane_that_never_moved_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]

            launcher._verified_handle_binding(handle)

            self.assertEqual(handle.pane_id, "w1HY:p2")
            self.assertNotIn(("pane", "list"), [call[:2] for call in herdr.calls])


class ADifferentTerminalStillRefuses(unittest.TestCase):
    def test_a_replacement_pane_under_another_terminal_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=True)
            herdr.pane["terminal_id"] = "term_deadbeef"

            with self.assertRaises(lch.LaunchRefused) as caught:
                launcher._verified_handle_binding(handle)

            self.assertIs(
                caught.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertEqual(handle.pane_id, "w1HY:p2")

    def test_a_vanished_terminal_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=False)
            herdr.pane["terminal_id"] = "term_deadbeef"

            with self.assertRaises(lch.LaunchRefused) as caught:
                launcher._verified_handle_binding(handle)

            self.assertIs(
                caught.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )

    def test_a_relocated_pane_holding_another_session_refuses(self) -> None:
        # The terminal survived the move, but the named agent is no longer the
        # one sitting in it. That is a different session and must not receive
        # this lane's prompt.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=True)
            original = herdr.__call__

            def call(*args: str, **kwargs: object) -> dict:
                payload = original(*args, **kwargs)
                if args[:2] == ("agent", "get"):
                    payload["result"]["agent"]["pane_id"] = "w1FA:p9"
                return payload

            launcher._herdr = call  # type: ignore[method-assign]

            with self.assertRaises(lch.LaunchRefused) as caught:
                launcher._verified_handle_binding(handle)

            self.assertIs(
                caught.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertEqual(handle.pane_id, "w1HY:p2")

    def test_a_handle_with_no_recorded_terminal_keeps_the_strict_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            launcher = _launcher()
            handle = _bound(launcher, cwd)
            object.__setattr__(handle, "terminal_id", "")
            herdr = _Herdr(cwd, handle.agent_name)
            launcher._herdr = herdr  # type: ignore[method-assign]
            herdr.relocate("w1FA:p7", "w1FA:t1", keep_alias=True)

            with self.assertRaises(lch.LaunchRefused) as caught:
                launcher._verified_handle_binding(handle)

            self.assertIs(
                caught.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )


if __name__ == "__main__":
    unittest.main()
