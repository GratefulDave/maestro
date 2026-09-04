"""A build lane's merge releases its predecessor suite into the integration ref.

Sealed tests are vault-only while a builder is working so the candidate cannot
be shaped to the assertions. That protection ends with the lane's accepted
merge, and until this change nothing followed it: FDAdb run a33d5e9b published
`refs/heads/integration` with 2/2 lanes MERGED, 15/15 sealed cases green, and
zero test files in the repository. Every one of those cases existed only in the
vault. Nobody could run them.

The seam is the lane merge (`_ready_to_merge` -> `merge_or_reconcile`), which
constructs the integration commit before the final review reads it.
Publication is a bare compare-and-swap of `main` to the reviewed SHA and builds
no commit, so it cannot be the seam. These cases pin what the release must and
must not do:

- the suite is in the tree of the `INTEGRATION_MERGE` `after_sha`, at the
  tests lane's declared paths, byte-identical to the vault, and `main` carries
  it after publication;
- the builder never had it: every base the build lane read lacks the path;
- a crashed merge re-derives the same commit with the suite, and a re-derivation
  that forgets the suite is refused rather than adopted;
- a zero-delta first merge still releases the suite (with a commit, since the
  tree changes), and only a head that already carries it is revalidated;
- an unchanged tests lane re-seals after a build amendment even though its
  accepted bytes are now legitimately in the product repository, while a
  private blob anywhere else in that repository still refuses the seal.

What A1 deliberately does not guarantee: after an amendment the build lane's
new base is the integration head, which now carries its own predecessor suite.
Keeping that out of the builder's checkout is the strip guard in
`maestro._refresh_builder_checkout` (A2), not the merge. The amendment case
records that base for exactly that reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules.git_helper import BoundGit  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.runtime_state import RuntimeStateRoot  # noqa: E402
from tests import test_private_review_contract as prc  # noqa: E402
from tests.test_tests_lane_handoff import (  # noqa: E402
    SECRET,
    HandoffActor,
    _git,
    _init_repo,
    _plan_bytes,
)

SUITE_PATH = "tests/public_contract.py"


class _RecordingActor(HandoffActor):
    """HandoffActor that keeps every base SHA a builder was handed."""

    def __init__(self, repo: Path, worktrees: Path) -> None:
        super().__init__(repo, worktrees)
        self.builder_bases: list[tuple[str, str]] = []

    def build(self, ctx: sch.LaneContext) -> dict:
        self.builder_bases.append((ctx.lane.lane_id, ctx.builder_base_sha))
        return super().build(ctx)


class _StopBeforeFinalReview(_RecordingActor):
    def review_integration(self, ctx, lanes, integration_sha):
        del ctx, lanes, integration_sha
        raise RuntimeError("stop-before-final-review")


class _ZeroDeltaActor(_RecordingActor):
    """A builder that finds the product already correct and changes nothing."""

    def build(self, ctx: sch.LaneContext) -> dict:
        self.builder_bases.append((ctx.lane.lane_id, ctx.builder_base_sha))
        self.build_lanes.append(ctx.lane.lane_id)
        return {"candidate_sha": ctx.builder_base_sha, "changed": False}

    def review_code(self, ctx: sch.LaneContext):
        del ctx
        return st.ReviewerVerdict.PASS, ()


def _tree_has(repo: Path, sha: str, path: str) -> bool:
    return BoundGit(repo).tree_blob(sha, path) is not None


def _blob_text(repo: Path, sha: str, path: str) -> str:
    return _git(repo, "show", "{0}:{1}".format(sha, path))


class SealedReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")

    def _start(self, compiled: st.CompiledPlan, actor, run_id: str) -> sch.FactoryScheduler:
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )
        return self._scheduler(compiled, actor, run_id)

    def _scheduler(self, compiled, actor, run_id: str) -> sch.FactoryScheduler:
        return sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, self.target, compiled=compiled
        )

    def _merge(self, run_id: str, lane_id: str = "lane-build") -> dict:
        row = self.store.conn.execute(
            "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence DESC LIMIT 1",
            (run_id, lane_id, st.ArtifactKind.INTEGRATION_MERGE.value),
        ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def _sealed_bytes(self, scheduler: sch.FactoryScheduler, run_id: str) -> dict[str, bytes]:
        lane = next(
            item
            for item in self.store.active_projection(run_id)
            if item.lane_id == "lane-build"
        )
        return sch._released_sealed_files(self.store, self.runtime.path, run_id, lane)

    # -- the release itself -------------------------------------------------

    def test_a_build_merge_releases_its_predecessor_suite(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:release"
        )
        actor = _RecordingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-release")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)

        merge = self._merge("run-release")
        self.assertFalse(merge["revalidated"])
        # The tree before the merge has no suite; the tree after it does, at
        # the tests lane's declared path, byte-identical to the vault.
        self.assertFalse(_tree_has(self.repo, merge["before_sha"], SUITE_PATH))
        sealed = self._sealed_bytes(scheduler, "run-release")
        self.assertEqual(set(sealed), {SUITE_PATH})
        self.assertEqual(
            _blob_text(self.repo, merge["after_sha"], SUITE_PATH).encode("utf-8") + b"\n",
            sealed[SUITE_PATH],
        )
        self.assertIn(SECRET, sealed[SUITE_PATH].decode("utf-8"))
        # The recorded tree is the tree of the recorded commit -- what a crash
        # reconciliation compares -- and it is the suite-carrying one.
        self.assertEqual(
            merge["expected_tree_sha"],
            _git(self.repo, "rev-parse", merge["after_sha"] + "^{tree}"),
        )
        # The candidate itself never carried the suite: it was added at merge.
        self.assertFalse(_tree_has(self.repo, merge["candidate_sha"], SUITE_PATH))
        # Publication is a CAS of main to that exact SHA, so main carries it.
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), merge["after_sha"])
        self.assertIn(SECRET, (self.repo / SUITE_PATH).read_text(encoding="utf-8"))

    def test_the_builder_never_read_a_base_that_carried_its_own_suite(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:base"
        )
        actor = _RecordingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-base")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        # Two builds: the opening candidate and the CODE_REVISE round. Both
        # precede the lane's merge, which is the only thing that releases.
        self.assertEqual([lane for lane, _ in actor.builder_bases], ["lane-build", "lane-build"])
        for _lane, base in actor.builder_bases:
            self.assertFalse(_tree_has(self.repo, base, SUITE_PATH), base)
        self.assertNotIn(SECRET, json.dumps(actor.builder_contracts))

    # -- crash reconciliation ------------------------------------------------

    def test_a_crashed_merge_rederives_the_suite_or_refuses(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:reconcile"
        )
        actor = _StopBeforeFinalReview(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-reconcile")
        with self.assertRaisesRegex(RuntimeError, "stop-before-final-review"):
            scheduler.run()
        merge = self._merge("run-reconcile")
        row = sch.run_row(self.store, "run-reconcile")
        kwargs = dict(
            run_id="run-reconcile",
            lane_id="lane-build",
            stage_input_digest=merge["input_digest"],
            builder_artifact_id=merge["builder_output_artifact_id"],
            code_review_artifact_id=merge["code_review_artifact_id"],
            builder_base_sha=merge["builder_base_sha"],
            candidate_ref=merge["candidate_ref"],
            candidate_sha=merge["candidate_sha"],
            before_sha=merge["before_sha"],
            epoch_seconds=sch.merge_epoch_seconds(row["created_at"]),
            input_digest=merge["input_digest"],
        )
        sealed = self._sealed_bytes(scheduler, "run-reconcile")
        # The ref already moved and the ledger already knows: reconciling with
        # the suite re-derives exactly the recorded commit.
        again = gitpub.reconcile_integration_merge(self.target, sealed_files=sealed, **kwargs)
        self.assertEqual(again["after_sha"], merge["after_sha"])
        self.assertEqual(again["expected_tree_sha"], merge["expected_tree_sha"])
        # A re-derivation that forgets the suite describes a different commit
        # and is refused, never adopted as this merge.
        with self.assertRaises(gitpub.GitPublicationRefused) as raised:
            gitpub.reconcile_integration_merge(self.target, **kwargs)
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")

    # -- zero delta ------------------------------------------------------------

    def test_a_zero_delta_first_merge_still_releases_the_suite(self) -> None:
        # Brownfield: the product already satisfies the suite, so the builder
        # changes nothing. The integration tree still has to gain the suite.
        (self.repo / "product.py").write_text("already ready\n", encoding="utf-8")
        _git(self.repo, "add", "product.py")
        _git(self.repo, "commit", "-m", "existing product")
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:zero"
        )
        actor = _ZeroDeltaActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-zero")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        merge = self._merge("run-zero")
        self.assertEqual(merge["before_sha"], merge["candidate_sha"])
        self.assertNotEqual(merge["after_sha"], merge["before_sha"])
        self.assertFalse(merge["revalidated"])
        self.assertEqual(
            BoundGit(self.repo).commit_parents(merge["after_sha"]), (merge["before_sha"],)
        )
        self.assertFalse(_tree_has(self.repo, merge["before_sha"], SUITE_PATH))
        self.assertTrue(_tree_has(self.repo, merge["after_sha"], SUITE_PATH))
        self.assertEqual(
            _blob_text(self.repo, merge["after_sha"], "product.py"), "already ready"
        )
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), merge["after_sha"])
        # The durable tip still chains through it as an ordinary merge record.
        tip = gitpub.durable_integration_tip(
            sch.run_row(self.store, "run-zero")["integration_initial_sha"], [merge]
        )
        self.assertEqual(tip, merge["after_sha"])

    def test_the_merge_decision_revalidates_only_a_head_that_carries_the_suite(self) -> None:
        head = "a" * 40
        stale = "b" * 40
        common = dict(builder_base_sha=head, candidate_sha=head, integration_head=head)
        self.assertEqual(
            gitpub.decide_merge_action(changed=False, sealed_present=True, **common).action,
            "REVALIDATE",
        )
        self.assertEqual(
            gitpub.decide_merge_action(changed=False, sealed_present=False, **common).action,
            "MERGE",
        )
        # A stale base is BASE_INVALIDATION before anything is released.
        self.assertEqual(
            gitpub.decide_merge_action(
                changed=False,
                sealed_present=False,
                builder_base_sha=stale,
                candidate_sha=stale,
                integration_head=head,
            ).action,
            "BASE_INVALIDATION",
        )
        # A changing candidate merges regardless; the suite rides in its commit.
        self.assertEqual(
            gitpub.decide_merge_action(
                changed=True,
                sealed_present=False,
                builder_base_sha=head,
                candidate_sha=stale,
                integration_head=head,
            ).action,
            "MERGE",
        )

    def test_sealed_files_present_is_byte_exact(self) -> None:
        git = BoundGit(self.repo)
        head = git.rev_parse("HEAD")
        seed = (self.repo / "seed.txt").read_bytes()
        self.assertTrue(gitpub.sealed_files_present(self.target, head, {"seed.txt": seed}))
        self.assertFalse(
            gitpub.sealed_files_present(self.target, head, {"seed.txt": seed + b"x"})
        )
        self.assertFalse(gitpub.sealed_files_present(self.target, head, {"absent.py": b""}))
        self.assertTrue(gitpub.sealed_files_present(self.target, head, {}))

    def test_overlay_tree_touches_neither_index_nor_worktree(self) -> None:
        git = BoundGit(self.repo)
        base_tree = git.resolve_tree("HEAD")
        status_before = _git(self.repo, "status", "--porcelain")
        overlaid = git.overlay_tree(base_tree, {SUITE_PATH: b"def test_x():\n    pass\n"})
        self.assertNotEqual(overlaid, base_tree)
        self.assertEqual(git.overlay_tree(base_tree, {}), base_tree)
        # Deterministic: the same bytes over the same tree is the same tree.
        self.assertEqual(
            overlaid, git.overlay_tree(base_tree, {SUITE_PATH: b"def test_x():\n    pass\n"})
        )
        self.assertEqual(_git(self.repo, "status", "--porcelain"), status_before)
        self.assertFalse((self.repo / SUITE_PATH).exists())
        listed = _git(self.repo, "ls-tree", "-r", "--name-only", overlaid).splitlines()
        self.assertEqual(sorted(listed), sorted(["seed.txt", SUITE_PATH]))

    # -- amendment -----------------------------------------------------------

    def test_an_unchanged_tests_lane_reseals_after_a_build_amendment(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:amend"
        )
        first = _StopBeforeFinalReview(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, first, "run-amend")
        with self.assertRaisesRegex(RuntimeError, "stop-before-final-review"):
            scheduler.run()
        for lane in ("lane-tests", "lane-build"):
            self.assertEqual(self.store.lane_stage("run-amend", lane), st.LaneStage.MERGED)
        released_once = self._merge("run-amend")["after_sha"]
        self.assertTrue(_tree_has(self.repo, released_once, SUITE_PATH))
        # The accepted blob is now, legitimately, in the product repository.
        sealed = self._sealed_bytes(scheduler, "run-amend")
        blob = BoundGit(self.repo).hash_object(sealed[SUITE_PATH], write=False)
        self.assertFalse(hv.object_is_absent(self.repo, blob))

        amended = plan_compiler.compile_plan(
            _plan_bytes(build_goal="implement product.py with extra detail"),
            plan_revision=2,
            plan_artifact_ref="plan:amend-2",
        )
        sch.apply_factory_amendment(
            self.store, "run-amend", amended, runtime=self.runtime, target=self.target
        )
        self.assertEqual(
            self.store.lane_stage("run-amend", "lane-tests"), st.LaneStage.TESTS_SEALED
        )
        self.assertEqual(
            self.store.lane_stage("run-amend", "lane-build"), st.LaneStage.PLANNED
        )
        resume = _RecordingActor(self.repo, self.runtime.path / "worktrees")
        # The amended build's first candidate is green and its reviewer agrees.
        # `_sealed_error_history` counts CODE_REVIEW rounds across plan
        # revisions (only a USER_WAIT resets it), so a third round that fails to
        # set a strict new low pauses the lane NO_PROGRESS. Both seeds are
        # needed for that: `build_lanes` makes the candidate "ready" so the
        # suite is green, and `code_rounds` skips `HandoffActor`'s scripted
        # opening REVISE, which since #208 is no longer rewritten to PASS by a
        # green suite and would otherwise be the stalling third round. The
        # NO_PROGRESS rule is not what this case is about; it is tracked
        # separately.
        resume.build_lanes.append("lane-build")
        resume.code_rounds["lane-build"] = 1
        scheduler = self._scheduler(amended, resume, "run-amend")
        # The re-seal proves absence against a repository that holds the
        # released blob at the released path; that is not a leak, and the
        # run continues to publication.
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(resume.write_tests_lanes, [])
        seals = [
            (row[0], json.loads(row[1])["sealed_digest"])
            for row in self.store.conn.execute(
                "SELECT plan_revision, payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
                ("run-amend", "lane-tests", st.ArtifactKind.SEALED_TEST_BUNDLE.value),
            )
        ]
        self.assertEqual([revision for revision, _ in seals], [1, 2])
        self.assertEqual(seals[0][1], seals[1][1])
        merge = self._merge("run-amend")
        self.assertTrue(_tree_has(self.repo, merge["after_sha"], SUITE_PATH))
        self.assertEqual(
            _blob_text(self.repo, merge["after_sha"], SUITE_PATH).encode("utf-8") + b"\n",
            sealed[SUITE_PATH],
        )
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), merge["after_sha"])
        # What this change leaves to A2: the amended build's base is the
        # integration head, which carries the lane's own predecessor suite.
        # The merge cannot un-release it; the builder checkout strip in
        # `maestro._refresh_builder_checkout` is what keeps it from the builder.
        bases = [base for lane, base in resume.builder_bases if lane == "lane-build"]
        self.assertTrue(bases)
        for base in bases:
            self.assertTrue(_tree_has(self.repo, base, SUITE_PATH), base)


class SealAbsenceProofTests(unittest.TestCase):
    """`released` narrows the seal's absence proof to the released blobs only."""

    def setUp(self) -> None:
        self.case = prc.PrivateReviewContract("setUp")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def _accepted(self) -> tuple[st.LaneArtifact, st.LaneArtifact, str]:
        digest = prc._digest("draft-1")
        draft = self.case._draft(digest)
        review = self.case._review(draft, digest, st.ReviewerVerdict.PASS)
        vault = hv.vault_path(self.case.state, self.case.run_id)
        blob = hv.blob_id_in(vault, hv.rev_parse(vault, draft.artifact_ref), prc.TEST_PATH)
        self.assertIsNotNone(blob)
        return draft, review, str(blob)

    def _seal(self, draft, review, released=()) -> st.LaneArtifact:
        return tc.seal_accepted_tests(
            request=prc._request(
                run_id=self.case.run_id,
                lane_id=self.case.lane_id,
                input_digest=prc._digest("seal-" + draft.input_digest),
            ),
            state_root=self.case.state,
            run_repo=self.case.repo,
            builder_worktree=None,
            test_draft=draft,
            test_review=review,
            released=released,
        )

    def test_a_released_blob_in_the_run_repository_does_not_refuse_the_seal(self) -> None:
        draft, review, blob = self._accepted()
        # Put the accepted bytes into the product repository the way a merge
        # does: as an object, not via the vault.
        written = BoundGit(self.case.repo).hash_object(prc.TEST_SOURCE.encode("utf-8"))
        self.assertEqual(written, blob)
        with self.assertRaises(hv.VaultError):
            self._seal(draft, review)
        sealed = self._seal(draft, review, released=frozenset({blob}))
        self.assertIs(sealed.kind, st.ArtifactKind.SEALED_TEST_BUNDLE)

    def test_an_unrelated_private_object_still_refuses_the_seal(self) -> None:
        draft, review, blob = self._accepted()
        # The draft commit itself is never released; naming the blob as
        # released does not excuse the commit turning up in the run repo.
        vault = hv.vault_path(self.case.state, self.case.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        subprocess.run(
            ["git", "-C", str(self.case.repo), "fetch", "-q", str(vault), commit],
            check=True,
            capture_output=True,
        )
        with self.assertRaises(hv.VaultError):
            self._seal(draft, review, released=frozenset({blob}))


if __name__ == "__main__":
    unittest.main()
