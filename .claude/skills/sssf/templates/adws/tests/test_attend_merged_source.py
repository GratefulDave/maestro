"""The attend agents see the merged source the parked lane builds on.

A tests lane parked because its review demanded the contract name merged
producer callables and their report schemas. The builder's checkout held
them -- earlier lanes had merged them to the integration head -- but the
attend author and reviewer were handed only the lane's contract, reviews,
gate table and suite. The author could not name what it could not read, the
reviewer said so in every round, and the session stopped REVIEW_UNRESOLVED.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

PRODUCER = "src/producer.py"


def _projection(
    lane_id: str, outputs: tuple[str, ...], needs: tuple[str, ...] = (), kind=None
) -> st.LaneProjection:
    spec_digest = st.digest_canonical({"lane": lane_id})
    needs = tuple(sorted(needs))
    outputs = tuple(sorted(outputs))
    return st.LaneProjection(
        lane_id=lane_id,
        needs=needs,
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=st.lane_projection_digest(
            spec_digest, needs, outputs, lane_kind=kind
        ),
        lane_kind=kind,
    )


#: lane-base <- lane-producer <- lane-run-tests <- lane-run (build).
#: lane-unrelated is merged too, and is nothing the parked pair builds on.
LANES = (
    _projection("lane-base", ("src/base.py",)),
    _projection("lane-producer", (PRODUCER,), ("lane-base",)),
    _projection("lane-run", ("src/run.py",), ("lane-producer", "lane-run-tests"), st.LANE_KIND_BUILD),
    _projection("lane-run-tests", ("tests/test_run.py",), ("lane-producer",), st.LANE_KIND_TESTS),
    _projection("lane-unrelated", ("src/other.py",)),
)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def commit(self, files: dict) -> str:
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "c")
        return self._git("rev-parse", "HEAD")


class MergedSourceScope(unittest.TestCase):
    def test_every_lane_the_pair_builds_on_is_in_scope_and_nothing_else(self) -> None:
        self.assertEqual(
            att.merged_source_lane_ids(LANES, "lane-run-tests"),
            ("lane-base", "lane-producer"),
        )
        self.assertEqual(
            att.merged_source_lane_ids(LANES, "lane-run"),
            ("lane-base", "lane-producer"),
        )
        self.assertEqual(att.merged_source_lane_ids(LANES, "lane-base"), ())


class MergedSourceEvidence(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.repo = _Repo(self.tmp / "repo")
        self.head = self.repo.commit(
            {
                "src/base.py": "BASE = 1\n",
                PRODUCER: "def produce_report():\n    return {'schema': 'v1'}\n",
                "src/other.py": "OTHER = 1\n",
                "src/bin.dat": "x",
            }
        )
        (self.repo.root / "src" / "bin.dat").write_bytes(b"\xff\xfe\x00")
        self.head = self.repo.commit({})

    def _evidence(self, head: str) -> att.LaneEvidence:
        store = mock.Mock()
        store.lane_stage.return_value = st.LaneStage.WAITING_FOR_USER
        store._run.return_value = {"target_repository_root": str(self.repo.root)}
        store.lane_artifact_payloads.return_value = []
        store.latest_lane_artifact_payload.return_value = None
        store.accepted_suite_artifact_id.return_value = None
        compiled = SimpleNamespace(lanes=LANES)
        with (
            mock.patch.object(maestro, "durable_integration_tip", return_value=head),
            mock.patch.object(maestro, "_attend_lane_gates", return_value="gates"),
        ):
            return maestro._attend_lane_evidence(
                store, None, {}, "run-1", "lane-run-tests",
                st.WaitReason.NO_PROGRESS.value, compiled,
            )

    def test_the_merged_producer_is_read_off_the_integration_head(self) -> None:
        lane = self._evidence(self.head)
        self.assertEqual(lane.integration_head, self.head)
        self.assertEqual(
            lane.merged_source[PRODUCER],
            "def produce_report():\n    return {'schema': 'v1'}\n",
        )
        self.assertEqual(lane.merged_source["src/base.py"], "BASE = 1\n")
        self.assertNotIn("src/other.py", lane.merged_source)
        # The pair's own outputs are the suite and the candidate, not merged source.
        self.assertNotIn("src/run.py", lane.merged_source)

    def test_a_changed_head_changes_the_digest(self) -> None:
        before = self._evidence(self.head)
        later = self.repo.commit(
            {PRODUCER: "def produce_report():\n    return {'schema': 'v2'}\n"}
        )
        after = self._evidence(later)
        self.assertIn("'v2'", after.merged_source[PRODUCER])
        self.assertNotEqual(before.merged_source_sha256, after.merged_source_sha256)
        self.assertEqual(before.merged_source_sha256, self._evidence(self.head).merged_source_sha256)

    def test_a_non_text_blob_is_left_out(self) -> None:
        files = maestro._attend_merged_source(self.repo.root, self.head, ["src"])
        self.assertNotIn("src/bin.dat", files)
        self.assertIn(PRODUCER, files)


def _lane(merged: dict) -> att.LaneEvidence:
    return att.LaneEvidence(
        lane_id="lane-run-tests",
        wait_reason=st.WaitReason.NO_PROGRESS.value,
        stage=st.LaneStage.WAITING_FOR_USER.value,
        round_number=1,
        public_contract={},
        reviews=(),
        failure_output=(),
        lane_gates="",
        suite_files={"tests/test_run.py": "def test_x(): pass\n"},
        integration_head="a" * 40,
        merged_source=merged,
    )


class MergedSourceReachesBothAgents(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.ir = self.tmp / "current.ir.json"
        self.ir.write_text('{"rev": 1}', encoding="utf-8")
        self.lane = _lane({PRODUCER: "def produce_report(): ...\n"})
        self.actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        self.launched: dict = {}

    def _dispatch(self, role_dir: Path, call, answer):
        def launch(ctx, role, cwd, extra, prepare_cwd, provision=True):
            prepare_cwd(cwd)
            self.launched.update(cwd=cwd, extra=extra)
            return answer(extra), None, cwd

        with (
            mock.patch.object(self.actor, "_role_dir", return_value=role_dir, create=True),
            mock.patch.object(self.actor, "_launch", side_effect=launch, create=True),
            mock.patch.object(self.actor, "_attend_checkout", create=True),
            mock.patch.object(self.actor, "_bind_checkout", create=True),
            mock.patch.object(self.actor, "_role_key", create=True),
            mock.patch.object(self.actor, "_close_attend_pane", create=True),
            mock.patch.object(maestro.att, "read_operator_envelope", create=True),
        ):
            call()

    def _assert_merged(self) -> None:
        cwd = self.launched["cwd"]
        self.assertEqual(
            (cwd / "merged" / "lane-run-tests" / PRODUCER).read_text(),
            "def produce_report(): ...\n",
        )
        parked = self.launched["extra"]["parked_lanes"][0]
        self.assertEqual(parked["merged_source_files"], [PRODUCER])
        self.assertEqual(parked["integration_head"], "a" * 40)
        self.assertEqual(parked["merged_source_sha256"], self.lane.merged_source_sha256)
        self.assertEqual(
            Path(parked["merged_source_dir"]),
            (cwd / "merged" / "lane-run-tests").resolve(),
        )

    def test_the_operator_tree_carries_the_merged_source(self) -> None:
        request = att.OperatorRequest(
            run_id="run-1", plan_revision=1, next_plan_revision=2,
            lanes=(self.lane,), ir_path=str(self.ir),
            revision_out_path=str(self.tmp / "op" / "checkout" / "revisions" / "r2.ir.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-run-tests",),
        )
        self._dispatch(
            self.tmp / "op",
            lambda: self.actor.attend_operator(SimpleNamespace(), request),
            lambda extra: {},
        )
        self._assert_merged()

    def test_the_reviewer_tree_carries_the_merged_source(self) -> None:
        revision = self.tmp / "r2.ir.json"
        revision.write_text('{"rev": 2}', encoding="utf-8")
        request = att.ReviewRequest(
            run_id="run-1", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=(self.lane,), current_ir_path=str(self.ir),
            revision_ir_path=str(revision), question_surface="{}", rationales=(),
            findings_out_path=str(self.tmp / "rev" / "f.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-run-tests",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )

        def answer(extra):
            findings = Path(extra["findings_out_path"])
            findings.parent.mkdir(parents=True, exist_ok=True)
            findings.write_text('{"findings": []}')
            return {"verdict": "PASS", "findings": []}

        self._dispatch(
            self.tmp / "rev",
            lambda: self.actor.attend_reviewer(SimpleNamespace(), request),
            answer,
        )
        self._assert_merged()

    def test_both_role_contracts_permit_reading_the_merged_source(self) -> None:
        texts = {}
        for role, coder in (("operator", "claude"), (maestro._ATTEND_REVIEWER_ROLE, "omp")):
            root = self.tmp / role
            root.mkdir()
            self.actor.target = type("T", (), {"target_repository_root": str(root)})()
            texts[role] = self.actor._materialize_role_instructions(
                root, role, coder
            ).read_text()
            self.assertIn("merged/<lane_id>/", texts[role], role)
        self.assertIn("permitted review input", texts[maestro._ATTEND_REVIEWER_ROLE])


if __name__ == "__main__":
    unittest.main()
