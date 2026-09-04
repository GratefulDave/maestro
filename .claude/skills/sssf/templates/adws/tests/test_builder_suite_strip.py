"""A builder's checkout never holds the suite that lane is graded against.

A1 releases a build lane's predecessor suite into the integration ref at
merge, which is what makes a sealed test runnable in the repository at all.
It also puts that suite in the lane's own next base: after a plan amendment
or a `BASE_INVALIDATION` reset the lane restarts at `BUILDING` with
`builder_base_sha` set to an integration head that carries the very tests it
is measured by. The merge cannot un-release it.

A builder that can read its acceptance tests writes to the assertions. That
is measured behaviour in this repository, not a worry: `sealed_probe` gave a
builder one bit per query about the hidden suite and it recovered thirteen
hidden string literals character by character in about 205 probes, then
shipped a candidate that looked clean (deleted in #204).

So the guard is in the checkout, not in the base: `_refresh_builder_checkout`
removes the lane's own sealed paths from the working tree before every
builder turn, and `_commit_declared` removes them again after it puts the
tree back. What these cases pin:

- the file is gone from the working tree at the moment the builder is
  launched -- the observable state, not that a function was called;
- the removal never reaches the candidate. It stays out of the index and out
  of the commit pathspec, so the delta `validate_declared_ownership` reads is
  the same delta it would have read without the guard, and the candidate tree
  still carries the suite byte-for-byte;
- that holds even when the lane's own declared outputs cover the suite path,
  which is the case where a recorded deletion would be admitted as owned and
  would delete a released suite from the integration ref;
- stripping is not builder work: it does not make the checkout read dirty and
  does not manufacture a candidate;
- and end to end: after a real amendment, the base a real scheduler hands the
  builder carries the suite, the context names exactly that lane's own suite,
  and the real `_refresh_builder_checkout` takes it away.

The scheduler-side fakes cannot see any of this. `HandoffActor` replaces
`HerdrStageActor` outright, and `test_bound_surface_wiring` stubs both
`_refresh_builder_checkout` and `_commit_declared` to no-ops. Every case
below drives the real methods against a real git checkout for that reason.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Sequence, cast

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.runtime_state import RuntimeStateRoot  # noqa: E402
from adw_modules.scheduler import FactoryRefused  # noqa: E402
from tests import test_actor_delegation_capability as adc  # noqa: E402
from tests.test_sealed_release import SUITE_PATH, _RecordingActor  # noqa: E402
from tests.test_tests_lane_handoff import (  # noqa: E402
    SECRET,
    _git,
    _init_repo,
    _plan_bytes,
)

_ROLE_ROUTES = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}

SUITE_TEXT = "def test_contract():\n    assert 'secret-{0}' == 'secret-{0}'\n".format(
    SECRET
)


class _SilentLauncher:
    """Never launched. `HerdrStageActor` only needs the attribute to exist."""


def _tree_paths(repo: Path, sha: str) -> frozenset[str]:
    listed = _git(repo, "ls-tree", "-r", "--name-only", sha)
    return frozenset(line for line in listed.splitlines() if line)


def _delta_paths(repo: Path, base: str, candidate: str) -> frozenset[str]:
    raw = _git(repo, "diff-tree", "-r", "--name-only", "--no-commit-id", base, candidate)
    return frozenset(line for line in raw.splitlines() if line)


def _grep_tree(root: Path, needle: str) -> list[str]:
    """Every file under `root` (outside .git) whose bytes contain `needle`."""
    hits = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or not path.is_file() or path.is_symlink():
            continue
        try:
            if needle in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(root)))
        except (OSError, UnicodeError):
            continue
    return hits


class BuilderCheckoutStripTest(unittest.TestCase):
    """The real `HerdrStageActor` against a real checkout carrying the suite."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.checkout = self.root / "checkout"
        _init_repo(self.checkout)
        # The base a build lane reads after its own merge released the suite.
        (self.checkout / "tests").mkdir()
        (self.checkout / SUITE_PATH).write_text(SUITE_TEXT, encoding="utf-8")
        (self.checkout / "product.py").write_text("value = 0\n", encoding="utf-8")
        _git(self.checkout, "add", "--", SUITE_PATH, "product.py")
        _git(self.checkout, "commit", "-m", "released suite")
        self.base = _git(self.checkout, "rev-parse", "HEAD")
        self.suite_blob = _git(self.checkout, "rev-parse", "HEAD:" + SUITE_PATH)

        product = self.root / "product"
        _init_repo(product)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, _SilentLauncher()),
            state,
            gitpub.bind_target_worktree(product, "refs/heads/main"),
            _ROLE_ROUTES,
        )

    def refresh(self, outputs: Sequence[str] = ("product.py",)) -> None:
        self.actor._refresh_builder_checkout(
            self.checkout, outputs, self.base, strip=(SUITE_PATH,)
        )

    def commit(self, outputs: Sequence[str] = ("product.py",)) -> tuple[str, bool]:
        return self.actor._commit_declared(
            self.checkout, outputs, self.base, strip=(SUITE_PATH,)
        )

    # -- the observable state ------------------------------------------------

    def test_the_suite_is_absent_from_the_working_tree(self) -> None:
        self.assertTrue((self.checkout / SUITE_PATH).exists())
        self.refresh()
        self.assertFalse((self.checkout / SUITE_PATH).exists())
        # Not merely renamed or emptied: the assertion text is nowhere the
        # builder can read, and the directory that named the suite is gone.
        self.assertEqual(_grep_tree(self.checkout, SECRET), [])
        self.assertFalse((self.checkout / "tests").exists())
        # The base still holds it. The guard is in the checkout, not the ref.
        self.assertIn(SUITE_PATH, _tree_paths(self.checkout, self.base))

    def test_the_suite_is_still_absent_after_the_candidate_commit(self) -> None:
        self.refresh()
        (self.checkout / "product.py").write_text("value = 1\n", encoding="utf-8")
        candidate, changed = self.commit()
        self.assertTrue(changed)
        self.assertNotEqual(candidate, self.base)
        # `_commit_declared` resets hard to the base, which puts the suite
        # back. The builder's session lives in this directory between turns.
        self.assertFalse((self.checkout / SUITE_PATH).exists())
        self.assertEqual(_grep_tree(self.checkout, SECRET), [])

    def test_a_guard_that_did_nothing_would_fail_these_cases(self) -> None:
        # The negative control for the two cases above: without `strip` the
        # same calls leave the suite readable, so those assertions are about
        # the guard and not about some other property of the checkout.
        self.actor._refresh_builder_checkout(
            self.checkout, ("product.py",), self.base
        )
        self.assertTrue((self.checkout / SUITE_PATH).exists())
        self.assertEqual(_grep_tree(self.checkout, SECRET), [SUITE_PATH])
        self.actor._commit_declared(self.checkout, ("product.py",), self.base)
        self.assertTrue((self.checkout / SUITE_PATH).exists())

    # -- the removal never reaches the candidate -----------------------------

    def test_the_candidate_does_not_record_the_removal(self) -> None:
        self.refresh()
        (self.checkout / "product.py").write_text("value = 1\n", encoding="utf-8")
        candidate, changed = self.commit()
        self.assertTrue(changed)
        # The delta `validate_declared_ownership` reads names the declared
        # output and nothing else -- no deletion for it to admit or refuse.
        self.assertEqual(_delta_paths(self.checkout, self.base, candidate), {"product.py"})
        # And the candidate tree still carries the suite, byte-for-byte.
        self.assertIn(SUITE_PATH, _tree_paths(self.checkout, candidate))
        self.assertEqual(
            _git(self.checkout, "rev-parse", candidate + ":" + SUITE_PATH),
            self.suite_blob,
        )

    def test_a_declared_output_covering_the_suite_still_keeps_it(self) -> None:
        """The unsafe case: a lane whose declared outputs cover the suite.

        Nothing in the plan compiler forbids a build lane declaring an output
        that contains its predecessor's test path. If the removal were staged,
        `validate_declared_ownership` would admit the deletion as owned and
        the merge would delete a released suite from the integration ref. The
        strip paths are subtracted from the commit pathspec, so it cannot be
        staged whatever the lane declares.
        """
        self.actor._refresh_builder_checkout(
            self.checkout, ("tests", "product.py"), self.base, strip=(SUITE_PATH,)
        )
        self.assertFalse((self.checkout / SUITE_PATH).exists())
        (self.checkout / "product.py").write_text("value = 2\n", encoding="utf-8")
        candidate, changed = self.actor._commit_declared(
            self.checkout, ("tests", "product.py"), self.base, strip=(SUITE_PATH,)
        )
        self.assertTrue(changed)
        self.assertEqual(_delta_paths(self.checkout, self.base, candidate), {"product.py"})
        self.assertIn(SUITE_PATH, _tree_paths(self.checkout, candidate))

    def test_the_builder_may_still_write_a_sibling_of_the_suite(self) -> None:
        """Stripping one path does not close the directory that held it."""
        self.actor._refresh_builder_checkout(
            self.checkout, ("tests", "product.py"), self.base, strip=(SUITE_PATH,)
        )
        helper = self.checkout / "tests" / "helper.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("HELPER = 1\n", encoding="utf-8")
        candidate, changed = self.actor._commit_declared(
            self.checkout, ("tests", "product.py"), self.base, strip=(SUITE_PATH,)
        )
        self.assertTrue(changed)
        self.assertEqual(
            _delta_paths(self.checkout, self.base, candidate), {"tests/helper.py"}
        )

    # -- stripping is not builder work ---------------------------------------

    def test_the_strip_alone_is_not_a_change(self) -> None:
        self.refresh()
        candidate, changed = self.commit()
        self.assertFalse(changed)
        self.assertEqual(candidate, self.base)
        self.assertFalse((self.checkout / SUITE_PATH).exists())

    def test_a_stripped_checkout_does_not_read_dirty(self) -> None:
        """The second turn takes the same path through the refresh as the first.

        A deleted tracked file is dirty to `git status`. If the measurement
        counted it, an already-stripped checkout would fall through to
        `_commit_declared` on every later turn -- correct, but it would mean
        the guard silently changed which branch the refresh takes.
        """
        self.refresh()
        head = _git(self.checkout, "rev-parse", "HEAD")
        self.assertNotEqual(_git(self.checkout, "status", "--porcelain"), "")
        self.refresh()
        self.assertEqual(_git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertFalse((self.checkout / SUITE_PATH).exists())

    def test_an_absent_suite_is_not_an_error(self) -> None:
        """A lane whose base never carried the suite strips nothing."""
        self.refresh()
        self.refresh()
        self.assertFalse((self.checkout / SUITE_PATH).exists())

    def test_a_strip_path_that_escapes_the_checkout_refuses(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(FactoryRefused):
            self.actor._refresh_builder_checkout(
                self.checkout, ("product.py",), self.base, strip=("../outside.txt",)
            )
        self.assertTrue(outside.exists())

    def test_a_symlink_at_a_sealed_path_is_removed_not_followed(self) -> None:
        target = self.root / "elsewhere.txt"
        target.write_text("keep\n", encoding="utf-8")
        (self.checkout / SUITE_PATH).unlink()
        (self.checkout / SUITE_PATH).symlink_to(target)
        self.actor._strip_paths(self.checkout, (SUITE_PATH,))
        self.assertFalse((self.checkout / SUITE_PATH).is_symlink())
        self.assertTrue(target.exists())


class LaunchedBuilderTest(unittest.TestCase):
    """Through `build()` itself: what the launcher finds in the worktree.

    The cases above call the refresh directly. This one goes through the
    production path -- `build` -> `_launch` -> `prepare_cwd` -> the refresh --
    and reads the answer off `RecordingLauncher`, which records whether the
    hidden test was a file in the worktree at the instant the builder was
    launched. That is the observable the guard exists to produce.
    """

    def _bench(self, root: Path) -> tuple[Path, str, object, object]:
        product = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        adc._init_repo(product)
        hidden = product / "tests" / "hidden.py"
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text(SUITE_TEXT, encoding="utf-8")
        _git(product, "add", "--", "tests/hidden.py")
        _git(product, "commit", "-m", "released suite")
        head = _git(product, "rev-parse", "HEAD")
        target = gitpub.bind_target_worktree(product, "refs/heads/main")
        recorder = adc.RecordingLauncher(
            files={"a.txt": "a\n"},
            envelope={"candidate_sha": head, "changed": False},
        )
        actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, recorder), state, target, adc._ROLE_ROUTES
        )
        return product, head, recorder, actor

    @staticmethod
    def _ctx(head: str, sealed: tuple[str, ...]) -> sch.LaneContext:
        return sch.LaneContext(
            run_id="run-launch",
            lane=adc._lane(),
            plan_revision=1,
            plan_digest="cd" * 32,
            plan_artifact_ref="plan:x",
            input_digest="22" * 32,
            stage=st.LaneStage.BUILDING,
            artifacts={},
            builder_base_sha=head,
            public_contract={
                "acceptance_criteria": ["a.txt is written"],
                "declared_outputs": ["a.txt"],
            },
            sealed_digest="33" * 32,
            sealed_private_paths=sealed,
        )

    def test_the_launcher_finds_no_suite_in_the_builder_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            product, head, recorder, actor = self._bench(Path(tmp))
            result = actor.build(self._ctx(head, ("tests/hidden.py",)))
            self.assertFalse(recorder.launches[0]["hidden_test_at_launch"])
            candidate = str(result["candidate_sha"])
            # The declared output is the whole delta, and the suite survives
            # in the candidate tree: nothing here records a deletion.
            self.assertEqual(_delta_paths(product, head, candidate), {"a.txt"})
            self.assertIn("tests/hidden.py", _tree_paths(product, candidate))

    def test_the_next_turn_finds_no_suite_either(self) -> None:
        """A CODE_REVISE turn adopts the same directory and is resubmitted."""
        with tempfile.TemporaryDirectory() as tmp:
            _product, head, recorder, actor = self._bench(Path(tmp))
            ctx = self._ctx(head, ("tests/hidden.py",))
            actor.build(ctx)
            actor.build(ctx)
            self.assertTrue(recorder.resubmits)
            self.assertIsNone(recorder.resubmits[0]["hidden_test_body"])

    def test_without_the_guard_the_launcher_finds_it(self) -> None:
        """The negative control: the base really does carry the suite."""
        with tempfile.TemporaryDirectory() as tmp:
            _product, head, recorder, actor = self._bench(Path(tmp))
            actor.build(self._ctx(head, ()))
            self.assertTrue(recorder.launches[0]["hidden_test_at_launch"])


class AmendedBuilderCheckoutTest(unittest.TestCase):
    """End to end: the base carries the suite, the checkout does not.

    A1's `test_an_unchanged_tests_lane_reseals_after_a_build_amendment`
    records the bases the amended build lane read and asserts every one of
    them carries the lane's own suite, naming this guard as what keeps it from
    the builder. This is the other half of that assertion, driven from the
    same real run: the context the scheduler builds names exactly that suite,
    and the real refresh removes it from a real worktree at that real base.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")

    def _run(self, compiled, actor, run_id: str) -> sch.FactoryScheduler:
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )
        return sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, self.target, compiled=compiled
        )

    def test_the_amended_builder_checkout_loses_its_own_suite(self) -> None:
        class _StopBeforeFinalReview(_RecordingActor):
            def review_integration(self, ctx, lanes, integration_sha):
                del ctx, lanes, integration_sha
                raise RuntimeError("stop-before-final-review")

        class _StripRecordingActor(_RecordingActor):
            def __init__(self, repo, worktrees):
                super().__init__(repo, worktrees)
                self.strips: list[tuple[str, tuple[str, ...]]] = []

            def build(self, ctx):
                self.strips.append((ctx.lane.lane_id, tuple(ctx.sealed_private_paths)))
                return super().build(ctx)

        worktrees = self.runtime.path / "worktrees"
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:strip"
        )
        first = _StopBeforeFinalReview(self.repo, worktrees)
        scheduler = self._run(compiled, first, "run-strip")
        with self.assertRaisesRegex(RuntimeError, "stop-before-final-review"):
            scheduler.run()
        for lane in ("lane-tests", "lane-build"):
            self.assertEqual(self.store.lane_stage("run-strip", lane), st.LaneStage.MERGED)

        amended = plan_compiler.compile_plan(
            _plan_bytes(build_goal="implement product.py with extra detail"),
            plan_revision=2,
            plan_artifact_ref="plan:strip-2",
        )
        sch.apply_factory_amendment(
            self.store, "run-strip", amended, runtime=self.runtime, target=self.target
        )
        resume = _StripRecordingActor(self.repo, worktrees)
        resume.build_lanes.append("lane-build")
        scheduler = sch.FactoryScheduler(
            self.store, "run-strip", resume, self.runtime, self.target, compiled=amended
        )
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)

        # The context names exactly this lane's own pair -- the same map A1's
        # release derives, so the paths stripped are the paths released.
        lane = next(
            item
            for item in self.store.active_projection("run-strip")
            if item.lane_id == "lane-build"
        )
        own = sch._released_sealed_files(
            self.store, self.runtime.path, "run-strip", lane
        )
        self.assertEqual(set(own), {SUITE_PATH})
        strips = [paths for lane_id, paths in resume.strips if lane_id == "lane-build"]
        self.assertTrue(strips)
        for paths in strips:
            self.assertEqual(paths, (SUITE_PATH,))

        # The base the amended builder read carries the suite ...
        bases = [base for lane_id, base in resume.builder_bases if lane_id == "lane-build"]
        self.assertTrue(bases)
        base = bases[-1]
        self.assertIn(SUITE_PATH, _tree_paths(self.repo, base))

        # ... and the real refresh takes it out of a real worktree at it.
        checkout = worktrees / "run-strip-lane-build-a1" / "checkout"
        _git(self.repo, "worktree", "add", "--detach", str(checkout), base)
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(checkout)],
                check=False,
                capture_output=True,
            )
        )
        actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, _SilentLauncher()),
            self.runtime.path,
            self.target,
            _ROLE_ROUTES,
        )
        self.assertTrue((checkout / SUITE_PATH).exists())
        actor._refresh_builder_checkout(
            checkout, lane.declared_outputs, base, strip=strips[-1]
        )
        self.assertFalse((checkout / SUITE_PATH).exists())
        self.assertEqual(_grep_tree(checkout, SECRET), [])


class OwnPairScopeTest(unittest.TestCase):
    """`sealed_private_paths` is the lane's own pair, resolved by `_sealed_for`.

    `_own_sealed_paths` reads the bundle `_sealed_for` returned, so scoping is
    that method's rule and not a second one: a build lane gets its tests-lane
    predecessor's bundle, an untyped lane its own. What this case pins is that
    the helper returns the paths of that bundle and nothing else -- notably
    not another lane's released suite, which the builder may legitimately read.
    """

    def test_the_helper_returns_the_paths_of_the_bundle_it_was_given(self) -> None:
        scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
        scheduler.run_id = "run1"
        recorded: dict[str, object] = {}

        def _files(vault, artifact):
            recorded["artifact"] = artifact
            return {"tests/b.py": "b" * 40, "tests/a.py": "a" * 40}

        lane = adc._lane(
            lane_id="lane-build",
            needs=("lane-tests",),
            outputs=("product.py",),
            lane_kind=st.LANE_KIND_BUILD,
        )
        sealed = object()
        with mock.patch.object(sch.tc, "sealed_private_files", _files), \
            mock.patch.object(sch.hv, "ensure_vault", lambda root, run: root), \
            mock.patch.object(
                sch, "_record_as_lane_artifact", lambda record, lane: record
            ):
            scheduler.runtime = type("R", (), {"path": Path("/state")})()
            paths = sch.FactoryScheduler._own_sealed_paths(scheduler, lane, sealed)
        self.assertEqual(paths, ("tests/a.py", "tests/b.py"))
        self.assertIs(recorded["artifact"], sealed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
