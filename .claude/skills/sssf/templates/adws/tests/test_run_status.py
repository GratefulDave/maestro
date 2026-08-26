"""`run status`, `run list`, `run cancel`, `run resume` resolve a real run (§11.1).

Every case here is built from the real schema through the real store, so the
projection is exercised against rows a scheduler would actually have written —
including the one shape that broke the verb in the field: a named plan with a
configured repository and no flags at all.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import lifecycle as lc
from adw_modules import receipt_crypto
from adw_modules import scheduler_types as st


AGENT_NODE = st.PlanNode(
    node_id="lane-one",
    kind=st.NodeKind.AGENT,
    depth=0,
    instruction="Build lane one.",
    gate_command=("pytest",),
    gate_selector="tests/test_one.py",
)
SECOND_NODE = st.PlanNode(
    node_id="lane-two",
    kind=st.NodeKind.AGENT,
    depth=0,
    instruction="Build lane two.",
    gate_command=("pytest",),
    gate_selector="tests/test_two.py",
)


class RunStatusFixture(unittest.TestCase):
    """A configured repository, an installed plan, and a real ledger."""

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
        plan_file.parent.mkdir(parents=True)
        plan_file.write_bytes(self.stored)
        other = self.repo / "plans" / "other" / "maestro-plan.v1"
        other.parent.mkdir(parents=True)
        other.write_bytes(b'{"plan":"other bytes"}\n')
        self.other_digest = maestro.plan_digest.digest_of(other.read_bytes())
        (self.repo / "adws").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = self.root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        route_dir = self.state / "route-receipts"
        route_dir.mkdir(parents=True)
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
        """Invoke the CLI exactly as an operator does — from the repository."""
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

    # ── ledger builders ──────────────────────────────────────────────────────

    def _live_run(self, run_id="run-live", digest=None):
        """Two attempts retried away and a third still in flight."""
        with self._store() as store:
            store.create_run(run_id, digest or self.digest, [AGENT_NODE])
            store.start_attempt(run_id, "lane-one", "a" * 40)
            store.fail_attempt(run_id, "lane-one", st.RetryClass.ENVIRONMENTAL)
            store.start_attempt(run_id, "lane-one", "a" * 40)
            store.fail_attempt(
                run_id,
                "lane-one",
                st.RetryClass.SEMANTIC,
                detail={"clause": 4, "verdict": "the gate stayed red"},
            )
            attempt_no = store.start_attempt(run_id, "lane-one", "a" * 40)
            store.mark_launched(
                run_id,
                "lane-one",
                attempt_no,
                4321,
                extra={"session_path": "/tmp/session.jsonl"},
            )
        return run_id

    def _accepted_run(self, run_id="run-done", digest=None):
        with self._store() as store:
            store.create_run(run_id, digest or self.digest, [AGENT_NODE])
            store.start_attempt(run_id, "lane-one", "b" * 40)
            store.mark_verified(run_id, "lane-one", "c" * 40)
            store.mark_merged(run_id, "lane-one")
            store.declare_outcome(run_id, acceptance_result=True)
        return run_id

    def _blocked_run(self, run_id="run-bad", digest=None):
        with self._store() as store:
            store.create_run(run_id, digest or self.digest, [AGENT_NODE, SECOND_NODE])
            store.start_attempt(run_id, "lane-one", "d" * 40)
            store.mark_blocked(
                run_id,
                "lane-one",
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                detail={"verdict": "three attempts, still red"},
                retry_class=st.RetryClass.SEMANTIC,
            )
            store.declare_outcome(run_id)
        return run_id


class RunStatusTest(RunStatusFixture):
    def test_named_plan_status_needs_no_flags_at_all(self):
        """The field failure: a plan name alone refused with '--db is required'."""
        self._live_run()
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("run-live", output)
        self.assertIn("RUNNING", output)

    def test_status_reports_no_ledger_rather_than_creating_one(self):
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")
        self.assertFalse(self.database.exists())

    def test_status_reports_a_plan_with_no_runs(self):
        self._accepted_run(digest=self.other_digest)
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 3)
        payload = json.loads(output)
        self.assertEqual(payload["outcome"], "RUN_NOT_FOUND")
        self.assertIn("named", payload["detail"])

    def test_live_run_reports_the_attempt_in_flight_and_why_earlier_ones_died(self):
        self._live_run()
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["run_id"], "run-live")
        self.assertEqual(progress["plan_name"], "named")
        self.assertEqual(progress["state"], "RUNNING")
        self.assertIsNone(progress["declared_outcome"])
        self.assertEqual([item["attempt_no"] for item in progress["in_flight"]], [3])
        node = progress["nodes"][0]
        self.assertEqual(node["state"], "RUNNING")
        self.assertEqual(node["attempt_no"], 3)
        self.assertEqual(
            [item["retry_class"] for item in node["attempts"]],
            ["ENVIRONMENTAL", "SEMANTIC", None],
        )
        self.assertEqual(node["attempts"][1]["verdict"], "the gate stayed red")
        self.assertEqual(node["attempts"][2]["session_path"], "/tmp/session.jsonl")
        self.assertTrue(node["attempts"][2]["running"])
        self.assertIsNotNone(node["attempts"][2]["elapsed_s"])
        self.assertIsNotNone(progress["elapsed_s"])

    def test_live_run_text_view_names_the_failure_reasons(self):
        self._live_run()
        code, output = self._run(["run", "status", "named"])
        self.assertEqual(code, 0, output)
        self.assertIn("why: the gate stayed red", output)
        self.assertIn("in flight", output)
        self.assertIn("ENVIRONMENTAL", output)
        self.assertIn("session: /tmp/session.jsonl", output)

    def test_finished_run_reports_its_declared_outcome_and_output(self):
        self._accepted_run()
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["state"], "MERGED")
        self.assertEqual(progress["declared_outcome"], "ACCEPTED")
        self.assertEqual(progress["nodes"][0]["output_sha"], "c" * 40)
        self.assertEqual(progress["in_flight"], [])

    def test_failed_run_reports_the_block_reason_on_the_node(self):
        self._blocked_run()
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["declared_outcome"], "BLOCKED")
        blocked = {node["node_id"]: node for node in progress["nodes"]}
        self.assertEqual(
            blocked["lane-one"]["block_reason"], "SEMANTIC_BUDGET_EXHAUSTED"
        )
        self.assertEqual(
            blocked["lane-one"]["attempts"][0]["verdict"], "three attempts, still red"
        )
        self.assertEqual(blocked["lane-two"]["state"], "PENDING")
        code, text = self._run(["run", "status", "named"])
        self.assertEqual(code, 0, text)
        self.assertIn("BLOCKED: SEMANTIC_BUDGET_EXHAUSTED", text)

    def test_live_state_is_reported_beside_a_stale_declared_outcome(self):
        """A rescued run is RUNNING even though `runs` still says BLOCKED."""
        run_id = self._blocked_run()
        with self._store() as store:
            store.retry(run_id, "lane-one", force=True)
            store.start_attempt(run_id, "lane-one", "e" * 40)
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["state"], "RUNNING")
        self.assertEqual(progress["declared_outcome"], "BLOCKED")

    def test_cancel_requested_shows_before_the_nodes_react(self):
        run_id = self._live_run()
        with self._store() as store:
            store.conn.execute(
                "UPDATE runs SET cancel_requested=1 WHERE run_id=?", (run_id,)
            )
        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["state"], "CANCELLING")


class RunSelectionTest(RunStatusFixture):
    def test_several_runs_default_to_the_newest_and_stay_reachable(self):
        self._accepted_run("run-first")
        self._blocked_run("run-second")
        newest = self._live_run("run-third")

        code, output = self._run(["run", "status", "named", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["run_id"], newest)

        code, output = self._run(
            ["run", "status", "named", "--run-id", "run-first", "--json"]
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["run_id"], "run-first")

    def test_a_run_id_is_accepted_positionally(self):
        self._accepted_run("run-first")
        self._live_run("run-third")
        code, output = self._run(["run", "status", "run-first", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["run_id"], "run-first")

    def test_an_unknown_run_id_is_a_typed_refusal(self):
        self._live_run()
        code, output = self._run(["run", "status", "named", "--run-id", "run-nope"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

    def test_an_unknown_name_is_a_typed_refusal(self):
        self._live_run()
        code, output = self._run(["run", "status", "no-such-plan"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

    def test_run_list_indexes_every_run_and_filters_by_plan(self):
        self._accepted_run("run-first")
        self._live_run("run-third")
        self._accepted_run("run-other", digest=self.other_digest)

        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        rows = json.loads(output)
        self.assertEqual(
            [row["run_id"] for row in rows], ["run-other", "run-third", "run-first"]
        )
        self.assertEqual({row["plan_name"] for row in rows}, {"named", "other"})

        code, output = self._run(["run", "list", "named", "--json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [row["run_id"] for row in json.loads(output)], ["run-third", "run-first"]
        )

    def test_run_list_text_view_survives_an_empty_ledger(self):
        self._accepted_run("run-first")
        with self._store() as store:
            store.conn.execute("DELETE FROM runs")
        code, output = self._run(["run", "list"])
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip(), "no runs")


class RunMutationSelectionTest(RunStatusFixture):
    def test_cancel_resolves_the_named_plans_newest_run(self):
        self._accepted_run("run-first")
        newest = self._live_run("run-third")
        code, output = self._run(["run", "cancel", "named", "--discard"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["run_id"], newest)
        with self._store() as store:
            self.assertEqual(
                store.get_node(newest, "lane-one").state, st.NodeState.CANCELLED
            )
            self.assertEqual(
                store.get_node("run-first", "lane-one").state, st.NodeState.MERGED
            )

    def test_cancel_reaches_an_older_run_by_id(self):
        older = self._live_run("run-first")
        self._live_run("run-third")
        code, output = self._run(
            ["run", "cancel", "named", "--discard", "--run-id", older]
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["run_id"], older)
        with self._store() as store:
            self.assertEqual(
                store.get_node("run-third", "lane-one").state, st.NodeState.RUNNING
            )

    def test_resume_re_enters_the_existing_run_instead_of_minting_one(self):
        run_id = self._blocked_run("run-second")
        with mock.patch.object(maestro, "_run_resume", return_value=0) as resume:
            code, output = self._run(["run", "resume", "named"])
        self.assertEqual(code, 0, output)
        args = resume.call_args.args[0]
        self.assertEqual(args.run_id, run_id)
        self.assertEqual(args.db, str(self.database))
        self.assertEqual(args.digest, self.digest)
        run_root = self.state / "runs" / run_id
        self.assertEqual(args.integration_path, str(run_root / "integration"))
        self.assertEqual(args.worktrees_root, str(run_root / "worktrees"))
        self.assertEqual(args.scratch_root, str(run_root / "scratch"))

    def test_resume_reaches_an_older_run_by_id(self):
        older = self._blocked_run("run-first")
        self._live_run("run-third")
        with mock.patch.object(maestro, "_run_resume", return_value=0) as resume:
            code, output = self._run(["run", "resume", "named", "--run-id", older])
        self.assertEqual(code, 0, output)
        self.assertEqual(resume.call_args.args[0].run_id, older)

    def test_resume_refuses_a_plan_that_has_never_run(self):
        self._accepted_run(digest=self.other_digest)
        code, output = self._run(["run", "resume", "named"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "RUN_NOT_FOUND")

    def test_start_still_mints_a_fresh_run_id(self):
        self._live_run("run-third")
        with mock.patch.object(maestro, "_run_start", return_value=0) as start:
            code, output = self._run(["run", "start", "named"])
        self.assertEqual(code, 0, output)
        minted = start.call_args.args[0].run_id
        self.assertTrue(minted.startswith("run-"))
        self.assertNotEqual(minted, "run-third")
        self.assertEqual(len(minted), len("run-") + 32)


class RunReaderTest(RunStatusFixture):
    def test_the_reader_never_creates_or_writes_the_ledger(self):
        with self.assertRaises(lc.LedgerUnavailable):
            lc.LifecycleReader.open(self.database)
        self.assertFalse(self.database.exists())

        self._accepted_run()
        before = self.database.stat().st_mtime_ns
        reader = lc.LifecycleReader.open(self.database)
        try:
            self.assertEqual([record.run_id for record in reader.runs()], ["run-done"])
            with self.assertRaises(Exception):
                reader.conn.execute("DELETE FROM runs")
        finally:
            reader.close()
        self.assertEqual(self.database.stat().st_mtime_ns, before)

    def test_a_finished_ledger_reads_after_its_sidecars_are_gone(self):
        """A cleanly closed WAL database has no -shm, which plain mode=ro refuses."""
        self._accepted_run()
        for sidecar in ("-wal", "-shm"):
            candidate = Path(str(self.database) + sidecar)
            if candidate.exists():
                candidate.unlink()
        reader = lc.LifecycleReader.open(self.database)
        try:
            self.assertEqual(len(reader.runs()), 1)
            self.assertEqual(len(reader.nodes("run-done")), 1)
        finally:
            reader.close()


class RunManualInvocationTest(RunStatusFixture):
    def test_an_explicit_db_still_drives_the_verb_by_hand(self):
        self._live_run()
        code, output = self._run(
            ["run", "status", "run-live", "--db", str(self.database), "--json"]
        )
        self.assertEqual(code, 0, output)
        progress = json.loads(output)
        self.assertEqual(progress["run_id"], "run-live")
        # No configuration was bound, so the digest has no name to map to.
        self.assertIsNone(progress["plan_name"])

    def test_status_without_a_database_is_a_typed_refusal(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as elsewhere:
            previous = Path.cwd()
            os.chdir(elsewhere)
            try:
                with contextlib.redirect_stdout(output):
                    code = maestro.main(["run", "status", "run-live"])
            finally:
                os.chdir(previous)
        self.assertEqual(code, 3)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")

    def test_start_still_refuses_runtime_flags_under_configuration(self):
        code, output = self._run(["run", "start", "named", "--db", str(self.database)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["outcome"], "MAESTRO_CONFIGURATION_INVALID")


class RunListStopsReportingDeadRunsAsLive(RunStatusFixture):
    """D4 through the operator's verbs, not through the derivation directly.

    `run list` reporting a dead run as RUNNING is the whole operator-visible
    bug: it is what sent an operator back to run-75dfc6914946487f998453fefb51a0cf
    twice believing it was live.
    """

    def _kill_the_scheduler(self, run_id, pid=424242):
        """Point the run's recorded owner at a pid that is not a process.

        Not a stub of the liveness probe: the row really names a pid, and the
        production `os.kill(pid, 0)` really finds nothing there.
        """
        with self._store() as store:
            store.conn.execute(
                "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
                (pid, lc.scheduler_host(), run_id),
            )

    def test_a_run_whose_scheduler_died_reads_abandoned_not_running(self):
        run_id = self._live_run()
        self._kill_the_scheduler(run_id)
        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        row = [r for r in json.loads(output) if r["run_id"] == run_id][0]
        self.assertEqual(row["state"], "ABANDONED")
        self.assertIs(row["scheduler_alive"], False)

    def test_a_live_run_is_still_reported_as_running(self):
        """The false positive that must never happen. The fixture's own run is
        owned by this process, which is definitely alive."""
        run_id = self._live_run()
        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        row = [r for r in json.loads(output) if r["run_id"] == run_id][0]
        self.assertEqual(row["state"], "RUNNING")
        self.assertIs(row["scheduler_alive"], True)

    def test_a_finished_cancellation_reads_cancelled_not_cancelling(self):
        run_id = self._live_run("run-cancelled")
        with self._store() as store:
            store.cancel_run(run_id)
        self._kill_the_scheduler(run_id)
        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        row = [r for r in json.loads(output) if r["run_id"] == run_id][0]
        # No scheduler ever declared an outcome for it, and it is still
        # reported terminally.
        self.assertIsNone(row["declared_outcome"])
        self.assertEqual(row["state"], "CANCELLED")

    def test_status_shows_the_facts_the_verdict_was_derived_from(self):
        run_id = self._live_run()
        self._kill_the_scheduler(run_id)
        code, output = self._run(["run", "status", run_id])
        self.assertEqual(code, 0, output)
        self.assertIn("ABANDONED", output)
        self.assertIn("pid 424242", output)
        self.assertIn("gone", output)

    def test_an_accepted_run_is_unaffected_by_its_scheduler_being_gone(self):
        """A scheduler that declared and exited is supposed to be gone."""
        run_id = self._accepted_run()
        self._kill_the_scheduler(run_id)
        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        row = [r for r in json.loads(output) if r["run_id"] == run_id][0]
        self.assertEqual(row["state"], "MERGED")
        self.assertEqual(row["declared_outcome"], "ACCEPTED")

    def test_a_blocked_declared_run_is_not_relabelled_as_abandoned(self):
        run_id = self._blocked_run()
        self._kill_the_scheduler(run_id)
        code, output = self._run(["run", "list", "--json"])
        self.assertEqual(code, 0, output)
        row = [r for r in json.loads(output) if r["run_id"] == run_id][0]
        self.assertEqual(row["state"], "BLOCKED")


class PauseAndDiscardCancelTest(RunStatusFixture):
    """§7.3's two operator stops: one reversible, one not.

    `_live_run` creates the run from *this* process, so the ledger's
    `scheduler_pid` and `scheduler_start_epoch` are this process's own — which
    is what makes the identity proof below a real comparison of recorded
    numbers rather than a stub.
    """

    def _forget_the_scheduler(self, run_id, pid=424242):
        """Point the run's recorded owner at a pid that is not a process."""
        with self._store() as store:
            store.conn.execute(
                "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
                (pid, lc.scheduler_host(), run_id),
            )

    def _misrecord_the_start_epoch(self, run_id, epoch=1.0):
        """Leave the pid alive and correct; falsify only its start epoch.

        Exactly the reused-pid shape `scheduler_signal_pid` exists for: the
        number still names a live process, and that process is no longer the
        one that claimed the run.
        """
        with self._store() as store:
            store.conn.execute(
                "UPDATE runs SET scheduler_start_epoch=? WHERE run_id=?",
                (epoch, run_id),
            )

    def test_run_pause_is_the_named_non_terminal_stop(self):
        run_id = self._live_run()
        with mock.patch.object(maestro.os, "kill") as killed:
            code, output = self._run(["run", "pause", "named"])
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["outcome"], "PAUSE_REQUESTED")
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(
            [
                call.args
                for call in killed.call_args_list
                if call.args[1] == signal.SIGINT
            ],
            [(os.getpid(), signal.SIGINT)],
        )
        with self._store() as store:
            # Nothing moved. That is the whole property (§1.2).
            self.assertIsNone(store.latest_outcome(run_id))
            self.assertIs(
                store.get_node(run_id, "lane-one").state, st.NodeState.RUNNING
            )
            self.assertFalse(
                store.conn.execute(
                    "SELECT cancel_requested FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )

    def test_pause_refuses_to_signal_an_unproven_pid(self):
        """The recorded pid is alive and is this process; only the recorded
        start epoch disagrees. `scheduler_liveness` would still say True, and
        that is exactly why it is not the authority to signal (#37)."""
        run_id = self._live_run()
        self._misrecord_the_start_epoch(run_id)
        reader = maestro._open_reader(str(self.database))
        try:
            self.assertIs(lc.scheduler_liveness(reader.run(run_id)), True)
        finally:
            reader.close()
        with mock.patch.object(maestro.os, "kill") as killed:
            code, output = self._run(["run", "pause", "named"])
        self.assertEqual(code, 3, output)
        self.assertEqual(json.loads(output)["outcome"], "PAUSE_PID_UNPROVEN")
        self.assertEqual(
            [
                call.args
                for call in killed.call_args_list
                if call.args[1] == signal.SIGINT
            ],
            [],
        )

    def test_pause_reports_an_already_stopped_scheduler_without_signalling(self):
        run_id = self._live_run()
        self._forget_the_scheduler(run_id)
        code, output = self._run(["run", "pause", "named"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["outcome"], "ALREADY_STOPPED")

    def test_cancel_without_discard_pauses_instead_of_discarding(self):
        run_id = self._live_run()
        with mock.patch.object(maestro.os, "kill") as killed:
            code, output = self._run(["run", "cancel", "named"])
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["outcome"], "PAUSE_REQUESTED")
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(
            [
                call.args
                for call in killed.call_args_list
                if call.args[1] == signal.SIGINT
            ],
            [(os.getpid(), signal.SIGINT)],
        )
        with self._store() as store:
            self.assertIs(
                store.get_node(run_id, "lane-one").state, st.NodeState.RUNNING
            )
            self.assertIsNone(store.latest_outcome(run_id))

    def test_discard_declares_cancelled_and_refuses_resume(self):
        run_id = self._live_run()
        code, output = self._run(["run", "cancel", run_id, "--discard"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["outcome"], "CANCELLED")
        with self._store() as store:
            self.assertIs(store.latest_outcome(run_id), st.RunOutcome.CANCELLED)
            self.assertIs(store.run_cancel_cause(run_id), st.CancelCause.DISCARDED)
            with self.assertRaises(lc.ResumeRefused):
                store.resume_run(run_id)

    def test_discard_warns_about_verified_unmerged_work(self):
        run_id = "run-verified"
        with self._store() as store:
            store.create_run(run_id, self.digest, [AGENT_NODE])
            store.start_attempt(run_id, "lane-one", "a" * 40)
            store.mark_verified(run_id, "lane-one", "c" * 40)
        err = io.StringIO()
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(self.repo)
        try:
            with (
                mock.patch.dict(os.environ, self.environment, clear=False),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(err),
            ):
                code = maestro.main(["run", "cancel", run_id, "--discard"])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("lane-one", err.getvalue())
        self.assertIn("verified", err.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcome"], "CANCELLED")
        self.assertEqual(payload["unreachable"][0]["node_id"], "lane-one")

    def test_discard_refuses_to_rewrite_an_accepted_run(self):
        run_id = self._accepted_run()
        code, output = self._run(["run", "cancel", run_id, "--discard"])
        self.assertEqual(code, 3, output)
        self.assertEqual(json.loads(output)["outcome"], "RUN_ALREADY_TERMINAL")
        with self._store() as store:
            self.assertIs(store.latest_outcome(run_id), st.RunOutcome.ACCEPTED)


class LegacyAttemptDetailTest(unittest.TestCase):
    """Old free-form transition detail must not break run status."""

    def test_a_string_detail_is_ignored_before_a_typed_verdict(self):
        entries = (
            {"detail": "legacy free-form detail"},
            {"detail": {"verdict": "accepted"}},
        )
        self.assertEqual(maestro._attempt_verdict(entries), "accepted")


if __name__ == "__main__":
    unittest.main()
