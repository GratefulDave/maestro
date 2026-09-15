"""An amendment closes the panes of the revision it supersedes.

`complete_run_spaces` reads `self._roles` -- what *this* process launched. A
`run amend` invocation launched nothing, so every pane of the parked run is
unreachable through it, and before `close_run_panes` existed nothing closed
them at all: run c9e5b420 parked with ten lane panes alive, and an amendment
would have opened a second set beside them with no way for the operator to tell
which was live.

The selector is the safety property, and it is `tools/cleanup.py`'s: Maestro's
own `kind=lane` token *and* a `run_id` naming this run. An operator's own pane
carries neither. Another run's lane pane carries the first and not the second.
Both sit in the same `herdr pane list` reply as the panes this is for.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from adw_modules import launcher as lch  # noqa: E402

RUN = "c9e5b42013a84883b5ce260b6b69c577"
OTHER_RUN = "2489c772d7c04ad5a2f2bcaa2f4de11c"


def _lane_pane(pane_id: str, run_id: str, lane: str, role: str) -> dict:
    return {
        "pane_id": pane_id,
        "tokens": {
            lch.METADATA_TOKEN_KIND: lch.METADATA_KIND_LANE,
            lch.METADATA_TOKEN_RUN: run_id,
            lch.METADATA_TOKEN_LANE: lane,
            lch.METADATA_TOKEN_ROLE: role,
        },
    }


PANES = [
    _lane_pane("w1FV:p2", RUN, "lane-wp4-release-tests", "tester"),
    _lane_pane("w1FV:p3", RUN, "lane-wp4-release-tests", "test-reviewer"),
    _lane_pane("w1FZ:p2", RUN, "lane-wp4-recalls-build", "builder"),
    # Another run's lane pane: carries kind=lane, wrong run.
    _lane_pane("w1AA:p2", OTHER_RUN, "lane-wp4-release-tests", "tester"),
    # The operator's own pane on the repository: no tokens at all.
    {"pane_id": "w1FA:p1", "cwd": "/Users/davidandrews/PycharmProjects/FDAdb"},
    # A pane carrying a run token but not Maestro's lane kind.
    {
        "pane_id": "w1FA:p9",
        "tokens": {lch.METADATA_TOKEN_RUN: RUN, lch.METADATA_TOKEN_KIND: "console"},
    },
]


class _Herdr:
    """Records the herdr argv this method actually builds."""

    def __init__(self, panes: List[dict], *, missing: Tuple[str, ...] = ()) -> None:
        self.panes = panes
        self.missing = set(missing)
        self.calls: List[Tuple[str, ...]] = []

    def __call__(self, *args: str, **kwargs: object) -> dict:
        self.calls.append(tuple(args))
        if args[:2] == ("pane", "list"):
            return {"result": {"panes": self.panes, "type": "pane_list"}}
        if args[:2] == ("pane", "close"):
            if args[2] in self.missing:
                raise lch.HerdrCallError("gone", lch.PANE_NOT_FOUND)
            return {"result": {}}
        raise AssertionError("unexpected herdr call: {0}".format(args))


def _launcher(herdr: _Herdr) -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher._herdr = herdr  # type: ignore[method-assign]
    launcher._cleaned_absent = set()
    return launcher


def _closed(calls: List[Tuple[str, ...]]) -> List[str]:
    return [call[2] for call in calls if call[:2] == ("pane", "close")]


def test_only_this_runs_lane_panes_are_closed() -> None:
    herdr = _Herdr(list(PANES))
    closed = _launcher(herdr).close_run_panes(RUN)

    assert list(closed) == ["w1FV:p2", "w1FV:p3", "w1FZ:p2"]
    assert _closed(herdr.calls) == ["w1FV:p2", "w1FV:p3", "w1FZ:p2"]
    # The three that must survive, each for its own reason.
    for survivor in ("w1AA:p2", "w1FA:p1", "w1FA:p9"):
        assert survivor not in _closed(herdr.calls)


def test_a_pane_that_vanished_is_the_outcome_not_a_refusal() -> None:
    herdr = _Herdr(list(PANES), missing=("w1FV:p3",))
    closed = _launcher(herdr).close_run_panes(RUN)

    # It was attempted, it was already gone, and the rest still closed.
    assert _closed(herdr.calls) == ["w1FV:p2", "w1FV:p3", "w1FZ:p2"]
    assert list(closed) == ["w1FV:p2", "w1FZ:p2"]


def test_an_unrelated_close_failure_is_raised() -> None:
    class _Angry(_Herdr):
        def __call__(self, *args: str, **kwargs: object) -> dict:
            if args[:2] == ("pane", "close"):
                raise lch.HerdrCallError("boom", "some_other_code")
            return super().__call__(*args, **kwargs)

    with pytest.raises(lch.HerdrCallError):
        _launcher(_Angry(list(PANES))).close_run_panes(RUN)


def test_an_empty_run_id_closes_nothing() -> None:
    herdr = _Herdr(list(PANES))
    assert _launcher(herdr).close_run_panes("") == ()
    assert herdr.calls == []


def test_amend_closes_the_superseded_revisions_panes() -> None:
    """The wiring: `_run_amend` calls it before the new scheduler launches."""
    source = (RUNTIME_ROOT / "maestro.py").read_text()
    body = source.split("def _run_amend(", 1)[1].split("\ndef ", 1)[0]

    assert "close_panes(run_id)" in body, "amend does not close panes"
    assert body.index("apply_factory_amendment(") < body.index(
        "close_panes(run_id)"
    ), "panes are closed before the amendment lands"
    assert body.index("close_panes(run_id)") < body.index(
        "scheduler.run()"
    ), "panes are closed after the new scheduler has launched its own"


def test_the_selector_matches_the_cleanup_tool() -> None:
    """Two readers that close panes must not disagree about which are closeable."""
    tool = (RUNTIME_ROOT / "tools" / "cleanup.py").read_text()
    method = (RUNTIME_ROOT / "adw_modules" / "launcher.py").read_text()
    method = method.split("def close_run_panes(", 1)[1].split("\n    def ", 1)[0]

    for token in ("METADATA_TOKEN_KIND", "METADATA_KIND_LANE", "METADATA_TOKEN_RUN"):
        assert token in tool, token
        assert token in method, token
