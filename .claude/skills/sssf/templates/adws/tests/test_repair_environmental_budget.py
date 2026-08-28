"""A repair cycle must not spend the environmental retry budget.

A retained builder that commits its own repair leaves HEAD at a linear
descendant of the attempt's base. That is recover_sealed_descendant's
lineage, not a machine fault. commit_measured_delta against the stale base
used to raise HeadMoved, classify ENVIRONMENTAL, and burn
environmental_retries (2) so the third repair blocked
ENVIRONMENTAL_BUDGET_EXHAUSTED.

Every test builds a real git repository. Nothing stubs subprocess.run.
Drive one `_attempt` rather than `run()`: the persistent review/repair loop
already lives inside that call, and `run()`'s outer loop is not the subject.
"""

from __future__ import annotations

import sys
from unittest import mock
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from test_review_repair_basis import _Review  # noqa: E402
from test_scheduler import SchedulerFixture, _git  # noqa: E402


def _reject(seq: int) -> _Review:
    return _Review(
        False,
        f"review-c{seq}",
        (
            {
                "check_id": "correct",
                "object_id": "a.py",
                "message": f"repair candidate {seq}",
            },
        ),
    )


class RepairEnvironmentalBudgetTests(SchedulerFixture):
    def _builder_commit(self, attempt, content: str, message: str) -> str:
        (attempt.path / "a.py").write_text(content)
        _git(attempt.path, "add", "a.py")
        _git(attempt.path, "commit", "-qm", message)
        return _git(attempt.path, "rev-parse", "HEAD")

    def _deps(self, verdicts, continue_node):
        remaining = list(verdicts)

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            self.reviewed.append(candidate_sha)
            return remaining.pop(0)

        return self.deps(review_attempt=review, continue_node=continue_node)

    def _drive(self, deps, config=None):
        scheduler = self.schedule([self.agent("a")], config=config, deps=deps)
        scheduler.project()
        scheduler._attempt("a")
        return scheduler

    def _env_spends(self):
        return [
            spend
            for spend in self.store.lane_retry_spends("run1", "a")
            if spend.retry_class is st.LaneRetryClass.ENVIRONMENTAL
        ]

    def setUp(self) -> None:
        super().setUp()
        self.written = {"a": {"a.py": "first candidate\n"}}
        self.reviewed = []

    def test_builder_descendant_commit_spends_zero_environmental_budget(self):
        builder_shas = []

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            sha = self._builder_commit(
                attempt, "repaired candidate\n", "test(seo): repair descendant"
            )
            builder_shas.append(sha)
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        self._drive(
            self._deps([_reject(1), _Review(True, "review-c2")], continue_node)
        )

        self.assertEqual(len(builder_shas), 1)
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[1].candidate_sha, builder_shas[0])
        self.assertEqual(self.reviewed[1], builder_shas[0])
        self.assertEqual(self._env_spends(), [])

    def test_a_head_that_is_not_a_descendant_still_raises_headmoved_and_classifies_environmental(
        self,
    ):
        lineage_parents = []
        original = wt.sealed_descendant_tip

        def wrapped(attempt, parent):
            lineage_parents.append(parent)
            return original(attempt, parent)

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            ancestor = _git(attempt.path, "rev-parse", f"{attempt.base}^")
            _git(attempt.path, "reset", "--hard", ancestor)
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        with mock.patch.object(wt, "sealed_descendant_tip", side_effect=wrapped):
            self._drive(
                self._deps([_reject(1)], continue_node),
                config=self.config(environmental_retries=0),
            )

        self.assertTrue(
            lineage_parents,
            "non-descendant HEAD must go through sealed_descendant_tip, "
            "not only commit_measured_delta",
        )
        node = self.store.get_node("run1", "a")
        self.assertIs(
            node.block_reason, st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED
        )
        env_spends = self._env_spends()
        self.assertEqual(len(env_spends), 1)
        self.assertIn("HeadMoved", str(env_spends[0].detail))

    def test_three_consecutive_repair_cycles_do_not_block_the_lane(self):
        builder_shas = []

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            n = len(builder_shas) + 1
            sha = self._builder_commit(
                attempt, f"repair {n}\n", f"test(seo): repair {n}"
            )
            builder_shas.append(sha)
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        self._drive(
            self._deps(
                [_reject(1), _reject(2), _reject(3), _Review(True, "review-c4")],
                continue_node,
            ),
            config=self.config(review_ceiling=6),
        )

        self.assertEqual(len(builder_shas), 3)
        self.assertIsNot(
            self.store.get_node("run1", "a").block_reason,
            st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
        )
        self.assertEqual(self._env_spends(), [])
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(candidates), 4)
        self.assertEqual(
            [c.candidate_sha for c in candidates[1:]],
            builder_shas,
        )

    def test_an_adopted_tip_that_does_not_carry_the_measured_tree_is_refused(self):
        """The adopted commit must hold what the harness measured.

        A tip is adopted rather than built by commit_measured_delta, so it
        never crosses §8.4's staging assertion, and check_post_commit compares
        the working tree rather than the sealed commit's tree. A builder that
        commits one version of a path and then leaves a different version
        uncommitted would otherwise publish a candidate whose bytes no harness
        measurement ever saw.
        """

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            self._builder_commit(
                attempt, "COMMITTED VERSION\n", "test(seo): repair descendant"
            )
            (attempt.path / "a.py").write_text("WORKTREE VERSION\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        self._drive(
            self._deps([_reject(1)], continue_node),
            config=self.config(environmental_retries=0),
        )

        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(
            [c.candidate_sha for c in candidates[1:]],
            [],
            "a commit whose tree differs from the measured after-state must "
            "not be published as a candidate",
        )
        self.assertEqual(len(self.reviewed), 1)
        node = self.store.get_node("run1", "a")
        self.assertIs(
            node.block_reason, st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED
        )
        env_spends = self._env_spends()
        self.assertEqual(len(env_spends), 1)
        self.assertIn("StagingMismatch", str(env_spends[0].detail))
