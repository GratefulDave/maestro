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

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from . import retry_policy as rp
from . import scheduler_types as st
from . import watchdog as wd
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

class SchedulerStillAlive(EscapeRefused):
    """An escape against RUNNING was refused because the scheduler pid is
    still a process. The race the node-state gate was standing in for."""

    refusal = st.EscapeRefusal.SCHEDULER_STILL_ALIVE


class SchedulerLivenessUnknown(EscapeRefused):
    """An escape against RUNNING was refused because liveness cannot be
    said. Fail closed: unknown is not dead."""

    refusal = st.EscapeRefusal.SCHEDULER_LIVENESS_UNKNOWN


class ResumeRefused(LifecycleError):
    """`run resume` was attempted against ACCEPTED or CANCELLED (§7.3)."""


class SkipAncestryRefused(LifecycleError):
    """`skip --accept-sha` could not prove the identity it was handed (§11.3).

    Named for the ancestry proof because that is the check operators reach for
    it with, but it is the refusal channel for every identity check `skip`
    makes and always has been: no attempt base, no checked-out branch, a SHA
    that is not the current HEAD, an unclean worktree, and — since #78 — a SHA
    that is not a full object digest. Skip bypasses none of them. The *message*
    is what distinguishes them, which is why a check whose message described a
    different failure was a defect worth fixing rather than a wording nit.
    """


class BaselineUnrecorded(LifecycleError):
    """No measurement baseline was ever recorded for this attempt.

    The distinguishing case: an attempt row written before the baseline was
    persisted at all. Nothing may reconstruct the baseline from the attempt's
    base commit to cover the gap — `git ls-tree` sees tracked paths only, and
    the baseline deliberately includes provisioned untracked ones, so the
    reconstruction reports every provisioned path as work the attempt added
    (§1.1 item 4).
    """


class BaselineCorrupt(LifecycleError):
    """A baseline exists but does not verify against its recorded digest."""


class LedgerUnavailable(LifecycleError):
    """A read verb was pointed at a lifecycle database that does not exist.

    Distinct from every other error here because it is the *expected* answer
    to `run status` before a first run: an operator asking to read a ledger
    that was never written must be told so, and must not have one created for
    them as a side effect of asking.
    """


# ── schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id             TEXT PRIMARY KEY,
  plan_digest        TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  last_transition_at TEXT NOT NULL,
  latest_outcome     TEXT,              -- NULL = no scheduler ever declared quiescence
  latest_outcome_at  TEXT,
  cancel_requested   INTEGER NOT NULL DEFAULT 0,
  -- Why the latest declared outcome was CANCELLED (`st.CancelCause`), NULL
  -- for every other outcome. An attribute of `latest_outcome`, rewritten in
  -- the same transaction, never a second copy of `cancel_requested`: that
  -- column is the live request an operator made, this one is the cause the
  -- scheduler recorded, and a resume clears the first while leaving the
  -- second standing as the record of the outcome it is superseding.
  cancel_cause       TEXT,
  -- The scheduler process that last took ownership of this run. Written when a
  -- process projects the plan or resumes the run, never cleared: a pid that is
  -- no longer running is the structural fact that says the run has no owner,
  -- and clearing it on a clean exit would delete the evidence for the case
  -- that matters -- the process that did *not* exit cleanly.
  scheduler_pid       INTEGER,
  scheduler_host      TEXT,
  scheduler_claimed_at TEXT,
  -- The start time of that process, as epoch seconds at whatever resolution
  -- the platform records it. A pid is not an identity: the kernel reuses it,
  -- and a later occupant of the same number reads as "alive" to any check
  -- that asks only whether the pid exists. Recorded here so identity can be
  -- *proven* by comparing this against the live process's start time, which
  -- is what separates authority to signal a process from a guess that it is
  -- the same one (#37). NULL on a ledger written before this column, and on
  -- a platform that cannot answer -- both read as unproven, never as proven.
  scheduler_start_epoch REAL
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
  -- Why this node is CANCELLED (`st.CancelCause`), NULL in every other state.
  -- The node-level twin of `runs.cancel_cause`, and not a convenience: a run
  -- an operator cancelled after abandoning one lane holds both causes at
  -- once, and a resume that reopened every CANCELLED node would resurrect the
  -- lane the operator gave up on. The column is what lets the resume reopen
  -- exactly the nodes the stop request took, and nothing else.
  cancel_cause           TEXT,
  -- How this node reached MERGED (`st.MergeCause`), NULL in every other
  -- state. The twin of `cancel_cause` one state along, and it exists for the
  -- same reason: `MERGED` was one word carrying two facts, and the only way
  -- to tell them apart was to read the integration branch's git log, where a
  -- run-merged lane leaves a merge commit and an operator-accepted one
  -- leaves only the attempt commit (#93). NULL on a MERGED row written
  -- before this column means *unrecorded*, never `SCHEDULER`: the migration
  -- invents no facts, and reading an unrecorded merge as a run-merged one
  -- would have every older row assert an evidence chain nobody checked.
  merge_cause            TEXT,
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
CREATE TABLE IF NOT EXISTS attempt_baselines (
  run_id         TEXT NOT NULL,
  node_id        TEXT NOT NULL,
  attempt_no     INTEGER NOT NULL,
  -- sha256 over the canonical serialization of `inventory_json`. Duplicated
  -- into `attempts.extra_json` so the attempt row itself binds the baseline
  -- it was measured against: editing this table without editing that row is
  -- a detectable mismatch rather than a silent substitution.
  digest         TEXT NOT NULL,
  -- The provisioned baseline inventory as {relpath: "<mode> <blob>"}. Its own
  -- table rather than a key in `attempts.extra_json` because the attempt row
  -- is read on every watchdog poll and every heartbeat, and a repo-sized
  -- inventory parsed on each of those is a cost the row does not otherwise
  -- carry.
  inventory_json TEXT NOT NULL,
  -- The gitignored files present at `take_baseline`, as {relpath: sha256}.
  -- A *disjoint* universe from `inventory_json` by construction: the
  -- inventory's universe is `git ls-files --cached --others
  -- --exclude-standard`, and this holds exactly what that excludes, so the
  -- baseline can never answer a question about it (§8.3).
  --
  -- Nullable, and the difference between NULL and '{}' is load-bearing.
  -- '{}' says the tree had no ignored files when the bracket opened. NULL
  -- says nobody looked -- an attempt written before this column existed --
  -- and a reader must say "unknown" rather than "none", because reading a
  -- missing before-side as empty attributes a whole provisioned dependency
  -- tree to the node, which is the false positive `existing_ignored_outputs`
  -- exists to avoid.
  ignored_json   TEXT,
  recorded_at    TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  attempt_no    INTEGER NOT NULL,
  subject_sha   TEXT NOT NULL,
  payload_json  TEXT NOT NULL,     -- non-null by §7.7: the payload is the row
  adjudication  TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orphans (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,
  node_id     TEXT,
  attempt_no  INTEGER,
  pid         INTEGER,
  handle      TEXT,
  reason      TEXT,
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_attempt_per_node
  ON attempts(run_id, node_id) WHERE state='RUNNING';
CREATE INDEX IF NOT EXISTS idx_ready_set
  ON node_lifecycle(run_id, state);
"""

#: §10.3's partial unique index — "at most one live attempt per node as a
#: declarative constraint that releases automatically when status changes".
#: Partial, so it constrains live rows only and never caps a node's history.
LIVE_ATTEMPT_INDEX = "idx_one_live_attempt_per_node"

#: §10.3's "one index on the ready-set query". The ready-set *predicate* — every
#: dependency MERGED — is computed in Python over the projection rather than in
#: SQL, so what an index can serve is the state-filtered lifecycle read that the
#: ready-set computation, resume, and cancellation all issue.
READY_SET_INDEX = "idx_ready_set"

#: The state an attempt row takes when its attempt ends without a verdict about
#: the work: an environmental retry, a cancellation, an abandon. Chosen from the
#: existing vocabulary rather than adding a seventh state, and it matters that
#: the row stops reading RUNNING for three separate reasons — the partial unique
#: index above must release, §7.6's watchdog must stop polling a dead attempt,
#: and §7.7 adjudicates a late arrival against `attempts.state`, so a row left
#: RUNNING would ACCEPT a result from an attempt nobody is waiting for.
CLOSED_ATTEMPT_STATE = st.NodeState.CANCELLED


#: Columns added to `runs` after the first ledgers were written. `SCHEMA` uses
#: `CREATE TABLE IF NOT EXISTS`, so it is inert against a database that already
#: has the table: a ledger created before these columns existed keeps its old
#: shape forever unless something adds them. Declared as a list rather than
#: inlined so the migration and the schema cannot drift into two answers.
_RUNS_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("scheduler_pid", "INTEGER"),
    ("scheduler_host", "TEXT"),
    ("scheduler_claimed_at", "TEXT"),
    ("cancel_cause", "TEXT"),
    ("scheduler_start_epoch", "REAL"),
)

#: The same, for `node_lifecycle`. Kept as a second list rather than folded
#: into the one above because the read-only projection selects the `runs`
#: additions by name and must not be handed a column from another table.
_NODE_LIFECYCLE_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("cancel_cause", "TEXT"),
    ("merge_cause", "TEXT"),
)

#: Every table an older ledger may be missing a column from, in one place so
#: `_migrate` cannot silently cover one table and not the other.
#: The same, for `attempt_baselines`. Nullable with no default, so a ledger
#: written before the column existed reads NULL — "nobody looked" — rather
#: than `'{}'`, which would claim the tree had no ignored files at base.
_ATTEMPT_BASELINE_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("ignored_json", "TEXT"),
)

_ADDED_COLUMNS: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    ("runs", _RUNS_ADDED_COLUMNS),
    ("node_lifecycle", _NODE_LIFECYCLE_ADDED_COLUMNS),
    ("attempt_baselines", _ATTEMPT_BASELINE_ADDED_COLUMNS),
)


def _host_label(name: str) -> str:
    """The first DNS label. An FQDN's DHCP suffix is not machine identity."""
    return name.strip().split(".", 1)[0]


def _host_identity(name: str) -> str:
    return _host_label(name).casefold()


def _same_scheduler_host(recorded: str, current: str) -> bool:
    """True only when both names share a nonempty short label.

    `Mac.attlocal.net` and `Mac` are the same machine after a network
    change rewrote the suffix. `Mac` and `OtherBox` are not. Empty
    identities do not match: unknown is not same-host.
    """
    left = _host_identity(recorded)
    right = _host_identity(current)
    return bool(left) and left == right


def scheduler_host() -> str:
    """The machine whose pid namespace `scheduler_pid` was taken from.

    A pid is only meaningful on the host that issued it. A ledger on a shared
    filesystem, or copied between machines, would otherwise have its pids read
    against a completely unrelated process table — and the direction that
    breaks is the dangerous one: some *other* machine's pid 41022 is very
    likely alive here, so a dead scheduler would read as live forever. Stored
    beside the pid so the liveness question can be *declined* rather than
    answered wrongly (`scheduler_liveness` returns `None`).

    Stored as the short hostname. `socket.gethostname()` may return an FQDN
    whose domain suffix is a DHCP assignment and changes with the network;
    that suffix is not identity, and comparing it as one made every run
    recorded under the old suffix unresumable on the same laptop.
    """
    try:
        return _host_label(socket.gethostname())
    except OSError:
        return ""


def _table_columns(conn: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    """The column names a table actually has right now."""
    return tuple(str(row[1]) for row in
                 conn.execute("PRAGMA table_info({0})".format(table)).fetchall())


def _migrate(conn: sqlite3.Connection) -> Tuple[str, ...]:
    """Add any column this version needs and an older ledger lacks.

    `ADD COLUMN` is the only shape used, and every added column is nullable
    with no default: an existing row keeps NULL, and NULL is read everywhere
    below as "nobody recorded this", never as a value. So the migration cannot
    invent a fact about a run that predates the column. A pre-migration
    `CANCELLED` therefore carries no cause, and `run resume` refuses it — the
    safe direction, since the alternative is guessing that a run nobody
    recorded a cause for was merely paused.

    Concurrent openers are a supported case — `_enable_wal` says so
    explicitly. The `PRAGMA table_info` check and the `ALTER TABLE` that
    follows therefore have to be one serialized transaction: two processes
    that both observe a missing column would otherwise both issue ALTER, and
    the second dies with `duplicate column name` at the first run or resume
    that touches a pre-migration ledger.

    Returns `table.column` for each addition, so a caller cannot mistake two
    same-named columns on different tables for one.
    """
    added = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, columns in _ADDED_COLUMNS:
            present = set(_table_columns(conn, table))
            for name, kind in columns:
                if name in present:
                    continue
                try:
                    conn.execute(
                        "ALTER TABLE {0} ADD COLUMN {1} {2}".format(
                            table, name, kind))
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).lower():
                        raise
                    continue
                added.append("{0}.{1}".format(table, name))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tuple(added)


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


def _audit_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """One audit row as a plain dict (§5.3, §10.5).

    The three prohibitions this function exists to satisfy, none of which is
    obvious from the call site:

    * **Never a validating model.** The house style is pydantic, so
      `Transition(**row)` is the natural thing to write and is exactly how
      digest-revalidation-on-load returns. The return type is `dict`, always.
    * **Unknown keys are ignored.** A column or blob key a later version wrote
      arrives here and is carried through untouched; nothing enumerates the
      expected set, so nothing can reject a row for carrying more than it knew.
    * **A NULL column is absent, never defaulted.** `no default applied on read`
      is a rule about silence: a defaulted `node_id` on a run-level transition
      would read as a real node to any caller comparing it. An absent key makes
      the caller supply its own default at the point of use, where the meaning
      of the absence is known.

    `*_json` columns are `json.loads`ed to plain dicts and lose the suffix, so
    `detail_json` reads back as `detail`.
    """
    out: Dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is None:
            continue
        if key.endswith("_json"):
            out[key[:-len("_json")]] = json.loads(value)
        else:
            out[key] = value
    return out


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
    #: Which of §7.3's two cancellation shapes reached `CANCELLED`, and `None`
    #: for every other outcome. The evidence behind the `CANCELLED` arm in
    #: exactly the way `blocked_nodes` is the evidence behind `BLOCKED`.
    cancel_cause: Optional[st.CancelCause] = None


def total_run_outcome(
    node_states: Sequence[Tuple[str, st.NodeState, Optional[st.BlockReason]]],
    *,
    stuck: bool,
    cancel_requested: bool,
    acceptance_result: Optional[bool],
    requested_cause: Optional[st.CancelCause] = None,
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

    `requested_cause` is the cause the verb that asked for the stop named for
    itself, and it is honoured only inside the `CANCELLED` arm. It exists
    because the two stop requests this function can see are indistinguishable
    from `cancel_requested` alone: `run cancel --discard` and a plain
    `run cancel` both set it, and only the first must be terminal. Left
    `None` — every scheduler-side declaration — the derivation below is
    exactly what it was.
    """
    if stuck:
        return OutcomeReport(outcome=st.RunOutcome.STUCK)

    all_cancelled = bool(node_states) and all(
        state is st.NodeState.CANCELLED for _, state, _ in node_states)
    if cancel_requested or all_cancelled:
        cancelled = tuple(nid for nid, state, _ in node_states
                          if state is st.NodeState.CANCELLED)
        # The two conditions are not interchangeable and the outcome records
        # which one fired. `cancel_requested` is the operator's stop control,
        # under which nothing was adjudicated; `all_cancelled` without it is a
        # run given up on node by node, each node individually adjudicated as
        # work the run should finish without. `run resume` reopens the first
        # and refuses the second (§7.8), and the precedence here is the same
        # precedence the arm above it already had.
        cause = requested_cause or (st.CancelCause.RUN_CANCEL if cancel_requested
                                    else st.CancelCause.ABANDONED)
        return OutcomeReport(outcome=st.RunOutcome.CANCELLED,
                             abandoned_nodes=cancelled, cancel_cause=cause)

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

def _guard_transition(current: st.NodeState, to_state: st.NodeState, *, actor: str,
                      cancel_cause: Optional[st.CancelCause] = None) -> None:
    """The legal-transition guard, with one exception it states rather than hides.

    `MERGED` and `CANCELLED` are absolutely terminal (§7.3) — with the single
    exception that a node written `CANCELLED` by `run cancel` may be returned
    to `PENDING` by an operator resuming that same run. The exception is
    narrow on purpose and every conjunct earns its place: only from
    `CANCELLED`, only to `PENDING`, only for `RUN_CANCEL`, only by the
    operator. It exists because a `RUN_CANCEL` `CANCELLED` is terminal only in
    the sense that the operator asked the machine to stop, and a resume is the
    same operator withdrawing the request — nothing was adjudicated about the
    node, so there is no verdict the terminality is protecting. An `ABANDONED`
    `CANCELLED` is a decision about the work itself and stays absolutely
    terminal, as does `MERGED`.
    """
    if current in st.ABSOLUTELY_TERMINAL:
        reopening = (current is st.NodeState.CANCELLED
                     and to_state is st.NodeState.PENDING
                     and actor == "operator"
                     and cancel_cause in st.REOPENABLE_CANCEL_CAUSES)
        if not reopening:
            raise IllegalTransition(
                f"{current.value} is absolutely terminal (§7.3); no transition leaves it, "
                f"including this attempted move to {to_state.value}")
    if (current is st.NodeState.BLOCKED and to_state is not st.NodeState.BLOCKED
            and actor != "operator"):
        raise IllegalTransition(
            "BLOCKED is operator-terminal (§7.3): no automatic transition leaves it, "
            f"only an operator escape may — refusing the move to {to_state.value}")


# ── what the ledger could show about a node at the moment it was accepted ────

@dataclass(frozen=True)
class MergeEvidence:
    """What the ledger held about a node's evidence chain when `skip` ran.

    §1.1 item 4 requires every merged node to carry a complete evidence chain
    scoped to its kind, and `skip` merges a node that by construction may
    carry none of it. The gap was not *recorded* anywhere, so an audit had no
    way to find it afterwards: one real agent node was reported `MERGED` with
    an `output_sha` while every one of its attempts was `CANCELLED` or
    `BLOCKED`, no reviewer had ever produced a verdict on it, and 928 lines
    merged on a gate run and an operator's word (#93).

    These are facts counted off the ledger, not a judgement about them. In
    particular there is no `missing` field enumerating what is absent: that
    is derivable from the counts below by any reader, and storing the
    conclusion beside its inputs is one fact in two representations (RC1) —
    the copy that goes stale being the one nothing recomputes.

    `verified_ever` is the load-bearing one, and it is load-bearing because
    of *where* `mark_verified` sits. The review gate runs after the node's
    context has settled and before `mark_verified` is called, so a node that
    ever reached `VERIFIED` is a node whose post-node gate passed and whose
    reviewer, where one was configured, did not reject the diff. Zero
    `VERIFIED` transitions is therefore the structural statement that no part
    of the machine's own chain was ever completed for this node — not an
    inference from prose, and not a reading of the git log (§1.2).

    It is genuinely discriminating rather than always false at skip time: a
    node that verified and then blocked on a merge conflict (§8.7) reaches
    `skip` with the chain intact, and reads `verified_ever=True` here.
    """

    #: Count of `VERIFIED` transitions ever recorded for this node.
    verified_transitions: int
    #: Count of attempt rows a reviewer rejected the diff of
    #: (`rp.REVIEW_REJECTED_KEY`). Distinct from the above and not its
    #: complement — a rejection says a reviewer *looked*, which is more than
    #: zero says, and is why both are recorded rather than one derived.
    review_rejections: int
    #: Count of attempt rows, the denominator the other two are read against.
    attempts_recorded: int
    #: The stored `block_reason` the node carried when the operator accepted
    #: it, or `None` where the escape was taken against a stranded RUNNING
    #: node, which stores no reason.
    block_reason: Optional[st.BlockReason]

    @property
    def verified_ever(self) -> bool:
        """Did any part of the machine's own evidence chain ever complete?"""
        return self.verified_transitions > 0

    def as_detail(self) -> Dict[str, Any]:
        """The typed transition-detail payload, which is where this lands.

        The authority tier gets the one fact a reader must key on — the
        `merge_cause` column — and the audit tier gets the evidence, exactly
        as §11.3 settled for `retry --grant`: the grant's magnitude is a
        typed field on the transition's detail that `run status` reads back,
        while the guard reads the column. §5.3 forbids the *runtime* reading
        the audit tier; the read verbs are not the runtime.
        """
        return {
            "verified_ever": self.verified_ever,
            "verified_transitions": self.verified_transitions,
            "review_rejections": self.review_rejections,
            "attempts_recorded": self.attempts_recorded,
            "block_reason": (self.block_reason.value
                             if self.block_reason else None),
        }


#: Where `MergeEvidence.as_detail` lands on the skip transition. Named so the
#: writer, `run status`, and the tests agree on one key rather than three
#: spellings of it.
MERGE_EVIDENCE_KEY = "merge_evidence"


# ── the store ────────────────────────────────────────────────────────────────

# ── the recorded measurement baseline ────────────────────────────────────────

#: Where the attempt row carries the digest of its recorded baseline. The
#: inventory itself lives in `attempt_baselines`; this key is what binds the
#: two, so a baseline swapped in that table without the attempt row's consent
#: fails to verify instead of quietly redefining what the attempt started from.
ATTEMPT_BASELINE_DIGEST_KEY = "baseline_digest"


def encode_baseline(baseline: Mapping[str, Sequence[str]]) -> Dict[str, str]:
    """The baseline inventory as a JSON-safe mapping of path to "mode blob"."""
    encoded: Dict[str, str] = {}
    for rel, tuple_ in baseline.items():
        mode, blob = tuple_
        encoded[rel] = "{0} {1}".format(mode, blob)
    return encoded


def decode_baseline(encoded: Mapping[str, Any]) -> Dict[str, Tuple[str, str]]:
    """The inverse of `encode_baseline`. Raises on anything it cannot parse."""
    inventory: Dict[str, Tuple[str, str]] = {}
    for rel, value in encoded.items():
        if not isinstance(value, str):
            raise BaselineCorrupt(
                "baseline entry for {0!r} is not a string".format(rel))
        mode, sep, blob = value.partition(" ")
        if not sep or not mode or not blob:
            raise BaselineCorrupt(
                "baseline entry for {0!r} is not '<mode> <blob>'".format(rel))
        inventory[rel] = (mode, blob)
    return inventory


def _baseline_bytes(encoded: Mapping[str, str]) -> bytes:
    return json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode("utf-8")


def baseline_digest(encoded: Mapping[str, str]) -> str:
    """sha256 over the canonical serialization of an encoded baseline."""
    return hashlib.sha256(_baseline_bytes(encoded)).hexdigest()


class LifecycleStore:
    """SQLite-backed ledger for two of §5.3's three tiers.

    Authority — `runs`, `dag_nodes`, `node_lifecycle`, `attempts` — is read at
    runtime. Audit — `transitions`, `results`, `orphans` — is written here and
    read only post-mortem, through the `audit_*` methods, which return plain
    dicts precisely so nothing can start treating them as authority (§10.5).

    Safe to share across a thread pool."""

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
        _migrate(self.conn)

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
                " latest_outcome, latest_outcome_at, cancel_requested,"
                " scheduler_pid, scheduler_host, scheduler_claimed_at,"
                " scheduler_start_epoch)"
                " VALUES (?,?,?,?,NULL,NULL,0,?,?,?,?)",
                (run_id, plan_digest, now, now,
                 os.getpid(), scheduler_host(), now,
                 wd.process_start_epoch(os.getpid())))
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
    def node_outputs(self, run_id: str, node_id: str) -> Tuple[str, ...]:
        """The node's declared outputs, as stored when the run was created."""
        row = self.conn.execute(
            "SELECT outputs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, node_id)).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no dag row")
        return tuple(json.loads(row[0]))

    @serialized
    def record_baseline(self, run_id: str, node_id: str, attempt_no: int,
                        baseline: Mapping[str, Sequence[str]],
                        ignored_at_base: Optional[Mapping[str, str]] = None
                        ) -> str:
        """Persist the measurement baseline the bracket just opened on.

        Written the moment `take_baseline` returns, because that walk is the
        only thing that ever sees the provisioned tree. It covers paths git
        does not track — an ecosystem's install output, a fixture the pre-gate
        materialized — and those paths exist in no commit, so once the attempt
        process is gone the baseline is unreconstructable from git. Anything
        that later needs the attempt's before-side reads this row; nothing may
        re-derive it from the base commit, which would report every
        provisioned untracked path as content the attempt produced.

        The digest is returned and also stamped on the attempt row, so the two
        records have to agree before either is believed.
        """
        row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None:
            raise UnknownNode(
                f"{run_id}/{node_id}#{attempt_no}: no attempt row to record a baseline on")
        encoded = encode_baseline(baseline)
        digest = baseline_digest(encoded)
        existing = self.conn.execute(
            "SELECT digest FROM attempt_baselines"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if existing is not None and existing[0] != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} already recorded baseline "
                f"{existing[0]}; a second, different baseline would rewrite "
                "what the attempt started from")
        payload = json.loads(row[0] or "{}")
        if not isinstance(payload, dict):
            payload = {}
        payload[ATTEMPT_BASELINE_DIGEST_KEY] = digest
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO attempt_baselines"
                " (run_id, node_id, attempt_no, digest, inventory_json,"
                "  ignored_json, recorded_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, node_id, attempt_no, digest,
                 json.dumps(encoded, sort_keys=True, separators=(",", ":")),
                 None if ignored_at_base is None else json.dumps(
                     dict(ignored_at_base), sort_keys=True,
                     separators=(",", ":")),
                 now_iso()))
            self.conn.execute(
                "UPDATE attempts SET extra_json=?"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (json.dumps(payload, sort_keys=True), run_id, node_id, attempt_no))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return digest

    @serialized
    def attempt_baseline(self, run_id: str, node_id: str,
                         attempt_no: int) -> Dict[str, Tuple[str, str]]:
        """The recorded baseline, or a refusal. Never a reconstruction.

        Raises `BaselineUnrecorded` when the attempt predates the recording,
        and `BaselineCorrupt` when the stored inventory and the digest the
        attempt row carries disagree. Both are refusals on purpose: the
        alternative — falling back to the base commit's tree — is what turns
        provisioned untracked content into a measured delta the attempt never
        produced.
        """
        row = self.conn.execute(
            "SELECT digest, inventory_json FROM attempt_baselines"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        attempt_row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if attempt_row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        extra = json.loads(attempt_row[0] or "{}")
        stamped = extra.get(ATTEMPT_BASELINE_DIGEST_KEY) if isinstance(extra, dict) else None
        if row is None:
            raise BaselineUnrecorded(
                f"{run_id}/{node_id}#{attempt_no} recorded no measurement baseline")
        digest, inventory_json = row
        if stamped != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} carries baseline digest "
                f"{stamped!r} but the stored baseline digests to {digest!r}")
        try:
            encoded = json.loads(inventory_json)
        except json.JSONDecodeError as exc:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline is not JSON: {exc}") from exc
        if not isinstance(encoded, dict):
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline is not an object")
        if baseline_digest(encoded) != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline does not match its "
                f"recorded digest {digest}")
        return decode_baseline(encoded)

    @serialized
    def attempt_ignored_at_base(self, run_id: str, node_id: str,
                                attempt_no: int) -> Optional[Dict[str, str]]:
        """The gitignored files present when this attempt's bracket opened.

        `None` means **nobody looked** — an attempt whose baseline was
        recorded before `ignored_json` existed — and every caller must carry
        that distinction rather than collapsing it. An empty dict means the
        walk ran and found nothing ignored, which is a real answer and is
        safe to measure against. Reading `None` as `{}` would report a whole
        provisioned dependency tree as content the attempt wrote, which is
        the false positive `worktree.existing_ignored_outputs` was built to
        avoid and the reason it refuses a `None` before-side outright.

        This is a separate read from `attempt_baseline` because the two
        answer questions about disjoint universes: the baseline inventory is
        `git ls-files --cached --others --exclude-standard`, and this is
        exactly what that command excludes. No amount of the first can
        reconstruct the second, which is why salvage could not simply derive
        it from the recorded baseline (#67).
        """
        row = self.conn.execute(
            "SELECT ignored_json FROM attempt_baselines"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None:
            raise BaselineUnrecorded(
                f"{run_id}/{node_id}#{attempt_no} recorded no measurement baseline")
        if row[0] is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} ignored-at-base map is not "
                f"JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} ignored-at-base map is not "
                "an object")
        return {str(key): str(value) for key, value in payload.items()}

    @serialized
    def record_salvage(self, run_id: str, node_id: str, attempt_no: int,
                       extra: Mapping[str, Any]) -> None:
        """Merge salvage facts into the attempt row and close it if live.

        Salvage does not transition the node: the post-gate never ran, so
        VERIFIED/MERGED would launder an unmeasured predicate. The attempt
        that produced the files is no longer live once those files have a
        commit, so a RUNNING row is closed here the same way the other
        escapes close it.
        """
        row = self.conn.execute(
            "SELECT extra_json, state FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        try:
            merged = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            merged = {}
        if not isinstance(merged, dict):
            merged = {}
        merged.update(extra)
        self.conn.execute(
            "UPDATE attempts SET extra_json=?, state=CASE WHEN state=? THEN ? ELSE state END"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (json.dumps(merged, sort_keys=True),
             st.NodeState.RUNNING.value, CLOSED_ATTEMPT_STATE.value,
             run_id, node_id, attempt_no))


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
                       pid: Optional[int], launched_at: Optional[float] = None,
                       extra: Optional[Mapping[str, Any]] = None) -> None:
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
        row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None:
            raise UnknownNode(
                f"{run_id}/{node_id}#{attempt_no}: no attempt row to mark launched")
        payload = json.loads(row[0] or "{}")
        if extra:
            payload.update(extra)
        self.conn.execute(
            "UPDATE attempts SET launched_at=?, pid=?, extra_json=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (launched_at if launched_at is not None else time.time(), pid,
             json.dumps(payload), run_id, node_id, attempt_no))

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

    # ── the audit tier (§5.3, §7.7, §7.8, §10.5, §10.6) ─────────────────────

    def _audit_query(self, sql: str, params: Tuple[Any, ...]) -> Tuple[Dict[str, Any], ...]:
        """§10.6's one query path.

        Every audit read — live or post-mortem, renderer or test — goes through
        this one cursor pattern, so there is no dashboard-only schema and no
        fixture-only truth to drift from what the run actually wrote. The row
        factory is set on the **cursor**, never on the shared connection: the
        authority-tier reads above index their rows by position, and flipping
        the connection's factory under them would break every one of them.
        """
        cursor = self.conn.cursor()
        cursor.row_factory = sqlite3.Row
        try:
            return tuple(_audit_dict(row) for row in cursor.execute(sql, params))
        finally:
            cursor.close()

    @serialized
    def audit_transitions(self, run_id: str,
                           node_id: Optional[str] = None) -> Tuple[Dict[str, Any], ...]:
        """The ordered transition history, oldest first (§5.3's audit tier).

        Post-mortem only. Nothing in the scheduler may read this at runtime —
        §11.2's backstop reads `runs.last_transition_at` for exactly that
        reason, and `last_transition_at`'s docstring says why.
        """
        sql = "SELECT * FROM transitions WHERE run_id=?"
        params: Tuple[Any, ...] = (run_id,)
        if node_id is not None:
            sql += " AND node_id=?"
            params = (run_id, node_id)
        return self._audit_query(sql + " ORDER BY id", params)

    @serialized
    def record_result(self, run_id: str, result: st.ResultRecord) -> None:
        """Append one adjudicated result — payload and verdict in one row (§7.7).

        `ResultRecord` refuses to exist without its payload and the column is
        `NOT NULL`, so the failure this prevents is closed twice: an
        adjudication stored without the payload it judged is how a correct FAIL
        carrying two real findings vanished behind a byte-identical journal.

        Its own `BEGIN IMMEDIATE` (§7.9) rather than a hitchhiker on a
        transition's: a result arrives from a worker thread whenever the agent
        finishes, which is not a lifecycle transition and must not refresh
        `last_transition_at`.
        """
        payload = json.dumps(result.payload)  # outside the transaction on purpose
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO results (run_id, node_id, attempt_no, subject_sha,"
                " payload_json, adjudication, created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, result.node_id, result.attempt_no, result.subject_sha,
                 payload,
                 result.adjudication.value if result.adjudication else None,
                 now_iso()))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def audit_results(self, run_id: str,
                       node_id: Optional[str] = None) -> Tuple[Dict[str, Any], ...]:
        """Result rows with their payloads, oldest first (§7.7)."""
        sql = "SELECT * FROM results WHERE run_id=?"
        params: Tuple[Any, ...] = (run_id,)
        if node_id is not None:
            sql += " AND node_id=?"
            params = (run_id, node_id)
        return self._audit_query(sql + " ORDER BY id", params)

    @serialized
    def result_adjudication(self, run_id: str, node_id: str, attempt_no: int
                             ) -> Optional[st.Adjudication]:
        """The typed verdict on one attempt's declared result, or None (§7.7).

        The per-attempt lookup `audit_results` never offered: that one returns
        every row for a run or a node, which answers "what has this node
        produced" and not "did *this generation* declare a result". §7.6's two
        clock guards need the second question, keyed exactly as §7.7 keys a
        result — `(run_id, node_id, attempt_no)`.

        **`payload_json` is deliberately absent from the projection.** §1.2 is
        binding: no lifecycle transition may be caused by a free-text envelope
        field, and a guard able to reach the payload's `summary` is one edit
        away from keying on model prose. What this returns is a closed enum,
        and the payload's absence from the SELECT is what makes reading it
        impossible here rather than merely discouraged.

        The latest row wins. A reclaimed attempt's late arrival lands a second
        row against the same `(node_id, attempt_no)` and adjudicates
        SUPERSEDED (§7.7), and that newer verdict is the one describing what
        the generation is now — an older ACCEPTED must not outlive it.
        """
        row = self.conn.execute(
            "SELECT adjudication FROM results"
            " WHERE run_id=? AND node_id=? AND attempt_no=?"
            " ORDER BY id DESC LIMIT 1",
            (run_id, node_id, attempt_no)).fetchone()
        if row is None or row[0] is None:
            return None
        return st.Adjudication(row[0])

    @serialized
    def record_orphan(self, run_id: str, *, node_id: Optional[str] = None,
                       attempt_no: Optional[int] = None, pid: Optional[int] = None,
                       handle: Optional[str] = None, reason: str = "") -> None:
        """Record a pane this process cannot reach (§7.8).

        Audit, not authority: an orphan row changes no node's state and gates
        nothing. It exists because resume's cost is stated rather than hidden —
        one abandoned pane per in-flight node, visible and killed by hand — and
        a cost nobody can enumerate is not visible.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO orphans (run_id, node_id, attempt_no, pid, handle,"
                " reason, created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, node_id, attempt_no, pid, handle, reason or None, now_iso()))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def audit_orphans(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        """Every unreachable pane recorded for this run, oldest first (§7.8).
        `run status` reports these so an operator can kill them by hand."""
        return self._audit_query(
            "SELECT * FROM orphans WHERE run_id=? ORDER BY id", (run_id,))

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
        cancel_cause: Optional[st.CancelCause] = None,
        merge_cause: Optional[st.MergeCause] = None,
        extra_writes: Optional[Callable[[st.NodeLifecycle], Sequence[Tuple[str, Tuple]]]] = None,
    ) -> st.NodeLifecycle:
        """Guard, then write the lifecycle row, the audit row, and the run's
        `last_transition_at`, all inside one `BEGIN IMMEDIATE` (§7.9). Any
        caller-supplied `extra_writes` (an attempt-row update, say) runs inside
        the same transaction, so the write set stays atomic."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, attempt_no, block_reason, output_sha,"
                " granted_extra_attempts, cancel_cause"
                " FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, node_id)).fetchone()
            if row is None:
                raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
            current = st.NodeState(row[0])
            current_cause = st.CancelCause(row[5]) if row[5] else None
            _guard_transition(current, to_state, actor=actor,
                              cancel_cause=current_cause)
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

            # Scoped to CANCELLED exactly as `block_reason` is scoped to
            # BLOCKED: any transition out of CANCELLED clears the cause with
            # the state that made it meaningful, so no row can carry a cause
            # for a cancellation it is no longer under.
            new_cancel_cause = (cancel_cause
                                if to_state is st.NodeState.CANCELLED else None)
            # Scoped to MERGED exactly as the line above is scoped to
            # CANCELLED. Nothing transitions out of MERGED (§7.3), so the
            # clearing arm is unreachable by construction rather than by
            # care — it is written anyway so that the scoping rule is one
            # rule stated twice rather than a rule and an assumption, and so
            # that a later narrowing of absolute terminality cannot leave a
            # node carrying a merge cause for a merge it is no longer under.
            new_merge_cause = (merge_cause
                               if to_state is st.NodeState.MERGED else None)
            now = now_iso()
            self.conn.execute(
                "UPDATE node_lifecycle SET state=?, attempt_no=?, block_reason=?,"
                " output_sha=?, granted_extra_attempts=?, cancel_cause=?,"
                " merge_cause=?, updated_at=?"
                " WHERE run_id=? AND node_id=?",
                (lifecycle.state.value, lifecycle.attempt_no,
                 lifecycle.block_reason.value if lifecycle.block_reason else None,
                 lifecycle.output_sha, lifecycle.granted_extra_attempts,
                 new_cancel_cause.value if new_cancel_cause else None,
                 new_merge_cause.value if new_merge_cause else None, now,
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
        """VERIFIED -> MERGED, absolutely terminal from here on (§7.3, §8.6).

        Stamps `SCHEDULER`, which is the whole of the distinction §11.3's
        `skip` could not previously be told apart by. The value is written
        here rather than defaulted in `_transition_node` deliberately: a
        default would make `SCHEDULER` the answer for any future caller that
        forgot to say, and the one thing this column must never do is assert
        an evidence chain by omission. `VERIFIED` is the require_state, so
        the claim is not a courtesy — reaching this line means the node
        passed §7.3's four-clause predicate and, for an agent node, a
        reviewer that did not reject its diff.
        """
        return self._transition_node(
            run_id, node_id, st.NodeState.MERGED, actor="scheduler", reason="merged",
            merge_cause=st.MergeCause.SCHEDULER,
            require_state=(st.NodeState.VERIFIED,))

    def mark_blocked(self, run_id: str, node_id: str, reason: st.BlockReason, *,
                      detail: Optional[Mapping[str, Any]] = None,
                      retry_class: Optional[st.RetryClass] = None,
                      attempt_extra: Optional[Mapping[str, Any]] = None) -> st.NodeLifecycle:
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
            if attempt_extra:
                # The blocking attempt's marker matters as much as a retrying
                # one's: without it the budget-exhausting attempt leaves no
                # stored evidence, and after `retry --force` the count restarts
                # one short — which turns a granted single attempt into an
                # unbounded loop for as long as the operator keeps forcing.
                row = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, lifecycle.attempt_no)).fetchone()
                try:
                    merged = dict(json.loads(row[0]) if row and row[0] else {})
                except (TypeError, ValueError):
                    merged = {}
                merged.update(dict(attempt_extra))
                writes.append((
                    "UPDATE attempts SET extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (json.dumps(merged, sort_keys=True),
                     run_id, node_id, lifecycle.attempt_no)))
            return writes
        return self._transition_node(
            run_id, node_id, st.NodeState.BLOCKED, actor="scheduler",
            reason=f"blocked:{reason.value}", block_reason=reason,
            require_state=(st.NodeState.RUNNING, st.NodeState.VERIFIED),
            detail=detail, extra_writes=extra)

    def fail_attempt(self, run_id: str, node_id: str,
                      retry_class: st.RetryClass,
                      detail: Optional[Mapping[str, Any]] = None,
                      attempt_extra: Optional[Mapping[str, Any]] = None) -> st.NodeLifecycle:
        """RUNNING -> PENDING: an ENVIRONMENTAL/LAUNCHER_TRANSIENT failure that
        earns another attempt automatically (§7.5) — not an operator escape.

        Closes the attempt row in the same transaction, and that write is not
        bookkeeping. Leaving it RUNNING meant the node's *next* attempt row
        collided with §10.3's partial unique index, §7.6's watchdog kept
        polling an attempt nobody was running, and §7.7 adjudicated a late
        arrival from the dead attempt as ACCEPTED rather than SUPERSEDED
        because it reads `attempts.state`. The retry class is recorded
        alongside, so the row still says what the attempt failed as.

        `attempt_extra` merges into the attempt row's `extra_json` in the same
        transaction. It exists so a budget can be counted over a dimension the
        three retry classes do not name — code review is one: a rejected diff
        is a SEMANTIC failure by §7.5's own rule, but it must not share the
        semantic ceiling, and adding a fourth `RetryClass` would break §7.5's
        stated "three, mutually exclusive" and every guard written against it.
        A marker in the row the count already ranges over is the smaller
        change, and it keeps the count a `COUNT(*)` over stored facts rather
        than a counter this store maintains.
        """
        def extra(lifecycle: st.NodeLifecycle):
            writes = [(
                "UPDATE attempts SET retry_class=?, state=?"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (retry_class.value, CLOSED_ATTEMPT_STATE.value,
                 run_id, node_id, lifecycle.attempt_no))]
            if attempt_extra:
                row = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, lifecycle.attempt_no)).fetchone()
                try:
                    merged = dict(json.loads(row[0]) if row and row[0] else {})
                except (TypeError, ValueError):
                    merged = {}
                merged.update(dict(attempt_extra))
                writes.append((
                    "UPDATE attempts SET extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (json.dumps(merged, sort_keys=True),
                     run_id, node_id, lifecycle.attempt_no)))
            return writes
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="scheduler",
            reason=f"retry:{retry_class.value}", require_state=(st.NodeState.RUNNING,),
            detail=detail, extra_writes=extra)

    # ── run-level: cancellation, outcome, resume ────────────────────────────

    @serialized
    def adoptable_attempts(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        """Completed, non-rejected, unmerged work a discard would lose (§7.3).

        `VERIFIED` nodes, and nodes whose latest result was adjudicated
        `ACCEPTED` and which have not merged. Both are work that reached a
        measured predicate and would simply stop being reachable through
        Maestro once the run is terminal — which is the one cost of
        `run cancel --discard` that the operator cannot see from the run's
        state, because a node in either shape reports as neither finished nor
        failed.

        Rejected attempts are deliberately not listed: losing those is the
        correct outcome, not a cost. Review is recorded *after* result
        acceptance, so an `ACCEPTED` row whose attempt carries
        `retry_policy.REVIEW_REJECTED_KEY` is rejected work wearing an
        accepted result, and is filtered on that typed key rather than on
        anything a reviewer wrote in prose (§1.2).
        """
        nodes = self.conn.execute(
            "SELECT node_id, state, attempt_no FROM node_lifecycle WHERE run_id=?",
            (run_id,)).fetchall()
        found: List[Dict[str, Any]] = []
        for node_id, state, attempt_no in nodes:
            current = st.NodeState(state)
            if current in st.ABSOLUTELY_TERMINAL:
                continue
            if current is st.NodeState.VERIFIED:
                found.append({"node_id": node_id, "state": current.value,
                              "attempt_no": attempt_no, "why": "verified"})
                continue
            row = self.conn.execute(
                "SELECT adjudication FROM results"
                " WHERE run_id=? AND node_id=? AND attempt_no=?"
                " ORDER BY id DESC LIMIT 1",
                (run_id, node_id, attempt_no)).fetchone()
            if not row or row[0] != st.Adjudication.ACCEPTED.value:
                continue
            extra_row = self.conn.execute(
                "SELECT extra_json FROM attempts"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (run_id, node_id, attempt_no)).fetchone()
            try:
                extra = json.loads(extra_row[0]) if extra_row and extra_row[0] else {}
            except (TypeError, ValueError):
                extra = {}
            if not isinstance(extra, dict) or extra.get(rp.REVIEW_REJECTED_KEY):
                continue
            found.append({"node_id": node_id, "state": current.value,
                          "attempt_no": attempt_no,
                          "why": "accepted-unmerged"})
        return tuple(found)

    @serialized
    def cancel_run(self, run_id: str, *,
                   cause: st.CancelCause = st.CancelCause.RUN_CANCEL
                   ) -> Tuple[str, ...]:
        """Write CANCELLED for every non-terminal node in ONE transaction (§7.8).
        Never blocks on a kill — that is the adapter's problem, not this store's.

        Every node it takes is stamped with `cause`, which by default is
        `RUN_CANCEL` and is what makes the stop reversible: a resume reopens
        exactly those nodes and leaves a node the operator had separately
        abandoned where it is (§7.8). `run cancel --discard` passes
        `DISCARDED` instead, and the same stamp is then what makes the stop
        irreversible — `_run_cancelled_node_ids` selects on the value, so a
        discarded node is not among the nodes a resume would reopen, and
        `_guard_transition` refuses it individually as well.

        `MERGED` nodes are absolutely terminal and are skipped, so the merged,
        gate-verified, reviewed work a long run has already landed survives
        the stop and is not re-executed by the resume."""
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
                    "UPDATE node_lifecycle SET state=?, block_reason=NULL,"
                    " cancel_cause=?, updated_at=?"
                    " WHERE run_id=? AND node_id=?",
                    (st.NodeState.CANCELLED.value,
                     cause.value, now, run_id, node_id))
                self.conn.execute(
                    "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                    " reason, actor, detail_json, created_at)"
                    " VALUES (?,?,'node',?,?, 'run-cancel','operator','{}',?)",
                    (run_id, node_id, current.value, st.NodeState.CANCELLED.value, now))
                # §7.8's "its result is rejected because its attempt is no
                # longer running" is only true if cancellation writes it. The
                # scheduler never blocks on the kill, so the surviving pane may
                # still report — and §7.7 reads this column to refuse it.
                self.conn.execute(
                    "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND state=?",
                    (CLOSED_ATTEMPT_STATE.value, run_id, node_id,
                     st.NodeState.RUNNING.value))
                cancelled.append(node_id)
            self.conn.execute(
                "UPDATE runs SET last_transition_at=?, cancel_requested=1 WHERE run_id=?",
                (now, run_id))
            # `cancel_cause` is written by `declare_outcome` alone -- it is an
            # attribute of the declared outcome, and this verb declares
            # nothing. Writing it here would state a cause for an outcome no
            # scheduler has reached, and a cancel that never quiesces would
            # leave that claim standing.
            self.conn.execute("COMMIT")
            return tuple(cancelled)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def declare_outcome(self, run_id: str, *, stuck: bool = False,
                         acceptance_result: Optional[bool] = None,
                         cancel_cause: Optional[st.CancelCause] = None
                         ) -> OutcomeReport:
        """Compute and record the run's outcome (§7.3) — a record, not a
        tombstone: `runs` keeps only the latest value; the ordered history is
        the `transitions` row this same transaction appends.

        `cancel_cause` names the cause the caller's stop request carries, and
        reaches the outcome function as `requested_cause` — honoured in the
        `CANCELLED` arm and ignored everywhere else. Only `run cancel
        --discard` passes it; a scheduler declaring its own quiescence never
        does, and the cause it gets is derived exactly as before."""
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
                acceptance_result=acceptance_result,
                requested_cause=cancel_cause)
            now = now_iso()
            # `cancel_cause` is written on every declaration, including the
            # ones that clear it. A run that was cancelled, resumed, and then
            # accepted must not keep the cause of the outcome it superseded --
            # `runs` holds the latest outcome only (§7.3), and a stale cause
            # beside a fresh outcome is the two-representations shape RC1
            # convicts.
            self.conn.execute(
                "UPDATE runs SET latest_outcome=?, latest_outcome_at=?,"
                " last_transition_at=?, cancel_cause=? WHERE run_id=?",
                (report.outcome.value, now, now,
                 report.cancel_cause.value if report.cancel_cause else None,
                 run_id))
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,?, 'declare-outcome','scheduler',?,?)",
                (run_id, report.outcome.value,
                 json.dumps({"blocked_nodes": list(report.blocked_nodes),
                            "abandoned_nodes": list(report.abandoned_nodes),
                            "acceptance_result": report.acceptance_result,
                            "cancel_cause": (report.cancel_cause.value
                                             if report.cancel_cause else None)}), now))
            self.conn.execute("COMMIT")
            return report
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def claim_run(self, run_id: str) -> None:
        """Record *this* process as the run's scheduler (§7.3, §11.2).

        The only durable statement that a run has an owner. Without it a run's
        state is whatever the last live scheduler remembered to write, so a
        scheduler that died — crash, SIGKILL, machine sleep, an operator
        closing the pane — left `latest_outcome` NULL and its nodes RUNNING,
        and every read verb went on reporting the run as live. There was no
        fact in the ledger that could contradict them.

        Written on projection and on resume, which are the two moments a
        process takes ownership. Never cleared: see the schema comment.
        """
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE runs SET scheduler_pid=?, scheduler_host=?,"
                " scheduler_claimed_at=?, scheduler_start_epoch=?"
                " WHERE run_id=?",
                (os.getpid(), scheduler_host(), now,
                 wd.process_start_epoch(os.getpid()), run_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def _write_resume_transition(self, run_id: str) -> None:
        """The resume boundary, in one transaction (§7.8, §11.2).

        `cancel_requested` is cleared here, and it is not bookkeeping: the
        flag is the outcome function's input for the `CANCELLED` arm (§7.3),
        so a resumed run that left it set would declare `CANCELLED` again at
        the quiescence it reaches after doing all of its remaining work — and
        `derive_run_state` would report it `CANCELLING` for the whole of that
        life. The operator withdrew the stop request by resuming; the request
        stops standing at that instant.

        `cancel_cause` is deliberately *not* cleared. It is an attribute of
        the latest declared outcome, which this resume supersedes but does not
        erase, and the next `declare_outcome` rewrites it. Clearing it here
        would strand the next resume: a scheduler that dies before declaring
        leaves `latest_outcome` at `CANCELLED`, and a `CANCELLED` with no
        cause is refused.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            now = now_iso()
            self.conn.execute(
                "UPDATE runs SET last_transition_at=?, cancel_requested=0,"
                " scheduler_pid=?, scheduler_host=?, scheduler_claimed_at=?,"
                " scheduler_start_epoch=? WHERE run_id=?",
                (now, os.getpid(), scheduler_host(), now,
                 wd.process_start_epoch(os.getpid()), run_id))
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

    @serialized
    def run_cancel_cause(self, run_id: str) -> Optional[st.CancelCause]:
        """Why the latest declared outcome was CANCELLED, or None.

        None both for a run whose outcome is not CANCELLED and for a
        `CANCELLED` written by a ledger older than the column. The two are not
        distinguished on purpose: neither is a recorded operator stop, and
        `resume_run` refuses both.
        """
        row = self.conn.execute(
            "SELECT cancel_cause FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise UnknownNode(f"run {run_id} does not exist")
        return st.CancelCause(row[0]) if row[0] else None

    @serialized
    def _run_cancelled_node_ids(self, run_id: str) -> Tuple[str, ...]:
        """Nodes this run's `run cancel` took, in id order.

        Nodes stamped `ABANDONED` are excluded by the predicate rather than by
        a later filter, because a mixed run — one lane abandoned by hand, then
        the whole run cancelled — is the shape where reopening every
        `CANCELLED` node would resurrect the lane the operator gave up on.
        """
        rows = self.conn.execute(
            "SELECT node_id FROM node_lifecycle"
            " WHERE run_id=? AND state=? AND cancel_cause=? ORDER BY node_id",
            (run_id, st.NodeState.CANCELLED.value,
             st.CancelCause.RUN_CANCEL.value)).fetchall()
        return tuple(r[0] for r in rows)

    def _reopen_run_cancelled_node(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        """CANCELLED(RUN_CANCEL) -> PENDING, the resume's half of the stop.

        `attempt_no` is carried forward unchanged and no retry class is
        debited: `cancel_run` closed the attempt row without classifying it,
        so a node stopped mid-attempt returns to the frontier with the budget
        it had. Nothing about it was adjudicated, so there is nothing to
        charge it for.
        """
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="operator",
            reason="resume:run-cancel",
            require_state=(st.NodeState.CANCELLED,))

    def resume_run(self, run_id: str) -> Tuple[str, ...]:
        """Legal against BLOCKED, STUCK, NULL, and a run the operator stopped
        with `run cancel`; refused against ACCEPTED and against a run given up
        on node by node (§7.3). Writes the resume transition — refreshing
        `last_transition_at` — BEFORE touching any inherited RUNNING attempt,
        so the backstop measures silence from the resume, not from the dead
        run's last act (§7.8, §11.2). Every inherited RUNNING attempt is
        returned to PENDING and re-launched, never adopted — the resumed
        process owns none of their panes and cannot resume reading an agent
        mid-turn.

        What it is *charged* depends on whether it declared a result. §9.7:
        an artifact a worker wrote outranks any status a supervisor observes
        about that worker, and a resume is the supervisor here. An inherited
        attempt may be one that had already finished — envelope written, work
        committed, result row adjudicated ACCEPTED — and was sitting in review
        when the scheduler died. Charging that ENVIRONMENTAL debits an infra
        retry for an attempt that did its work, the same accounting distortion
        the watchdog's turn clock produced before it learned to read the same
        row. Such an attempt is closed UNCLASSIFIED instead, and
        `attempts_spent` counts only classified rows, so it costs nothing.
        Everything else is charged ENVIRONMENTAL exactly as before.

        Re-launched either way, and deliberately: adoption would mean binding
        the resumed process to a worktree, a pane, and a gate run it never
        started, which is a different design. Only the budget is corrected
        here, never the work.

        Each one's pane is recorded in `orphans` before its attempt row is
        closed — the pid lives on that row, so reading it afterwards would read
        a row whose launch details are no longer the live ones. The resumed
        process cannot reach those panes and does not try: §7.8's stated cost is
        one abandoned pane per in-flight node, and these rows are what makes
        `run status` able to name them for the operator to kill by hand.

        **The two CANCELLED runs are not alike, and the refusal keys on the
        recorded cause rather than on the word.** `ACCEPTED` is terminal
        because the run reached its declared outcome, and reopening it would
        reopen an adjudicated result. A run whose every node was individually
        abandoned is closer to that: each `abandon` was a decision about the
        work. A run the operator stopped with `run cancel` is neither — the
        machine was asked to stop, nothing was adjudicated, and there is no
        result to protect. Refusing it discarded every merged, gate-verified,
        reviewed node the run had already landed, which on a long multi-lane
        plan is hours of work thrown away by the operator's only stop control.
        So a `RUN_CANCEL` run resumes: its `MERGED` nodes stay `MERGED` and
        are never re-executed, the nodes the stop took return to `PENDING`,
        the frontier recomputes over them, and the integration branch is taken
        back rather than re-created (the caller's job, and unchanged).

        A `DISCARDED` run is refused for a third reason, and the reason is the
        verb's whole purpose: `run cancel --discard` is destructive by
        request, and a resume that reopened it would leave the operator with
        no way to end a run. The non-destructive stop is `run pause`, which
        declares no outcome and therefore never reaches this predicate.

        A `CANCELLED` carrying no cause at all — a ledger written before the
        column — is refused with the abandoned case. The migration invents no
        facts, and guessing that an unrecorded cancellation was merely a pause
        is the guess that reopens an adjudicated run."""
        outcome = self.latest_outcome(run_id)
        cause = self.run_cancel_cause(run_id)
        if outcome is st.RunOutcome.ACCEPTED:
            raise ResumeRefused(
                f"{run_id}: resume is refused against a declared "
                f"{outcome.value} run (§7.3) — it is not reopenable")
        if (outcome is st.RunOutcome.CANCELLED
                and cause not in st.REOPENABLE_CANCEL_CAUSES):
            if cause is st.CancelCause.ABANDONED:
                why = ("was given up on node by node, and each of those nodes "
                       "was adjudicated as work the run should finish without")
            elif cause is st.CancelCause.DISCARDED:
                why = ("was discarded — `run cancel --discard` is the verb "
                       "that ends a run for good, and `run pause` is the one "
                       "that stops a run you mean to come back to")
            else:
                why = ("records no cause at all — its ledger predates the "
                       "column, and reading an unrecorded cancellation as a "
                       "pause is the guess that reopens an adjudicated run")
            raise ResumeRefused(
                f"{run_id}: resume is refused against a run declared CANCELLED "
                f"with cause {cause.value if cause else 'unrecorded'} (§7.3) — "
                f"only a run stopped by an operator's `run cancel` is "
                f"reopenable; this one {why}")
        self._write_resume_transition(run_id)
        # Before the inherited attempts, and deliberately: these nodes hold no
        # attempt this process could inherit -- `cancel_run` closed every one
        # of them in the transaction that wrote the state -- so reopening them
        # first keeps the two halves of a resume in the order an operator
        # reads them, the stop undone and then the wreckage of the crash
        # cleared. Empty on every resume that is not undoing a `run cancel`.
        for node_id in self._run_cancelled_node_ids(run_id):
            self._reopen_run_cancelled_node(run_id, node_id)
        reclaimed = []
        for node_id in self._running_node_ids(run_id):
            node = self.get_node(run_id, node_id)
            try:
                inherited = self.get_attempt(run_id, node_id, node.attempt_no)
            except UnknownNode:
                inherited = None
            if inherited is not None:
                self.record_orphan(
                    run_id, node_id=node_id, attempt_no=inherited.attempt_no,
                    pid=inherited.pid,
                    handle=str(inherited.extra.get("pane")) if inherited.extra.get("pane") else None,
                    reason="resume: inherited a RUNNING attempt this process does not own")
            if (self.result_adjudication(run_id, node_id, node.attempt_no)
                    is st.Adjudication.ACCEPTED):
                # Only ACCEPTED spares it. The other three verdicts each say
                # the row does not describe this generation's own work:
                # SUPERSEDED named an attempt that was no longer live,
                # UNKNOWN_ATTEMPT names no attempt at all, and SHA_MISMATCH
                # names a different base (§7.7).
                self._release_unclassified_attempt(run_id, node_id)
            else:
                self.fail_attempt(run_id, node_id, st.RetryClass.ENVIRONMENTAL)
            reclaimed.append(node_id)
        return tuple(reclaimed)

    def _release_unclassified_attempt(self, run_id: str,
                                       node_id: str) -> st.NodeLifecycle:
        """RUNNING -> PENDING with the attempt row closed but UNCLASSIFIED.

        The same write set as `fail_attempt` minus the one column that costs
        something: `retry_class` stays NULL, and `attempts_spent` counts only
        rows already classified, so this decrements no budget. Closing the row
        is not optional — leaving it RUNNING collides the node's next attempt
        with §10.3's partial unique index, keeps §7.6's watchdog polling an
        attempt nobody is running, and makes §7.7 adjudicate a late arrival
        ACCEPTED rather than SUPERSEDED.
        """
        def extra(lifecycle: st.NodeLifecycle):
            return [(
                "UPDATE attempts SET state=?"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (CLOSED_ATTEMPT_STATE.value, run_id, node_id,
                 lifecycle.attempt_no))]
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="scheduler",
            reason="resume:result-declared",
            require_state=(st.NodeState.RUNNING,), extra_writes=extra)

    # ── operator escapes (§11.3) ────────────────────────────────────────────

    def _require_escape_legal(self, run_id: str) -> None:
        outcome = self.latest_outcome(run_id)
        if outcome not in (st.RunOutcome.BLOCKED, st.RunOutcome.STUCK):
            raise EscapeRefused(
                f"{run_id}: escapes are legal only against a run declared BLOCKED or "
                f"STUCK (§7.3, §11.2); latest outcome is "
                f"{outcome.value if outcome else 'NULL'} — an escape against an "
                "undeclared run would race a scheduler that may still be alive")

    @serialized
    def _scheduler_claim(self, run_id: str) -> Tuple[Optional[int], Optional[str]]:
        row = self.conn.execute(
            "SELECT scheduler_pid, scheduler_host FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
        if row is None:
            raise UnknownNode(f"run {run_id} does not exist")
        pid = int(row[0]) if row[0] is not None else None
        return pid, row[1]

    def _require_scheduler_dead(self, run_id: str) -> None:
        """RUNNING is escapable only when the scheduler is provably not a process.

        The node-state gate (`require_state=BLOCKED`) existed to stop an
        escape racing a scheduler that may still own the node. That reason is
        entirely a run-level liveness fact. Fail closed on UNKNOWN.
        """
        pid, recorded_host = self._scheduler_claim(run_id)
        record = RunRecord(
            run_id=run_id, plan_digest="", created_at="", last_transition_at="",
            latest_outcome=None, latest_outcome_at=None, cancel_requested=False,
            scheduler_pid=pid, scheduler_host=recorded_host)
        alive = scheduler_liveness(record)
        if alive is True:
            raise SchedulerStillAlive(
                f"{run_id}: scheduler pid {pid} is still alive on "
                f"{recorded_host or scheduler_host()}; an escape against a "
                "RUNNING node would race a scheduler that is still there")
        if alive is None:
            if not pid or pid <= 0:
                condition = "no scheduler pid is recorded"
            else:
                condition = (
                    f"scheduler pid {pid} was recorded on {recorded_host}, "
                    "not this host")
            raise SchedulerLivenessUnknown(
                f"{run_id}: scheduler liveness is unknown ({condition}); "
                "refusing rather than guessing the scheduler is dead")

    def _close_running_attempt(
            self, run_id: str, node_id: str, *,
            retry_class: Optional[st.RetryClass] = None):
        """Close the live attempt row in the same transaction as the escape.

        The row stays; only its state (and optional classification) change.
        Leaving it RUNNING collides the next attempt with §10.3's partial
        unique index and makes §7.7 adjudicate a late arrival ACCEPTED.
        """
        def extra(lifecycle: st.NodeLifecycle):
            if retry_class is None:
                return [(
                    "UPDATE attempts SET state=? "
                    "WHERE run_id=? AND node_id=? AND state=?",
                    (CLOSED_ATTEMPT_STATE.value, run_id, node_id,
                     st.NodeState.RUNNING.value))]
            return [(
                "UPDATE attempts SET retry_class=?, state=? "
                "WHERE run_id=? AND node_id=? AND attempt_no=?",
                (retry_class.value, CLOSED_ATTEMPT_STATE.value,
                 run_id, node_id, lifecycle.attempt_no))]
        return extra

    def _prepare_stranded_running(self, run_id: str, node_id: str, *,
                                    retry_class: Optional[st.RetryClass] = None):
        """If the node is RUNNING, demand a dead scheduler and close the attempt.

        Returns `(extra_writes, detail)` for `_transition_node`. BLOCKED is
        unchanged. Other states fall through to `require_state`.
        """
        current = self.get_node(run_id, node_id).state
        if current is not st.NodeState.RUNNING:
            return None, None
        self._require_scheduler_dead(run_id)
        return (self._close_running_attempt(run_id, node_id,
                                            retry_class=retry_class),
                {"scheduler_liveness": False})

    def retry(self, run_id: str, node_id: str, *, force: bool = False,
              grant: int = 0) -> st.NodeLifecycle:
        """BLOCKED -> PENDING, or stranded RUNNING -> PENDING when the
        scheduler is provably dead. `force` grants exactly one extra attempt
        beyond the semantic ceiling, and `grant` grants exactly that many,
        neither raising the cap itself (§7.5, §11.3).

        `grant` exists because the escape was capped at +1 by construction, in
        the situation that needs more than +1 by construction (#81). The grant
        is spent by a *cumulative* count — `review_attempts_total` and
        `semantic_attempts_total` are scoped to `(run_id, node_id)` across
        every base and never decrease — so a node already past its ceiling
        needs a grant sized to the distance, not another +1. Repeating
        `--force` cannot supply that distance: the first call moves the node to
        PENDING and `require_state` below then refuses the second, which is
        the guard doing its job rather than a bug in it. So the magnitude
        belongs on the one call the state admits.

        The escape's *identity* is unchanged: any grant is still
        `Escape.RETRY_FORCE`, because a grant of three is the same operator
        decision as a grant of one taken three rounds later, and a second
        escape verb would have to be threaded through `exits_for` and every
        refusal surface to say nothing new. The magnitude is a typed field on
        the transition's detail, where `run status` reads it back, rather than
        a distinction in the vocabulary.
        """
        if grant < 0:
            raise EscapeRefused(
                f"{node_id}: retry grant must be positive, got {grant}")
        if grant and force:
            # Not a style preference: `--force` *is* a grant of one, so
            # accepting both would leave the total silently ambiguous between
            # 1, `grant`, and `grant + 1` — the exact arithmetic an operator
            # sizing a grant cannot afford to guess at.
            raise EscapeRefused(
                f"{node_id}: --force is a grant of one; pass one of "
                "--force or --grant, never both")
        delta = grant if grant else (1 if force else 0)
        self._require_escape_legal(run_id)
        extra, detail = self._prepare_stranded_running(
            run_id, node_id, retry_class=st.RetryClass.ENVIRONMENTAL)
        reason = st.Escape.RETRY_FORCE.value if delta else st.Escape.RETRY.value
        detail = dict(detail or {})
        if delta:
            detail["granted_extra_delta"] = delta
        return self._transition_node(
            run_id, node_id, st.NodeState.PENDING, actor="operator", reason=reason,
            require_state=(st.NodeState.BLOCKED, st.NodeState.RUNNING),
            granted_extra_delta=delta,
            detail=detail or None, extra_writes=extra)

    def _merge_evidence(self, run_id: str, node_id: str) -> MergeEvidence:
        """Count what the ledger holds about this node's evidence chain.

        Read *before* the transition is written, and that ordering matters:
        `skip` closes a stranded RUNNING attempt in the same transaction, so
        counting afterwards would count a row the escape itself had just
        changed. Every figure here is a `COUNT(*)` over rows the run wrote,
        which is what makes the record a fact rather than an assertion (§1.2).
        """
        verified = self.conn.execute(
            "SELECT COUNT(*) FROM transitions"
            " WHERE run_id=? AND node_id=? AND kind='node' AND to_state=?",
            (run_id, node_id, st.NodeState.VERIFIED.value)).fetchone()
        attempts = self.attempts_for(run_id, node_id)
        return MergeEvidence(
            verified_transitions=int(verified[0]) if verified else 0,
            review_rejections=rp.review_attempts_total(attempts, node_id),
            attempts_recorded=len(attempts),
            block_reason=self.get_node(run_id, node_id).block_reason)

    def skip(self, run_id: str, node_id: str, *, accept_sha: str, repo_path) -> st.NodeLifecycle:
        """BLOCKED -> MERGED, or stranded RUNNING -> MERGED when the
        scheduler is provably dead: the operator supplied the work by hand.
        Verifies `git merge-base --is-ancestor` and the four worktree checks
        against the supplied SHA before accepting — it does not bypass those
        gates (§11.3).

        The state it writes is `MERGED` and the cause it stamps is
        `OPERATOR_ACCEPTED`, and those are two facts rather than one said
        twice. The state is what the scheduler and the merge frontier read:
        the node is done, its descendants are eligible, and nothing about
        that changes because an operator supplied the work. The cause is what
        every *reader* needs and none of them had — `run status` and the
        visualizer reported an operator-accepted node identically to one the
        run merged, so the only way to tell them apart was the integration
        branch's git log, where a merged lane leaves a merge commit and this
        one leaves only the attempt commit (#93, §1.2).

        The evidence chain §1.1 item 4 requires is recorded as what the
        ledger could show, on the transition, in the same write. Not as a
        refusal: an operator who has done the work by hand and proved its
        identity five ways is exercising the escape §11.3 exists for, and a
        `skip` that refused an unreviewed node would leave the operator
        exactly where issue #81 left them. What was missing was never the
        permission — it was the record that the chain is absent, which is
        what makes an audit possible afterwards instead of a re-derivation
        from git.
        """
        self._require_escape_legal(run_id)
        evidence = self._merge_evidence(run_id, node_id)
        extra, detail = self._prepare_stranded_running(run_id, node_id)
        repo = Path(repo_path)
        latest_attempt = self.conn.execute(
            "SELECT base_sha FROM attempts"
            " WHERE run_id=? AND node_id=? ORDER BY attempt_no DESC LIMIT 1",
            (run_id, node_id)).fetchone()
        if latest_attempt is None:
            raise SkipAncestryRefused(
                f"{node_id}: no attempt base exists; skip cannot prove output identity")
        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True)
        if branch.returncode != 0:
            raise SkipAncestryRefused(
                f"{node_id}: {repo_path} has no checked-out branch; "
                "skip does not bypass worktree identity (§11.3)")
        # Checked before the predicate below, which folds shape, existence and
        # ancestry into one boolean and so can only be reported as the last of
        # the three. An abbreviated SHA fails on shape and was told it did not
        # descend from its base -- about a commit that descended from it
        # perfectly well, in a repository where `git merge-base --is-ancestor`
        # agreed (#78). The requirement itself is not relaxed: a canonical
        # digest is what makes the recorded identity durable, and an
        # abbreviation is ambiguous by construction. What changes is that the
        # refusal names the actual defect and the one-line remedy.
        if not wt.is_object_digest(accept_sha):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not a full object digest; skip "
                "records a durable identity and an abbreviated SHA is "
                "ambiguous by construction. Pass the full 40- or 64-hex "
                f"digest: git -C {repo_path} rev-parse {accept_sha}")
        if not wt.is_valid_output_commit(
                repo, accept_sha, expected_base=str(latest_attempt[0])):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not a valid output commit descending "
                f"from attempt base {latest_attempt[0]} in {repo_path}; "
                "skip does not bypass identity (§11.3)")
        if not _is_ancestor(repo_path, accept_sha):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not an ancestor of HEAD in {repo_path}; "
                "skip does not bypass the ancestry proof (§11.3)")
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True)
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", accept_sha],
            capture_output=True, text=True)
        if head.returncode != 0 or resolved.returncode != 0 or (
                resolved.stdout.strip() != head.stdout.strip()):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is an older ancestor of HEAD in {repo_path}; "
                "skip accepts only the current HEAD (§11.3)")
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True)
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise SkipAncestryRefused(
                f"{node_id}: {repo_path} is not a clean worktree at {accept_sha}; "
                "skip does not bypass cleanliness (§11.3)")
        detail = dict(detail or {})
        detail[MERGE_EVIDENCE_KEY] = evidence.as_detail()
        return self._transition_node(
            run_id, node_id, st.NodeState.MERGED, actor="operator",
            reason=st.Escape.SKIP.value,
            require_state=(st.NodeState.BLOCKED, st.NodeState.RUNNING),
            output_sha=accept_sha,
            merge_cause=st.MergeCause.OPERATOR_ACCEPTED,
            detail=detail, extra_writes=extra)

    def abandon(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        """Any non-absolutely-terminal state -> CANCELLED (§7.3, §7.8, §11.3).
        Absolutely terminal from here on — stamped `ABANDONED`, the cause a
        resume never reopens (§7.8): the operator adjudicated this node as
        work the run should finish without, which is a decision about the work
        rather than a request to stop the machine. Descendants become
        derived-unready (§8.7) with no state written for them at all.

        A RUNNING node is admitted only when the scheduler is provably dead —
        the same liveness fact retry/skip consult. BLOCKED needs no probe:
        the declaration already says the scheduler left.

        Closes the node's live attempt row in the same transaction, for the
        same reason `cancel_run` does: `abandon` is the node-level form of
        cancellation, and a result arriving from the abandoned attempt must
        adjudicate as SUPERSEDED (§7.7)."""
        self._require_escape_legal(run_id)
        extra, _detail = self._prepare_stranded_running(run_id, node_id)
        if extra is None:
            extra = self._close_running_attempt(run_id, node_id)
        return self._transition_node(
            run_id, node_id, st.NodeState.CANCELLED, actor="operator",
            reason=st.Escape.ABANDON.value,
            cancel_cause=st.CancelCause.ABANDONED, extra_writes=extra)


# ── the read-only projection the operator's read verbs use (§11.1) ───────────

@dataclass(frozen=True)
class RunRecord:
    """One `runs` row, as the operator reads it.

    `latest_outcome` is `None` while no scheduler has ever declared quiescence
    for this run — which is exactly the shape of a run that is still going, and
    the reason the column is nullable rather than defaulted to a fifth outcome.
    """

    run_id: str
    plan_digest: str
    created_at: str
    last_transition_at: str
    latest_outcome: Optional[st.RunOutcome]
    latest_outcome_at: Optional[str]
    cancel_requested: bool
    #: Why `latest_outcome` was CANCELLED (§7.3), and `None` for every other
    #: outcome and for a ledger written before the column. This is the fact an
    #: operator needs to know whether `run resume` will take the run back.
    cancel_cause: Optional[st.CancelCause] = None
    #: The scheduler process that last claimed the run, and the host whose pid
    #: namespace it belongs to. `None` on a ledger written before the columns
    #: existed, which is why `scheduler_liveness` answers `None` there rather
    #: than guessing: an absent pid is not a dead one.
    scheduler_pid: Optional[int] = None
    scheduler_host: Optional[str] = None
    scheduler_claimed_at: Optional[str] = None
    #: The start time of the claiming process, recorded beside its pid so the
    #: two together are an identity rather than a number the kernel may hand
    #: to somebody else. `None` on a ledger written before the column and on a
    #: platform that cannot answer; `scheduler_signal_pid` reads both as
    #: unproven, which is the only safe direction (#37).
    scheduler_start_epoch: Optional[float] = None


@dataclass(frozen=True)
class NodeRow:
    """One node's plan shape joined to its lifecycle row, for display only."""

    node_id: str
    kind: str
    depth: int
    needs: Tuple[str, ...]
    state: st.NodeState
    attempt_no: int
    block_reason: Optional[st.BlockReason]
    output_sha: Optional[str]
    granted_extra_attempts: int
    updated_at: str
    #: How this node reached MERGED (§7.3, §11.3), and `None` both for a node
    #: that is not MERGED and for a MERGED row written before the column
    #: existed. The two `None`s are told apart by `state`, exactly as
    #: `cancel_cause`'s are, and `st.merge_cause_label` is the one place that
    #: does it — a reader must not re-derive the pair (RC1).
    merge_cause: Optional[st.MergeCause] = None

    @property
    def merge_provenance(self) -> Optional[str]:
        """`SCHEDULER`, `OPERATOR_ACCEPTED`, `UNRECORDED`, or `None`."""
        return st.merge_cause_label(self.state, self.merge_cause)


# ── the run's live state, derived rather than remembered (§7.3, §11.2) ───────

#: What `derive_run_state` can answer. Two of these are new, and both name a
#: fact the old vocabulary could not express:
#:
#: * `CANCELLED` — the cancellation *finished*. `CANCELLING` used to be
#:   returned for the whole remaining life of any run whose `cancel_requested`
#:   flag was set, so run-1907d9c1f9d84def80272cb39b5fc137 sat at CANCELLING
#:   permanently with all 14 of its nodes correctly CANCELLED. Deleting the row
#:   was the only way to clear it.
#: * `ABANDONED` — the run has no scheduler process behind it and never
#:   declared an outcome. run-75dfc6914946487f998453fefb51a0cf read RUNNING for
#:   half an hour after its scheduler died, with two nodes RUNNING, no pid, and
#:   nothing left alive to move them.
RUN_STATES: Tuple[str, ...] = (
    "EMPTY", "PENDING", "RUNNING", "BLOCKED", "QUIESCENT",
    "CANCELLING", "CANCELLED", "MERGED", "ABANDONED",
)


def scheduler_liveness(
        record: RunRecord, *,
        is_alive: Callable[[int], bool] = wd.process_is_alive,
        host: Optional[str] = None) -> Optional[bool]:
    """Is a scheduler process still behind this run? `None` = cannot be said.

    Three answers, and the third is the one that keeps this honest:

    * `True` — a pid was recorded on this host and the process exists. Pid
      reuse can make this wrong, and it is wrong in the *safe* direction: the
      run keeps being reported exactly as it is today.
    * `False` — a pid was recorded on this host and no such process exists.
      That is a structural fact about the process table, read with
      `os.kill(pid, 0)` and nothing else. Not stdout, not a log line, not a
      pane (§1.2).
    * `None` — no pid was recorded (a ledger older than the column), or it was
      recorded on another host, whose pid namespace this machine cannot be
      asked about. Host identity is the short hostname, case-insensitive:
      a recorded FQDN still matches its own first label, so a DHCP suffix
      change does not turn this machine into a stranger. Callers must treat
      `None` as "unknown" and never as "dead": declaring a live run dead is
      the failure that would strand working work.
    """
    pid = record.scheduler_pid
    if not pid or pid <= 0:
        return None
    recorded_host = record.scheduler_host
    current_host = host if host is not None else scheduler_host()
    if recorded_host and not _same_scheduler_host(recorded_host, current_host):
        return None
    return bool(is_alive(int(pid)))


def scheduler_signal_pid(
        record: RunRecord, *,
        is_alive: Callable[[int], bool] = wd.process_is_alive,
        start_epoch: Callable[[int], Optional[float]] = wd.process_start_epoch,
        host: Optional[str] = None) -> Optional[int]:
    """The pid it is legal to signal, or `None` when identity is unproven.

    `scheduler_liveness` answers "is there a process with this number?", and
    that is a weak witness for anything that intends to *act* on the process.
    The kernel reuses pids; a pid the scheduler released and an unrelated
    program then acquired answers `True` to that question, and signalling it
    would deliver a SIGINT to a stranger. Liveness is a precondition here, not
    the proof.

    Identity is the recorded start epoch compared against the live process's
    start epoch — a float equality over two recorded numbers, not a judgement
    about text. `watchdog.process_start_epoch` resolves finer than a second,
    so a pid reused within the same second the original process started still
    reports a different start and reads as unproven (#37). A whole-second
    `started <= scheduler_claimed_at` comparison would not distinguish that
    case, which is why the epoch is recorded rather than derived from the
    claim timestamp.

    Three ways to be unproven, and all of them return `None` rather than the
    pid: liveness is not `True`; no start epoch was recorded (a ledger written
    before the column, or a platform that cannot answer); the live process's
    start does not equal the recorded one. Unproven is never authority.
    """
    if scheduler_liveness(record, is_alive=is_alive, host=host) is not True:
        return None
    pid = int(record.scheduler_pid)
    recorded = record.scheduler_start_epoch
    if recorded is None:
        return None
    started = start_epoch(pid)
    if started is None:
        return None
    if started != recorded:
        return None
    return pid


def derive_run_state(
        record: RunRecord, nodes: Sequence[NodeRow], *,
        is_alive: Callable[[int], bool] = wd.process_is_alive,
        host: Optional[str] = None) -> str:
    """What the run *is*, computed from durable facts alone.

    `runs.latest_outcome` is written by exactly one actor — a live scheduler
    declaring quiescence — so it says nothing at all about a scheduler that
    never got to declare. A run's state therefore cannot be a thing a process
    remembers to write; it has to be derivable from what is already durable:
    the node rows, the cancellation flag, and whether the pid that claimed the
    run is still a process. All three are typed records (§1.2). Nothing here
    reads prose, and this function performs no write — `LifecycleReader` opens
    the database `mode=ro` precisely so that asking cannot change the answer.

    Order matters, and it is chosen so that no branch can claim liveness it has
    not established:

    1. A run with no nodes is EMPTY.
    2. A run whose every node is absolutely terminal is *settled*, whatever any
       process is doing. Cancellation that reached every node is CANCELLED, not
       CANCELLING — this is the second observed defect, and it needs no
       liveness check at all because the node rows already say it.

       This is also the one branch that reads `latest_outcome`. Live node
       rows that already contradict a leftover declaration win. `run resume`
       clears `cancel_requested` but deliberately retains
       `latest_outcome=CANCELLED` until the scheduler declares again. Once
       every reopened node is MERGED the rows say MERGED; treating the
       leftover declaration as the live state is §19 M5's stale-outcome
       projection during the final-acceptance window. Mixes of MERGED and
       CANCELLED still read the declaration so an abandon-by-node run stays
       CANCELLED — §7.3's outcome function declares CANCELLED and sets no
       `cancel_requested`, and the declaration is the typed statement that
       it is. A resume still in flight returns nodes to PENDING and leaves
       this branch entirely.
    3. Otherwise there is work left. Work left with a provably dead scheduler
       is ABANDONED, but only where the death matters: a run with a node still
       RUNNING (nothing can finish it), or one that never declared an outcome
       (nothing will ever declare one). A run that *did* declare — BLOCKED,
       say — and then exited is reported by its node states as before; its
       scheduler is supposed to be gone.
    4. Only then may the node states be read as live.
    """
    states = [node.state for node in nodes]
    if not states:
        return "EMPTY"
    if all(state in st.ABSOLUTELY_TERMINAL for state in states):
        all_merged = all(state is st.NodeState.MERGED for state in states)
        if all_merged and not record.cancel_requested:
            return "MERGED"
        if (record.cancel_requested
                or record.latest_outcome is st.RunOutcome.CANCELLED):
            return "CANCELLED"
        return "QUIESCENT"
    running = any(state is st.NodeState.RUNNING for state in states)
    alive = scheduler_liveness(record, is_alive=is_alive, host=host)
    if alive is False and (running or record.latest_outcome is None):
        return "ABANDONED"
    if record.cancel_requested:
        return "CANCELLING"
    if running:
        return "RUNNING"
    if any(state is st.NodeState.BLOCKED for state in states):
        return "BLOCKED"
    if any(state is st.NodeState.PENDING for state in states):
        return "PENDING"
    return "QUIESCENT"


class LifecycleReader:
    """Every read the operator's read verbs make, and no write at all.

    `LifecycleStore.__init__` runs `ensure_dir`, switches journal mode, and
    executes `SCHEMA` — three writes — because it is the *scheduler's* handle.
    Reusing it for `run status` would make asking about a run that never
    happened create the ledger it asked about, and would take a write lock on
    the database a live scheduler is transacting against. So the read verbs
    get their own handle, opened `mode=ro`, and the queries still live in this
    module rather than in the CLI (§10.6's one-query-path rule).

    Read-only WAL access needs the `-shm` file, which SQLite deletes on the
    last clean close. When it is gone there is provably no live writer, so the
    fallback to `immutable=1` cannot read behind one — it is the only way to
    read a finished run's ledger at all.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, db_path) -> "LifecycleReader":
        path = Path(db_path)
        if not path.is_file():
            raise LedgerUnavailable(f"no lifecycle database at {path}")
        located = path.resolve().as_uri()[len("file://"):]
        try:
            conn = cls._probed("file:{}?mode=ro".format(located))
        except sqlite3.OperationalError:
            conn = cls._probed("file:{}?mode=ro&immutable=1".format(located))
        conn.row_factory = sqlite3.Row
        return cls(conn)

    @staticmethod
    def _probed(uri: str) -> sqlite3.Connection:
        """Connect and force the open, so a `-shm` refusal surfaces here.

        `sqlite3.connect` is lazy about some failures; the probe makes the two
        read-only modes distinguishable at the one place that chooses between
        them instead of at an arbitrary later query.
        """
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except Exception:
            conn.close()
            raise
        return conn

    def close(self) -> None:
        self.conn.close()

    def _rows(self, sql: str, params: Tuple[Any, ...]) -> Tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(sql, params).fetchall())

    def runs(self, plan_digest: Optional[str] = None) -> Tuple[RunRecord, ...]:
        """Run rows, newest first — the index `run list` renders and `run
        status` resolves a plan name through."""
        # The reader is `mode=ro`, so it cannot migrate a ledger that predates
        # the scheduler-ownership columns — and must not refuse to read one
        # either. Ask the database what it has and select only that; an absent
        # column reads back as `None`, which `scheduler_liveness` already
        # treats as "cannot be said".
        available = set(_table_columns(self.conn, "runs"))
        optional = tuple(name for name, _ in _RUNS_ADDED_COLUMNS
                         if name in available)
        sql = ("SELECT run_id, plan_digest, created_at, last_transition_at,"
               " latest_outcome, latest_outcome_at, cancel_requested"
               + "".join(", " + name for name in optional)
               + " FROM runs")
        params: Tuple[Any, ...] = ()
        if plan_digest is not None:
            sql += " WHERE plan_digest=?"
            params = (plan_digest,)
        return tuple(
            RunRecord(
                run_id=row["run_id"], plan_digest=row["plan_digest"],
                created_at=row["created_at"],
                last_transition_at=row["last_transition_at"],
                latest_outcome=(st.RunOutcome(row["latest_outcome"])
                                if row["latest_outcome"] else None),
                latest_outcome_at=row["latest_outcome_at"],
                cancel_requested=bool(row["cancel_requested"]),
                cancel_cause=(st.CancelCause(row["cancel_cause"])
                              if "cancel_cause" in optional
                              and row["cancel_cause"] else None),
                scheduler_pid=(row["scheduler_pid"]
                               if "scheduler_pid" in optional else None),
                scheduler_host=(row["scheduler_host"]
                                if "scheduler_host" in optional else None),
                scheduler_claimed_at=(row["scheduler_claimed_at"]
                                      if "scheduler_claimed_at" in optional
                                      else None),
                scheduler_start_epoch=(row["scheduler_start_epoch"]
                                       if "scheduler_start_epoch" in optional
                                       else None))
            for row in self._rows(sql + " ORDER BY created_at DESC, run_id DESC",
                                  params))

    def run(self, run_id: str) -> Optional[RunRecord]:
        found = [record for record in self.runs() if record.run_id == run_id]
        return found[0] if found else None

    def nodes(self, run_id: str) -> Tuple[NodeRow, ...]:
        # `mode=ro`, so this reader cannot migrate a ledger that predates
        # `merge_cause` and must not refuse to read one either — the same
        # rule `runs()` follows for the scheduler-ownership columns. An
        # absent column is simply not selected and reads back `None`, which
        # `st.merge_cause_label` renders as `UNRECORDED` for a MERGED row
        # rather than guessing `SCHEDULER`.
        available = set(_table_columns(self.conn, "node_lifecycle"))
        merge_cause_sql = (" l.merge_cause,"
                           if "merge_cause" in available else " NULL AS merge_cause,")
        rows = self._rows(
            "SELECT d.node_id, d.kind, d.depth, d.needs_json, l.state,"
            " l.attempt_no, l.block_reason, l.output_sha,"
            + merge_cause_sql +
            " l.granted_extra_attempts, l.updated_at"
            " FROM dag_nodes d JOIN node_lifecycle l"
            " ON l.run_id = d.run_id AND l.node_id = d.node_id"
            " WHERE d.run_id=? ORDER BY d.depth, d.node_id", (run_id,))
        return tuple(
            NodeRow(
                node_id=row["node_id"], kind=row["kind"], depth=row["depth"],
                needs=tuple(json.loads(row["needs_json"])),
                state=st.NodeState(row["state"]), attempt_no=row["attempt_no"],
                block_reason=(st.BlockReason(row["block_reason"])
                              if row["block_reason"] else None),
                output_sha=row["output_sha"],
                merge_cause=(st.MergeCause(row["merge_cause"])
                             if row["merge_cause"] else None),
                granted_extra_attempts=row["granted_extra_attempts"],
                updated_at=row["updated_at"])
            for row in rows)

    def attempts(self, run_id: str) -> Tuple[st.AttemptRecord, ...]:
        rows = self._rows(
            "SELECT node_id, attempt_no, base_sha, state, started_at,"
            " launched_at, pid, turn_count, retry_class, extra_json"
            " FROM attempts WHERE run_id=? ORDER BY node_id, attempt_no",
            (run_id,))
        return tuple(
            st.AttemptRecord(
                run_id=run_id, node_id=row["node_id"],
                attempt_no=row["attempt_no"], base_sha=row["base_sha"],
                state=st.NodeState(row["state"]),
                started_at=row["started_at"] or 0.0,
                launched_at=row["launched_at"], pid=row["pid"],
                turn_count=row["turn_count"] or 0,
                retry_class=(st.RetryClass(row["retry_class"])
                             if row["retry_class"] else None),
                extra=json.loads(row["extra_json"]))
            for row in rows)

    def transitions(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        return tuple(_audit_dict(row) for row in self._rows(
            "SELECT * FROM transitions WHERE run_id=? ORDER BY id", (run_id,)))

    def results(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        return tuple(_audit_dict(row) for row in self._rows(
            "SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,)))
