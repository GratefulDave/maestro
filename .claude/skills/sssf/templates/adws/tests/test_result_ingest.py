"""§7.7 — every result lands in one adjudicated row, payload and all.

The mechanism was inert end to end. `run status` rendered a `results` section
(`maestro.py::_render_progress`) from a live reader (`LifecycleReader.results`),
and nothing in the runtime had ever written a row: `record_result` and
`adjudicate_result` had test callers only, and no production path constructed a
`ResultRecord`. Measured against the deployment ledger on 2026-08-17: 10 runs,
25 attempts, **0 rows**. An operator was being shown a section that could never
be non-empty.

§16.3 item 43 held the ruling open because `NodeExecution` was deliberately
structural and "the scheduler never possesses a result payload to record". That
premise turned out to be false in the useful direction: `_poll_agent_execution`
was already calling `json.loads` on the envelope and keeping only
`envelope_parsed`, discarding the object §7.7 needs. The payload was not
missing, it was dropped — §7.6's lesson about a typed value computed at the
point of failure and never recorded, in its second instance.

Three things are asserted here, and the third is the one that matters:

* the real adapter carries the parsed envelope rather than discarding it;
* a live attempt's result is stored ACCEPTED, with its payload;
* a **superseded** attempt's result is stored rather than dropped. That is the
  case §7.7 was written for — "a reclaimed attempt's late result … the direct
  repair of a correct FAIL carrying two real findings that vanished with a
  byte-identical journal" — and it is reachable only because the write happens
  before the ownership check. Record after it and SUPERSEDED is unreachable,
  three of the four adjudications are decoration, and the payload the section
  exists to preserve is exactly the one thrown away.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro                                   # noqa: E402
from adw_modules import launcher as lch          # noqa: E402
from adw_modules import scheduler as sch         # noqa: E402
from adw_modules import scheduler_types as st    # noqa: E402
from adw_modules import verification as vf       # noqa: E402

from test_scheduler import SchedulerFixture      # noqa: E402


class _ExitedAdapter:
    """A launcher whose poll reports the agent settled. `_poll_agent_execution`
    is the real production function under test; only the transport is stood in
    for, which is the seam §12.3 already injects everywhere else."""

    def poll(self, handle):
        return lch.PollResult(state=lch.PollState.EXITED, exit_code=0,
                              detail="settled")

    def classify(self, exc):
        return lch.ErrorClass.EXECUTION


class _Handle:
    process_group = None


# ── the adapter half: the payload is carried, not discarded ─────────────────

class PollCarriesTheEnvelopeTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.envelope = Path(self.tmp) / "agent-envelope.json"

    def _execute(self):
        return maestro._poll_agent_execution(
            _ExitedAdapter(), _Handle(), self.envelope, record=None,
            cancel_requested=lambda: False, quiesce_attempt=lambda r, p: None)

    def test_a_parsed_envelope_reaches_the_scheduler_as_a_payload(self):
        self.envelope.write_text(json.dumps(
            {"success": False, "findings": ["a", "b"]}), encoding="utf-8")
        execution = self._execute()
        self.assertTrue(execution.envelope_parsed)
        self.assertEqual(execution.envelope_payload,
                         {"success": False, "findings": ["a", "b"]})

    def test_an_unparseable_envelope_carries_no_payload(self):
        self.envelope.write_text("{not json", encoding="utf-8")
        execution = self._execute()
        self.assertFalse(execution.envelope_parsed)
        self.assertIsNone(execution.envelope_payload)

    def test_a_missing_envelope_carries_no_payload(self):
        execution = self._execute()
        self.assertFalse(execution.envelope_parsed)
        self.assertIsNone(execution.envelope_payload)

    def test_a_parseable_non_mapping_carries_no_payload(self):
        """A bare list is a parseable envelope that declares nothing a result
        row can hold, and `ResultRecord` refuses to exist without a payload."""
        self.envelope.write_text("[1, 2, 3]", encoding="utf-8")
        execution = self._execute()
        self.assertTrue(execution.envelope_parsed)
        self.assertIsNone(execution.envelope_payload)


class LateEnvelopeRecoveryParserTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.scratch = Path(self.tmp)
        self.attempt = type("Attempt", (), {"scratch": self.scratch})()

    def _read(self):
        return maestro._late_agent_execution(self.attempt, record=None)

    def test_only_an_explicit_success_is_recoverable(self):
        payload = {"success": True, "summary": "finished"}
        (self.scratch / "agent-envelope.json").write_text(
            json.dumps(payload), encoding="utf-8")
        execution = self._read()
        self.assertIsNotNone(execution)
        self.assertTrue(execution.envelope_parsed)
        self.assertEqual(execution.envelope_payload, payload)
        self.assertEqual(execution.exit_code, 0)

    def test_a_failed_or_malformed_declaration_is_not_recoverable(self):
        envelope = self.scratch / "agent-envelope.json"
        for content in ('{"success": false}', '{"summary": "missing"}',
                        "[1, 2]", "{broken"):
            with self.subTest(content=content):
                envelope.write_text(content, encoding="utf-8")
                self.assertIsNone(self._read())


# ── the scheduler half: one adjudicated row per result ──────────────────────

class ResultRowsAreWrittenTests(SchedulerFixture):

    PAYLOAD = {"success": True, "notes": "done"}

    def _runner_declaring(self, payload, fence=None):
        inner = self.run_node

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            execution = inner(attempt, node, record, retry_prompt, on_launch,
                              cancel_requested)
            if fence is not None:
                fence(record)
            return sch.NodeExecution(
                envelope_parsed=execution.envelope_parsed,
                exit_code=execution.exit_code,
                envelope_payload=payload)

        return run_node

    def _results(self, run_id="run1"):
        return self.store.audit_results(run_id)

    def test_a_live_attempts_result_is_stored_accepted_with_its_payload(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self._runner_declaring(self.PAYLOAD))).run()

        rows = self._results()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["adjudication"],
                         st.Adjudication.ACCEPTED.value)
        self.assertEqual(rows[0]["payload"], self.PAYLOAD)

    def test_a_code_node_writes_no_result_row(self):
        """No envelope, so no result — an absence, not a dropped one."""
        self.schedule([self.code("c")]).run()
        self.assertEqual(self._results(), ())

    def test_a_superseded_attempts_result_is_retained_not_dropped(self):
        """§7.7's originating incident, driven.

        The attempt is fenced by the watchdog before its result is recorded,
        so by the time the row is adjudicated this generation no longer owns
        RUNNING. The payload must still land, carrying `SUPERSEDED` — that is
        the whole difference between a late result being *judged* and a late
        result *vanishing*.
        """
        self.written = {"a": {"a.py": "A\n"}}
        captured = {}

        def fence(record):
            captured["record"] = record
            # Exactly what the watchdog's conviction does to a generation.
            self.scheduler._fence_watchdog_generation(record)

        self.scheduler = self.schedule(
            [self.agent("a")],
            config=self.config(concurrency=1, environmental_retries=1),
            deps=self.deps(
                run_node=self._runner_declaring(self.PAYLOAD, fence=fence)))
        self.scheduler.run()

        rows = self._results()
        self.assertTrue(rows, "a fenced attempt's declared result vanished")
        self.assertEqual(rows[0]["payload"], self.PAYLOAD)
        self.assertEqual(rows[0]["adjudication"],
                         st.Adjudication.SUPERSEDED.value)


# ── the adjudicator is not re-implemented here ──────────────────────────────

class AdjudicationIsDelegatedTests(unittest.TestCase):
    """§7.7 says a result is judged solely against the attempt row it names.
    The scheduler supplies the row and stores the answer; it must not decide.
    """

    def test_the_scheduler_calls_the_adjudicator(self):
        source = (ADWS / "adw_modules" / "scheduler.py").read_text()
        self.assertIn("vf.adjudicate_result(", source)

    def test_the_adjudicator_ignores_node_state(self):
        """Guarded here as well as in test_verification, because this is the
        caller that could be tempted to pre-filter on it."""
        live = st.AttemptRecord(run_id="r", node_id="n", attempt_no=1,
                                base_sha="s" * 40, state=st.NodeState.RUNNING)
        adjudged = vf.adjudicate_result(
            st.ResultRecord(node_id="n", attempt_no=1, subject_sha="s" * 40,
                            payload={"k": "v"}),
            [live], node_state=st.NodeState.MERGED)
        self.assertIs(adjudged.adjudication, st.Adjudication.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
