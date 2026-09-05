"""`cleanup.py` closes this repository's finished lane panes and nothing else.

The cases are about what it cannot close. An operator's own pane, another
repository's lane pane, and the parent Space Maestro never closes all sit in the
same `herdr pane list` reply as the panes this tool is for, so the selector is
the whole safety property: Maestro's `kind=lane` token, plus a `run_id` token
naming a run in *this* installation's ledger.

The other refusal is liveness. A lane pane belonging to a run that is still
executing is that run's agent, and closing it kills the turn it is in. So a live
`maestro.py run` process for the repository refuses the close and names the pid,
and `--force` is the operator overriding that rather than a way past an
inconvenient message.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = RUNTIME_ROOT / "tools" / "cleanup.py"


def _load_tool():
    if str(RUNTIME_ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("maestro_cleanup", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load_tool()

OURS = "a" * 32
ALSO_OURS = "b" * 32
THEIRS = "c" * 32


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    return root


def _ledger(path: Path, run_ids: List[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "create table runs (run_id text primary key, updated_at text)"
        )
        connection.executemany(
            "insert into runs values (?, ?)",
            [(run_id, "2026-09-05T01:24:04Z") for run_id in run_ids],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _pane(pane_id: str, workspace: str, **tokens: str) -> dict:
    item: dict = {"pane_id": pane_id, "workspace_id": workspace}
    if tokens:
        item["tokens"] = dict(tokens)
    return item


#: One `herdr pane list` reply carrying every kind of pane at once.
PANE_LIST = {
    "result": {
        "type": "pane_list",
        "panes": [
            # This repository's finished run: the panes to close.
            _pane(
                "w1:p2",
                "w1",
                kind="lane",
                run_id=OURS,
                lane="lane-wp8-store-build",
                role="builder",
                repo="fingerprint",
            ),
            _pane(
                "w1:p3",
                "w1",
                kind="lane",
                run_id=OURS,
                lane="lane-wp8-store-build",
                role="code-reviewer",
                repo="fingerprint",
            ),
            _pane(
                "w2:p2",
                "w2",
                kind="lane",
                run_id=ALSO_OURS,
                lane="lane-wp7-tests",
                role="tester",
                repo="fingerprint",
            ),
            # The parent Space: tagged `kind=run`, and never closed.
            _pane("w0:p1", "w0", kind="run", run_id=OURS, repo="fingerprint"),
            # Another repository's run, in the same Herdr.
            _pane(
                "w9:p2",
                "w9",
                kind="lane",
                run_id=THEIRS,
                lane="lane-wp1-build",
                role="builder",
                repo="other",
            ),
            # The operator's own pane. No Maestro tokens at all.
            _pane("wA:p1", "wA"),
        ],
    }
}


class _Herdr:
    """Records every herdr call the tool makes, answers `pane list`."""

    def __init__(self) -> None:
        self.calls: List[tuple[str, ...]] = []

    def __call__(self, herdr: str, *args: str) -> dict:
        del herdr
        self.calls.append(args)
        if args[:2] == ("pane", "list"):
            return PANE_LIST
        if args[:2] == ("pane", "close"):
            return {"result": {"type": "ok"}}
        raise AssertionError(args)

    @property
    def closed(self) -> List[str]:
        return [call[2] for call in self.calls if call[:2] == ("pane", "close")]


@pytest.fixture()
def bound(tmp_path, monkeypatch):
    """A repository, its ledger, a registry naming both, and a fake herdr."""
    repository = _git_repo(tmp_path / "repo")
    state = tmp_path / "state"
    database = _ledger(state / "lifecycle.sqlite3", [OURS, ALSO_OURS])
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "installations": [
                    {
                        "database": str(database),
                        "repository": str(repository),
                        "state": str(state),
                    },
                    {
                        "database": str(tmp_path / "other.sqlite3"),
                        "repository": str(tmp_path / "elsewhere"),
                        "state": str(tmp_path / "other-state"),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    herdr = _Herdr()
    monkeypatch.setattr(cleanup, "_herdr", herdr)
    monkeypatch.setattr(cleanup, "_herdr_path", lambda: "herdr")
    monkeypatch.setattr(cleanup, "_live_run_pids", lambda repository: [])
    return {
        "repository": repository,
        "registry": registry,
        "herdr": herdr,
        "argv": ["--repo", str(repository), "--registry", str(registry)],
    }


def test_a_dry_run_closes_nothing(bound, capsys) -> None:
    assert cleanup.main(bound["argv"]) == 0
    assert bound["herdr"].closed == []
    assert "dry run" in capsys.readouterr().out


def test_apply_closes_exactly_this_repositorys_lane_panes(bound) -> None:
    assert cleanup.main([*bound["argv"], "--apply"]) == 0
    assert sorted(bound["herdr"].closed) == ["w1:p2", "w1:p3", "w2:p2"]


def test_the_parent_space_is_never_closed(bound) -> None:
    cleanup.main([*bound["argv"], "--apply"])
    assert "w0:p1" not in bound["herdr"].closed


def test_another_repositorys_run_is_never_closed(bound) -> None:
    # Same Herdr, same `pane list` reply, a run this ledger does not hold.
    cleanup.main([*bound["argv"], "--apply"])
    assert "w9:p2" not in bound["herdr"].closed


def test_an_operators_own_pane_is_never_closed(bound) -> None:
    cleanup.main([*bound["argv"], "--apply"])
    assert "wA:p1" not in bound["herdr"].closed


def test_run_scoping_leaves_the_other_runs_panes(bound) -> None:
    assert cleanup.main([*bound["argv"], "--run", OURS, "--apply"]) == 0
    assert sorted(bound["herdr"].closed) == ["w1:p2", "w1:p3"]


def test_a_live_run_process_refuses_the_close(bound, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cleanup, "_live_run_pids", lambda repository: ["4242"])
    assert cleanup.main([*bound["argv"], "--apply"]) == 3
    assert bound["herdr"].closed == []
    reported = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(reported)
    assert payload["outcome"] == "CLEANUP_REFUSED"
    assert payload["detail"] == "RUN_PROCESS_ALIVE:4242"


def test_force_overrides_a_live_run_process(bound, monkeypatch) -> None:
    monkeypatch.setattr(cleanup, "_live_run_pids", lambda repository: ["4242"])
    assert cleanup.main([*bound["argv"], "--apply", "--force"]) == 0
    assert sorted(bound["herdr"].closed) == ["w1:p2", "w1:p3", "w2:p2"]


def test_a_repository_with_no_installation_refuses(bound, tmp_path, capsys) -> None:
    outsider = _git_repo(tmp_path / "outsider")
    code = cleanup.main(
        ["--repo", str(outsider), "--registry", str(bound["registry"])]
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["outcome"] == "CLEANUP_REFUSED"
    assert payload["detail"].startswith("NO_INSTALLATION_FOR:")
    assert bound["herdr"].closed == []


def test_a_missing_registry_refuses_rather_than_raising(bound, tmp_path, capsys) -> None:
    code = cleanup.main(
        ["--repo", str(bound["repository"]), "--registry", str(tmp_path / "none.json")]
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["detail"].startswith("REGISTRY_ABSENT:")


def test_a_refused_close_is_reported_and_exits_nonzero(bound, monkeypatch) -> None:
    def refusing(herdr: str, *args: str) -> dict:
        if args[:2] == ("pane", "close") and args[2] == "w1:p3":
            raise cleanup.CleanupRefused("HERDR_REFUSED:pane close: busy")
        return bound["herdr"](herdr, *args)

    monkeypatch.setattr(cleanup, "_herdr", refusing)
    assert cleanup.main([*bound["argv"], "--apply"]) == 3
    # The refusal stops that one pane, never the rest.
    assert sorted(bound["herdr"].closed) == ["w1:p2", "w2:p2"]
