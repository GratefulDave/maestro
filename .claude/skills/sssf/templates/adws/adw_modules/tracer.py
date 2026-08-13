"""Tracer: every event lands in JSONL and SQLite AS IT HAPPENS.

Files are the raw record; sssf.db is the queryable mirror the UI polls.
No push transport — the flow is always: agents -> sqlite -> web ui.
WAL mode so the UI can read while ADW processes write.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from pathlib import Path

from .data_types import AgentConfig, EventRecord, GateReport, Phase
from .utils import ensure_dir, new_id, now_iso


def serialized(method):
    """Run a tracer method as one indivisible step against the connection.

    Every public method below is wrapped, rather than only the ones that
    obviously need it, because "this one is a single statement so SQLite
    already serialises it" is a claim about how the local SQLite was compiled
    — `sqlite3.threadsafety` is 3 only for a serialised build — and about how
    the method is written today. Several are not single statements now:
    `session_start` reads a session's ADW names and writes them back, `event`
    appends to the JSONL file and then inserts, and the key rebuild runs an
    explicit transaction that a second thread's insert would otherwise join
    and have rolled back with it. One rule that holds for all of them is
    cheaper to keep true than a per-method judgement that a later edit
    silently invalidates.

    The lock is re-entrant so a method may call another, which
    `session_finish` does.
    """
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


# One definition of the agent-session table, rendered twice: once for a fresh
# database and once under a temporary name when an older database's primary key
# is rebuilt (_rebuild_agent_sessions_key). Writing it out twice is how the two
# copies drift apart.
AGENT_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  adw_id        TEXT REFERENCES sessions,
  agent         TEXT,
  node_id       TEXT NOT NULL DEFAULT '',   -- '' = no DAG node, i.e. a classic ADW
  dag_attempt_no INTEGER NOT NULL DEFAULT 0,-- 0 = not a DAG attempt
  coding_agent  TEXT, model TEXT, color TEXT,
  session_id    TEXT,
  context_tokens INTEGER,           -- window occupancy after the agent's last turn
  context_window INTEGER,           -- the model's ceiling; 0/NULL = unknown
  created_at    TEXT, last_used_at TEXT,
  -- Node and attempt are in the key because two concurrent nodes on one
  -- roster role would otherwise write one row: the second node's session id
  -- replaces the first's and both resume one coding-agent session.
  PRIMARY KEY (adw_id, agent, node_id, dag_attempt_no)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  adw_id        TEXT PRIMARY KEY,
  adw_name      TEXT,                -- ADW script(s) run, e.g. "adw_plan + adw_build_test"
  request       TEXT,
  status        TEXT,
  engineer      TEXT,
  started_at    TEXT, ended_at TEXT,
  total_tokens  INTEGER DEFAULT 0, total_cost REAL DEFAULT 0,
  archived      INTEGER DEFAULT 0   -- review triage, set by the UI; never by a run
);
CREATE TABLE IF NOT EXISTS phases (
  phase_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  seq           INTEGER,
  name TEXT, kind TEXT, owner TEXT, description TEXT,
  status        TEXT DEFAULT 'fail',
  attempt       INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
  error         TEXT,
  started_at    TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  parent_id     TEXT,
  type          TEXT,
  name          TEXT,
  payload_json  TEXT,
  tokens        INTEGER,
  started_at    TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS envelopes (
  envelope_id   TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  agent         TEXT,
  output_type   TEXT,
  payload_json  TEXT,
  valid         INTEGER,
  attempt       INTEGER,             -- the gate/JSON retry within one phase
  dag_attempt_no INTEGER DEFAULT 0,  -- the DAG node's attempt; 0 = not a DAG run
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  attempt       INTEGER,             -- the gate retry within one phase
  dag_attempt_no INTEGER DEFAULT 0,  -- the DAG node's attempt; 0 = not a DAG run
  gate          TEXT,
  passed        INTEGER,
  violations_json TEXT,
  checks_json   TEXT,               -- [{item, ok, note}] — WHAT the gate verified
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS processes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  kind          TEXT,                -- 'adw' (the workflow process) | 'agent' (a coding-agent child)
  name          TEXT,                -- '' for the adw, the agent name for a child
  pid           INTEGER,
  command       TEXT,                -- what the pid was, so a recycled pid is not killed by mistake
  started_at    TEXT, ended_at TEXT  -- ended_at NULL = believed alive
);
""" + AGENT_SESSIONS_DDL.format(table="agent_sessions")

# Columns added after a schema shipped. CREATE TABLE IF NOT EXISTS never
# revisits an existing table, so additive changes need an explicit ALTER.
MIGRATIONS = [("agent_sessions", "color", "TEXT"),
              ("gate_results", "checks_json", "TEXT"),
              ("sessions", "adw_name", "TEXT"),
              ("agent_sessions", "context_tokens", "INTEGER"),
              ("agent_sessions", "context_window", "INTEGER"),
              ("sessions", "archived", "INTEGER DEFAULT 0"),
              ("envelopes", "dag_attempt_no", "INTEGER DEFAULT 0"),
              ("gate_results", "dag_attempt_no", "INTEGER DEFAULT 0")]

# The columns the key rebuild carries across, when the old table has them. A
# database old enough to predate `color` is read for the columns it actually
# has, not for the ones this version ships.
AGENT_SESSION_CARRIED = ("coding_agent", "model", "color", "session_id",
                         "context_tokens", "context_window",
                         "created_at", "last_used_at")


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Put the database in WAL mode, tolerating a concurrent opener.

    Switching journal mode needs an exclusive lock, and SQLite answers
    SQLITE_BUSY for that one statement *without* consulting busy_timeout — the
    pragma is exempt from it. Two ADW processes opening one database in the
    same instant therefore had one of them die on `PRAGMA journal_mode=WAL`
    with "database is locked" before it had run anything at all. This was
    always true of the base; it only became reachable when a DAG started
    opening the trace from more than one place (§7.2).

    Losing the race is not an error, because the mode is a property of the
    file rather than of the connection: whoever wins sets it permanently, and
    the loser only has to wait for that to land. So the mode is read first and
    the write skipped when it is already WAL, and a busy answer is retried
    briefly before being believed. It is an error only if the retries run out
    and the database is still not in WAL, which would mean nobody set it.
    """
    for _ in range(attempts):
        current = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if str(current).lower() == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) and "busy" not in str(error).lower():
                raise
            time.sleep(0.01)
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    if str(mode).lower() != "wal":
        raise sqlite3.OperationalError(
            f"could not put the trace database in WAL mode: it is still {mode!r}")


class Tracer:
    def __init__(self, db_path: str | Path, events_jsonl: str | Path):
        ensure_dir(Path(db_path).parent)
        self.db_path = str(db_path)
        self.events_jsonl = Path(events_jsonl)
        ensure_dir(self.events_jsonl.parent)
        # A DAG runs its nodes on a thread pool (§7.2) and they share the run's
        # tracer, so the connection must outlive the thread that opened it —
        # sqlite3 otherwise raises ProgrammingError on the first write a worker
        # attempts, and the run records nothing at all. Turning the check off
        # moves the obligation here: `serialized` is what makes the sharing
        # safe, and one is not correct without the other.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, isolation_level=None,
                                    check_same_thread=False)
        # busy_timeout comes FIRST, before any statement that can contend.
        # Switching journal mode takes an exclusive lock, and `executescript`
        # and the key rebuild below both write; with the default timeout of
        # zero, a second connection opening the same database at the same
        # moment does not wait for the lock, it fails immediately with
        # "database is locked". The base set this pragma third, which was
        # invisible while one process opened the db at a time and is not once
        # a DAG runs nodes concurrently (§7.2).
        self.conn.execute("PRAGMA busy_timeout=5000;")
        _enable_wal(self.conn)
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(SCHEMA)
        self._migrate()

    @serialized
    def _migrate(self) -> None:
        """Bring an older db up to this schema: columns first, then the key.

        Order matters. The key rebuild below copies every column of the old
        agent_sessions table across, so the columns earlier versions added by
        ALTER have to be there before it runs — otherwise a database old
        enough to predate `color` is copied by reading a column it does not
        have.
        """
        for table, column, decl in MIGRATIONS:
            columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._rebuild_agent_sessions_key()

    def _agent_sessions_columns(self) -> set[str]:
        return {row[1] for row in self.conn.execute("PRAGMA table_info(agent_sessions)")}

    @serialized
    def _rebuild_agent_sessions_key(self) -> None:
        """Widen agent_sessions' primary key to include node and attempt.

        `PRIMARY KEY (adw_id, agent)` was written when a run was one script:
        one agent per roster role, one session, one row. Under a DAG, two
        concurrent nodes can hold the same role, and they then write the same
        key — the second node's session id replaces the first's, both nodes
        resume the same coding-agent session, and two live agents append to
        one context window. Nothing raises; the row simply has one value where
        two are needed.

        SQLite cannot widen a primary key with ALTER TABLE, which is all the
        additive mechanism above can do, so the table is rebuilt: create,
        copy, drop, rename, in one transaction so a crash leaves either the
        old table or the new one and never half of each. Existing rows are
        copied into the identity a run with no node still writes ('' and 0),
        which is exactly what those rows meant. The guard is the presence of
        the new column rather than a version number, so the rebuild happens
        once and every later open — including a second Tracer on the same
        file — returns immediately.
        """
        if self._agent_sessions_columns().issuperset({"node_id"}):
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Read the schema again now that this connection holds the write
            # lock: two processes opening one database can both have seen the
            # old table before either began, and the loser must not rebuild a
            # table the winner already rebuilt.
            columns = self._agent_sessions_columns()
            if "node_id" in columns:
                self.conn.execute("COMMIT")
                return
            carried = ", ".join(c for c in AGENT_SESSION_CARRIED if c in columns)
            self.conn.execute("DROP TABLE IF EXISTS agent_sessions_rebuild")
            self.conn.execute(AGENT_SESSIONS_DDL.format(table="agent_sessions_rebuild"))
            self.conn.execute(
                "INSERT INTO agent_sessions_rebuild"
                f" (adw_id, agent, node_id, dag_attempt_no, {carried})"
                f" SELECT adw_id, agent, '', 0, {carried} FROM agent_sessions")
            self.conn.execute("DROP TABLE agent_sessions")
            self.conn.execute("ALTER TABLE agent_sessions_rebuild RENAME TO agent_sessions")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ── events ──────────────────────────────────────────────────────────────
    @serialized
    def event(self, record: EventRecord) -> str:
        event_id = f"evt_{new_id(12)}"
        ts = now_iso()
        line = {"event_id": event_id, "ts": ts, **record.model_dump()}
        with self.events_jsonl.open("a") as f:
            f.write(json.dumps(line) + "\n")
        self.conn.execute(
            "INSERT INTO events (event_id, adw_id, phase_id, parent_id, type, name,"
            " payload_json, tokens, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, record.adw_id, record.phase_id, record.parent_id, record.type,
             record.name, json.dumps(record.payload), record.tokens,
             record.started_at or ts, record.ended_at),
        )
        return event_id

    # ── sessions ────────────────────────────────────────────────────────────
    @serialized
    def session_start(self, adw_id: str, engineer: str, adw_name: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO sessions (adw_id, status, engineer, started_at) VALUES (?,?,?,?) "
            "ON CONFLICT(adw_id) DO UPDATE SET status='running'",
            (adw_id, "running", engineer, now_iso()),
        )
        if not adw_name:
            return
        # A joined session chains ADWs — record each distinct one, in run order.
        row = self.conn.execute("SELECT adw_name FROM sessions WHERE adw_id=?",
                                (adw_id,)).fetchone()
        names = row[0].split(" + ") if row and row[0] else []
        if adw_name not in names:
            names.append(adw_name)
            self.conn.execute("UPDATE sessions SET adw_name=? WHERE adw_id=?",
                              (" + ".join(names), adw_id))

    @serialized
    def session_request(self, adw_id: str, request: str) -> None:
        self.conn.execute("UPDATE sessions SET request=? WHERE adw_id=?",
                          (request[:500], adw_id))

    @serialized
    def session_finish(self, adw_id: str, ok: bool) -> None:
        self.conn.execute(
            "UPDATE sessions SET status=?, ended_at=? WHERE adw_id=?",
            ("success" if ok else "fail", now_iso(), adw_id),
        )
        self.processes_end_all(adw_id)   # nothing of this run is alive any more

    @serialized
    def session_add_usage(self, adw_id: str, tokens: int, cost: float) -> None:
        self.conn.execute(
            "UPDATE sessions SET total_tokens=total_tokens+?, total_cost=total_cost+? WHERE adw_id=?",
            (tokens, cost, adw_id),
        )

    # ── processes (adw_id → pid, so a hung run can be found and killed) ─────
    @serialized
    def process_start(self, adw_id: str, kind: str, name: str, pid: int,
                      command: str) -> None:
        """Record a live process for this run.

        A coding agent that hangs produces no events at all, which is exactly
        when you need its pid — and `ps` cannot tell you which adw_id it
        belongs to. Writing it here makes the trace the answer to "what is this
        run running, and how do I stop it".
        """
        self.conn.execute(
            "INSERT INTO processes (adw_id, kind, name, pid, command, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (adw_id, kind, name, pid, command[:500], now_iso()),
        )

    @serialized
    def process_end(self, adw_id: str, pid: int) -> None:
        """Mark the newest live row for this pid as finished."""
        self.conn.execute(
            "UPDATE processes SET ended_at=? WHERE id = ("
            "  SELECT id FROM processes WHERE adw_id=? AND pid=? AND ended_at IS NULL"
            "  ORDER BY id DESC LIMIT 1)",
            (now_iso(), adw_id, pid),
        )

    @serialized
    def processes_end_all(self, adw_id: str) -> None:
        """Close out every live row for a run — called when the session ends."""
        self.conn.execute(
            "UPDATE processes SET ended_at=? WHERE adw_id=? AND ended_at IS NULL",
            (now_iso(), adw_id),
        )

    # ── phases ──────────────────────────────────────────────────────────────
    @serialized
    def max_phase_seq(self, adw_id: str) -> int:
        """Highest seq already recorded for this session; 0 when it is new.

        A joined run continues the sequence instead of restarting at 1 — which
        would collide with the first run's phases on both `seq` (breaking
        ordering) and `phase_id` (silently overwriting a row through the
        phase_upsert conflict clause).

        The maximum is read, not reserved, so two callers racing it get the
        same number. That is why a phase id is no longer built from it unless
        the run has no node to be named after (`Run.phase_id`): for a node,
        seq is the ordering key and nothing else, and two nodes sharing an
        ordinal is untidy rather than lossy.
        """
        row = self.conn.execute("SELECT MAX(seq) FROM phases WHERE adw_id = ?",
                                (adw_id,)).fetchone()
        return row[0] if row and row[0] is not None else 0

    @serialized
    def phase_upsert(self, phase: Phase) -> None:
        p = phase.params
        self.conn.execute(
            "INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, description,"
            " status, attempt, retries, error, started_at, ended_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(phase_id) DO UPDATE SET status=excluded.status,"
            " attempt=excluded.attempt, error=excluded.error, ended_at=excluded.ended_at",
            (phase.phase_id, phase.adw_id, phase.seq, p.name, p.kind, p.owner,
             p.description, phase.status, phase.attempt, p.retries, phase.error,
             phase.started_at, phase.ended_at),
        )

    # ── envelopes / gates / agent sessions ──────────────────────────────────
    @serialized
    def envelope_row(self, phase: Phase, agent: str, output_type: str,
                     payload_json: str, valid: bool, attempt: int,
                     dag_attempt_no: int = 0) -> None:
        """`attempt` and `dag_attempt_no` count different things, on purpose.

        `attempt` is the retry inside this phase — a re-ask for valid JSON or
        a gate correction sent into the same session. `dag_attempt_no` is the
        scheduler's attempt at the whole node, a new worktree and a new agent.
        Reading one as the other would say a node that failed twice tried
        once. Zero means the writer is not a DAG node at all.
        """
        self.conn.execute(
            "INSERT INTO envelopes (envelope_id, adw_id, phase_id, agent, output_type,"
            " payload_json, valid, attempt, dag_attempt_no, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"env_{new_id(12)}", phase.adw_id, phase.phase_id, agent, output_type,
             payload_json, int(valid), attempt, dag_attempt_no, now_iso()),
        )

    @serialized
    def gate_row(self, phase: Phase, gate: str, report: GateReport, attempt: int,
                 dag_attempt_no: int = 0) -> None:
        """The report carries both the verdict and the evidence behind it.

        `dag_attempt_no` is the scheduler's attempt at the node, distinct from
        the in-phase gate retry `attempt` counts; a node's verification is
        read per attempt, so a gate result that cannot say which attempt it
        judged cannot answer the question it exists for.
        """
        self.conn.execute(
            "INSERT INTO gate_results (adw_id, phase_id, attempt, dag_attempt_no, gate,"
            " passed, violations_json, checks_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (phase.adw_id, phase.phase_id, attempt, dag_attempt_no, gate,
             int(report.passed), json.dumps(report.violations),
             json.dumps([c.model_dump() for c in report.checks]), now_iso()),
        )

    @serialized
    def agent_session_row(self, adw_id: str, agent: AgentConfig, session_id: str,
                          context_tokens: int = 0, context_window: int = 0,
                          node_id: str = "", dag_attempt_no: int = 0) -> None:
        """The agent's config row is the source of truth for its label and color.

        Context is carried here rather than derived from events because the lane
        wants one number per agent — the latest — and a session that runs the
        same agent twice overwrites it, exactly like model and session_id.

        "The same agent" now means the same agent in the same node's attempt.
        A run with no node keeps writing one row per agent, which is what the
        visualizer's lanes are built from; a DAG node owns its own row, so a
        sibling node on the same roster role cannot overwrite the session id
        this one is going to resume.
        """
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO agent_sessions (adw_id, agent, node_id, dag_attempt_no,"
            " coding_agent, model, color, session_id, context_tokens, context_window,"
            " created_at, last_used_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(adw_id, agent, node_id, dag_attempt_no) DO UPDATE SET"
            " model=excluded.model,"
            " color=excluded.color, session_id=excluded.session_id,"
            " context_tokens=excluded.context_tokens,"
            " context_window=excluded.context_window,"
            " last_used_at=excluded.last_used_at",
            (adw_id, agent.name, node_id, dag_attempt_no, agent.coding_agent,
             agent.model, agent.color, session_id, context_tokens, context_window,
             ts, ts),
        )
