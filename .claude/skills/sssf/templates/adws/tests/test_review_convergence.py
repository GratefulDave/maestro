"""#30 — a run reports its own findings-per-attempt convergence profile.

`execution.review_ceiling` was raised 3 -> 6 from three lanes of one run read
off a terminal. Nothing measured convergence and nothing warned that a ceiling
sat below a length the run itself had already exceeded.

Every ledger below is built through the real `LifecycleStore`, so the profile
is derived from rows a scheduler would actually have written — the review
marker `fail_attempt`/`mark_blocked` merge into `attempts.extra_json`, the
`blocking_checks` detail on the transition, and the VERIFIED attempt state
`mark_verified` writes. Nothing here reads pane text, prompt text, or any
free-text field (§1.2).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import lifecycle as lc
from adw_modules import receipt_crypto
from adw_modules import retry_policy as rp
from adw_modules import review_convergence as rc
from adw_modules import scheduler_types as st
from test_lifecycle import _init_git_repo


def _node(node_id: str) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
        instruction="Build " + node_id + ".",
        gate_command=("pytest",),
        gate_selector="tests/test_{}.py".format(node_id))


CONVERGING = _node("converging")
DESCENDING = _node("descending")
UNREVIEWED = _node("unreviewed")

BASE = "a" * 40


class ConvergenceLedgerFixture(unittest.TestCase):
    """A configured repository and a real ledger, as `run status`'s tests do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.state = (self.root / "maestro-state" / "repo").resolve()
        self.stored = b'{"plan":"stored bytes"}\n'
        self.digest = maestro.plan_digest.digest_of(self.stored)
        self.review_ceiling = 3
        self._install_repository()

    # ── the installed repository ────────────────────────────────────────────

    def _install_repository(self, review_ceiling: int = 3):
        self.review_ceiling = review_ceiling
        plan_file = self.repo / "plans" / "named" / "maestro-plan.v1"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_bytes(self.stored)
        (self.repo / "adws").mkdir(exist_ok=True)
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = self.root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        route_dir = self.state / "route-receipts"
        route_dir.mkdir(parents=True, exist_ok=True)
        for route in ("omp", "claude"):
            (route_dir / (route + ".json")).write_text("{}", encoding="utf-8")
        seed = receipt_crypto.generate_seed()
        route_seed = receipt_crypto.generate_seed()
        self.environment = {
            "MAESTRO_TEST_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(route_seed).hex(),
        }
        config = {
            "schema": "maestro-config.v1",
            "plans_dir": "plans",
            "state_root": "../maestro-state",
            "keys": {
                "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
            },
            "executables": binaries,
            "route_receipts": {"omp": "route-receipts/omp.json",
                               "claude": "route-receipts/claude.json"},
            "reviewer": {"route": "claude", "model": "review-model",
                         "effort": "high", "finalization_timeout_s": 60,
                         "turn_timeout_s": 20, "poll_interval_s": 1},
            "execution": {"route": "omp", "model": "execution-model",
                          "effort": "medium", "concurrency": 2,
                          "node_timeout_s": 120, "turn_timeout_s": 30,
                          "final_acceptance_timeout_s": 45,
                          "backstop_t_s": 600, "semantic_ceiling": 3,
                          "review_ceiling": review_ceiling},
        }
        (self.repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8")

    @property
    def database(self) -> Path:
        return self.state / "lifecycle.sqlite3"

    @contextlib.contextmanager
    def _store(self):
        store = lc.LifecycleStore(self.database)
        try:
            yield store
        finally:
            store.close()

    def _run(self, argv):
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, self.environment, clear=False), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(argv)
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    # ── ledger builders, through the production store ───────────────────────

    @staticmethod
    def _reject(store, run_id, node_id, findings, *, blocked=False):
        """One rejected review, written exactly as `_settle_review_rejection`.

        Both halves in one call: the marker the review budget is counted over,
        and the transition detail carrying the check ids. The digest binds
        them, so the count can be recovered from either side.
        """
        lifecycle = store.get_node(run_id, node_id)
        digest = "{}-a{}".format(node_id, lifecycle.attempt_no)
        marker = {rp.REVIEW_REJECTED_KEY: True,
                  rc.REVIEW_SUBJECT_DIGEST_KEY: digest}
        detail = {"reason": "code review rejected the diff",
                  "subject_digest": digest, "replayed": False,
                  "blocking_checks": ["diff.check_{}".format(index)
                                      for index in range(findings)]}
        if blocked:
            store.mark_blocked(
                run_id, node_id, st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                detail=detail, attempt_extra=marker)
        else:
            store.fail_attempt(run_id, node_id, st.RetryClass.SEMANTIC,
                               detail=detail, attempt_extra=marker)

    def _three_lane_run(self, run_id="run-profile"):
        """One lane that converged, one cut off, one that never met a reviewer.

        `converging`  — 3 findings, then 1, then an attempt that passed review.
        `descending`  — 3 findings, then 1, and the run ended there.
        `unreviewed`  — one environmental failure and nothing else.
        """
        with self._store() as store:
            store.create_run(run_id, self.digest,
                             [CONVERGING, DESCENDING, UNREVIEWED])
            for node_id, counts in (("converging", (3, 1)),
                                    ("descending", (3, 1))):
                for findings in counts:
                    store.start_attempt(run_id, node_id, BASE)
                    self._reject(store, run_id, node_id, findings)
            store.start_attempt(run_id, "converging", BASE)
            store.mark_verified(run_id, "converging", "c" * 40)
            store.mark_merged(run_id, "converging")

            store.start_attempt(run_id, "unreviewed", BASE)
            store.fail_attempt(run_id, "unreviewed",
                               st.RetryClass.ENVIRONMENTAL)
            store.declare_outcome(run_id)
        return run_id

    def _profile(self, run_id, review_ceiling=None):
        reader = lc.LifecycleReader.open(self.database)
        try:
            return rc.run_convergence(
                run_id, reader.nodes(run_id), reader.attempts(run_id),
                reader.transitions(run_id), review_ceiling=review_ceiling)
        finally:
            reader.close()


class ConvergenceProfileTest(ConvergenceLedgerFixture):

    def test_the_three_lane_shapes_are_each_reported_as_what_they_are(self):
        profile = self._profile(self._three_lane_run())
        lanes = {lane.node_id: lane for lane in profile.lanes}
        self.assertEqual(set(lanes), {"converging", "descending", "unreviewed"})

        converged = lanes["converging"]
        self.assertEqual(converged.findings_per_attempt, ((1, 3), (2, 1)))
        self.assertIs(converged.outcome, rc.Outcome.CONVERGED)
        self.assertIsNone(converged.cause)
        self.assertEqual(converged.passed_at_attempt, 3)
        self.assertEqual(converged.convergence_length, 3)
        self.assertIs(converged.descending, True)

        cut_off = lanes["descending"]
        self.assertEqual(cut_off.findings_per_attempt, ((1, 3), (2, 1)))
        self.assertIs(cut_off.outcome, rc.Outcome.NOT_CONVERGED)
        self.assertIs(cut_off.cause, rc.Cause.RUN_ENDED)
        self.assertIsNone(cut_off.passed_at_attempt)
        self.assertIsNone(cut_off.convergence_length)
        self.assertIs(cut_off.descending, True)

        never = lanes["unreviewed"]
        self.assertEqual(never.findings_per_attempt, ())
        self.assertIs(never.outcome, rc.Outcome.NO_REVIEW)
        self.assertIsNone(never.convergence_length)
        self.assertIsNone(never.descending)

    def test_a_lane_the_ceiling_cut_off_names_the_ceiling_not_the_run(self):
        run_id = "run-ceiling"
        with self._store() as store:
            store.create_run(run_id, self.digest, [DESCENDING])
            for findings in (4, 3):
                store.start_attempt(run_id, "descending", BASE)
                self._reject(store, run_id, "descending", findings)
            store.start_attempt(run_id, "descending", BASE)
            self._reject(store, run_id, "descending", 2, blocked=True)
            store.declare_outcome(run_id)

        lane = self._profile(run_id).lanes[0]
        self.assertEqual(lane.findings_per_attempt, ((1, 4), (2, 3), (3, 2)))
        self.assertIs(lane.outcome, rc.Outcome.NOT_CONVERGED)
        self.assertIs(lane.cause, rc.Cause.REVIEW_CEILING_REACHED)
        self.assertIs(lane.descending, True)
        self.assertEqual(lane.block_reason, "REVIEW_BUDGET_EXHAUSTED")

    def test_a_flat_series_is_reported_as_not_descending(self):
        run_id = "run-flat"
        with self._store() as store:
            store.create_run(run_id, self.digest, [DESCENDING])
            for findings in (2, 2, 2):
                store.start_attempt(run_id, "descending", BASE)
                self._reject(store, run_id, "descending", findings)
            store.declare_outcome(run_id)

        lane = self._profile(run_id).lanes[0]
        self.assertIs(lane.descending, False)
        self.assertIn("findings flat or rising", rc.render(
            self._profile(run_id)))

    def test_a_lane_that_passed_review_first_time_is_not_a_lane_without_review(self):
        """MERGED with no rejection is convergence at one, not `NO_REVIEW`."""
        run_id = "run-clean"
        with self._store() as store:
            store.create_run(run_id, self.digest, [CONVERGING])
            store.start_attempt(run_id, "converging", BASE)
            store.mark_verified(run_id, "converging", "c" * 40)
            store.mark_merged(run_id, "converging")
            store.declare_outcome(run_id, acceptance_result=True)

        lane = self._profile(run_id).lanes[0]
        self.assertIs(lane.outcome, rc.Outcome.CONVERGED)
        self.assertEqual(lane.findings_per_attempt, ())
        self.assertEqual(lane.convergence_length, 1)


class SkippedLaneTest(ConvergenceLedgerFixture):
    """MERGED is not a review pass — `skip` merges over the reviewer."""

    def _skipped_run(self, run_id="run-skipped"):
        """Rejected to exhaustion, then merged by the operator escape.

        Through `store.skip` against a real git repository, because the
        escape verifies ancestry and the four worktree checks before it will
        write MERGED — a hand-written row would prove the classification
        against a state the production escape cannot produce.
        """
        repo = self.root / "skipped-repo"
        repo.mkdir()
        head = _init_git_repo(repo)
        with self._store() as store:
            store.create_run(run_id, self.digest, [DESCENDING])
            for findings in (3, 3):
                store.start_attempt(run_id, "descending", head)
                self._reject(store, run_id, "descending", findings)
            store.start_attempt(run_id, "descending", head)
            self._reject(store, run_id, "descending", 3, blocked=True)
            store.declare_outcome(run_id)
            store.skip(run_id, "descending", accept_sha=head, repo_path=repo)
        return run_id

    def test_a_lane_merged_by_operator_skip_never_counts_as_converged(self):
        profile = self._profile(self._skipped_run(), review_ceiling=3)
        lane = profile.lanes[0]
        self.assertEqual(lane.state, "MERGED")
        self.assertIsNone(lane.passed_at_attempt)
        self.assertIs(lane.outcome, rc.Outcome.NOT_CONVERGED)
        self.assertIs(lane.cause, rc.Cause.MERGED_WITHOUT_PASSING_REVIEW)
        self.assertIsNone(lane.convergence_length)

    def test_a_skipped_lane_contributes_no_length_and_raises_no_warning(self):
        """The failure mode this guards: a skipped lane's attempt count is the
        length of a loop that never closed, and feeding it to the ceiling
        would raise the ceiling on evidence that says the opposite."""
        profile = self._profile(self._skipped_run(), review_ceiling=3)
        self.assertEqual(profile.converged, ())
        self.assertIsNone(profile.longest)
        self.assertIsNone(profile.ceiling_warning)
        self.assertIn("no lane in this run converged", rc.render(profile))


class CountRecoveryTest(ConvergenceLedgerFixture):
    """Where the number comes from, and what happens when it is not there."""

    def test_the_stored_count_is_preferred_over_the_transition_detail(self):
        """`review_findings_count` wins, so the two sources cannot disagree
        silently: a ledger holding both reports the attempt row's number."""
        run_id = "run-both"
        with self._store() as store:
            store.create_run(run_id, self.digest, [DESCENDING])
            store.start_attempt(run_id, "descending", BASE)
            store.fail_attempt(
                run_id, "descending", st.RetryClass.SEMANTIC,
                detail={"subject_digest": "d1",
                        "blocking_checks": ["a", "b", "c"]},
                attempt_extra={rp.REVIEW_REJECTED_KEY: True,
                               rp.REVIEW_FINDINGS_COUNT_KEY: 9,
                               rc.REVIEW_SUBJECT_DIGEST_KEY: "d1"})
            store.declare_outcome(run_id)

        lane = self._profile(run_id).lanes[0]
        self.assertEqual(lane.findings_per_attempt, ((1, 9),))

    def test_a_row_with_neither_count_reports_unknown_and_never_zero(self):
        """A rejection with no recoverable count is `None`. Zero would read as
        'the reviewer found nothing', which a rejection cannot mean."""
        run_id = "run-unknown"
        with self._store() as store:
            store.create_run(run_id, self.digest, [DESCENDING])
            store.start_attempt(run_id, "descending", BASE)
            store.fail_attempt(
                run_id, "descending", st.RetryClass.SEMANTIC,
                attempt_extra={rp.REVIEW_REJECTED_KEY: True})
            store.declare_outcome(run_id)

        lane = self._profile(run_id).lanes[0]
        self.assertEqual(lane.findings_per_attempt, ((1, None),))
        self.assertEqual(lane.rejections, 1)
        self.assertIsNone(lane.descending)
        self.assertIn("a1:?", rc.render(self._profile(run_id)))

    def test_a_count_is_joined_by_digest_and_never_by_position(self):
        """Two lanes rejecting into one transition log must not swap counts."""
        run_id = "run-interleaved"
        with self._store() as store:
            store.create_run(run_id, self.digest, [CONVERGING, DESCENDING])
            store.start_attempt(run_id, "converging", BASE)
            store.start_attempt(run_id, "descending", BASE)
            self._reject(store, run_id, "descending", 5)
            self._reject(store, run_id, "converging", 1)
            store.declare_outcome(run_id)

        lanes = {lane.node_id: lane for lane in self._profile(run_id).lanes}
        self.assertEqual(lanes["converging"].findings_per_attempt, ((1, 1),))
        self.assertEqual(lanes["descending"].findings_per_attempt, ((1, 5),))


class CeilingWarningTest(ConvergenceLedgerFixture):

    def _long_run(self, run_id="run-long", rejections=3):
        with self._store() as store:
            store.create_run(run_id, self.digest, [CONVERGING])
            for index in range(rejections):
                store.start_attempt(run_id, "converging", BASE)
                self._reject(store, run_id, "converging", rejections - index)
            store.start_attempt(run_id, "converging", BASE)
            store.mark_verified(run_id, "converging", "c" * 40)
            store.mark_merged(run_id, "converging")
            store.declare_outcome(run_id, acceptance_result=True)
        return run_id

    def test_a_ceiling_below_an_observed_convergence_warns_and_names_the_lane(self):
        profile = self._profile(self._long_run(), review_ceiling=3)
        self.assertEqual(profile.longest.convergence_length, 4)
        warning = profile.ceiling_warning
        self.assertIsNotNone(warning)
        self.assertIn("execution.review_ceiling is 3", warning)
        self.assertIn("converging", warning)
        self.assertIn("4 review attempts", warning)
        self.assertIn("WARNING:", rc.render(profile))

    def test_a_ceiling_that_equals_the_observed_length_is_not_warned_about(self):
        """4 is the smallest ceiling that would have let the lane land: it is
        blocked when its rejections reach the ceiling, and it spent three."""
        profile = self._profile(self._long_run(), review_ceiling=4)
        self.assertIsNone(profile.ceiling_warning)
        self.assertNotIn("WARNING:", rc.render(profile))

    def test_an_unconfigured_ceiling_warns_about_nothing(self):
        profile = self._profile(self._long_run(), review_ceiling=None)
        self.assertIsNone(profile.ceiling_warning)
        self.assertIn("not configured", rc.render(profile))


class ConvergenceVerbTest(ConvergenceLedgerFixture):
    """The verb an operator actually types."""

    def test_the_verb_exists_in_the_real_parser(self):
        self.assertIn("run convergence",
                      maestro.parser_verbs(maestro.build_parser()))

    def test_a_named_plan_needs_no_flags_and_prints_every_lane(self):
        self._three_lane_run()
        code, output = self._run(["run", "convergence", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("converging", output)
        self.assertIn("a1:3 a2:1", output)
        self.assertIn("converged at a3", output)
        self.assertIn("no review-reaching attempt", output)

    def test_the_configured_ceiling_reaches_the_warning(self):
        """B15's direction: the ceiling this verb binds has a reader, and the
        reader is what turns a run into evidence about its own configuration."""
        self._install_repository(review_ceiling=2)
        with self._store() as store:
            store.create_run("run-warn", self.digest, [CONVERGING])
            for findings in (3, 1):
                store.start_attempt("run-warn", "converging", BASE)
                self._reject(store, "run-warn", "converging", findings)
            store.start_attempt("run-warn", "converging", BASE)
            store.mark_verified("run-warn", "converging", "c" * 40)
            store.mark_merged("run-warn", "converging")
            store.declare_outcome("run-warn", acceptance_result=True)

        code, output = self._run(["run", "convergence", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("review_ceiling 2", output)
        self.assertIn("WARNING:", output)
        self.assertIn("3 review attempts", output)

    def test_json_carries_every_rendered_number(self):
        self._three_lane_run()
        code, output = self._run(["run", "convergence", "named", "--json"])
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["review_ceiling"], self.review_ceiling)
        lanes = {lane["node_id"]: lane for lane in payload["lanes"]}
        self.assertEqual(
            lanes["converging"]["findings_per_attempt"],
            [{"attempt_no": 1, "findings": 3},
             {"attempt_no": 2, "findings": 1}])
        self.assertEqual(lanes["converging"]["outcome"], "CONVERGED")
        self.assertEqual(lanes["converging"]["convergence_length"], 3)
        self.assertEqual(lanes["descending"]["cause"], "RUN_ENDED")
        self.assertEqual(lanes["unreviewed"]["outcome"], "NO_REVIEW")
        self.assertEqual(payload["longest_convergence"],
                         {"node_id": "converging", "convergence_length": 3})

    def test_an_unknown_run_refuses_rather_than_printing_an_empty_profile(self):
        self._three_lane_run()
        code, output = self._run(
            ["run", "convergence", "named", "--run-id", "run-nope"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

    def test_a_manual_invocation_reads_the_named_database(self):
        run_id = self._three_lane_run()
        code, output = self._run(
            ["run", "convergence", run_id, "--db", str(self.database)])
        self.assertEqual(code, 0, output)
        self.assertIn(run_id, output)

    def test_the_verb_creates_no_ledger_when_asked_about_a_run_that_never_ran(self):
        code, output = self._run(["run", "convergence", "named"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")
        self.assertFalse(self.database.exists())


class NonVacuityTest(ConvergenceLedgerFixture):
    """The checks above must be able to fail."""

    def test_the_fixture_writes_the_markers_the_profile_is_derived_from(self):
        """Without this the whole file could pass over an empty ledger."""
        run_id = self._three_lane_run()
        reader = lc.LifecycleReader.open(self.database)
        try:
            attempts = reader.attempts(run_id)
            transitions = reader.transitions(run_id)
        finally:
            reader.close()
        rejected = [a for a in attempts
                    if (a.extra or {}).get(rp.REVIEW_REJECTED_KEY)]
        self.assertEqual(len(rejected), 4)
        verified = [a for a in attempts if a.state is st.NodeState.VERIFIED]
        self.assertEqual(len(verified), 1)
        checks = [row for row in transitions
                  if (row.get("detail") or {}).get("blocking_checks")]
        self.assertEqual(len(checks), 4)

    def test_removing_the_passing_attempt_turns_convergence_into_a_cut_off(self):
        """A planted change to the one row convergence keys on must flip the
        verdict; if it does not, the verdict is not reading that row."""
        run_id = self._three_lane_run()
        before = {lane.node_id: lane for lane in self._profile(run_id).lanes}
        self.assertIs(before["converging"].outcome, rc.Outcome.CONVERGED)

        reader = lc.LifecycleReader.open(self.database)
        try:
            nodes = reader.nodes(run_id)
            transitions = reader.transitions(run_id)
            attempts = tuple(
                a for a in reader.attempts(run_id)
                if not (a.node_id == "converging"
                        and a.state is st.NodeState.VERIFIED))
        finally:
            reader.close()
        after = {lane.node_id: lane for lane in rc.run_convergence(
            run_id, nodes, attempts, transitions).lanes}
        self.assertIs(after["converging"].outcome, rc.Outcome.NOT_CONVERGED)

    def test_dropping_the_blocking_checks_detail_loses_the_count_not_the_row(self):
        run_id = self._three_lane_run()
        reader = lc.LifecycleReader.open(self.database)
        try:
            nodes = reader.nodes(run_id)
            attempts = reader.attempts(run_id)
        finally:
            reader.close()
        lanes = {lane.node_id: lane for lane in rc.run_convergence(
            run_id, nodes, attempts, ()).lanes}
        self.assertEqual(lanes["converging"].findings_per_attempt,
                         ((1, None), (2, None)))
        self.assertEqual(lanes["converging"].rejections, 2)
        self.assertIs(lanes["converging"].outcome, rc.Outcome.CONVERGED)


if __name__ == "__main__":
    unittest.main()
