"""G2 — a merged node with a rejecting finding is discoverable after exit.

The findings already sit on the attempt row. This suite is the proof that a
reader exists after the process that wrote them is gone: a real ledger, the
production store's ``record_review_advisory``, then ``run findings`` and
``run status`` against that file. Nothing here fails an attempt or blocks a
merge — ``test_code_review.ReviewStageTests.test_a_rejected_diff_still_merges``
owns that half.
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
from adw_modules import review_findings as rf
from adw_modules import scheduler_types as st


def _node(node_id: str) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
        instruction="Build " + node_id + ".",
        gate_command=("pytest",),
        gate_selector="tests/test_{}.py".format(node_id))


LANE = _node("lane-one")
OTHER = _node("lane-two")
BASE = "a" * 40
OUTPUT = "c" * 40
DIGEST = "subject-digest-lane-one"


class FindingsLedgerFixture(unittest.TestCase):
    """A configured repository and a real ledger, as ``run status``'s tests do."""

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
                          "review_ceiling": 3},
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

    DEAD_PID = 2_000_000_000

    def _scheduler_exited(self, store, run_id):
        with mock.patch("os.getpid", return_value=self.DEAD_PID):
            store.claim_run(run_id)

    def _end_accepted(self, store, run_id):
        self._scheduler_exited(store, run_id)
        return store.declare_outcome(run_id, acceptance_result=True)

    @staticmethod
    def _advisory_extra(findings, *, digest=DIGEST):
        """The extra ``_record_review_advisory`` writes, then merges anyway."""
        marker = {
            rp.REVIEW_REJECTED_KEY: True,
            rp.REVIEW_FINDINGS_COUNT_KEY: sum(
                1 for item in findings if item.get("blocking")),
            "review_subject_digest": digest,
            "review_advisory": True,
        }
        marker.update({rp.GUIDANCE_KEY: {
            "surface": "review",
            "subject_digest": digest,
            "findings": list(findings),
        }})
        return marker

    def _merge_with_findings(self, store, run_id, node_id, findings, *,
                             digest=DIGEST):
        store.start_attempt(run_id, node_id, BASE)
        lifecycle = store.get_node(run_id, node_id)
        store.record_review_advisory(
            run_id, node_id, lifecycle.attempt_no,
            self._advisory_extra(findings, digest=digest))
        store.mark_verified(run_id, node_id, OUTPUT)
        store.mark_merged(run_id, node_id)

    def _accepted_with_rejecting_findings(self, run_id="run-r7"):
        """The r7 shape: ACCEPTED, every merged node still carrying BLOCKING findings."""
        findings = [
            {"check_id": "diff.implements_the_stated_instruction",
             "object_id": "diff:" + OUTPUT,
             "message": "the instruction is not what merged",
             "blocking": True},
            {"check_id": "diff.is_coherent_with_its_surroundings",
             "object_id": "src/mod.py:12",
             "message": "style only",
             "blocking": False},
        ]
        with self._store() as store:
            store.create_run(run_id, self.digest, [LANE, OTHER])
            self._merge_with_findings(store, run_id, "lane-one", findings)
            store.start_attempt(run_id, "lane-two", BASE)
            store.mark_verified(run_id, "lane-two", OUTPUT)
            store.mark_merged(run_id, "lane-two")
            self._end_accepted(store, run_id)
        return run_id

    def _profile(self, run_id):
        reader = lc.LifecycleReader.open(self.database)
        try:
            record = reader.run(run_id)
            outcome = None
            if record is not None and record.latest_outcome is not None:
                outcome = record.latest_outcome.value
            return rf.run_findings(
                run_id, reader.nodes(run_id), reader.attempts(run_id),
                declared_outcome=outcome)
        finally:
            reader.close()


class RunFindingsProfileTest(FindingsLedgerFixture):

    def test_a_merged_node_with_a_rejecting_finding_is_named(self):
        run_id = self._accepted_with_rejecting_findings()
        profile = self._profile(run_id)
        self.assertEqual(profile.declared_outcome, "ACCEPTED")
        self.assertEqual([node.node_id for node in profile.nodes], ["lane-one"])
        node = profile.nodes[0]
        self.assertEqual(node.attempt_no, 1)
        self.assertEqual(node.subject_digest, DIGEST)
        self.assertEqual(len(node.findings), 1)
        self.assertEqual(node.findings[0].check_id,
                         "diff.implements_the_stated_instruction")
        self.assertTrue(node.findings[0].blocking)
        self.assertIn("not what merged", node.findings[0].message)

    def test_a_clean_merge_is_absent(self):
        run_id = self._accepted_with_rejecting_findings()
        profile = self._profile(run_id)
        self.assertNotIn("lane-two", [node.node_id for node in profile.nodes])

    def test_an_advisory_finding_is_not_a_rejecting_finding(self):
        with self._store() as store:
            store.create_run("run-adv", self.digest, [LANE])
            self._merge_with_findings(store, "run-adv", "lane-one", [{
                "check_id": "diff.is_coherent_with_its_surroundings",
                "object_id": "src/mod.py:1",
                "message": "nit",
                "blocking": False,
            }])
            self._end_accepted(store, "run-adv")
        profile = self._profile("run-adv")
        self.assertEqual(profile.nodes, ())

    def test_a_repaired_earlier_rejection_does_not_travel_with_the_merge(self):
        """The merged attempt is clean; the previous attempt's findings died with it."""
        with self._store() as store:
            store.create_run("run-repair", self.digest, [LANE])
            store.start_attempt("run-repair", "lane-one", BASE)
            first = store.get_node("run-repair", "lane-one")
            store.record_review_advisory(
                "run-repair", "lane-one", first.attempt_no,
                self._advisory_extra([{
                    "check_id": "diff.implements_the_stated_instruction",
                    "object_id": "diff:old",
                    "message": "stale",
                    "blocking": True,
                }]))
            store.fail_attempt("run-repair", "lane-one", st.RetryClass.SEMANTIC)
            store.start_attempt("run-repair", "lane-one", BASE)
            store.mark_verified("run-repair", "lane-one", OUTPUT)
            store.mark_merged("run-repair", "lane-one")
            self._end_accepted(store, "run-repair")
        profile = self._profile("run-repair")
        self.assertEqual(profile.nodes, ())

    def test_an_unmerged_node_with_findings_is_not_the_surface(self):
        with self._store() as store:
            store.create_run("run-open", self.digest, [LANE])
            store.start_attempt("run-open", "lane-one", BASE)
            row = store.get_node("run-open", "lane-one")
            store.record_review_advisory(
                "run-open", "lane-one", row.attempt_no,
                self._advisory_extra([{
                    "check_id": "diff.implements_the_stated_instruction",
                    "object_id": "diff:x",
                    "message": "still running",
                    "blocking": True,
                }]))
        profile = self._profile("run-open")
        self.assertEqual(profile.nodes, ())


class FindingsVerbTest(FindingsLedgerFixture):
    """The verb an operator types the morning after the process exited."""

    def test_the_verb_exists_in_the_real_parser(self):
        self.assertIn("run findings",
                      maestro.parser_verbs(maestro.build_parser()))

    def test_a_named_plan_needs_no_flags_and_prints_the_rejecting_finding(self):
        self._accepted_with_rejecting_findings()
        code, output = self._run(["run", "findings", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("ACCEPTED", output)
        self.assertIn("lane-one", output)
        self.assertIn("diff.implements_the_stated_instruction", output)
        self.assertIn("the instruction is not what merged", output)
        self.assertNotIn("lane-two", output)
        self.assertNotIn("style only", output)

    def test_json_names_every_merged_node_that_carried_a_rejecting_finding(self):
        run_id = self._accepted_with_rejecting_findings()
        code, output = self._run(["run", "findings", "named", "--json"])
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["declared_outcome"], "ACCEPTED")
        self.assertEqual(len(payload["nodes"]), 1)
        node = payload["nodes"][0]
        self.assertEqual(node["node_id"], "lane-one")
        self.assertEqual(node["attempt_no"], 1)
        self.assertEqual(node["subject_digest"], DIGEST)
        self.assertEqual(node["findings"][0]["blocking"], True)
        self.assertEqual(node["findings"][0]["check_id"],
                         "diff.implements_the_stated_instruction")

    def test_status_reports_accepted_alongside_the_rejecting_findings(self):
        """A run cannot be reported as ACCEPTED with the findings hidden."""
        self._accepted_with_rejecting_findings()
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("ACCEPTED", output)
        self.assertIn("rejecting findings", output)
        self.assertIn("lane-one", output)
        self.assertIn("diff.implements_the_stated_instruction", output)

        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["declared_outcome"], "ACCEPTED")
        self.assertEqual(progress["review_findings"][0]["node_id"], "lane-one")
        attempt = progress["nodes"][0]["attempts"][0]
        self.assertEqual(attempt["review_findings"][0]["check_id"],
                         "diff.implements_the_stated_instruction")

    def test_an_unknown_run_refuses_rather_than_printing_an_empty_profile(self):
        self._accepted_with_rejecting_findings()
        code, output = self._run(
            ["run", "findings", "named", "--run-id", "run-nope"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

    def test_the_verb_creates_no_ledger_when_asked_about_a_run_that_never_ran(self):
        code, output = self._run(["run", "findings", "named"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")
        self.assertFalse(self.database.exists())


class NonVacuityTest(FindingsLedgerFixture):

    def test_a_clean_accepted_run_is_not_reported_as_carrying_findings(self):
        with self._store() as store:
            store.create_run("run-clean", self.digest, [LANE])
            store.start_attempt("run-clean", "lane-one", BASE)
            store.mark_verified("run-clean", "lane-one", OUTPUT)
            store.mark_merged("run-clean", "lane-one")
            self._end_accepted(store, "run-clean")
        profile = self._profile("run-clean")
        self.assertEqual(profile.declared_outcome, "ACCEPTED")
        self.assertEqual(profile.nodes, ())
        rendered = rf.render(profile)
        self.assertIn("ACCEPTED", rendered)
        self.assertIn("no merged node carried a rejecting finding", rendered)
        self.assertNotIn("blocking", rendered)

    def test_dropping_the_blocking_filter_would_fail_this(self):
        """The advisory in the r7 fixture is real; counting it is the bug."""
        run_id = self._accepted_with_rejecting_findings()
        profile = self._profile(run_id)
        self.assertEqual(len(profile.nodes[0].findings), 1)
        extra = None
        reader = lc.LifecycleReader.open(self.database)
        try:
            for attempt in reader.attempts(run_id):
                if attempt.node_id == "lane-one":
                    extra = attempt.extra
                    break
        finally:
            reader.close()
        assert extra is not None
        stored = extra[rp.GUIDANCE_KEY]["findings"]
        self.assertEqual(len(stored), 2)
        self.assertEqual(sum(1 for item in stored if item["blocking"]), 1)


if __name__ == "__main__":
    unittest.main()
