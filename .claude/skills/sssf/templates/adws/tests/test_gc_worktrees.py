"""`gc_worktrees.py` reclaims only what is derived, and only when told to.

The cases here are about what the tool refuses. A reclaimer that deletes a live
run's builder checkout destroys uncommitted work that no artifact carries, and a
reclaimer that follows a symlink out of the worktrees root deletes something
nobody asked it about. So the assertions are not "the right thing was removed"
but "the wrong thing cannot be", under every flag: a live run's checkout is kept
with `--all` and `--keep 0`, a dry run leaves every byte in place, and an escape
is refused even when the path is handed straight to the removal function.

The byte count is asserted against the tree measured independently before the
run, because a reclaim report that overstates what it freed is the number an
operator uses to decide whether to run it again.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = RUNTIME_ROOT / "tools" / "gc_worktrees.py"


def _load_tool():
    if str(RUNTIME_ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("gc_worktrees", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines dataclasses, and
    # `dataclasses` resolves annotations through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gcw = _load_tool()


LIVE_RUN = "f" * 32
DONE_RUN = "a" * 32
ORPHAN_RUN = "b" * 32

#: Old enough that the liveness window never protects anything the case built.
OLD = time.time() - 30 * 24 * 3600


def _write_tree(root: Path, *, payload: bytes = b"x" * 1024) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "file.txt").write_bytes(payload)
    return root


def _age(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _ledger(database: Path, repository: Path) -> None:
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, "
            "target_repository_root TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE lane_state (run_id TEXT NOT NULL, lane_id TEXT NOT NULL, "
            "stage TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE run_artifacts (run_id TEXT NOT NULL, "
            "artifact_kind TEXT NOT NULL, input_digest TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO runs VALUES (?, ?)",
            [(LIVE_RUN, str(repository)), (DONE_RUN, str(repository))],
        )
        conn.executemany(
            "INSERT INTO lane_state VALUES (?, ?, ?)",
            [
                (LIVE_RUN, "lane-a", "BUILDING"),
                (LIVE_RUN, "lane-b", "MERGED"),
                (DONE_RUN, "lane-a", "MERGED"),
                (DONE_RUN, "lane-b", "MERGED"),
            ],
        )
        conn.execute(
            "INSERT INTO run_artifacts VALUES (?, ?, ?)",
            (DONE_RUN, "MAIN_PUBLICATION", "d" * 64),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def installation(tmp_path: Path):
    """A state root holding a live run, a finished run, an orphan, and scratch."""
    state = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir()
    worktrees = state / "worktrees"
    worktrees.mkdir(parents=True)
    _ledger(state / "lifecycle.sqlite3", repository)

    built: dict[str, Path] = {}

    for run_id in (LIVE_RUN, DONE_RUN, ORPHAN_RUN):
        for role in ("builder", "code-reviewer"):
            tree = _write_tree(worktrees / run_id / "lane-a" / role / "checkout")
            _age(tree, OLD)
        _age(worktrees / run_id, OLD)
        built[run_id] = worktrees / run_id

    # Four review rounds for one lane, aged so the recency window is decidable.
    for index in range(4):
        name = "review-lane-a-{0}".format("0123456789ab"[:11] + str(index))
        tree = _write_tree(worktrees / name)
        _age(tree, OLD + index)
        built[name] = tree

    # A draft tree registered in the vault of the live run, and one whose vault
    # is gone. Only the first is anchoring an unpinned commit.
    vaults = state / "vaults"
    live_reg = vaults / "{0}.git".format(LIVE_RUN) / "worktrees" / "draft-lane-a-c0ffee01"
    live_reg.mkdir(parents=True)
    live_draft = _write_tree(worktrees / "draft-lane-a-c0ffee01")
    (live_draft / ".git").write_text("gitdir: {0}\n".format(live_reg), encoding="utf-8")
    _age(live_draft, OLD)
    built["live-draft"] = live_draft

    dead_draft = _write_tree(worktrees / "draft-lane-z-deadbeef")
    (dead_draft / ".git").write_text(
        "gitdir: {0}/{1}.git/worktrees/draft-lane-z-deadbeef\n".format(
            vaults, ORPHAN_RUN
        ),
        encoding="utf-8",
    )
    _age(dead_draft, OLD)
    built["dead-draft"] = dead_draft

    # The run-level integration gate materializes the same way a review tree
    # does: no registration, rebuilt from the integration head on demand.
    gate = _write_tree(worktrees / "integration-gate-lane-a-ba0e81e38364")
    _age(gate, OLD)
    built["integration-gate"] = gate

    # A name the factory never emits. Unknown means keep.
    stranger = _write_tree(worktrees / "notes-i-left-here")
    _age(stranger, OLD)
    built["stranger"] = stranger

    _age(worktrees, OLD)
    inst = gcw.Installation(
        state=state,
        database=state / "lifecycle.sqlite3",
        repository=repository,
    )
    return inst, built


def _decisions(plan) -> dict[str, str]:
    return {entry.path.name: entry.decision for entry in plan.entries}


def _plan(inst, **kwargs):
    runs = gcw.read_runs(inst.database)
    kwargs.setdefault("min_age_s", 0.0)
    return gcw.classify(inst, runs, **kwargs)


def test_dry_run_removes_nothing(installation):
    inst, built = installation
    before = sorted(
        str(path.relative_to(inst.state))
        for path in inst.state.rglob("*")
    )
    plans = gcw.gc([inst], keep=0, min_age_s=0.0, apply=False)
    assert plans[0].reclaimable, "the fixture must offer something to reclaim"
    after = sorted(
        str(path.relative_to(inst.state))
        for path in inst.state.rglob("*")
    )
    assert after == before
    rendered = gcw.render(plans, applied=False, verbose=False)
    assert "nothing was removed" in rendered


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep": 0, "take_all": True},
        {"keep": 0, "take_all": False},
        {"keep": 10, "take_all": True},
    ],
)
def test_live_run_checkouts_are_never_selected(installation, kwargs):
    inst, built = installation
    plan = _plan(inst, **kwargs)
    decisions = _decisions(plan)
    assert decisions[LIVE_RUN] == gcw.KEEP
    reclaimed = {entry.path for entry in plan.reclaimable}
    checkout = built[LIVE_RUN] / "lane-a" / "builder" / "checkout"
    assert not any(
        path == checkout or path in checkout.parents for path in reclaimed
    )


def test_terminal_and_orphan_runs_are_selected(installation):
    inst, _ = installation
    decisions = _decisions(_plan(inst, keep=0))
    assert decisions[DONE_RUN] == gcw.RECLAIM
    assert decisions[ORPHAN_RUN] == gcw.RECLAIM


def test_scratch_trees_are_selected_and_strangers_are_not(installation):
    inst, _ = installation
    decisions = _decisions(_plan(inst, keep=0, take_all=True))
    assert decisions["draft-lane-z-deadbeef"] == gcw.RECLAIM
    assert decisions["integration-gate-lane-a-ba0e81e38364"] == gcw.RECLAIM
    assert all(
        decisions[name] == gcw.RECLAIM
        for name in decisions
        if name.startswith("review-lane-a-")
    )
    assert decisions["notes-i-left-here"] == gcw.KEEP


def test_registered_draft_of_a_live_run_is_kept(installation):
    inst, _ = installation
    decisions = _decisions(_plan(inst, keep=0, take_all=True))
    assert decisions["draft-lane-a-c0ffee01"] == gcw.KEEP


def test_keep_window_is_honoured_and_all_overrides_it(installation):
    inst, _ = installation
    windowed = _decisions(_plan(inst, keep=2))
    reviews = {
        name: decision
        for name, decision in windowed.items()
        if name.startswith("review-lane-a-")
    }
    assert sorted(reviews.values()).count(gcw.KEEP) == 2
    assert sorted(reviews.values()).count(gcw.RECLAIM) == 2

    taken = _decisions(_plan(inst, keep=2, take_all=True))
    assert all(
        decision == gcw.RECLAIM
        for name, decision in taken.items()
        if name.startswith("review-lane-a-")
    )


def test_liveness_window_keeps_a_freshly_touched_tree(installation):
    inst, built = installation
    fresh = built["review-lane-a-0123456789a0"]
    now = time.time()
    _age(fresh, now)
    decisions = _decisions(
        _plan(inst, keep=0, take_all=True, min_age_s=3600.0, now=now)
    )
    assert decisions[fresh.name] == gcw.KEEP


def test_a_path_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "state" / "worktrees"
    root.mkdir(parents=True)
    outside = tmp_path / "precious"
    outside.mkdir()
    with pytest.raises(gcw.GcRefused):
        gcw.assert_within(root, outside)
    with pytest.raises(gcw.GcRefused):
        gcw.assert_within(root, root)
    with pytest.raises(gcw.GcRefused):
        gcw.assert_within(root, root / ".." / "vault")


def test_a_symlink_out_of_the_root_is_refused_by_removal(tmp_path):
    root = tmp_path / "state" / "worktrees"
    root.mkdir(parents=True)
    outside = _write_tree(tmp_path / "precious")
    link = root / "review-lane-a-deadbeef"
    link.symlink_to(outside, target_is_directory=True)
    entry = gcw.Entry(
        path=link,
        kind="review",
        group="review-lane-a",
        size=1024,
        mtime=OLD,
        decision=gcw.RECLAIM,
        reason="handed in directly",
    )
    with pytest.raises(gcw.GcRefused):
        gcw.remove_entry(entry, worktrees_root=root, index={})
    assert (outside / "src" / "file.txt").exists()


def test_a_symlinked_entry_is_classified_keep(installation):
    inst, _ = installation
    outside = _write_tree(inst.state.parent / "elsewhere")
    link = inst.worktrees / "review-lane-a-facefeed"
    link.symlink_to(outside, target_is_directory=True)
    decisions = _decisions(_plan(inst, keep=0, take_all=True))
    assert decisions[link.name] == gcw.KEEP
    assert (outside / "src" / "file.txt").exists()


def test_apply_removes_exactly_what_it_reported(installation):
    inst, _ = installation
    plan = gcw.classify(
        inst, gcw.read_runs(inst.database), keep=0, take_all=True, min_age_s=0.0
    )
    selected = {entry.path: gcw.directory_size(entry.path) for entry in plan.reclaimable}
    assert selected
    kept = [entry.path for entry in plan.kept]

    applied = gcw.gc([inst], keep=0, take_all=True, min_age_s=0.0, apply=True)
    reported = sum(entry.size for entry in applied[0].reclaimable)

    assert reported == sum(selected.values())
    for path in selected:
        assert not path.exists()
    for path in kept:
        assert path.exists()


def test_an_unreadable_ledger_keeps_everything(installation):
    inst, _ = installation
    inst.database.unlink()
    plans = gcw.gc([inst], keep=0, take_all=True, min_age_s=0.0, apply=True)
    assert plans[0].reclaimable == []
    assert "ledger unreadable" in plans[0].note
    assert (inst.worktrees / DONE_RUN).exists()


def test_registry_reading_is_by_state_root(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "installations": [
                    {
                        "state": "/one",
                        "database": "/one/lifecycle.sqlite3",
                        "repository": "/repo",
                    },
                    {"state": "/one", "database": "/one/other.sqlite3"},
                    {"nope": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    found = gcw.read_registry(registry)
    assert [item.state for item in found] == [Path("/one")]
    assert found[0].repository == Path("/repo")
    assert gcw.read_registry(tmp_path / "absent.json") == ()
