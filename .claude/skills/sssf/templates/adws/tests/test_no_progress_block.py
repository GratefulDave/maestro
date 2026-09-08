"""Lane-local convergence uses measured outcomes and substantive repetition."""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests.test_tests_lane_handoff import FINDING, HandoffActor, _git, _init_repo, _unified_plan_bytes

def _with_dependency_lane(plan_bytes):
    document = json.loads(plan_bytes)
    document["lanes"].append({
        "id": "lane-dependency",
        "needs": [],
        "outputs": ["dependency.txt"],
        "spec": {
            "goal": "write the dependency",
            "integration": {"integration_branch": "refs/heads/main"},
        },
        "acceptance": ["dependency.txt is ready"],
    })
    document["lanes"].append({
        "id": "lane-dependent",
        "needs": ["lane-a"],
        "outputs": ["dependent.txt"],
        "spec": {
            "goal": "consume lane-a",
            "integration": {"integration_branch": "refs/heads/main"},
        },
        "acceptance": ["dependent.txt is ready"],
    })
    return json.dumps(document).encode("utf-8")


def _merge_dependency(scheduler, repo, runtime):
    """Move the base through an admitted sibling merge, never a raw ref edit."""
    actor = scheduler.actor
    scheduler.actor = HandoffActor(repo, runtime.path / "dependency-worktrees")
    lane = "lane-dependency"
    try:
        scheduler._planned(lane)
        scheduler._writing_tests(lane)
        scheduler._reviewing_tests(lane)
        scheduler._tests_sealed(lane)
        for _ in range(2):
            scheduler._building(lane)
            scheduler._reviewing_code(lane)
        scheduler._ready_to_merge(lane)
        assert scheduler.store.lane_stage(scheduler.run_id, lane) is st.LaneStage.MERGED
    finally:
        scheduler.actor = actor



class SubstantiveFingerprintTest(unittest.TestCase):
    def fingerprint(self, path, source):
        raw = source.encode()
        oid = hashlib.sha1(raw).hexdigest()
        with mock.patch.object(sch.hv, "cat_blob", return_value=raw):
            return sch._substantive_blob(Path("."), path, oid)

    def test_python_docstrings_comments_and_formatting_are_not_progress(self):
        first = '"""Envelope1"""\ndef f():\n    """old"""\n    return 4\n'
        second = '# new\n"""Envelope31"""\ndef f( ):\n    """new"""\n    return (4)\n'
        self.assertEqual(self.fingerprint("a.py", first), self.fingerprint("a.py", second))
        self.assertNotEqual(self.fingerprint("a.py", first), self.fingerprint("a.py", second.replace("(4)", "(6)")))

    def test_javascript_cosmetics_repeat_but_executable_syntax_does_not(self):
        self.assertEqual(self.fingerprint("a.ts", "const x=4;"), self.fingerprint("a.ts", "const /* detail */ x = 4;"))
        for path, before, after in (
            ("a.js", "if (x) /a b/.test(y);", "if (x) /ab/.test(y);"),
            ("a.js", "return /abc/;", "return /xyz/;"),
            ("a.ts", "return `a ${x}`;", "return `a ${y}`;"),
            ("a.ts", "return value;", "return\nvalue;"),
            ("a.jsx", "const x=<p>a b</p>;", "const x=<p>ab</p>;"),
            ("a.tsx", "const x=<p>a b</p>;", "const x=<p>ab</p>;"),
        ):
            with self.subTest(path=path, before=before):
                self.assertNotEqual(self.fingerprint(path, before), self.fingerprint(path, after))


class ContentActor(HandoffActor):
    def __init__(self, repo, worktrees, bodies):
        super().__init__(repo, worktrees)
        self.bodies = iter(bodies)
        self.attempt = 0
        self.numeric = False

    def write_tests(self, ctx):
        result = super().write_tests(ctx)
        if self.numeric:
            result["files"] = {"tests/test_counts.py": (
                "from pathlib import Path\nimport pytest\n"
                "@pytest.mark.parametrize('index', range(8))\n"
                "def test_outcome(index):\n"
                "    assert index < int(Path('a.txt').read_text())\n"
            )}
        return result

    def build(self, ctx):
        self.attempt += 1
        work = self.worktrees / "content" / str(self.attempt)
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        (work / "a.txt").write_text(next(self.bodies), encoding="utf-8")
        _git(work, "add", "a.txt")
        # Different metadata must not disguise byte-identical attempts.
        _git(work, "commit", "-m", "attempt-{0}".format(self.attempt))
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}

    def review_code(self, ctx):
        return st.ReviewerVerdict.REVISE, (FINDING,)


class ReviewedContentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        _init_repo(self.repo)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.addCleanup(self.runtime.close)
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.run_id = "run-content"
        self.lane_id = "lane-a"

    def start(self, bodies, *, dependency=False, numeric=False):
        plan = _unified_plan_bytes()
        if dependency:
            plan = _with_dependency_lane(plan)
        compiled = plan_compiler.compile_plan(
            plan, plan_revision=1, plan_artifact_ref="plan:content"
        )
        sch.create_factory_run(store=self.store, run_id=self.run_id, compiled=compiled,
                               runtime=self.runtime, target=self.target)
        actor = ContentActor(self.repo, self.state / "worktrees", bodies)
        actor.numeric = numeric
        self.scheduler = sch.FactoryScheduler(self.store, self.run_id, actor,
                                             self.runtime, self.target, compiled=compiled)
        self.scheduler._planned(self.lane_id)
        self.scheduler._writing_tests(self.lane_id)
        self.scheduler._reviewing_tests(self.lane_id)
        self.scheduler._tests_sealed(self.lane_id)

    def round(self):
        self.scheduler._building(self.lane_id)
        self.scheduler._reviewing_code(self.lane_id)

    def stage(self):
        return self.store.lane_stage(self.run_id, self.lane_id)

    def artifacts(self, kind):
        return self.store.conn.execute(
            "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence",
            (self.run_id, self.lane_id, kind.value),
        ).fetchall()

    def test_zero_pass_plateau_distinct_candidates_pauses(self):
        self.start(["a", "b", "c"])
        for _ in range(2):
            self.round()
            self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)
        reviews = [json.loads(row["payload_json"]) for row in self.artifacts(st.ArtifactKind.CODE_REVIEW)]
        self.assertEqual([r["public_result_summary"]["passed"] for r in reviews], [0] * 3)

    def test_metadata_only_recommits_repeat_after_grace(self):
        self.start(["a", "a", "a"])
        self.round()
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)
        builders = [json.loads(row["payload_json"]) for row in self.artifacts(st.ArtifactKind.BUILDER_OUTPUT)]
        self.assertEqual(len({b["candidate_sha"] for b in builders}), 3)
        wait = json.loads(self.artifacts(st.ArtifactKind.USER_WAIT)[0]["payload_json"])
        self.assertEqual(wait["wait_reason"], st.WaitReason.NO_PROGRESS.value)

    def test_a_b_a_cycle_stops(self):
        self.start(["a", "b", "a"])
        for _ in range(3):
            self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)

    def test_green_suite_revise_does_not_merge_distinct_candidates(self):
        self.start(["a ready", "b ready", "c ready"])
        for _ in range(3):
            self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(_git(self.repo, "rev-parse", st.integration_ref(self.run_id)),
                         self.target.target_initial_main_sha)
        for row in self.artifacts(st.ArtifactKind.CODE_REVIEW):
            review = json.loads(row["payload_json"])
            self.assertEqual(review["verdict"], "REVISE")
            self.assertEqual(review["public_result_summary"]["failed"], 0)

    def test_exact_named_candidate_not_newest_unreviewed_candidate(self):
        self.start(["a", "b", "c", "a"])
        for _ in range(2):
            self.round()
        self.scheduler._building(self.lane_id)
        history = sch._review_content_history(self.store, self.run_id, self.lane_id,
                                              st.ArtifactKind.CODE_REVIEW)
        self.assertFalse(sch._stalled(history))

    def test_native_pass_counts_increase_then_regress(self):
        self.start(["4", "6", "8", "6"], numeric=True)
        for _ in range(3):
            self.round()
            self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)
        reviews = [json.loads(row["payload_json"]) for row in self.artifacts(st.ArtifactKind.CODE_REVIEW)]
        self.assertEqual([r["public_result_summary"]["passed"] for r in reviews], [4, 6, 8, 6])

    def test_independent_lane_merges_while_stalled_lane_waits(self):
        self.start(["a", "b", "c"], dependency=True)
        for _ in range(3):
            self.round()
        self.assertIn("lane-dependency", self.store.ready_lane_ids(self.run_id))
        self.assertNotIn("lane-dependent", self.store.ready_lane_ids(self.run_id))
        _merge_dependency(self.scheduler, self.repo, self.runtime)
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(self.store.lane_stage(self.run_id, "lane-dependency"), st.LaneStage.MERGED)
        self.assertEqual(self.store.lane_stage(self.run_id, "lane-dependent"), st.LaneStage.PLANNED)
        self.assertNotIn("lane-dependent", self.store.ready_lane_ids(self.run_id))

    def test_user_wait_resets_repeated_work_window(self):
        self.start(["a", "a", "a", "b", "c", "d"])
        for _ in range(3):
            self.round()
        history = sch._review_content_history(self.store, self.run_id, self.lane_id,
                                              st.ArtifactKind.CODE_REVIEW)
        self.assertEqual(history, [])
        self.scheduler.resume_waiting()
        self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)

    def test_revision_resets_window(self):
        self.start(["a", "b", "c"])
        for _ in range(3):
            self.round()
        amended = plan_compiler.compile_plan(_unified_plan_bytes(goal="emit a.txt with detail"),
            plan_revision=2, plan_artifact_ref="plan:content-2")
        sch.apply_factory_amendment(self.store, self.run_id, amended,
                                   runtime=self.runtime, target=self.target)
        history = sch._review_content_history(self.store, self.run_id, self.lane_id,
                                              st.ArtifactKind.CODE_REVIEW)
        self.assertEqual(history, [])

    def test_changed_applicable_base_resets_grace(self):
        self.start(["a", "a", "a", "a", "a"], dependency=True)
        self.round()
        self.round()
        _merge_dependency(self.scheduler, self.repo, self.runtime)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertEqual(self.stage(), st.LaneStage.WAITING_FOR_USER)


class WaitReasonTest(unittest.TestCase):
    def test_no_progress_is_resumable_like_a_pause(self) -> None:
        self.assertIn(st.WaitReason.NO_PROGRESS, st.RESUMABLE_WAIT_REASONS)
        self.assertIn(st.WaitReason.PAUSE, st.RESUMABLE_WAIT_REASONS)

    def test_amendment_required_is_not_resumable_by_a_plain_resume(self) -> None:
        self.assertNotIn(
            st.WaitReason.AMENDMENT_REQUIRED, st.RESUMABLE_WAIT_REASONS
        )

    def test_a_blocked_lane_stays_waiting_across_an_unchanged_amendment(
        self,
    ) -> None:
        for reason in st.RESUMABLE_WAIT_REASONS:
            self.assertIs(
                st.amendment_reset_stage(
                    st.LaneStage.BUILDING, changed=False, wait_reason=reason
                ),
                st.LaneStage.WAITING_FOR_USER,
                reason,
            )
