"""Persistent same-attempt candidate review/repair loop regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from test_scheduler import SchedulerFixture, _git, green, red  # noqa: E402


class _Review:
    def __init__(self, passed: bool, digest: str, findings=()):
        self.passed = passed
        self.subject_digest = digest
        self.findings = tuple(findings)
        self.advisories = ()
        self.unreachable = ()

    def findings_text(self) -> str:
        return "\n".join(finding["message"] for finding in self.findings)


class PersistentCandidateLoopTests(SchedulerFixture):
    def _deps(
        self,
        verdicts,
        *,
        transient_handoff_failures=0,
        replace_builder_on_handoff=False,
    ):
        self.reviewed = []
        self.continued = []
        self.closed = []
        remaining_handoff_failures = transient_handoff_failures

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            self.reviewed.append((attempt.path, record.attempt_no, candidate_sha))
            return verdicts.pop(0)

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            self.continued.append(
                (
                    attempt.path,
                    record.attempt_no,
                    prompt,
                    rejected_sha,
                    builder_generation,
                )
            )
            nonlocal remaining_handoff_failures
            if remaining_handoff_failures:
                remaining_handoff_failures -= 1
                raise sch.LaunchFailed(
                    rp.LauncherFailure.STARTUP,
                    "repair prompt submission proof raced the live actor",
                )
            if replace_builder_on_handoff:
                if (
                    self.store.current_actor_session("run1", node.node_id, "builder")
                    is None
                ):
                    self.store.register_actor_session(
                        "run1",
                        node.node_id,
                        "builder",
                        generation=builder_generation,
                        pane_id="original-pane",
                        session_path="/sessions/original-builder",
                        correlation_token="original-token",
                    )
                generation = builder_generation + 1
                recovered = self.store.recover_builder_handoff(
                    "run1",
                    node.node_id,
                    rejected_sha,
                    expected_generation=builder_generation,
                    generation=generation,
                    pane_id="replacement-pane",
                    session_path="/sessions/replacement-builder",
                    correlation_token="replacement-token",
                )
                self.assertTrue(recovered.recovered)
                builder_generation = generation
            (attempt.path / "a.py").write_text("second candidate\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        return self.deps(
            review_attempt=review,
            continue_node=continue_node,
            close_review=self.closed.append,
            receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
        )

    def test_rejection_repairs_same_attempt_worktree_and_session(self):
        self.written = {"a": {"a.py": "first candidate\n"}}
        deps = self._deps(
            [
                _Review(
                    False,
                    "review-c1",
                    (
                        {
                            "check_id": "correct",
                            "object_id": "a.py",
                            "message": "repair the first candidate",
                        },
                    ),
                ),
                _Review(True, "review-c2"),
            ]
        )
        scheduler = self.schedule([self.agent("a")], deps=deps)

        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(self.prompts["a"], [None])
        self.assertEqual(len(self.reviewed), 2)
        self.assertEqual(len(self.continued), 1)
        self.assertEqual(self.reviewed[0][0], self.continued[0][0])
        self.assertEqual(self.reviewed[0][1], self.continued[0][1])
        self.assertIn("repair the first candidate", self.continued[0][2])
        self.assertEqual(self.closed, ["a"])
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            candidates[1].parent_candidate_sha, candidates[0].candidate_sha
        )
        self.assertEqual(
            _git(
                self.repo,
                "merge-base",
                candidates[0].candidate_sha,
                candidates[1].candidate_sha,
            ),
            candidates[0].candidate_sha,
        )
        reviews = self.store.candidate_reviews("run1", "a::review")
        self.assertEqual(
            [review.verdict for review in reviews],
            [st.ReviewVerdict.REJECTED, st.ReviewVerdict.PASS],
        )
        handoff = self.store.repair_handoff("run1", "a", candidates[0].candidate_sha)
        self.assertIs(handoff.state, st.RepairHandoffState.ACKNOWLEDGED)
        self.assertEqual(self.store.get_node("run1", "a").state, st.NodeState.MERGED)

    def test_transient_handoff_retries_without_creating_a_new_attempt(self):
        self.written = {"a": {"a.py": "first candidate\n"}}
        deps = self._deps(
            [
                _Review(
                    False,
                    "review-c1",
                    (
                        {
                            "check_id": "correct",
                            "object_id": "a.py",
                            "message": "repair the first candidate",
                        },
                    ),
                ),
                _Review(True, "review-c2"),
            ],
            transient_handoff_failures=1,
        )

        report = self.schedule([self.agent("a")], deps=deps).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(len(self.store.attempts_for("run1", "a")), 1)
        self.assertEqual(len(self.continued), 2)
        spends = self.store.lane_retry_spends("run1", "a")
        self.assertEqual(
            [spend.retry_class for spend in spends],
            [st.LaneRetryClass.REVIEW_REJECTION, st.LaneRetryClass.LAUNCHER_TRANSIENT],
        )

    def test_proven_absent_builder_continues_with_replacement_generation(self):
        self.written = {"a": {"a.py": "first candidate\n"}}
        deps = self._deps(
            [
                _Review(
                    False,
                    "review-c1",
                    (
                        {
                            "check_id": "correct",
                            "object_id": "a.py",
                            "message": "repair the first candidate",
                        },
                    ),
                ),
                _Review(True, "review-c2"),
            ],
            replace_builder_on_handoff=True,
        )

        report = self.schedule([self.agent("a")], deps=deps).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(len(self.store.attempts_for("run1", "a")), 1)
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(
            [candidate.builder_generation for candidate in candidates], [1, 2]
        )
        sessions = self.store.actor_sessions("run1", "a", actor_role="builder")
        self.assertEqual(
            [(session.generation, session.state) for session in sessions],
            [(1, st.ActorSessionState.CLOSED), (2, st.ActorSessionState.ACTIVE)],
        )

    def test_environmental_retry_resumes_from_rejected_candidate(self):
        reviews = [
            _Review(
                False,
                "review-c1",
                (
                    {
                        "check_id": "direct-metadata",
                        "object_id": "a.py",
                        "message": "preserve the completed acquisition",
                        "grade_rationale": "the candidate is complete except for metadata",
                    },
                ),
            ),
            _Review(True, "review-c2"),
        ]
        reviewed = []
        continued = []
        launched = []

        def run_node(attempt, node, record, prompt, on_launch, _cancel_requested):
            on_launch(None)
            launched.append((record.attempt_no, attempt.base, prompt))
            (attempt.path / "a.py").write_text("first candidate\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            reviewed.append(candidate_sha)
            return reviews.pop(0)

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            continued.append(
                (record.attempt_no, rejected_sha, prompt, builder_generation)
            )
            if len(continued) == 1:
                self.store.register_actor_session(
                    "run1",
                    node.node_id,
                    "builder",
                    generation=builder_generation,
                    pane_id="timed-out-pane",
                    session_path="/sessions/timed-out-builder",
                    correlation_token="timed-out-token",
                )
                self.store.close_actor_session(
                    "run1", node.node_id, "builder", generation=builder_generation
                )
                raise TimeoutError("persistent builder generation timed out")
            recovered = self.store.recover_builder_handoff(
                "run1",
                node.node_id,
                rejected_sha,
                expected_generation=builder_generation,
                generation=builder_generation + 1,
                pane_id="replacement-pane",
                session_path="/sessions/replacement-builder",
                correlation_token="replacement-token",
            )
            self.assertTrue(recovered.recovered)
            self.assertEqual((attempt.path / "a.py").read_text(), "first candidate\n")
            (attempt.path / "a.py").write_text("second candidate\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation + 1,
            )

        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(
                run_node=run_node, review_attempt=review, continue_node=continue_node
            ),
        ).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        attempts = self.store.attempts_for("run1", "a")
        self.assertEqual(len(attempts), 1)
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(candidates), 2)
        self.assertIsNone(candidates[0].parent_candidate_sha)
        self.assertEqual(
            candidates[1].parent_candidate_sha, candidates[0].candidate_sha
        )
        self.assertEqual(len(reviewed), 2)
        self.assertEqual([item[0] for item in continued], [1, 1])
        self.assertEqual(
            [item[1] for item in continued], [candidates[0].candidate_sha] * 2
        )
        self.assertEqual(len(launched), 1)
        self.assertIn(candidates[0].candidate_sha, continued[1][2])
        self.assertIn("preserve the completed acquisition", continued[1][2])
        self.assertIn("the candidate is complete except for metadata", continued[1][2])
        handoff = self.store.repair_handoff("run1", "a", candidates[0].candidate_sha)
        self.assertIs(handoff.state, st.RepairHandoffState.ACKNOWLEDGED)
        self.assertEqual(handoff.builder_generation, 2)
        rejection_spends = [
            spend
            for spend in self.store.lane_retry_spends("run1", "a")
            if spend.retry_class is st.LaneRetryClass.REVIEW_REJECTION
        ]
        self.assertEqual(len(rejection_spends), 1)

    def test_scheduler_restart_reopens_rejected_candidate_attempt(self):
        reviews = [
            _Review(
                False,
                "review-c1",
                (
                    {
                        "check_id": "direct-metadata",
                        "object_id": "a.py",
                        "message": "preserve the completed acquisition",
                        "grade_rationale": "the candidate is complete except for metadata",
                    },
                ),
            ),
            _Review(True, "review-c2"),
        ]
        continued = []
        crashed_during_handoff = False

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            return reviews.pop(0)

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            nonlocal crashed_during_handoff
            if not crashed_during_handoff:
                crashed_during_handoff = True
                raise SystemExit("scheduler process stopped during handoff")
            continued.append((record.attempt_no, rejected_sha, prompt))
            self.assertEqual(
                (attempt.path / "a.py").read_text(), "unpublished repair\n"
            )
            (attempt.path / "a.py").write_text("second candidate\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        def run_node(attempt, node, record, prompt, on_launch, cancel_requested):
            execution = self.run_node(
                attempt, node, record, prompt, on_launch, cancel_requested
            )
            execution.envelope_payload = {"success": True}
            return execution

        self.written = {"a": {"a.py": "first candidate\n"}}
        deps = self.deps(
            run_node=run_node, review_attempt=review, continue_node=continue_node
        )
        first = self.schedule([self.agent("a")], deps=deps)
        first.project()
        first._attempt("a")

        stranded = self.store.get_node("run1", "a")
        self.assertIs(stranded.state, st.NodeState.PENDING)
        self.assertEqual(stranded.attempt_no, 1)
        candidate = self.store.lane_candidates("run1", "a")[0]
        self.assertIs(
            self.store.candidate_review(
                "run1", "a::review", candidate.candidate_sha
            ).verdict,
            st.ReviewVerdict.REJECTED,
        )
        handoff = self.store.repair_handoff("run1", "a", candidate.candidate_sha)
        self.assertIsNotNone(handoff)
        self.assertIs(handoff.state, st.RepairHandoffState.PENDING)
        self.assertEqual(
            self.store.get_attempt("run1", "a", 1).extra[
                lc.REPAIR_HANDOFF_RECOVERY_KEY
            ],
            candidate.candidate_sha,
        )

        attempt_record = self.store.get_attempt("run1", "a", 1)
        attempt = wt.reopen_attempt_worktree(
            self.repo,
            "run1",
            "a",
            1,
            attempt_record.base_sha,
            self.root / "wt",
            self.root / "scratch",
        )
        wt.prepare_descendant_candidate(attempt, candidate.candidate_sha)
        repair_baseline = wt.take_baseline(attempt)
        (attempt.path / "a.py").write_text("unpublished repair\n")
        after = wt.inventory(attempt.path)
        first_repair_commit = wt.commit_measured_delta(
            attempt,
            wt.delta(repair_baseline, after),
            after,
            "unpublished repair",
        )
        self.assertNotEqual(first_repair_commit, candidate.candidate_sha)
        (attempt.path / "a.py").write_text("final unpublished repair\n")
        _git(attempt.path, "add", "a.py")
        _git(attempt.path, "commit", "-m", "builder follow-up")
        final_repair_commit = _git(attempt.path, "rev-parse", "HEAD")
        self.assertNotEqual(final_repair_commit, first_repair_commit)
        self.assertEqual(
            wt.attempt_ref_commit(self.repo, "run1", "a", 1),
            final_repair_commit,
        )

        report = self.schedule([self.agent("a")], deps=deps).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(
            [attempt.attempt_no for attempt in self.store.attempts_for("run1", "a")],
            [1],
        )
        self.assertEqual(continued, [])
        candidates = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[1].candidate_sha, final_repair_commit)
        self.assertEqual(
            self.store.repair_handoff("run1", "a", candidate.candidate_sha).state,
            st.RepairHandoffState.ACKNOWLEDGED,
        )

    def test_scheduler_restart_resumes_unsealed_repair_handoff(self):
        reviews = [
            _Review(
                False,
                "review-c1",
                (
                    {
                        "check_id": "direct-metadata",
                        "object_id": "a.py",
                        "message": "finish the rejected candidate",
                    },
                ),
            ),
            _Review(True, "review-c2"),
        ]
        continued = []
        crashed_during_handoff = False

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            return reviews.pop(0)

        def continue_node(
            attempt,
            node,
            record,
            prompt,
            rejected_sha,
            builder_generation,
            cancel_requested,
        ):
            nonlocal crashed_during_handoff
            if not crashed_during_handoff:
                crashed_during_handoff = True
                raise SystemExit("scheduler stopped before the repair commit")
            continued.append((record.attempt_no, rejected_sha))
            (attempt.path / "a.py").write_text("second candidate\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_sha,
                builder_generation=builder_generation,
            )

        def run_node(attempt, node, record, prompt, on_launch, cancel_requested):
            execution = self.run_node(
                attempt, node, record, prompt, on_launch, cancel_requested
            )
            execution.envelope_payload = {"success": True}
            return execution

        self.written = {"a": {"a.py": "first candidate\n"}}
        deps = self.deps(
            run_node=run_node, review_attempt=review, continue_node=continue_node
        )
        first = self.schedule([self.agent("a")], deps=deps)
        first.project()
        first._attempt("a")

        candidate = self.store.lane_candidates("run1", "a")[0]
        self.assertEqual(
            wt.attempt_ref_commit(self.repo, "run1", "a", 1),
            candidate.candidate_sha,
        )
        self.assertEqual(
            self.store.get_attempt("run1", "a", 1).extra[
                lc.REPAIR_HANDOFF_RECOVERY_KEY
            ],
            candidate.candidate_sha,
        )

        report = self.schedule([self.agent("a")], deps=deps).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(continued, [(1, candidate.candidate_sha)])
        self.assertEqual(len(self.store.lane_candidates("run1", "a")), 2)
        self.assertIs(
            self.store.repair_handoff("run1", "a", candidate.candidate_sha).state,
            st.RepairHandoffState.ACKNOWLEDGED,
        )

    def test_reviewer_replacement_replays_instead_of_stamping_old_output(self):
        self.written = {"a": {"a.py": "candidate\n"}}
        calls = []

        def review(attempt, node, record, base_sha, candidate_sha, resumed):
            calls.append((candidate_sha, resumed))
            if len(calls) == 1:
                self.store.register_actor_session(
                    "run1",
                    node.node_id,
                    "reviewer",
                    generation=record.attempt_no,
                    pane_id="old-reviewer-pane",
                    session_path="/sessions/old-reviewer",
                    correlation_token="old-reviewer",
                )
                self.assertTrue(
                    self.store.close_actor_session(
                        "run1",
                        node.node_id,
                        "reviewer",
                        generation=record.attempt_no,
                    )
                )
                self.store.register_actor_session(
                    "run1",
                    node.node_id,
                    "reviewer",
                    generation=record.attempt_no + 1,
                    pane_id="replacement-reviewer-pane",
                    session_path="/sessions/replacement-reviewer",
                    correlation_token="replacement-reviewer",
                )
                return _Review(
                    False,
                    "stale-review-output",
                    ({"message": "must not create a handoff"},),
                )
            return _Review(True, "replacement-review-output")

        report = self.schedule(
            [self.agent("a")], deps=self.deps(review_attempt=review)
        ).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual([resumed for _, resumed in calls], [False, True])
        candidate_sha = calls[0][0]
        durable_review = self.store.candidate_review("run1", "a::review", candidate_sha)
        self.assertIs(durable_review.verdict, st.ReviewVerdict.PASS)
        self.assertEqual(durable_review.reviewer_generation, 2)
        self.assertEqual(durable_review.review_digest, "replacement-review-output")
        self.assertIsNone(self.store.repair_handoff("run1", "a", candidate_sha))

    def test_gate_failure_keeps_the_builder_attempt_and_worktree(self):
        self.written = {"a": {"a.py": "first candidate\n"}}
        self.gate_script[("a", "post")] = [red(), green()]
        deps = self._deps([_Review(True, "review-c2")])
        scheduler = self.schedule([self.agent("a")], deps=deps)

        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(self.prompts["a"], [None])
        self.assertEqual(len(self.continued), 1)
        self.assertEqual(len(self.reviewed), 1)
        self.assertEqual(self.continued[0][0], self.reviewed[0][0])
        self.assertEqual(self.continued[0][1], self.reviewed[0][1])
        self.assertEqual(len(self.store.attempts_for("run1", "a")), 1)
        self.assertEqual(len(self.store.lane_candidates("run1", "a")), 1)
