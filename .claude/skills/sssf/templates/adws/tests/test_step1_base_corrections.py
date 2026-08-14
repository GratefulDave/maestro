"""Executable proof of the Step 1 base defects (MAESTRO architecture.md, §12.2).

These tests are written against the vendored SSSF base as it exists at
de31374. They are expected to FAIL before the Step 1 corrections land and to
pass afterwards. Each test targets one numbered item from §12.2 Step 1 and
asserts the behaviour concurrency requires, not the behaviour the base has.

Run with:  just test        (or: uv run adws/adw_test.py)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# This file ships inside adws/tests/, so the package root is its parent's parent.
ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules.data_types import (  # noqa: E402
    AgentConfig,
    ObservabilityConfig,
    PhaseParams,
    PromptEngineering,
    SSSFConfig,
    ConfigDefaults,
)
from adw_modules.runner import Run  # noqa: E402
from adw_modules.tracer import Tracer  # noqa: E402


def _config(tmp: Path) -> SSSFConfig:
    """A config whose data and db both live inside the test's temp directory."""
    data_dir = tmp / "adw_data"
    return SSSFConfig(
        defaults=ConfigDefaults(data_dir=str(data_dir)),
        observability=ObservabilityConfig(db=str(data_dir / "sssf.db")),
    )


def _params(name: str = "build") -> PhaseParams:
    return PhaseParams(
        name=name,
        kind="code",
        owner="test",
        description="exercise the base under two concurrent DAG nodes",
    )


class Step1PhaseIdentity(unittest.TestCase):
    """§12.2 Step 1 item 1 — phase_id must be derived from run, node, attempt."""

    def test_two_nodes_running_the_same_phase_name_get_two_phase_rows(self):
        """Two concurrent DAG nodes share one run. In the base they share one row.

        Both nodes read `max_phase_seq` before either writes, so both compute
        seq=1 and both build phase_id "<adw>_01_build". `phase_upsert`'s
        ON CONFLICT DO UPDATE then makes the second node overwrite the first
        node's row instead of adding its own: one node's entire phase record
        disappears from the trace, silently.

        The nodes carry their scheduler-assigned ids here because that is the
        whole repair — the base had nowhere to put the one fact that tells two
        concurrent units of work apart, so no id built from what it did have
        could distinguish them. Both nodes still race the same seq, which is
        what keeps this a test of the collision rather than of the counter.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runshared"

            tracer_a = Tracer(cfg.observability.db, tmp / "a.jsonl")
            tracer_b = Tracer(cfg.observability.db, tmp / "b.jsonl")
            tracer_a.session_start(adw_id, "test")

            node_a = Run(cfg=cfg, adw_id=adw_id, tracer=tracer_a, engineer="test",
                         node_id="build_api", dag_attempt_no=1)
            node_b = Run(cfg=cfg, adw_id=adw_id, tracer=tracer_b, engineer="test",
                         node_id="build_cli", dag_attempt_no=1)

            with node_a.phase(_params()):
                with node_b.phase(_params()):
                    pass

            rows = tracer_a.conn.execute(
                "SELECT phase_id FROM phases WHERE adw_id=?", (adw_id,)
            ).fetchall()
            self.assertEqual(
                len(rows), 2,
                f"two concurrent nodes must produce two phase rows, got {rows}",
            )

    def test_a_second_attempt_of_one_node_does_not_overwrite_the_first(self):
        """§12.2 Step 1 item 1 — attempts must be distinguishable.

        A retried node re-runs the same phase name at the same node id, in a
        fresh Run built by the scheduler for the new attempt. Without the
        attempt number in the identity the two Runs agree on everything the id
        is made of, so attempt 2 updates attempt 1's row and the evidence that
        attempt 1 ever failed is destroyed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runretry"

            tracer = Tracer(cfg.observability.db, tmp / "e.jsonl")
            tracer.session_start(adw_id, "test")

            first = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test",
                        node_id="build_api", dag_attempt_no=1)
            try:
                with first.phase(_params()):
                    raise RuntimeError("attempt 1 fails")
            except RuntimeError:
                pass
            second = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test",
                         node_id="build_api", dag_attempt_no=2)
            with second.phase(_params()):
                pass

            statuses = [
                r[0] for r in tracer.conn.execute(
                    "SELECT status FROM phases WHERE adw_id=?", (adw_id,)
                ).fetchall()
            ]
            self.assertIn("fail", statuses,
                          "the failed attempt must survive in the trace")
            self.assertIn("success", statuses,
                          "the successful attempt must be recorded")


class Step1ConsoleAttribution(unittest.TestCase):
    """§12.2 Step 1 item 2 — the console's phase slot must be per-node."""

    def test_a_log_lands_on_the_phase_that_emitted_it(self):
        """One Console instance, two open phases. Today the last one wins.

        `Console.phase_id` is a single instance attribute set by
        `phase_started`. Opening node B's phase repoints it, so node A's next
        log event is written against node B's phase_id — the trace attributes
        one node's work to another.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runlog"

            tracer = Tracer(cfg.observability.db, tmp / "c.jsonl")
            tracer.session_start(adw_id, "test")
            run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test")

            with run.phase(_params("node_a")) as ph_a:
                with run.phase(_params("node_b")):
                    ph_a.log(marker="belongs_to_node_a")

            row = tracer.conn.execute(
                "SELECT phase_id FROM events WHERE type='log'"
                " AND payload_json LIKE '%belongs_to_node_a%'"
                " AND name='node_a'"
            ).fetchone()
            self.assertIsNotNone(row, "the log event was not recorded at all")
            self.assertEqual(
                row[0], ph_a.phase.phase_id,
                "node A's log was attributed to another node's phase",
            )


class Step1AgentSessionIdentity(unittest.TestCase):
    """§12.2 Step 1 item 4 — agent sessions must key by agent, node, attempt."""

    def test_two_nodes_on_one_role_do_not_share_a_coding_agent_session(self):
        """`agent_sessions` had PRIMARY KEY (adw_id, agent) — tracer.py:88.

        Two concurrent nodes assigned the same role write the same primary
        key, so the second node's session id overwrites the first's. Both
        nodes then resume the same coding-agent session: two live agents in
        one context window. The key now carries the node and the attempt, so
        each node's session row is its own; the calls below pass the node ids
        the scheduler assigned, because that fact is what the old key had no
        column for.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runagent"

            tracer = Tracer(cfg.observability.db, tmp / "d.jsonl")
            tracer.session_start(adw_id, "test")
            agent = AgentConfig(
                name="builder",
                prompt_engineering=PromptEngineering(system="s.md", user="u.md"),
            )

            tracer.agent_session_row(adw_id, agent, "session_for_node_a",
                                     node_id="build_api", dag_attempt_no=1)
            tracer.agent_session_row(adw_id, agent, "session_for_node_b",
                                     node_id="build_cli", dag_attempt_no=1)

            sessions = {
                r[0] for r in tracer.conn.execute(
                    "SELECT session_id FROM agent_sessions WHERE adw_id=?", (adw_id,)
                ).fetchall()
            }
            self.assertEqual(
                sessions, {"session_for_node_a", "session_for_node_b"},
                "each node must own its own coding-agent session",
            )


if __name__ == "__main__":
    unittest.main()
