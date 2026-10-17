"""Protected paths on both sides of a build: the builder's and the code reviewer's.

The builder may not create a file at an active probe's path (it is refused at
admission, before any reviewer reads the candidate), and the code reviewer is no
longer handed `test_paths=[]`: `_reviewing_code` used to build its context
without `protected_test_paths`.
"""

import unittest

from adw_modules import git_publication as gitpub
from adw_modules import scheduler_types as st
from tests.probe_lane_support import ProbeActor, ProbeLaneCase, probe_path, probe_source
from tests.test_tests_lane_handoff import _git

REVISE = st.ReviewerVerdict.REVISE
SUITE = "tests/test_counts.py"


class WritesAtTheProbePath(ProbeActor):
    """A builder that, from its second build on, writes the probe's path."""

    def build(self, ctx):
        if self.builds == 0:
            return super().build(ctx)
        self.builder_ctxs.append(ctx)
        self.builds += 1
        work = self.worktrees / "probe-build" / str(self.builds)
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        (work / "a.txt").write_text("8 alpha", encoding="utf-8")
        target = work / probe_path("alpha")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_alpha():\n    pass\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "touches the probe")
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}


class BuilderSideTest(ProbeLaneCase):
    def test_builder_context_protects_the_probe_and_carries_its_text(self):
        self.start(["8", "8 alpha"], [{"verdict": REVISE, "probes": ["alpha"]}, {"verdict": REVISE}])
        self.round()
        self.round()
        first, second = self.actor.builder_ctxs
        self.assertEqual(first.protected_test_paths, (SUITE,))
        self.assertIsNone(first.probe_texts)
        self.assertEqual(second.protected_test_paths, (probe_path("alpha"), SUITE))
        self.assertEqual(second.probe_texts, {probe_path("alpha"): probe_source("alpha")})

    def test_a_candidate_that_touches_a_probe_path_is_refused_at_admission(self):
        self.start(["8"], [{"verdict": REVISE, "probes": ["alpha"]}])
        self.actor.__class__ = WritesAtTheProbePath
        self.round()
        with self.assertRaises(gitpub.GitPublicationRefused) as refused:
            self.scheduler._building(self.lane_id)
        self.assertEqual(refused.exception.code, "CANDIDATE_TEST_PATH_REFUSED")
        self.assertEqual(refused.exception.detail, probe_path("alpha"))


class ReviewerSideTest(ProbeLaneCase):
    def test_code_reviewer_is_handed_the_accepted_suite_paths(self):
        self.start(["8"], [{"verdict": REVISE}])
        self.round()
        reviewer_ctx = self.actor.review_ctxs[0]
        self.assertEqual(reviewer_ctx.stage, st.LaneStage.REVIEWING_CODE)
        self.assertEqual(reviewer_ctx.protected_test_paths, (SUITE,))

    def test_reviewer_is_told_the_active_probes_and_the_budget(self):
        self.start(["8", "8"], [{"verdict": REVISE, "probes": ["alpha"]}, {"verdict": REVISE}])
        self.round()
        self.round()
        first, second = self.actor.review_ctxs
        self.assertEqual(first.existing_probes, ())
        self.assertEqual(first.probe_budget_left, 5)
        self.assertEqual([r["path"] for r in second.existing_probes], [probe_path("alpha")])
        self.assertEqual(second.probe_budget_left, 4)


if __name__ == "__main__":
    unittest.main()
