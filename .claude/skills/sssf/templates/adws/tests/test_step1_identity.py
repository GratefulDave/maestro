"""Identity under concurrency: phases, agent sessions, and DAG attempts.

MAESTRO architecture.md §12.2 Step 1 items 1, 4 and 5. Two facts are settled
here that the base gets wrong the moment more than one node runs at a time:
a phase's identity must come from the run, the node and the attempt rather
than from a counter two threads can read before either writes, and an agent's
session row must be owned by one node rather than shared by every node that
happens to hold the same roster role.

A third fact is settled alongside them and matters just as much: this code
installs into other people's repositories, so every classic single-node ADW
must keep working and must keep producing the phase ids its operators already
read. The back-compatibility tests below are therefore not politeness — they
are the constraint that shapes the design.

Run with:  uv run adws/adw_test.py -k identity
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# This file ships inside adws/tests/, so the package root is its parent's parent.
ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules.data_types import (  # noqa: E402
    AgentConfig,
    ConfigDefaults,
    EventRecord,
    GateReport,
    ObservabilityConfig,
    Phase,
    PhaseParams,
    PromptEngineering,
    SSSFConfig,
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
        description="exercise identity when two nodes share one run",
    )


def _agent(name: str = "builder") -> AgentConfig:
    return AgentConfig(
        name=name,
        prompt_engineering=PromptEngineering(system="s.md", user="u.md"),
    )


def _phase_ids(tracer: Tracer, adw_id: str) -> list[str]:
    return [r[0] for r in tracer.conn.execute(
        "SELECT phase_id FROM phases WHERE adw_id=? ORDER BY seq, phase_id", (adw_id,))]


class DagPhaseIdentity(unittest.TestCase):
    """§12.2 Step 1 item 1 — phase_id derives from run, node and attempt."""

    def test_two_nodes_running_one_phase_name_get_two_phase_rows(self):
        """Two nodes, one run, one phase name, and the same raced seq.

        Both Run objects seed their sequence from the same `max_phase_seq`
        before either writes, so both hold seq=1. That is exactly the base's
        collision, and it must no longer reach the identity: the node ids
        differ, so the phase ids differ, so `phase_upsert` inserts twice
        instead of the second node overwriting the first.
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
            self.assertEqual(node_a._seq, node_b._seq,
                             "the test only proves anything while both nodes race the counter")

            with node_a.phase(_params()):
                with node_b.phase(_params()):
                    pass

            self.assertEqual(len(_phase_ids(tracer_a, adw_id)), 2,
                             f"two nodes must produce two phase rows, got "
                             f"{_phase_ids(tracer_a, adw_id)}")

    def test_two_attempts_of_one_node_get_two_phase_rows(self):
        """A retried node re-runs the same phase name at a new attempt.

        Without the attempt in the identity, attempt 2 overwrites attempt 1 and
        the evidence that attempt 1 ever failed is destroyed — the failure a
        retry exists to react to becomes unreadable in the trace.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runattempt"

            tracer = Tracer(cfg.observability.db, tmp / "b.jsonl")
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

            statuses = sorted(r[0] for r in tracer.conn.execute(
                "SELECT status FROM phases WHERE adw_id=?", (adw_id,)))
            self.assertEqual(statuses, ["fail", "success"],
                             "both attempts must survive as their own rows")

    def test_a_nodes_phase_id_does_not_move_when_the_run_counter_does(self):
        """The same node at the same attempt names the same phase, always.

        The counter is seeded from whatever is already in the database, so a
        node scheduled after five other phases would get a different id from
        the same node scheduled first. Reproducible identity is what lets a
        resumed run recognise the row it wrote before the crash.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)

            tracer = Tracer(cfg.observability.db, tmp / "c.jsonl")
            tracer.session_start("runcounter", "test")

            early = Run(cfg=cfg, adw_id="runcounter", tracer=tracer, engineer="test",
                        node_id="build_api", dag_attempt_no=1)
            with early.phase(_params()) as ph_early:
                pass

            # Five unrelated phases move the counter under the node.
            filler = Run(cfg=cfg, adw_id="runcounter", tracer=tracer, engineer="test",
                         node_id="filler", dag_attempt_no=1)
            for i in range(5):
                with filler.phase(_params(f"filler_{i}")):
                    pass

            late = Run(cfg=cfg, adw_id="runcounter", tracer=tracer, engineer="test",
                       node_id="build_api", dag_attempt_no=1)
            with late.phase(_params()) as ph_late:
                pass

            self.assertEqual(ph_late.phase.phase_id, ph_early.phase.phase_id,
                             "identity must not depend on how much else the run has done")
            self.assertGreater(ph_late.phase.seq, ph_early.phase.seq,
                               "seq must still advance — it is the ordering key")


class ClassicRunBackCompat(unittest.TestCase):
    """Every ADW that ships in adws/ supplies no node. It must not change."""

    def test_a_run_with_no_node_keeps_the_historical_phase_id(self):
        """`<adw_id>_<seq>_<name>` is what operators and old rows already read.

        A run with no node identity is not a DAG node, so there is nothing to
        derive an id from but the sequence — and the sequence is safe there,
        because a classic ADW opens its phases one at a time in one thread.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runclassic"

            tracer = Tracer(cfg.observability.db, tmp / "d.jsonl")
            tracer.session_start(adw_id, "test")
            run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test")

            with run.phase(_params("request")) as ph_one:
                pass
            with run.phase(_params("plan")) as ph_two:
                pass

            self.assertEqual(ph_one.phase.phase_id, "runclassic_01_request")
            self.assertEqual(ph_two.phase.phase_id, "runclassic_02_plan")

    def test_a_joined_run_still_continues_the_sequence(self):
        """adw_plan + adw_build_test share an adw_id and both open "request".

        Two processes, one session, the same phase name in each. The second
        process seeds its counter from the first's maximum, so the ids stay
        distinct and neither row is overwritten. This is the case that makes
        the sequence part of the classic id in the first place.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runjoined"

            tracer = Tracer(cfg.observability.db, tmp / "e.jsonl")
            tracer.session_start(adw_id, "test")

            first = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test")
            with first.phase(_params("request")):
                pass
            second = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test")
            with second.phase(_params("request")):
                pass

            self.assertEqual(_phase_ids(tracer, adw_id),
                             ["runjoined_01_request", "runjoined_02_request"])


class AgentSessionIdentity(unittest.TestCase):
    """§12.2 Step 1 item 4 — an agent session belongs to one node's attempt."""

    def test_two_nodes_on_one_role_do_not_share_a_coding_agent_session(self):
        """Sharing the row means sharing the session id, which means sharing
        the context window: two live agents talking into one conversation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runagent"

            tracer = Tracer(cfg.observability.db, tmp / "f.jsonl")
            tracer.session_start(adw_id, "test")

            tracer.agent_session_row(adw_id, _agent(), "session_for_node_a",
                                     node_id="build_api", dag_attempt_no=1)
            tracer.agent_session_row(adw_id, _agent(), "session_for_node_b",
                                     node_id="build_cli", dag_attempt_no=1)

            sessions = {r[0] for r in tracer.conn.execute(
                "SELECT session_id FROM agent_sessions WHERE adw_id=?", (adw_id,))}
            self.assertEqual(sessions, {"session_for_node_a", "session_for_node_b"})

    def test_a_second_attempt_does_not_inherit_the_first_attempts_session(self):
        """A retry is a fresh agent, so it is a fresh row and a fresh id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runagentretry"

            tracer = Tracer(cfg.observability.db, tmp / "g.jsonl")
            tracer.session_start(adw_id, "test")

            tracer.agent_session_row(adw_id, _agent(), "session_attempt_1",
                                     node_id="build_api", dag_attempt_no=1)
            tracer.agent_session_row(adw_id, _agent(), "session_attempt_2",
                                     node_id="build_api", dag_attempt_no=2)

            sessions = {r[0] for r in tracer.conn.execute(
                "SELECT session_id FROM agent_sessions WHERE adw_id=?", (adw_id,))}
            self.assertEqual(sessions, {"session_attempt_1", "session_attempt_2"})

    def test_a_classic_run_still_keeps_one_row_per_agent(self):
        """One row per agent per run is what the visualizer's lanes are built
        from, and a classic ADW that runs an agent twice must still overwrite
        rather than accumulate a second lane for the same work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = _config(tmp)
            adw_id = "runagentclassic"

            tracer = Tracer(cfg.observability.db, tmp / "h.jsonl")
            tracer.session_start(adw_id, "test")

            tracer.agent_session_row(adw_id, _agent(), "first_session")
            tracer.agent_session_row(adw_id, _agent(), "resumed_session")

            rows = tracer.conn.execute(
                "SELECT session_id FROM agent_sessions WHERE adw_id=?", (adw_id,)).fetchall()
            self.assertEqual(rows, [("resumed_session",)],
                             "a classic run keeps one row per agent, latest wins")


class AgentMapScoping(unittest.TestCase):
    """agent_map.json is the file the tracer's row mirrors; both must scope.

    The map is what a run reads to decide whether to resume a coding-agent
    session, so leaving it keyed by agent name alone would hand two nodes on
    one role the same session id from the file even after the database stopped
    handing it to them.
    """

    def _run(self, tmp: Path, adw_id: str, **identity) -> Run:
        cfg = _config(tmp)
        tracer = Tracer(cfg.observability.db, tmp / "p.jsonl")
        tracer.session_start(adw_id, "test")
        return Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="test", **identity)

    def test_two_nodes_on_one_role_keep_their_own_session_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            node_a = self._run(tmp, "runmap", node_id="build_api", dag_attempt_no=1)
            node_b = self._run(tmp, "runmap", node_id="build_cli", dag_attempt_no=1)

            node_a.save_agent_map("builder", {"session_id": "a"})
            node_b.save_agent_map("builder", {"session_id": "b"})
            reread = self._run(tmp, "runmap", node_id="build_api", dag_attempt_no=1)

            self.assertEqual(node_a.agent_map_entry("builder"), {"session_id": "a"})
            self.assertEqual(node_b.agent_map_entry("builder"), {"session_id": "b"})
            self.assertEqual(reread.agent_map_entry("builder"), {"session_id": "a"},
                             "a node re-reading the file must find its own session")

    def test_a_second_attempt_does_not_resume_the_first_attempts_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            first = self._run(tmp, "runmapretry", node_id="build_api", dag_attempt_no=1)
            first.save_agent_map("builder", {"session_id": "attempt_1"})
            second = self._run(tmp, "runmapretry", node_id="build_api", dag_attempt_no=2)

            self.assertIsNone(second.agent_map_entry("builder"),
                              "a fresh attempt starts a fresh agent, not a resumed one")

    def test_a_classic_run_keeps_the_plain_agent_name_key(self):
        """agent_map.json is read by hand and by agents.py, which looks the
        agent's name up directly; a classic run's file must not change shape."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            run = self._run(tmp, "runmapclassic")
            run.save_agent_map("builder", {"session_id": "sid", "model": "m"})

            written = json.loads(
                (run.session_dir / "agent_map.json").read_text())
            self.assertEqual(written, {"builder": {"session_id": "sid", "model": "m"}})
            self.assertEqual(run.agent_map_entry("builder"),
                             {"session_id": "sid", "model": "m"})


OLD_AGENT_SESSIONS = """
CREATE TABLE agent_sessions (
  adw_id        TEXT REFERENCES sessions,
  agent         TEXT,
  coding_agent  TEXT, model TEXT, color TEXT,
  session_id    TEXT,
  context_tokens INTEGER,
  context_window INTEGER,
  created_at    TEXT, last_used_at TEXT,
  PRIMARY KEY (adw_id, agent)
);
"""


class AgentSessionMigration(unittest.TestCase):
    """A primary key cannot be widened by ALTER TABLE, so the table is rebuilt.

    An installed factory has databases with real history in them. The rebuild
    is only correct if that history survives it, so the test starts from the
    shipped schema rather than from an empty file.
    """

    def _old_database(self, path: Path) -> None:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.executescript(OLD_AGENT_SESSIONS)
        conn.execute(
            "INSERT INTO agent_sessions (adw_id, agent, coding_agent, model, color,"
            " session_id, context_tokens, context_window, created_at, last_used_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("legacy_run", "builder", "pi", "some/model", "#abcdef", "legacy_session",
             1234, 200000, "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
        )
        conn.close()

    def test_existing_rows_survive_the_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db = tmp / "legacy.db"
            self._old_database(db)

            tracer = Tracer(db, tmp / "i.jsonl")

            row = tracer.conn.execute(
                "SELECT agent, coding_agent, model, color, session_id, context_tokens,"
                " context_window, created_at FROM agent_sessions WHERE adw_id=?",
                ("legacy_run",)).fetchone()
            self.assertEqual(row, ("builder", "pi", "some/model", "#abcdef",
                                   "legacy_session", 1234, 200000,
                                   "2026-01-01T00:00:00Z"))

    def test_the_rebuilt_table_admits_two_nodes_and_re_running_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db = tmp / "legacy.db"
            self._old_database(db)

            Tracer(db, tmp / "j.jsonl")
            tracer = Tracer(db, tmp / "k.jsonl")   # a second open must not rebuild again

            tracer.agent_session_row("legacy_run", _agent(), "node_a_session",
                                     node_id="build_api", dag_attempt_no=1)
            tracer.agent_session_row("legacy_run", _agent(), "node_b_session",
                                     node_id="build_cli", dag_attempt_no=1)

            sessions = {r[0] for r in tracer.conn.execute(
                "SELECT session_id FROM agent_sessions WHERE adw_id=?", ("legacy_run",))}
            self.assertEqual(sessions,
                             {"legacy_session", "node_a_session", "node_b_session"},
                             "the legacy row and both nodes must coexist")

    def test_a_pre_migration_database_missing_later_columns_still_opens(self):
        """The colour and context columns were themselves added by migration.

        A database old enough to predate them must have them added before the
        rebuild copies rows across, or the copy reads a column that is not
        there yet.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db = tmp / "ancient.db"
            conn = sqlite3.connect(str(db), isolation_level=None)
            conn.executescript("""
                CREATE TABLE agent_sessions (
                  adw_id TEXT, agent TEXT, coding_agent TEXT, model TEXT,
                  session_id TEXT, created_at TEXT, last_used_at TEXT,
                  PRIMARY KEY (adw_id, agent)
                );
            """)
            conn.execute("INSERT INTO agent_sessions (adw_id, agent, coding_agent,"
                         " model, session_id, created_at, last_used_at)"
                         " VALUES ('ancient_run','builder','pi','m','sid','t0','t1')")
            conn.close()

            tracer = Tracer(db, tmp / "l.jsonl")
            row = tracer.conn.execute(
                "SELECT agent, session_id, color FROM agent_sessions WHERE adw_id=?",
                ("ancient_run",)).fetchone()
            self.assertEqual(row, ("builder", "sid", None))


class TracerThreads(unittest.TestCase):
    """One Tracer, several node threads. §7.2 runs nodes on a thread pool.

    These tests use real threads rather than asserting a property of the
    connection, because the failure they exist to catch is a runtime error
    raised inside a worker — the shape of failure that a ThreadPoolExecutor
    swallows into a future nobody reads (§7.5).
    """

    def _hammer(self, work, threads: int = 8):
        """Run `work(i)` on `threads` real threads released together."""
        start = threading.Barrier(threads)
        errors: list[str] = []

        def body(index: int) -> None:
            start.wait()
            try:
                work(index)
            except BaseException as error:            # noqa: BLE001 — the point is to see it
                errors.append(f"{type(error).__name__}: {error}")

        running = [threading.Thread(target=body, args=(i,)) for i in range(threads)]
        for thread in running:
            thread.start()
        for thread in running:
            thread.join()
        return errors

    def test_worker_threads_can_write_through_one_tracer(self):
        """A Tracer built by the scheduler is written to by every node thread.

        sqlite3 connections refuse cross-thread use by default, so the base
        raises ProgrammingError on the first write a worker attempts and the
        run records nothing at all.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "t.db", tmp / "t.jsonl")
            tracer.session_start("runthreads", "test")

            def work(index: int) -> None:
                for _ in range(10):
                    tracer.event(EventRecord(adw_id="runthreads", phase_id=f"p{index}",
                                             type="log", name="x", payload={"i": index}))

            errors = self._hammer(work)
            self.assertEqual(errors, [], "no worker may fail writing to the trace")
            self.assertEqual(
                tracer.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 80,
                "every event a worker wrote must be in the database")

    def test_concurrent_events_do_not_corrupt_the_jsonl_record(self):
        """The JSONL file is the raw record; the database mirrors it.

        Eight threads appending at once must produce eight hundred whole
        lines, because a torn line is a record that no longer parses — and
        the file is the copy that survives a database rebuild.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "t.db", tmp / "t.jsonl")
            tracer.session_start("runjsonl", "test")

            def work(index: int) -> None:
                for _ in range(100):
                    tracer.event(EventRecord(adw_id="runjsonl", phase_id=f"p{index}",
                                             type="log", name="x",
                                             payload={"i": index, "pad": "x" * 200}))

            errors = self._hammer(work)
            lines = (tmp / "t.jsonl").read_text().splitlines()
            self.assertEqual(errors, [])
            self.assertEqual(len(lines), 800)
            for line in lines:
                json.loads(line)          # a torn write raises here

    def test_a_read_modify_write_is_not_interleaved(self):
        """`session_start` reads adw_name, appends to it, and writes it back.

        Two threads joining one session concurrently must not lose a name:
        the read and the write have to be one indivisible step, which is a
        property of the tracer's own serialisation and not of SQLite's.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "t.db", tmp / "t.jsonl")
            tracer.session_start("runjoin", "test")

            errors = self._hammer(
                lambda i: tracer.session_start("runjoin", "test", adw_name=f"adw_{i}"))
            names = tracer.conn.execute(
                "SELECT adw_name FROM sessions WHERE adw_id=?", ("runjoin",)).fetchone()[0]

            self.assertEqual(errors, [])
            self.assertEqual(sorted(names.split(" + ")),
                             sorted(f"adw_{i}" for i in range(8)),
                             "no thread's ADW name may be lost to another's write")


class ConcurrentMigration(unittest.TestCase):
    """The key rebuild is what every existing install will run on first open.

    Each Tracer holds its own connection, so two Tracers contend for SQLite's
    write lock exactly as two processes would — which is the case that matters,
    since nothing stops an operator starting a second ADW while the first is
    still opening the database. A true multi-process test would have to spawn
    interpreters; the lock contention it would exercise is this one.
    """

    def test_four_tracers_opening_one_old_database_rebuild_it_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db = tmp / "legacy.db"
            conn = sqlite3.connect(str(db), isolation_level=None)
            conn.executescript(OLD_AGENT_SESSIONS)
            conn.execute("INSERT INTO agent_sessions (adw_id, agent, coding_agent, model,"
                         " session_id, created_at, last_used_at)"
                         " VALUES ('legacy_run','builder','pi','m','legacy_session','t0','t1')")
            conn.close()

            start = threading.Barrier(4)
            errors: list[str] = []

            def open_it(index: int) -> None:
                start.wait()
                try:
                    Tracer(db, tmp / f"m{index}.jsonl")
                except BaseException as error:        # noqa: BLE001
                    errors.append(f"{type(error).__name__}: {error}")

            threads = [threading.Thread(target=open_it, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [], "opening a database must not fail on contention")

            after = Tracer(db, tmp / "after.jsonl")
            self.assertEqual(
                after.conn.execute("SELECT adw_id, agent, session_id FROM agent_sessions")
                .fetchall(),
                [("legacy_run", "builder", "legacy_session")],
                "the legacy row must survive exactly once, not be duplicated or lost")
            self.assertIn("node_id", {r[1] for r in after.conn.execute(
                "PRAGMA table_info(agent_sessions)")})
            leftovers = after.conn.execute(
                "SELECT name FROM sqlite_master WHERE name='agent_sessions_rebuild'").fetchall()
            self.assertEqual(leftovers, [], "the scratch table must not outlive the rebuild")


class DagAttemptColumns(unittest.TestCase):
    """§12.2 Step 1 item 5 — envelopes and gate results carry the attempt."""

    def _phase(self, adw_id: str = "runenv") -> Phase:
        return Phase(phase_id=f"{adw_id}_build_a1_build", adw_id=adw_id, seq=1,
                     params=_params(), status="running")

    def test_an_envelope_records_the_dag_attempt_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "x.db", tmp / "m.jsonl")
            phase = self._phase()
            tracer.envelope_row(phase, "builder", "GenericOutput", "{}", True, 1,
                                dag_attempt_no=3)
            row = tracer.conn.execute(
                "SELECT attempt, dag_attempt_no FROM envelopes").fetchone()
            self.assertEqual(row, (1, 3),
                             "the gate-retry attempt and the DAG attempt are"
                             " different numbers and must both be recorded")

    def test_a_gate_result_records_the_dag_attempt_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "x.db", tmp / "n.jsonl")
            report = GateReport().check("something", True, "exit 0")
            tracer.gate_row(self._phase(), "a_gate", report, 2, dag_attempt_no=4)
            row = tracer.conn.execute(
                "SELECT attempt, dag_attempt_no FROM gate_results").fetchone()
            self.assertEqual(row, (2, 4))

    def test_a_classic_caller_omitting_the_dag_attempt_still_writes(self):
        """agents.py passes no DAG attempt today; zero means "no DAG node"."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tracer = Tracer(tmp / "x.db", tmp / "o.jsonl")
            tracer.envelope_row(self._phase(), "builder", "GenericOutput", "{}", True, 1)
            tracer.gate_row(self._phase(), "a_gate", GateReport(), 1)
            self.assertEqual(
                tracer.conn.execute("SELECT dag_attempt_no FROM envelopes").fetchone(), (0,))
            self.assertEqual(
                tracer.conn.execute("SELECT dag_attempt_no FROM gate_results").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
