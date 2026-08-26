"""Authoritative rejected-candidate findings remain queryable after exit."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import lifecycle as lc
from adw_modules import receipt_crypto
from adw_modules import review_findings as rf
from adw_modules import scheduler_types as st


def _node(node_id: str) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=0,
        instruction="Build " + node_id + ".",
        gate_command=("pytest",),
        gate_selector="tests/test_{}.py".format(node_id),
    )


LANE = _node("lane-one")
BASE = "a" * 40
CANDIDATE_ONE = "b" * 40
CANDIDATE_TWO = "c" * 40
DIGEST_ONE = "review-digest-one"
DIGEST_TWO = "review-digest-two"
REVIEW_NODE = "lane-one::review"
FINDINGS = (
    {
        "check_id": "diff.implements_the_stated_instruction",
        "object_id": "diff:" + CANDIDATE_ONE,
        "message": "the candidate does not implement the instruction",
        "grade": "error",
        "scope": "in_scope",
        "status": "finding",
    },
)


class FindingsLedgerFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.state = (self.root / "maestro-state" / "repo").resolve()
        self.stored = b'{"plan":"stored bytes"}\n'
        self.digest = maestro.plan_digest.digest_of(self.stored)
        self._install_repository()

    def _install_repository(self):
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
            "MAESTRO_TEST_VERIFY_KEY": receipt_crypto.seed_to_public_key(seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY": receipt_crypto.seed_to_public_key(
                route_seed
            ).hex(),
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
            "route_receipts": {
                "omp": "route-receipts/omp.json",
                "claude": "route-receipts/claude.json",
            },
            "reviewer": {
                "route": "claude",
                "model": "review-model",
                "effort": "high",
                "finalization_timeout_s": 60,
                "turn_timeout_s": 20,
                "poll_interval_s": 1,
            },
            "execution": {
                "route": "omp",
                "model": "execution-model",
                "effort": "medium",
                "concurrency": 2,
                "node_timeout_s": 120,
                "turn_timeout_s": 30,
                "final_acceptance_timeout_s": 45,
                "backstop_t_s": 600,
                "semantic_ceiling": 3,
                "review_ceiling": 3,
            },
        }
        (self.repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8"
        )

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
            with (
                mock.patch.dict(os.environ, self.environment, clear=False),
                contextlib.redirect_stdout(output),
            ):
                code = maestro.main(argv)
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    def _scheduler_exited(self, store, run_id):
        with mock.patch("os.getpid", return_value=2_000_000_000):
            store.claim_run(run_id)

    def _create_run(
        self, run_id="run-findings", *, reject=True, accept_descendant=True
    ):
        with self._store() as store:
            store.create_run(run_id, self.digest, [LANE], plan_name="named")
            store.ensure_derived_review_node(run_id, LANE.node_id, depth=1)
            store.start_attempt(run_id, LANE.node_id, BASE)
            store.publish_candidate(
                run_id, LANE.node_id, CANDIDATE_ONE, builder_generation=1
            )
            store.begin_review(
                run_id, REVIEW_NODE, CANDIDATE_ONE, reviewer_generation=1
            )
            store.mark_review_dispatched(
                run_id,
                REVIEW_NODE,
                CANDIDATE_ONE,
                reviewer_generation=1,
            )
            if reject:
                store.reject_and_create_handoff(
                    run_id,
                    REVIEW_NODE,
                    CANDIDATE_ONE,
                    reviewer_generation=1,
                    builder_generation=1,
                    review_digest=DIGEST_ONE,
                    receipt_path="/receipts/candidate-one",
                    findings=FINDINGS,
                )
            else:
                store.complete_review(
                    run_id,
                    REVIEW_NODE,
                    CANDIDATE_ONE,
                    reviewer_generation=1,
                    verdict=st.ReviewVerdict.PASS,
                    review_digest=DIGEST_ONE,
                    receipt_path="/receipts/candidate-one",
                    findings=(),
                )
                store.mark_review_accepted(run_id, REVIEW_NODE, CANDIDATE_ONE)
                store.mark_verified(run_id, LANE.node_id, CANDIDATE_ONE)
                store.mark_merged(run_id, LANE.node_id)
            if reject and accept_descendant:
                store.publish_candidate(
                    run_id,
                    LANE.node_id,
                    CANDIDATE_TWO,
                    parent_candidate_sha=CANDIDATE_ONE,
                    builder_generation=1,
                    ancestry_validator=lambda _parent, _child: True,
                )
                store.begin_review(
                    run_id, REVIEW_NODE, CANDIDATE_TWO, reviewer_generation=1
                )
                store.mark_review_dispatched(
                    run_id,
                    REVIEW_NODE,
                    CANDIDATE_TWO,
                    reviewer_generation=1,
                )
                store.complete_review(
                    run_id,
                    REVIEW_NODE,
                    CANDIDATE_TWO,
                    reviewer_generation=1,
                    verdict=st.ReviewVerdict.PASS,
                    review_digest=DIGEST_TWO,
                    receipt_path="/receipts/candidate-two",
                    findings=(),
                )
                store.mark_review_accepted(run_id, REVIEW_NODE, CANDIDATE_TWO)
                store.mark_verified(run_id, LANE.node_id, CANDIDATE_TWO)
                store.mark_merged(run_id, LANE.node_id)
            if (not reject) or accept_descendant:
                self._scheduler_exited(store, run_id)
                store.declare_outcome(run_id, acceptance_result=True)
        return run_id

    def _profile(self, run_id):
        reader = lc.LifecycleReader.open(self.database)
        try:
            record = reader.run(run_id)
            outcome = (
                record.latest_outcome.value
                if record and record.latest_outcome
                else None
            )
            return rf.run_findings(
                run_id,
                reader.candidate_reviews(run_id, limit=10_000),
                declared_outcome=outcome,
            )
        finally:
            reader.close()


class RunFindingsProfileTest(FindingsLedgerFixture):
    def test_rejected_candidate_is_named_by_exact_review_and_sha(self):
        run_id = self._create_run(accept_descendant=False)

        profile = self._profile(run_id)

        self.assertIsNone(profile.declared_outcome)
        self.assertEqual(len(profile.reviews), 1)
        review = profile.reviews[0]
        self.assertEqual(review.review_node_id, REVIEW_NODE)
        self.assertEqual(review.candidate_sha, CANDIDATE_ONE)
        self.assertEqual(review.review_digest, DIGEST_ONE)
        self.assertEqual(review.findings[0].check_id, FINDINGS[0]["check_id"])

    def test_earlier_rejection_remains_visible_after_descendant_passes(self):
        run_id = self._create_run()

        profile = self._profile(run_id)

        self.assertEqual(profile.declared_outcome, "ACCEPTED")
        self.assertEqual(
            [review.candidate_sha for review in profile.reviews], [CANDIDATE_ONE]
        )

    def test_passed_candidate_is_not_reported_as_rejected(self):
        run_id = self._create_run(reject=False)
        self.assertEqual(self._profile(run_id).reviews, ())

    def test_attempt_guidance_is_audit_only(self):
        run_id = self._create_run(reject=False)
        with self._store() as store:
            store.conn.execute(
                "UPDATE attempts SET extra_json=? WHERE run_id=? AND node_id=?",
                (
                    json.dumps(
                        {"guidance": {"surface": "review", "findings": list(FINDINGS)}}
                    ),
                    run_id,
                    LANE.node_id,
                ),
            )
        self.assertEqual(self._profile(run_id).reviews, ())
        self.assertEqual(
            rf.legacy_findings_from_extra(
                {"guidance": {"surface": "review", "findings": list(FINDINGS)}}
            )[0].check_id,
            FINDINGS[0]["check_id"],
        )


class FindingsVerbTest(FindingsLedgerFixture):
    def test_the_verb_exists_in_the_real_parser(self):
        self.assertIn("run findings", maestro.parser_verbs(maestro.build_parser()))

    def test_text_and_json_name_the_rejected_candidate(self):
        run_id = self._create_run()
        code, output = self._run(["run", "findings", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("ACCEPTED", output)
        self.assertIn(REVIEW_NODE, output)
        self.assertIn(CANDIDATE_ONE, output)
        self.assertIn(FINDINGS[0]["message"], output)
        self.assertNotIn("merged node", output)

        code, output = self._run(["run", "findings", "named", "--json"])
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["declared_outcome"], "ACCEPTED")
        self.assertEqual(len(payload["reviews"]), 1)
        self.assertEqual(payload["reviews"][0]["candidate_sha"], CANDIDATE_ONE)

    def test_status_reads_candidate_reviews_not_attempt_markers(self):
        self._create_run()
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["declared_outcome"], "ACCEPTED")
        self.assertEqual(progress["review_findings"][0]["review_node_id"], REVIEW_NODE)
        self.assertEqual(progress["review_findings"][0]["candidate_sha"], CANDIDATE_ONE)
        self.assertEqual(
            progress["nodes"][0]["attempts"][0]["legacy_review_findings"], []
        )

    def test_text_status_names_rejected_candidate_review(self):
        self._create_run()
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("rejected candidate reviews", output)
        self.assertIn(REVIEW_NODE, output)
        self.assertIn(CANDIDATE_ONE[:12], output)
        self.assertNotIn("merged nodes", output)

    def test_unknown_run_refuses_and_missing_ledger_is_not_created(self):
        self._create_run()
        code, output = self._run(["run", "findings", "named", "--run-id", "run-nope"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

        self.database.unlink()
        code, output = self._run(["run", "findings", "named"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
