"""`lane_gates.py` reports the gates and touches nothing.

The read-only cases are the point of this file. A diagnostic that can write is
not a diagnostic: the one on this path reads a ledger an operator is about to
resume, and a tool that could start a run while answering "what is the state?"
would be the failure it exists to prevent. So the assertions are not "it did
not happen to write" but "it cannot": SQLite refuses the handle, no argv it
spawns names `maestro.py`, and every byte under the state root is identical
afterwards.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from adw_modules import lifecycle
from adw_modules import scheduler_types as st

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = RUNTIME_ROOT / "tools" / "lane_gates.py"


def _load_tool():
    if str(RUNTIME_ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(RUNTIME_ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("lane_gates", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lane_gates = _load_tool()


RUN_ID = "run-testgates"
DIGEST = "a" * 64


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _review_payload(
    *,
    input_digest: str,
    verdict: str,
    executed: int,
    passed: int,
    failed: int,
    errored: int,
    findings=(),
    input_artifact_ids=(),
) -> str:
    return json.dumps(
        {
            "input_digest": input_digest,
            "input_artifact_ids": list(input_artifact_ids),
            "verdict": verdict,
            "findings": [dict(item) for item in findings],
            "public_result_summary": {
                "errored": errored,
                "executed": executed,
                "failed": failed,
                "passed": passed,
                "skipped": 0,
            },
        }
    )


def _insert_artifact(
    conn: sqlite3.Connection,
    *,
    lane_id: str,
    sequence: int,
    kind: str,
    completed_stage: str,
    payload_json: str,
) -> None:
    input_digest = _digest("{0}:{1}".format(lane_id, sequence))
    conn.execute(
        "INSERT INTO lane_artifacts(artifact_id, run_id, lane_id, sequence, "
        "completed_stage, artifact_kind, plan_revision, spec_digest, "
        "lane_projection_digest, input_digest, output_digest, artifact_ref, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _digest("id:{0}:{1}".format(lane_id, sequence)),
            RUN_ID,
            lane_id,
            sequence,
            completed_stage,
            kind,
            1,
            DIGEST,
            DIGEST,
            input_digest,
            DIGEST,
            "refs/maestro/{0}/{1}".format(lane_id, sequence),
            payload_json,
            "2026-09-02T00:00:0{0}+00:00".format(sequence % 10),
        ),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> dict:
    """A hand-built ledger with one stalled lane and one merged lane."""
    state_root = tmp_path / "state"
    (state_root / "worktrees").mkdir(parents=True)
    repository = tmp_path / "repo"
    (repository / "adws").mkdir(parents=True)
    database = state_root / "lifecycle.sqlite3"

    # A real builder checkout, because a fixture without one makes every
    # assertion about the git read vacuous: `git_head` returns early on a
    # missing `.git` and the argv the test is inspecting is never built.
    checkout = state_root / "worktrees" / RUN_ID / "lane-build" / "builder" / "checkout"
    checkout.mkdir(parents=True)
    (checkout / "file.txt").write_text("candidate", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": os.environ.get("PATH", ""),
    }
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "file.txt"],
        ["git", "commit", "-q", "-m", "candidate"],
    ):
        subprocess.run(argv, cwd=checkout, env=env, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    conn = sqlite3.connect(str(database))
    conn.executescript(lifecycle.SCHEMA)
    conn.execute(
        "INSERT INTO ledger_meta(schema_version) VALUES (?)", ("test",)
    )
    conn.execute(
        "INSERT INTO plan_revisions(run_id, plan_revision, plan_digest, "
        "parent_revision, plan_artifact_ref, amendment_artifact_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (RUN_ID, 1, DIGEST, None, "plan.json", None, "2026-09-02T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO runs(run_id, runtime_state_root, runtime_state_fingerprint, "
        "plan_digest, plan_revision, integration_ref, integration_initial_sha, "
        "target_repository_root, target_git_common_dir, target_worktree_git_dir, "
        "target_object_format, target_repository_fingerprint, "
        "target_sync_journal_fingerprint, target_initial_main_sha, "
        "target_main_ref, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            RUN_ID,
            str(state_root),
            DIGEST,
            DIGEST,
            1,
            "refs/heads/maestro/integration",
            "b" * 40,
            str(repository),
            str(repository / ".git"),
            str(repository / ".git"),
            "sha1",
            DIGEST,
            DIGEST,
            "b" * 40,
            "refs/heads/main",
            "2026-09-02T00:00:00+00:00",
            "2026-09-02T00:00:00+00:00",
        ),
    )
    for lane_id, stage in (
        ("lane-build", st.LaneStage.WAITING_FOR_USER.value),
        ("lane-done", st.LaneStage.MERGED.value),
    ):
        conn.execute(
            "INSERT INTO lane_state(run_id, lane_id, stage, updated_at) "
            "VALUES (?,?,?,?)",
            (RUN_ID, lane_id, stage, "2026-09-02T01:00:00+00:00"),
        )

    # Reviews name the candidate they measured. Repeated admitted content,
    # rather than equal failure counts, is the no-progress evidence.
    for round_index in range(3):
        producer_sequence = round_index * 2 + 1
        sequence = producer_sequence + 1
        failed, errored = 2, 0
        _insert_artifact(
            conn,
            lane_id="lane-build",
            sequence=producer_sequence,
            kind=st.ArtifactKind.BUILDER_OUTPUT.value,
            completed_stage=st.LaneStage.BUILDING.value,
            payload_json=json.dumps(
                {
                    "spec_digest": DIGEST,
                    "projection_digest": DIGEST,
                    "builder_base_sha": head,
                    "sealed_test_digest": DIGEST,
                    "tree_delta": [{"path": "file.txt", "blob": _digest("candidate")}],
                    "candidate_sha": head,
                }
            ),
        )
        _insert_artifact(
            conn,
            lane_id="lane-build",
            sequence=sequence,
            kind=st.ArtifactKind.CODE_REVIEW.value,
            completed_stage=st.LaneStage.REVIEWING_CODE.value,
            payload_json=_review_payload(
                input_digest=_digest("lane-build:{0}".format(sequence)),
                input_artifact_ids=(
                    _digest("id:lane-build:{0}".format(producer_sequence)),
                ),
                verdict=st.ReviewerVerdict.REVISE.value,
                executed=11,
                passed=9,
                failed=failed,
                errored=errored,
                findings=({"observed_behavior": "a located defect"},),
            ),
        )
    _insert_artifact(
        conn,
        lane_id="lane-build",
        sequence=7,
        kind=st.ArtifactKind.BUILDER_OUTPUT.value,
        completed_stage=st.LaneStage.BUILDING.value,
        payload_json=json.dumps(
            {
                "input_digest": _digest("lane-build:7"),
                "candidate_sha": head,
                "candidate_ref": "refs/maestro/candidate",
            }
        ),
    )
    conn.commit()
    conn.close()

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "installations": [
                    {
                        "database": str(database),
                        "plans_dir": str(repository / "plans"),
                        "repository": str(repository),
                        "state": str(state_root),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return {
        "database": database,
        "registry": registry,
        "state_root": state_root,
        "repository": repository,
        "head": head,
    }


def _tree_fingerprint(root: Path) -> dict[str, str]:
    prints: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            prints[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return prints


def _run(ledger: dict, capsys, *extra: str) -> tuple[int, str]:
    code = lane_gates.main(
        ["--run", RUN_ID, "--registry", str(ledger["registry"]), *extra]
    )
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------
# it reports what is there
# --------------------------------------------------------------------------


def test_reports_stage_and_wait_and_rounds(ledger, capsys):
    code, out = _run(ledger, capsys)
    assert code == 1  # WAITING_FOR_USER
    assert "LANE_GATES run={0} lane=lane-build".format(RUN_ID) in out
    assert "stage                   WAITING_FOR_USER" in " ".join(out.split("\n"))
    assert "stage" in out and "WAITING_FOR_USER" in out
    assert "executed=11 passed=9 failed=2 errored=0" in out
    assert "reviewed_attempts" in out and "sealed_error_history" not in out
    assert "stalled" in out
    assert "candidate_sha" in out and ledger["head"] in out
    assert "candidate_matches_head  True" in out


def test_new_content_is_progress_despite_unchanged_failure_counts(ledger, capsys):
    _code, out = _run(ledger, capsys, "--lane", "lane-build")
    rows = dict(
        line.split(None, 1) for line in out.splitlines()
        if line.startswith(("reviewed_attempts", "stalled"))
    )
    assert rows["reviewed_attempts"] == "3"
    assert rows["stalled"] == "True"
    assert st.digest_canonical(
        [{"path": "file.txt", "blob": _digest("candidate")}]
    ) not in out
    conn = sqlite3.connect(ledger["database"])
    try:
        for sequence in (1, 3, 5):
            payload = json.loads(conn.execute(
                "SELECT payload_json FROM lane_artifacts WHERE run_id=? "
                "AND lane_id=? AND sequence=?",
                (RUN_ID, "lane-build", sequence),
            ).fetchone()[0])
            payload["tree_delta"][0]["blob"] = _digest("changed:{0}".format(sequence))
            conn.execute(
                "UPDATE lane_artifacts SET payload_json=? WHERE run_id=? "
                "AND lane_id=? AND sequence=?",
                (json.dumps(payload), RUN_ID, "lane-build", sequence),
            )
        conn.commit()
    finally:
        conn.close()
    _code, out = _run(ledger, capsys, "--lane", "lane-build")
    rows = dict(
        line.split(None, 1) for line in out.splitlines()
        if line.startswith(("reviewed_attempts", "stalled"))
    )
    assert rows["reviewed_attempts"] == "3"
    assert rows["stalled"] == "False"


def test_lane_filter_selects_one_lane(ledger, capsys):
    _code, out = _run(ledger, capsys, "--lane", "lane-done")
    assert "lane=lane-done" in out
    assert "lane=lane-build" not in out


def test_waiting_for_user_exits_nonzero(ledger, capsys):
    code, _out = _run(ledger, capsys)
    assert code != 0


def test_unresolved_run_exits_nonzero(ledger, capsys):
    code = lane_gates.main(
        ["--run", "run-absent", "--registry", str(ledger["registry"])]
    )
    assert code == 1
    assert "UNRESOLVED" in capsys.readouterr().out


def test_reviewer_verdict_disagreement_is_visible():
    from adw_modules.code_review import _RUNNER_REVISE

    coerced_to_revise = {
        "verdict": st.ReviewerVerdict.REVISE.value,
        "findings": [dict(_RUNNER_REVISE)],
    }
    assert (
        lane_gates.reviewer_verdict(coerced_to_revise)
        == st.ReviewerVerdict.PASS.value
    )

    # A REVISE over a green suite is no longer coerced, so there is nothing
    # left behind to reconstruct: the recorded verdict is the voted one.
    revised = {
        "verdict": st.ReviewerVerdict.REVISE.value,
        "findings": [{"observed_behavior": "a located defect"}],
    }
    assert lane_gates.reviewer_verdict(revised) == st.ReviewerVerdict.REVISE.value

    passed = {
        "verdict": st.ReviewerVerdict.PASS.value,
        "findings": [],
    }
    assert lane_gates.reviewer_verdict(passed) == st.ReviewerVerdict.PASS.value


# --------------------------------------------------------------------------
# it cannot write
# --------------------------------------------------------------------------


def test_connection_is_readonly(ledger):
    conn = lane_gates.open_readonly(ledger["database"])
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            conn.execute(
                "UPDATE lane_state SET stage='MERGED' WHERE run_id=?", (RUN_ID,)
            )
        assert "readonly" in str(caught.value).lower()
    finally:
        conn.close()


def test_ledger_bytes_are_unchanged(ledger, capsys):
    before = hashlib.sha256(ledger["database"].read_bytes()).hexdigest()
    _run(ledger, capsys)
    after = hashlib.sha256(ledger["database"].read_bytes()).hexdigest()
    assert after == before


def test_state_root_tree_is_unchanged(ledger, capsys):
    before = _tree_fingerprint(ledger["state_root"])
    _run(ledger, capsys)
    assert _tree_fingerprint(ledger["state_root"]) == before


def test_never_spawns_maestro(ledger, capsys, monkeypatch):
    """No argv this tool spawns names `maestro.py`, or any run verb."""
    spawned: list[list[str]] = []
    real = lane_gates.subprocess.run

    def recording(argv, *args, **kwargs):
        spawned.append([str(item) for item in argv])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(lane_gates.subprocess, "run", recording)
    _run(ledger, capsys)
    flat = " ".join(" ".join(argv) for argv in spawned)
    assert "maestro.py" not in flat
    for verb in ("run start", "run resume", "run amend"):
        assert verb not in flat
    for argv in spawned:
        assert argv[0] == "git"
        assert "rev-parse" in argv


