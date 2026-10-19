"""What the attend agents are handed, and where they are handed it.

The operator's and the attend reviewer's trees are `--no-checkout` worktrees
holding no product file, so each parked lane's accepted suite and evidence are written into them as files. Two things have to hold: the bytes land inside the
tree the role contract confines the agent to, and the handoff is files rather
than prompt text. B13's size check is made against the
route's window at launch, and four rounds of findings plus a suite is a prompt
that can overflow -- an overflowing agent answers about a different lane.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def _lane(suite: dict, lane_id: str = "lane-a-build", **extra) -> att.LaneEvidence:
    fields = dict(
        lane_id=lane_id,
        wait_reason=st.WaitReason.NO_PROGRESS.value,
        stage=st.LaneStage.WAITING_FOR_USER.value,
        round_number=4,
        public_contract={"acceptance_criteria": ["emits records"]},
        reviews=({"kind": "CODE_REVIEW", "verdict": "REVISE"},),
        failure_output=("1 failed",),
        lane_gates="stage WAITING_FOR_USER",
        suite_files=suite,
    )
    fields.update(extra)
    return att.LaneEvidence(**fields)


class OperatorTree(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        self.actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)

    def _write(self, *lanes: att.LaneEvidence) -> None:
        self.actor._write_attend_tree(
            self.cwd,
            lanes,
            {
                "current.ir.json": json.dumps({"plan_id": "p-1"}).encode(),
                "amendment_rules.md": b"rules",
            },
        )

    def test_the_inputs_are_files_and_the_suite_is_on_disk(self) -> None:
        self._write(_lane({"tests/a.spec.ts": "expect(record.field)"}))
        inputs = self.cwd / "inputs"
        self.assertEqual(
            json.loads((inputs / "current.ir.json").read_text())["plan_id"], "p-1"
        )
        self.assertEqual((inputs / "amendment_rules.md").read_text(), "rules")
        lane_dir = inputs / "lanes" / "lane-a-build"
        self.assertEqual((lane_dir / "lane_gates.txt").read_text(), "stage WAITING_FOR_USER")
        reviews = json.loads((lane_dir / "reviews.json").read_text())
        self.assertEqual(reviews["reviews"][0]["verdict"], "REVISE")
        self.assertEqual(reviews["failure_output"], ["1 failed"])
        self.assertEqual(reviews["wait_reason"], "NO_PROGRESS")
        self.assertEqual(
            (self.cwd / "suite" / "lane-a-build" / "tests" / "a.spec.ts").read_text(),
            "expect(record.field)",
        )
        self.assertTrue((self.cwd / "revisions").is_dir())

    def test_every_lane_in_a_batch_gets_its_own_evidence(self) -> None:
        review = {"verdict": "REVISE", "affected_lanes": ["lane-b-build"]}
        self._write(
            _lane({"tests/a.spec.ts": "a"}),
            _lane(
                {"tests/a.spec.ts": "b"},
                lane_id="lane-b-build",
                wait_reason=st.WaitReason.AMENDMENT_REQUIRED.value,
                final_review=review,
            ),
        )
        self.assertEqual(
            (self.cwd / "suite" / "lane-a-build" / "tests" / "a.spec.ts").read_text(), "a"
        )
        self.assertEqual(
            (self.cwd / "suite" / "lane-b-build" / "tests" / "a.spec.ts").read_text(), "b"
        )
        written = json.loads(
            (self.cwd / "inputs" / "lanes" / "lane-b-build" / "final_review.json").read_text()
        )
        self.assertEqual(written, review)

    def test_a_second_dispatch_replaces_the_previous_round(self) -> None:
        self._write(_lane({"tests/old.spec.ts": "stale"}))
        self._write(_lane({"tests/new.spec.ts": "current"}))
        suite = self.cwd / "suite" / "lane-a-build" / "tests"
        self.assertFalse((suite / "old.spec.ts").exists())
        self.assertTrue((suite / "new.spec.ts").exists())

    def test_a_traversing_suite_path_is_refused_rather_than_written(self) -> None:
        outside = self.tmp / "escaped.ts"
        with self.assertRaises(maestro.FactoryRefused) as caught:
            self._write(_lane({"../../../escaped.ts": "leaked"}))
        self.assertIn("OPERATOR_TREE_PATH_ESCAPE", str(caught.exception))
        self.assertFalse(outside.exists())


class ResolvedUnder(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def test_a_nested_relative_path_resolves_inside(self) -> None:
        self.assertEqual(
            maestro._resolved_under(self.root, "a/b/c.ts"),
            (self.root / "a" / "b" / "c.ts").resolve(),
        )

    def test_a_parent_component_is_refused(self) -> None:
        with self.assertRaises(maestro.FactoryRefused):
            maestro._resolved_under(self.root, "../out.ts")

    def test_an_absolute_path_is_refused(self) -> None:
        with self.assertRaises(maestro.FactoryRefused):
            maestro._resolved_under(self.root, "/etc/passwd")


class OperatorPrompt(unittest.TestCase):
    def test_the_envelope_schema_names_every_rationale_key(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        schema = actor._schema("operator")
        self.assertEqual(sorted(schema["rationales"][0]), sorted(att.RATIONALE_KEYS))
        self.assertIn("revision_path", schema)

    def test_the_reviewer_envelope_schema_is_a_located_verdict(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        schema = actor._schema("attend-reviewer")
        self.assertEqual(schema["verdict"], "PASS|REVISE")
        self.assertEqual(sorted(schema["findings"][0]), sorted(att.REVIEW_FINDING_KEYS))
        self.assertFalse(actor._payload_ok("attend-reviewer", {"verdict": "MAYBE"}))
        self.assertTrue(actor._payload_ok("attend-reviewer", {"verdict": "REVISE"}))

    def test_the_role_contract_states_the_privilege_and_its_limit(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.target = type("T", (), {"target_repository_root": holder.name})()
        written = actor._materialize_role_instructions(
            Path(holder.name), "operator", "claude"
        )
        text = written.read_text()
        self.assertIn("accepted acceptance suite is in this checkout", text)
        self.assertIn("allowed_lane_ids", text)
        self.assertIn("Do not run any `maestro` verb", text)


class ReviewerDispatch(unittest.TestCase):
    """The attend reviewer gets its inputs as files in its own tree."""

    def test_the_reviewer_tree_carries_both_irs_and_the_question_surface(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        current = tmp / "current.ir.json"
        current.write_text('{"rev": 1}', encoding="utf-8")
        revision = tmp / "r2.ir.json"
        revision.write_text('{"rev": 2}', encoding="utf-8")
        request = att.ReviewRequest(
            run_id="run-1", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=(_lane({"tests/a.spec.ts": "a"}),),
            current_ir_path=str(current), revision_ir_path=str(revision),
            question_surface='{"plan_id": "p-1"}',
            rationales=({"lane": "lane-a-build"},),
            findings_out_path=str(tmp / "rev" / "revisions" / "f.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-a-build",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        launched = {}

        def launch(ctx, role, cwd, extra, prepare_cwd, provision=True):
            prepare_cwd(cwd)
            launched.update(role=role, extra=extra, cwd=cwd, provision=provision)
            return {"verdict": "PASS", "findings": []}, None, cwd

        with (
            mock.patch.object(actor, "_role_dir", return_value=tmp / "rev", create=True),
            mock.patch.object(actor, "_launch", side_effect=launch, create=True),
            # The tree's git shape is AttendTreesAreWorktreesOfTheTarget's.
            mock.patch.object(actor, "_attend_checkout", create=True),
            mock.patch.object(actor, "_bind_checkout", create=True),
            mock.patch.object(actor, "_role_key", create=True),
            # The pane close is EachAttendDispatchClosesThePaneItLaunched's.
            mock.patch.object(actor, "_close_attend_pane", create=True),
        ):
            payload = actor.attend_reviewer(SimpleNamespace(), request)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(launched["role"], "attend-reviewer")
        inputs = launched["cwd"] / "inputs"
        self.assertEqual((inputs / "current.ir.json").read_text(), '{"rev": 1}')
        self.assertEqual((inputs / "revision.ir.json").read_text(), '{"rev": 2}')
        self.assertEqual(
            json.loads((inputs / "question_surface.json").read_text()), {"plan_id": "p-1"}
        )
        self.assertIs(launched["provision"], False)
        self.assertEqual(launched["extra"]["findings_out_path"], request.findings_out_path)
        self.assertEqual(
            launched["extra"]["parked_lanes"][0]["lane_id"], "lane-a-build"
        )

    def test_the_reviewer_is_never_handed_bytes_other_than_the_recorded_ones(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        revision = tmp / "r2.ir.json"
        revision.write_text('{"rev": "edited"}', encoding="utf-8")
        request = att.ReviewRequest(
            run_id="run-1", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=(_lane({}),), current_ir_path=str(revision),
            revision_ir_path=str(revision), question_surface="{}", rationales=(),
            findings_out_path=str(tmp / "f.json"), amendment_rules="rules",
            allowed_lane_ids=("lane-a-build",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        with (
            mock.patch.object(actor, "_role_dir", return_value=tmp / "rev", create=True),
            mock.patch.object(actor, "_launch", create=True) as launch,
        ):
            with self.assertRaises(att.AttendRefused) as caught:
                actor.attend_reviewer(SimpleNamespace(), request)
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
        launch.assert_not_called()

    def test_earlier_rounds_revisions_survive_a_later_dispatch(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        cwd = Path(holder.name)
        (cwd / "revisions").mkdir()
        kept = cwd / "revisions" / "r2.round1.ir.json"
        kept.write_text("{}", encoding="utf-8")
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor._write_attend_tree(cwd, (_lane({}),), {})
        self.assertTrue(kept.is_file())

    def test_the_reviewer_role_contract_forbids_editing_and_names_the_shape(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.target = type("T", (), {"target_repository_root": holder.name})()
        written = actor._materialize_role_instructions(
            Path(holder.name), "attend-reviewer", "omp"
        )
        text = written.read_text()
        self.assertIn("you do not edit it", text)
        self.assertIn("required_change", text)


class AttendTreesAreNotProvisioned(unittest.TestCase):
    """The deployment's `provision_argv` installs a project; an attend tree holds none.

    `_launch` provisions every tree it prepares, and both attend roles reach it.
    With `uv sync --frozen` configured, the operator's dispatch refused
    `PROVISION_FAILED: No pyproject.toml found` and the batch was reported as
    `ATTEND_OPERATOR_FAILED`. The tests above patch `_launch` away, so none of
    them could see it; these drive the real one.
    """

    def setUp(self) -> None:
        from unittest import mock

        from adw_modules import provisioning
        from test_prepare_once_per_dispatch import _AdoptingLauncher, _Bench

        self.bench = _Bench(self, _AdoptingLauncher(target_cwd_fn=lambda tree: tree))
        routes = dict(self.bench.actor.role_routes)
        routes["operator"] = routes[maestro._ATTEND_REVIEWER_ROLE] = routes["code-reviewer"]
        self.bench.actor.role_routes = routes
        self.provisioned: list[Path] = []

        def refuse(dest, argv, timeout_s=None):
            self.provisioned.append(Path(dest))
            raise provisioning.ReviewProvisioningError(
                tuple(argv), 2, "error: No pyproject.toml found"
            )

        for patch in (
            mock.patch.object(provisioning, "provision_tree", side_effect=refuse),
            mock.patch.object(
                self.bench.actor,
                "_role_dir",
                side_effect=lambda ctx, role, **_: self.bench.root / role,
            ),
            # What the agent replies is not this case's subject; the tree it is
            # dispatched into is, and that is settled before the reply.
            mock.patch.object(
                self.bench.actor, "_await_envelope", return_value={"verdict": "PASS"}
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.ir = self.bench.root / "current.ir.json"
        self.ir.write_text('{"rev": 1}', encoding="utf-8")

    def test_the_operator_is_dispatched_without_provisioning_its_tree(self) -> None:
        request = att.OperatorRequest(
            run_id="run-once", plan_revision=1, next_plan_revision=2,
            lanes=(_lane({"tests/a.spec.ts": "a"}),), ir_path=str(self.ir),
            revision_out_path=str(self.bench.root / "operator" / "r2.ir.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-a-build",),
        )
        self.bench.actor.attend_operator(self.bench.ctx, request)
        self.assertEqual(self.provisioned, [])

    def test_the_attend_reviewer_is_dispatched_without_provisioning_its_tree(self) -> None:
        revision = self.bench.root / "r2.ir.json"
        revision.write_text('{"rev": 2}', encoding="utf-8")
        request = att.ReviewRequest(
            run_id="run-once", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=(_lane({"tests/a.spec.ts": "a"}),), current_ir_path=str(self.ir),
            revision_ir_path=str(revision), question_surface="{}", rationales=(),
            findings_out_path=str(self.bench.root / "f.json"), amendment_rules="rules",
            allowed_lane_ids=("lane-a-build",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )
        self.bench.actor.attend_reviewer(self.bench.ctx, request)
        self.assertEqual(self.provisioned, [])

    def test_a_checkout_role_still_provisions_through_the_same_path(self) -> None:
        with self.assertRaises(maestro.LaunchFailed) as caught:
            self.bench.launch("builder")
        self.assertIn("PROVISION_FAILED", str(caught.exception))
        self.assertEqual(self.provisioned, [self.bench.cwd])


class AttendTreesAreWorktreesOfTheTarget(unittest.TestCase):
    """Both attend trees get the lane trees' shape: a worktree of the target repo.

    A plain directory has no trusted Claude Code project, so the operator
    agent stopped at the folder-trust prompt and herdr reported `blocked`,
    while every lane worktree resolved to the trusted repository. Driven
    through the real `_launch` and a real git repository.
    """

    def setUp(self) -> None:
        from unittest import mock

        from test_prepare_once_per_dispatch import _AdoptingLauncher, _Bench

        self.bench = _Bench(self, _AdoptingLauncher(target_cwd_fn=lambda tree: tree))
        routes = dict(self.bench.actor.role_routes)
        routes["operator"] = routes[maestro._ATTEND_REVIEWER_ROLE] = routes["code-reviewer"]
        self.bench.actor.role_routes = routes
        patch = mock.patch.object(
            self.bench.actor, "_await_envelope", return_value={"verdict": "PASS"}
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.product = self.bench.root / "product"
        self.ir = self.bench.root / "current.ir.json"
        self.ir.write_text('{"rev": 1}', encoding="utf-8")

    def _git(self, *args: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    def _assert_target_worktree(self, role: str) -> None:
        cwd = self.bench.actor._role_dir(self.bench.ctx, role) / "checkout"
        common = Path(self._git("-C", str(cwd), "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = cwd / common
        self.assertEqual(common.resolve(), (self.product / ".git").resolve())
        listed = self._git("-C", str(self.product), "worktree", "list", "--porcelain")
        self.assertIn("worktree {0}".format(cwd.resolve()), listed)
        # --no-checkout: no product file is in the agent's tree.
        self.assertFalse((cwd / "seed.txt").exists())
        # Removal is the lanes' path, so no `git worktree` entry is orphaned.
        key = self.bench.actor._role_key(self.bench.ctx, role)
        stored = self.bench.actor._roles.pop(key)
        self.assertEqual(stored.checkout, cwd.resolve())
        self.bench.actor._safe_remove_attempt(stored.attempt, stored.checkout)
        listed = self._git("-C", str(self.product), "worktree", "list", "--porcelain")
        self.assertNotIn(str(cwd.resolve()), listed)

    def test_the_operator_tree_is_a_no_checkout_worktree_of_the_target(self) -> None:
        cwd = self.bench.actor._role_dir(self.bench.ctx, "operator") / "checkout"
        # A plain tree an earlier dispatch left keeps every recorded revision.
        kept = cwd / "revisions" / "r2.round1.ir.json"
        kept.parent.mkdir(parents=True)
        kept.write_text("{}", encoding="utf-8")
        request = att.OperatorRequest(
            run_id="run-once", plan_revision=1, next_plan_revision=2,
            lanes=(_lane({"tests/a.spec.ts": "a"}),), ir_path=str(self.ir),
            revision_out_path=str(cwd / "revisions" / "r2.round2.ir.json"),
            amendment_rules="rules", allowed_lane_ids=("lane-a-build",),
        )
        self.bench.actor.attend_operator(self.bench.ctx, request)
        self.assertEqual(kept.read_text(), "{}")
        self.assertTrue((cwd / "inputs" / "current.ir.json").is_file())
        self._assert_target_worktree("operator")

    def test_the_attend_reviewer_tree_is_a_no_checkout_worktree_of_the_target(self) -> None:
        revision = self.bench.root / "r2.ir.json"
        revision.write_text('{"rev": 2}', encoding="utf-8")
        request = att.ReviewRequest(
            run_id="run-once", plan_revision=1, next_plan_revision=2, review_round=1,
            lanes=(_lane({"tests/a.spec.ts": "a"}),), current_ir_path=str(self.ir),
            revision_ir_path=str(revision), question_surface="{}", rationales=(),
            findings_out_path=str(self.bench.root / "f.json"), amendment_rules="rules",
            allowed_lane_ids=("lane-a-build",),
            revision_sha256=att.sha256_hex(b'{"rev": 2}'),
        )
        self.bench.actor.attend_reviewer(self.bench.ctx, request)
        self._assert_target_worktree(maestro._ATTEND_REVIEWER_ROLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
