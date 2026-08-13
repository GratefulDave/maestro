"""The authority-tier lifecycle store (§5.3, §7.1, §7.3, §7.8, §7.9, §8.7, §11.2, §11.3).

Owns node state, attempt rows, and transitions — the lifecycle-authority tier
that is read at runtime. It does **not** launch agents, classify failures, or
watch liveness; those belong to the worker body, the retry policy, and the
watchdog, each reading and writing through the surface below.

Every transition is one `BEGIN IMMEDIATE` transaction (§7.9): it writes the
`node_lifecycle` row, appends the `transitions` audit row, and refreshes
`runs.last_transition_at`, all three together, so a crash never leaves the
lifecycle row ahead of its own audit trail. Immediate rather than deferred
because every transition reads a guard and then writes, and a deferred
transaction that upgrades to writer under WAL can hit a busy error the
handler cannot resolve.

`MERGED` and `CANCELLED` are absolutely terminal (§7.3): nothing built here
transitions out of them, ever. `BLOCKED` is operator-terminal: no *automatic*
transition leaves it, but the three escapes (§11.3) can. `UPSTREAM_BLOCKED`
is never stored — `ready_nodes` and `upstream_blocked` are computed fresh from
`dag_nodes`/`node_lifecycle` on every call, via `worktree.upstream_blocked`,
so an operator rescue needs no un-cascade rule anywhere (§8.7).

The run outcome (§7.3) is a record, not a tombstone: `runs` holds only the
*latest* declared outcome and its timestamp; the ordered history of a run
that blocked, was rescued, and finished lives in `transitions`, never
duplicated onto the run row. `total_run_outcome` is a pure, total function —
importable and testable with no database at all — so a grid of state
combinations can prove it never raises and never falls outside the four.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Mapping, Optional, Sequence, Tuple)

from . import scheduler_types as st
from . import worktree as wt
from .utils import ensure_dir, now_iso


# ── errors ───────────────────────────────────────────────────────────────────

class LifecycleError(RuntimeError):
    """Base for every refusal this module raises."""


class UnknownNode(LifecycleError):
    """No lifecycle row exists for `(run_id, node_id)`."""


class RunAlreadyExists(LifecycleError):
    """`create_run` called twice for the same `run_id`."""


class IllegalTransition(LifecycleError):
    """The legal-transition guard refused a move (§7.3)."""


class EscapeRefused(LifecycleError):
    """An escape verb was attempted against a run whose latest outcome does
    not admit it (§7.3, §11.2) — refusing here is what stops an escape from
    racing a scheduler that may still be alive."""


class ResumeRefused(LifecycleError):
    """`run resume` was attempted against ACCEPTED or CANCELLED (§7.3)."""


class SkipAncestryRefused(LifecycleError):
    """`skip --accept-sha` named a SHA that is not an ancestor of HEAD (§11.3).
    Skip does not bypass the ancestry proof."""


# ── schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id             TEXT PRIMARY KEY,
  plan_digest        TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  last_transition_at TEXT NOT NULL,
  latest_outcome     TEXT,              -- NULL = no scheduler ever declared quiescence
  latest_outcome_at  TEXT,
  cancel_requested   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dag_nodes (
  run_id       TEXT NOT NULL REFERENCES runs(run_id),
  node_id      TEXT NOT NULL,
  plan_digest  TEXT NOT NULL,           -- stamped on every row (§7.1)
  kind         TEXT NOT NULL,
  depth        INTEGER NOT NULL,
  needs_json   TEXT NOT NULL,
  outputs_json TEXT NOT NULL,
  specs_json   TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_lifecycle (
  run_id                 TEXT NOT NULL REFERENCES runs(run_id),
  node_id                TEXT NOT NULL,
  state                  TEXT NOT NULL,
  attempt_no             INTEGER NOT NULL DEFAULT 0,
  block_reason           TEXT,
  output_sha             TEXT,
  granted_extra_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at             TEXT NOT NULL,
  PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS attempts (
  run_id      TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  attempt_no  INTEGER NOT NULL,
  base_sha    TEXT NOT NULL,
  state       TEXT NOT NULL,
  started_at  REAL,
  launched_at REAL,
  pid         INTEGER,
  turn_count  INTEGER NOT NULL DEFAULT 0,
  retry_class TEXT,
  extra_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (run_id, node_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS transitions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL,
  node_id      TEXT,               -- NULL for a run-level transition
  kind         TEXT NOT NULL,      -- 'node' | 'run'
  from_state   TEXT,
  to_state     TEXT,
  reason       TEXT NOT NULL,
  actor        TEXT NOT NULL,      -- 'scheduler' | 'operator'
  detail_json  TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);
"""


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Put the database in WAL mode, tolerating a concurrent opener.

    Mirrors tracer.py's `_enable_wal`: switching journal mode takes an
    exclusive lock and is exempt from `busy_timeout`, so two processes
    opening one lifecycle database in the same instant could otherwise have
    one die on the `PRAGMA` before it ran a single statement.
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
            f"could not put the lifecycle database in WAL mode: it is still {mode!r}")


def serialized(method):
    """Run a store method as one indivisible step against the connection.

    One connection is shared across the thread pool's workers (the scheduler
    runs nodes concurrently, §7.2), so every public method is wrapped rather
    than judged individually — the same reasoning tracer.py's `serialized`
    documents. The lock is re-entrant so one guarded method may call another.
    """
    import functools

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


def _epoch_seconds(stamp: Optional[str]) -> float:
    """An ISO transition stamp as epoch seconds, for the backstop's arithmetic.

    Returns 0.0 for a missing or unparseable stamp, which fails *safe* in the
    direction that matters: a zero makes elapsed time enormous, so the
    backstop fires and declares STUCK rather than silently never firing on a
    run whose timestamp it could not read.
    """
    if not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 0.0


def _is_ancestor(repo_path, sha: str, ref: str = "HEAD") -> bool:
    """Whether `sha` is an ancestor of `ref` in the repo at `repo_path` (§11.3).

    This is `skip`'s ancestry proof — the same check §8.6 performs at merge —
    and it is the whole reason `skip` does not bypass correctness: it accepts
    operator-supplied work only when git itself agrees it is already there.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", sha, ref],
        capture_output=True, text=True)
    return result.returncode == 0


# ── the total run outcome function (§7.3) ───────────────────────────────────

@dataclass(frozen=True)
class OutcomeReport:
    """One declared outcome and the evidence behind it (§7.3)."""

    outcome: st.RunOutcome
    blocked_nodes: Tuple[str, ...] = ()
    block_reasons: Mapping[str, st.BlockReason] = field(default_factory=dict)
    abandoned_nodes: Tuple[str, ...] = ()
    acceptance_result: Optional[bool] = None


def total_run_outcome(
    node_states: Sequence[Tuple[str, st.NodeState, Optional[st.BlockReason]]],
    *,
    stuck: bool,
    cancel_requested: bool,
    acceptance_result: Optional[bool],
) -> OutcomeReport:
    """The total function of §7.3 — never raises, never returns a fifth value.

    `stuck` is the §11.2 liveness backstop having fired, passed in as a flag
    because that arm is declared with work still in flight: its domain is the
    run's stopping point, not quiescence. `cancel_requested` is `run cancel`
    having been invoked; it is a separate condition from "every node
    individually CANCELLED" because a run can be stopped deliberately before
    every node has finished reacting to that stop. `BLOCKED` is the residual
    class — the `else` arm — so a combination nobody designed for lands there
    with a report, never outside the set.
    """
    if stuck:
        return OutcomeReport(outcome=st.RunOutcome.STUCK)

    all_cancelled = bool(node_states) and all(
        state is st.NodeState.CANCELLED for _, state, _ in node_states)
    if cancel_requested or all_cancelled:
        cancelled = tuple(nid for nid, state, _ in node_states
                          if state is st.NodeState.CANCELLED)
        return OutcomeReport(outcome=st.RunOutcome.CANCELLED, abandoned_nodes=cancelled)

    merged = [nid for nid, state, _ in node_states if state is st.NodeState.MERGED]
    cancelled = tuple(nid for nid, state, _ in node_states
                      if state is st.NodeState.CANCELLED)
    stragglers = [nid for nid, state, _ in node_states
                  if state not in (st.NodeState.MERGED, st.NodeState.CANCELLED)]
    if merged and not stragglers and acceptance_result is True:
        return OutcomeReport(outcome=st.RunOutcome.ACCEPTED, abandoned_nodes=cancelled)

    blocked = tuple(nid for nid, state, _ in node_states if state is st.NodeState.BLOCKED)
    reasons = {nid: reason for nid, state, reason in node_states
               if state is st.NodeState.BLOCKED and reason is not None}
    return OutcomeReport(outcome=st.RunOutcome.BLOCKED, blocked_nodes=blocked,
                         block_reasons=reasons, abandoned_nodes=cancelled,
                         acceptance_result=acceptance_result)


# ── the legal transition guard (§7.3) ───────────────────────────────────────

def _guard_transition(current: st.NodeState, to_state: st.NodeState, *, actor: str) -> None:
    if current in st.ABSOLUTELY_TERMINAL:
        raise IllegalTransition(
            f"{current.value} is absolutely terminal (§7.3); no transition leaves it, "
            f"including this attempted move to {to_state.value}")
    if (current is st.NodeState.BLOCKED and to_state is not st.NodeState.BLOCKED
            and actor != "operator"):
        raise IllegalTransition(
            "BLOCKED is operator-terminal (§7.3): no automatic transition leaves it, "
            f"only an operator escape may — refusing the move to {to_state.value}")


# ── the store ────────────────────────────────────────────────────────────────

class LifecycleStore:
    """SQLite-backed authority tier: `dag_nodes`, `node_lifecycle`, `attempts`,
    `transitions`, and `runs` (§5.3). Safe to share across a thread pool."""

    def __init__(self, db_path):
        ensure_dir(Path(db_path).parent)
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        # One connection outlives every thread that touches it — `serialized`
        # is what makes sharing it safe (mirrors tracer.py, §7.2).
        self.conn = sqlite3.connect(self.db_path, isolation_level=None,
                                    check_same_thread=False)
        # busy_timeout first, before any statement that can contend with the
        # journal-mode switch (mirrors tracer.py's ordering fix).
        self.conn.execute("PRAGMA busy_timeout=5000;")
        _enable_wal(self.conn)
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(SCHEMA)

    # ── run / plan projection ────────────────────────────────────────────────

    @serialized
    def create_run(self, run_id: str, plan_digest: str,
                    nodes: Sequence[st.PlanNode]) -> None:
        """Project the plan's nodes into `dag_nodes`, seed every node PENDING,
        in one transaction (§7.1)."""
        existing = self.conn.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            raise RunAlreadyExists(f"run {run_id} already exists")
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO runs (run_id, plan_digest, created_at, last_transition_at,"
                " latest_outcome, latest_outcome_at, cancel_requested)"
                " VALUES (?,?,?,?,NULL,NULL,0)",
                (run_id, plan_digest, now, now))
            for node in nodes:
                self.conn.execute(
                    "INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind, depth,"
                    " needs_json, outputs_json, specs_json) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, node.node_id, plan_digest, node.kind.value, node.depth,
                     json.dumps(list(node.needs)), json.dumps(list(node.outputs)),
                     json.dumps(list(node.specs))))
                self.conn.execute(
                    "INSERT INTO node_lifecycle (run_id, node_id, state, attempt_no,"
                    " block_reason, output_sha, granted_extra_attempts, updated_at)"
                    " VALUES (?,?,?,0,NULL,NULL,0,?)",
                    (run_id, node.node_id, st.NodeState.PENDING.value, now))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ── reads ────────────────────────────────────────────────────────────────

    @serialized
    def get_node(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        row = self.conn.execute(
            "SELECT state, attempt_no, block_reason, output_sha, granted_extra_attempts"
            " FROM node_lifecycle WHERE run_id=? AND node_id=?", (run_id, node_id)).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
        state, attempt_no, block_reason, output_sha, granted = row
        return st.NodeLifecycle(
            node_id=node_id, state=st.NodeState(state), attempt_no=attempt_no,
            block_reason=st.BlockReason(block_reason) if block_reason else None,
            output_sha=output_sha, granted_extra_attempts=granted)

    @serialized
    def get_attempt(self, run_id: str, node_id: str, attempt_no: int) -> st.AttemptRecord:
        row = self.conn.execute(
            "SELECT base_sha, state, started_at, launched_at, pid, turn_count,"
            " retry_class, extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        base_sha, state, started_at, launched_at, pid, turn_count, retry_class, extra_json = row
        return st.AttemptRecord(
            run_id=run_id, node_id=node_id, attempt_no=attempt_no, base_sha=base_sha,
            state=st.NodeState(state), started_at=started_at or 0.0, launched_at=launched_at,
            pid=pid, turn_count=turn_count,
            retry_class=st.RetryClass(retry_class) if retry_class else None,
            extra=json.loads(extra_json))

    @serialized
    def last_transition_at(self, run_id: str) -> float:
        """The run row's `last_transition_at`, **as epoch seconds** — §11.2's
        backstop input.

        A lifecycle column, deliberately, and the obvious alternative is
        forbidden: `SELECT MAX(ts) FROM transitions` reads the audit tier,
        which §5.3 says is read at runtime never, and a scheduler mechanism
        consulting it is the first arrow in that section's own list of ways
        this design gets reimported.

        The column is stored as an ISO timestamp because every other audit
        row is, and the backstop measures an elapsed *duration* against `T`.
        Handing it a string would either raise or, worse, compare
        lexicographically and silently never fire. The conversion belongs
        here, at the one reader, rather than in the backstop — which must
        stay ignorant of how the column is spelled.
        """
        row = self.conn.execute(
            "SELECT last_transition_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise LifecycleError(f"no run row for {run_id}")
        return _epoch_seconds(row[0])

    @serialized
    def acceptance_started(self, run_id: str) -> None:
        """Open §8.8's final-acceptance window (§11.2's silence rule).

        Written when the scheduler observes the candidate-ACCEPTED quiescent
        shape, before the ancestry sweep and before any spec or gate runs.
        Nothing transitions between the last node's MERGED and the outcome
        declaration, and that gap is as long as everything acceptance
        executes — so without this refresh the backstop measures a healthy
        final acceptance as silence and declares STUCK. Because resume
        re-runs acceptance against the same quiescent shape, that misfire
        would be a livelock rather than one lost run.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            now = now_iso()
            self.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id))
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,NULL,'acceptance-start','scheduler','{}',?)",
                (run_id, now))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def attempts_for(self, run_id: str, node_id: Optional[str] = None
                      ) -> Tuple[st.AttemptRecord, ...]:
        """Attempt rows, for the counting the retry policy does over them.

        The semantic ceiling is a `COUNT(*)` over rows that already exist
        rather than a counter this store maintains, so what the policy needs
        from here is the rows themselves and nothing more (§7.5).
        """
        sql = ("SELECT run_id, node_id, attempt_no, base_sha, state, started_at,"
               " launched_at, pid, turn_count, retry_class, extra_json"
               " FROM attempts WHERE run_id=?")
        params: Tuple[Any, ...] = (run_id,)
        if node_id is not None:
            sql += " AND node_id=?"
            params = (run_id, node_id)
        return tuple(
            st.AttemptRecord(
                run_id=r[0], node_id=r[1], attempt_no=r[2], base_sha=r[3],
                state=st.NodeState(r[4]), started_at=r[5] or 0.0,
                launched_at=r[6], pid=r[7], turn_count=r[8] or 0,
                retry_class=st.RetryClass(r[9]) if r[9] else None,
                extra=json.loads(r[10]))
            for r in self.conn.execute(sql + " ORDER BY attempt_no", params).fetchall())

    @serialized
    def mark_launched(self, run_id: str, node_id: str, attempt_no: int,
                       pid: Optional[int], launched_at: Optional[float] = None) -> None:
        """Record that the adapter reported the agent launched (§7.6).

        This is what **arms** the process-alive and turn-count signals. Until
        it is written, `AttemptRecord.launched_at` is None and the watchdog
        correctly declines to evaluate either — the attempt window is open but
        no process and no transcript exist yet, so both signals are undefined
        by construction.

        Without this writer the arming condition is never satisfied and the
        watchdog silently degrades to its third signal alone: a node whose
        agent dies immediately would then be detected not at once, as §7.6
        requires, but only when the node wall-clock timeout expires. The
        columns existed for this from the start; nothing wrote them, which is
        the difference between a schema and a mechanism.

        Deliberately not a transition. Launch is an observation about an
        attempt already RUNNING, and writing it as one would refresh
        `last_transition_at` — see `record_heartbeat` for why that matters.
        """
        self.conn.execute(
            "UPDATE attempts SET launched_at=?, pid=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (launched_at if launched_at is not None else time.time(), pid,
             run_id, node_id, attempt_no))

    @serialized
    def record_heartbeat(self, attempt: st.AttemptRecord, turn_count: int,
                          observed_at: float) -> None:
        """Record a watchdog observation on the attempt row (§7.6).

        Deliberately **not** a transition: a heartbeat is not a state change,
        and writing one as a transition would refresh `last_transition_at` and
        silently disarm §11.2's backstop — the run would never look silent
        because a healthy watchdog was chattering into the column the backstop
        measures. That is the failure mode the backstop exists to catch,
        installed by the mechanism meant to help it.
        """
        self.conn.execute(
            "UPDATE attempts SET turn_count=?, launched_at=COALESCE(launched_at, ?)"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (turn_count, attempt.launched_at, attempt.run_id, attempt.node_id,
             attempt.attempt_no))

    def attempts_spent(self, run_id: str, node_id: str,
                        retry_class: st.RetryClass) -> int:
        """How many attempts of this node already failed in this class.

        Counts only rows already classified, which is what keeps §7.5's rule
        that no infra fault produces a budget decrement true of the other
        direction as well: an unclassified row contributes to nothing.
        """
        return sum(1 for a in self.attempts_for(run_id, node_id)
                   if a.retry_class is retry_class)

    def close(self) -> None:
        """Close the shared connection. One connection outlives every thread
        that touched it, so closing is the caller's explicit act rather than
        a per-thread teardown."""
        with self._lock:
            self.conn.close()

    @serialized
    def latest_outcome(self, run_id: str) -> Optional[st.RunOutcome]:
        row = self.conn.execute(
            "SELECT latest_outcome FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return st.RunOutcome(row[0])

    @serialized
    def node_records(self, run_id: str) -> Tuple[wt.NodeRecord, ...]:
        rows = self.conn.execute(
            "SELECT d.node_id, d.depth, d.needs_json, l.state, d.specs_json"
            " FROM dag_nodes d JOIN node_lifecycle l"
            " ON l.run_id = d.run_id AND l.node_id = d.node_id"
            " WHERE d.run_id=?", (run_id,)).fetchall()
        return tuple(
            wt.NodeRecord(node_id=node_id, depth=depth, needs=tuple(json.loads(needs)),
                          state=state, specs=tuple(json.loads(specs)))
            for node_id, depth, needs, state, specs in rows)

    def ready_nodes(self, run_id: str) -> Tuple[str, ...]:
        """Pending nodes whose deps are all MERGED, sorted `(depth, node_id)` (§7.1).
        The predicate is MERGED, never VERIFIED/SUCCEEDED."""
        records = self.node_records(run_id)
        merged = {r.node_id for r in records if r.state == st.NodeState.MERGED.value}
        pending = [r for r in records if r.state == st.NodeState.PENDING.value
                   and all(dep in merged for dep in r.needs)]
        return tuple(r.node_id for r in sorted(pending, key=lambda r: (r.depth, r.node_id)))

    def upstream_blocked(self, run_id: str) -> Tuple[str, ...]:
        """The derived `UPSTREAM_BLOCKED` predicate (§8.7) — never stored, so a
        rescue needs no un-cascade rule. Delegates to worktree.upstream_blocked,
        which already owns this computation."""
        return wt.upstream_blocked(self.node_records(run_id))

    # ── the generic guarded transition (§7.9) ───────────────────────────────

    @serialized
    def _transition_node(
        self, run_id: str, node_id: str, to_state: st.NodeState, *,
        actor: str, reason: str,
        block_reason: Optional[st.BlockReason] = None,
        output_sha: Optional[str] = None,
        new_attempt: bool = False,
        granted_extra_delta: int = 0,
        require_state: Optional[Tuple[st.NodeState, ...]] = None,
        detail: Optional[Mapping[str, Any]] = None,
        extra_writes: Optional[Callable[[st.NodeLifecycle], Sequence[Tuple[str, Tuple]]]] = None,
    ) -> st.NodeLifecycle:
        """Guard, then write the lifecycle row, the audit row, and the run's
        `last_transition_at`, all inside one `BEGIN IMMEDIATE` (§7.9). Any
        caller-supplied `extra_writes` (an attempt-row update, say) runs inside
        the same transaction, so the write set stays atomic."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, attempt_no, block_reason, output_sha, granted_extra_attempts"
                " FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, node_id)).fetchone()
            if row is None:
                raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
            current = st.NodeState(row[0])
            _guard_transition(current, to_state, actor=actor)
            if require_state is not None and current not in require_state:
                raise IllegalTransition(
                    f"{node_id}: expected state in "
                    f"{tuple(s.value for s in require_state)}, found {current.value}")

            new_attempt_no = row[1] + 1 if new_attempt else row[1]
            new_block_reason = block_reason if to_state is st.NodeState.BLOCKED else None
            new_output_sha = output_sha if output_sha is not None else row[3]
            new_granted = row[4] + granted_extra_delta

            # The vocabulary type itself validates the (state, block_reason)
            # pairing (§7.3) — reused rather than re-checked here.
            lifecycle = st.NodeLifecycle(
                node_id=node_id, state=to_state, attempt_no=new_attempt_no,
                block_reason=new_block_reason, output_sha=new_output_sha,
                granted_extra_attempts=new_granted)

            now = now_iso()
            self.conn.execute(
                "UPDATE node_lifecycle SET state=?, attempt_no=?, block_reason=?,"
                " output_sha=?, granted_extra_attempts=?, updated_at=?"
                " WHERE run_id=? AND node_id=?",
                (lifecycle.state.value, lifecycle.attempt_no,
                 lifecycle.block_reason.value if lifecycle.block_reason else None,
                 lifecycle.output_sha, lifecycle.granted_extra_attempts, now,
                 run_id, node_id))
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at) VALUES (?,?,'node',?,?,?,?,?,?)",
                (run_id, node_id, current.value, to_state.value, reason, actor,
                 json.dumps(detail or {}), now))
            self.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id))
            for sql, params in (extra_writes(lifecycle) if extra_writes else ()):
                self.conn.execute(sql, params)
            self.conn.execute("COMMIT")
            return lifecycle
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ── scheduler-driven transitions ────────────────────────────────────────

    def start_attempt(self, run_id: str, node_id: str, base_sha: str) -> int:
        """PENDING -> RUNNING, opening a new attempt row (§7.6's attempt window)."""
        def extra(lifecycle: st.NodeLifecycle):
            return [(
                "INSERT INTO attempts (run_id, node_id, attempt_no, base_sha, state,"
                " started_at, turn_count, extra_json) VALUES (?,?,?,?,?,?,0,'{}')",
                (run_id, node_id, lifecycle.attempt_no, base_sha,
                 st.NodeState.RUNNING.value, time.time()))]
        lifecycle = self._transition_node(
            run_id, node_id, st.NodeState.RUNNING, actor="scheduler", reason="attempt-start",
            new_attempt=True, require_state=(st.NodeState.PENDING,), extra_writes=extra)
        return lifecycle.attempt_no

    def mark_verified(self, run_id: str, node_id: str, output_sha: str) -> st.NodeLifecycle:
        """RUNNING -> VERIFIED (§7.3's four-clause predicate, evaluated by the caller)."""
        def extra(lifecycle: st.NodeLifecycle):
            return [(
                "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                (st.NodeState.VERIFIED.value, run_id, node_id, lifecycle.attempt_no))]
        return self._transition_node(
            run_id, node_id, st.NodeState.VERIFIED, actor="scheduler", reason="verified",
            output_sha=output_sha, require_state=(st.NodeState.RUNNING,), extra_writes=extra)

    def mark_merged(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        """VERIFIED -> MERGED, absolutely terminal from here on (§7.3, §8.6)."""
        return self._transition_node(
            run_id, node_id, st.NodeState.MERGED, actor="scheduler", reason="merged",
            require_state=(st.NodeState.VERIFIED,))

    def mark_blocked(self, run_id: str, node_id: str, reason: st.BlockReason, *,
                      detail: Optional[Mapping[str, Any]] = None,
                      retry_class: Optional[st.RetryClass] = None) -> st.NodeLifecycle:
        """RUNNING or VERIFIED -> BLOCKED with a stored reason (§7.3).

        `UPSTREAM_BLOCKED` is never a valid argument here — it is derived,
        never stored (§8.7).

        VERIFIED is a legal source, and leaving it out was a real gap: §8.7's
        merge conflict blocks a node **at merge time**, and only a VERIFIED
        node is ever eligible to merge, so a conflict could not be recorded at
        all while RUNNING was the only permitted source. The node's work is
        genuinely finished and genuinely unmergeable, which is exactly what
        `MERGE_CONFLICT` says.

        `retry_class` records what the *attempt* failed as, in the same
        transaction, for the case where a node blocks because its budget is
        exhausted. Without it the attempt row carries no reason at all — the
        classification was computed, used to decide the block, and then
        dropped — so `run status` could say the node blocked but not what its
        last attempt actually did.
        """
        def extra(lifecycle: st.NodeLifecycle):
            writes = [(
                "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                (st.NodeState.BLOCKED.value, run_id, node_id, lifecycle.attempt_no))]
            if retry_class is not None:
                writes.append((
                    "UPDATE attempts SET retry_class=? WHERE run_id=? AND node_id=?"
                    " AND attempt_no=?",
                    (retry_class.value, run_id, node_id, lifecycle.attempt_no)))
            return writes
        return self._transition_node(
            run_id, node_id, st.NodeState.BLOCKED, actor="scheduler",
            reason=f"blocked:{reason.value}", block_reason=reason,
            require_state=(st.NodeState.RUNNING, st.NodeState.VERIFIED),
            detail=detail, extra_writes=extra)

    def fail_attempt(self, run_id: str, node_id: str,
                      retry_class: st.RetryClass) -> st.NodeLifecycle:
        """RUNNING -> PENDING: an ENVIRONMENTAL/LAUNCHER_TRANSIENT failure that
        earns another attempt automatically (§7.5) — not an operator escape."""
        def extra(lifecycle: st.NodeLifecycle):
            return [(
                "UPDATE attempts SET retry_class=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                (retry_class.value, run_id, node_id, lifecycle.attempt_no))]
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="scheduler",
            reason=f"retry:{retry_class.value}", require_state=(st.NodeState.RUNNING,),
            extra_writes=extra)

    # ── run-level: cancellation, outcome, resume ────────────────────────────

    @serialized
    def cancel_run(self, run_id: str) -> Tuple[str, ...]:
        """Write CANCELLED for every non-terminal node in ONE transaction (§7.8).
        Never blocks on a kill — that is the adapter's problem, not this store's."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT node_id, state FROM node_lifecycle WHERE run_id=?",
                (run_id,)).fetchall()
            now = now_iso()
            cancelled = []
            for node_id, state in rows:
                current = st.NodeState(state)
                if current in st.ABSOLUTELY_TERMINAL:
                    continue
                self.conn.execute(
                    "UPDATE node_lifecycle SET state=?, block_reason=NULL, updated_at=?"
                    " WHERE run_id=? AND node_id=?",
                    (st.NodeState.CANCELLED.value, now, run_id, node_id))
                self.conn.execute(
                    "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                    " reason, actor, detail_json, created_at)"
                    " VALUES (?,?,'node',?,?, 'run-cancel','operator','{}',?)",
                    (run_id, node_id, current.value, st.NodeState.CANCELLED.value, now))
                cancelled.append(node_id)
            self.conn.execute(
                "UPDATE runs SET last_transition_at=?, cancel_requested=1 WHERE run_id=?",
                (now, run_id))
            self.conn.execute("COMMIT")
            return tuple(cancelled)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def declare_outcome(self, run_id: str, *, stuck: bool = False,
                         acceptance_result: Optional[bool] = None) -> OutcomeReport:
        """Compute and record the run's outcome (§7.3) — a record, not a
        tombstone: `runs` keeps only the latest value; the ordered history is
        the `transitions` row this same transaction appends."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run_row = self.conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownNode(f"run {run_id} does not exist")
            rows = self.conn.execute(
                "SELECT node_id, state, block_reason FROM node_lifecycle WHERE run_id=?",
                (run_id,)).fetchall()
            node_states = [
                (node_id, st.NodeState(state), st.BlockReason(reason) if reason else None)
                for node_id, state, reason in rows]
            report = total_run_outcome(
                node_states, stuck=stuck, cancel_requested=bool(run_row[0]),
                acceptance_result=acceptance_result)
            now = now_iso()
            self.conn.execute(
                "UPDATE runs SET latest_outcome=?, latest_outcome_at=?, last_transition_at=?"
                " WHERE run_id=?", (report.outcome.value, now, now, run_id))
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,?, 'declare-outcome','scheduler',?,?)",
                (run_id, report.outcome.value,
                 json.dumps({"blocked_nodes": list(report.blocked_nodes),
                            "abandoned_nodes": list(report.abandoned_nodes),
                            "acceptance_result": report.acceptance_result}), now))
            self.conn.execute("COMMIT")
            return report
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def _write_resume_transition(self, run_id: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            now = now_iso()
            self.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id))
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,NULL,'resume','operator','{}',?)",
                (run_id, now))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def _running_node_ids(self, run_id: str) -> Tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT node_id FROM node_lifecycle WHERE run_id=? AND state=?",
            (run_id, st.NodeState.RUNNING.value)).fetchall()
        return tuple(r[0] for r in rows)

    def resume_run(self, run_id: str) -> Tuple[str, ...]:
        """Legal against BLOCKED, STUCK, and NULL; refused against ACCEPTED and
        CANCELLED (§7.3). Writes the resume transition — refreshing
        `last_transition_at` — BEFORE touching any inherited RUNNING attempt,
        so the backstop measures silence from the resume, not from the dead
        run's last act (§7.8, §11.2). Every inherited RUNNING attempt is
        treated as ENVIRONMENTAL-failed and re-launched, never adopted."""
        outcome = self.latest_outcome(run_id)
        if outcome in (st.RunOutcome.ACCEPTED, st.RunOutcome.CANCELLED):
            raise ResumeRefused(
                f"{run_id}: resume is refused against a declared "
                f"{outcome.value} run (§7.3) — it is not reopenable")
        self._write_resume_transition(run_id)
        reclaimed = []
        for node_id in self._running_node_ids(run_id):
            self.fail_attempt(run_id, node_id, st.RetryClass.ENVIRONMENTAL)
            reclaimed.append(node_id)
        return tuple(reclaimed)

    # ── operator escapes (§11.3) ────────────────────────────────────────────

    def _require_escape_legal(self, run_id: str) -> None:
        outcome = self.latest_outcome(run_id)
        if outcome not in (st.RunOutcome.BLOCKED, st.RunOutcome.STUCK):
            raise EscapeRefused(
                f"{run_id}: escapes are legal only against a run declared BLOCKED or "
                f"STUCK (§7.3, §11.2); latest outcome is "
                f"{outcome.value if outcome else 'NULL'} — an escape against an "
                "undeclared run would race a scheduler that may still be alive")

    def retry(self, run_id: str, node_id: str, *, force: bool = False) -> st.NodeLifecycle:
        """BLOCKED -> PENDING. `force` grants exactly one extra attempt beyond
        the semantic ceiling, never raising the cap itself (§7.5, §11.3)."""
        self._require_escape_legal(run_id)
        reason = st.Escape.RETRY_FORCE.value if force else st.Escape.RETRY.value
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="operator", reason=reason,
            require_state=(st.NodeState.BLOCKED,), granted_extra_delta=1 if force else 0)

    def skip(self, run_id: str, node_id: str, *, accept_sha: str, repo_path) -> st.NodeLifecycle:
        """BLOCKED -> MERGED: the operator supplied the work by hand. Verifies
        `git merge-base --is-ancestor` before accepting — it does not bypass
        the ancestry proof (§11.3)."""
        self._require_escape_legal(run_id)
        if not _is_ancestor(repo_path, accept_sha):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not an ancestor of HEAD in {repo_path}; "
                "skip does not bypass the ancestry proof (§11.3)")
        return self._transition_node(
            run_id, node_id, st.NodeState.MERGED, actor="operator",
            reason=st.Escape.SKIP.value, require_state=(st.NodeState.BLOCKED,),
            output_sha=accept_sha)

    def abandon(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        """Any non-absolutely-terminal state -> CANCELLED (§7.3, §7.8, §11.3).
        Absolutely terminal from here on; descendants become derived-unready
        (§8.7) with no state written for them at all."""
        self._require_escape_legal(run_id)
        return self._transition_node(
            run_id, node_id, st.NodeState.CANCELLED, actor="operator",
            reason=st.Escape.ABANDON.value)
