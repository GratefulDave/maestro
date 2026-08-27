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

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

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


class ReviewSchemaMigrationFailed(LifecycleError):
    """Persistent-review schema setup was restored from its SQLite backup."""


class LegacyReviewMigrationBlocked(LifecycleError):
    """Legacy evidence is unsafe, so this lane must not dispatch another review."""

    def __init__(self, run_id: str, build_node_id: str, reason: str) -> None:
        super().__init__(
            f"{run_id}/{build_node_id}: legacy review migration is blocked: {reason}"
        )
        self.run_id = run_id
        self.build_node_id = build_node_id
        self.reason = reason


@dataclass(frozen=True)
class LegacyReviewEvidence:
    """One externally verified terminal inline-review record.

    The lifecycle ledger cannot read detached signed receipts or repositories.
    Callers therefore provide the immutable receipt facts and a validator below
    proves their digest/signature binding before this record becomes authority.
    """

    build_node_id: str
    candidate_seq: int
    candidate_sha: str
    base_sha: str
    review_digest: str
    receipt_path: str
    verdict: st.ReviewVerdict
    findings: Sequence[Mapping[str, Any]]
    builder_generation: int = 0
    reviewer_generation: int = 0


@dataclass(frozen=True)
class LegacyReviewMigration:
    """The durable disposition for one lane's pre-ledger review evidence."""

    build_node_id: str
    migrated: bool
    blocked: bool
    reason: Optional[str]
    candidates: Tuple[st.LaneCandidate, ...] = ()
    reviews: Tuple[st.CandidateReview, ...] = ()


@dataclass(frozen=True)
class TestGateEvidenceRecord:
    """One test candidate's measured gate strength, as the ledger holds it."""

    tests_node_id: str
    candidate_sha: str
    runner: str
    selector: str
    strong: bool
    refusal: Optional[str]
    evidence: Mapping[str, Any]
    created_at: str
    #: Whether *this* call inserted the row. A replay reads False, which is
    #: how a resumed scheduler tells "measured now" from "measured before the
    #: crash" without comparing timestamps.
    created: bool = False


@dataclass(frozen=True)
class TestPairing:
    """The exact (accepted test bytes, implementation bytes) pair."""

    build_node_id: str
    tests_node_id: str
    accepted_test_sha: str
    implementation_sha: str
    verifier_command: str
    selector: str
    executed_cases: int
    coverage: Mapping[str, Any]
    created_at: str


#: The classification a tests node carries when it reached a terminal state
#: under the legacy contract with no evidence uniquely attributable to its
#: candidate. Informational by construction: it names what is unproven, and
#: nothing keys a transition on it unless an operator migrates the run.
LEGACY_TEST_STRENGTH_UNPROVEN = "LEGACY_TEST_STRENGTH_UNPROVEN"


@dataclass(frozen=True)
class LegacyTestStrengthFinding:
    """One tests node's disposition under the new contract, for an old run."""

    tests_node_id: str
    state: str
    candidate_sha: Optional[str]
    classification: str
    blocking: bool
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class TestStrengthMigrationReport:
    """What migrating one run would do, or did. Dry-run and apply share it."""

    run_id: str
    contract: str
    applied: bool
    backup_path: Optional[str]
    findings: Tuple[LegacyTestStrengthFinding, ...] = ()
    blocked_nodes: Tuple[str, ...] = ()
    migrated_nodes: Tuple[str, ...] = ()
    reason: str = ""


#: The transition `reason` an operator's `--allow-exhausted-node` leaves
#: behind. Declared once so the writer and every reader name the same string,
#: rather than each spelling a literal that drifts.
NODE_BUDGET_ALLOWANCE_REASON = "allow-exhausted-node"


# ── schema ───────────────────────────────────────────────────────────────────


def _candidate_reviews_ddl(table: str, *, if_not_exists: bool) -> str:
    """The one candidate-review shape used for creation and table rebuilds."""
    prefix = "IF NOT EXISTS " if if_not_exists else ""
    return f"""CREATE TABLE {prefix}{table} (
  run_id              TEXT NOT NULL REFERENCES runs(run_id),
  review_node_id      TEXT NOT NULL,
  candidate_sha       TEXT NOT NULL CHECK (
    length(candidate_sha) IN (40, 64)
    AND candidate_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  reviewer_generation INTEGER NOT NULL CHECK (reviewer_generation >= 0),
  state               TEXT NOT NULL CHECK (
    state IN ('PUBLISHED', 'DISPATCHED', 'COMPLETED')),
  dispatched_at       TEXT,
  review_digest       TEXT,
  receipt_path        TEXT,
  findings_json       TEXT NOT NULL CHECK (
    json_valid(findings_json) AND json_type(findings_json) = 'array'),
  verdict             TEXT CHECK (verdict IS NULL OR verdict IN ('PASS', 'REJECTED')),
  completed_at        TEXT,
  CHECK (
    (state = 'PUBLISHED' AND dispatched_at IS NULL AND verdict IS NULL
     AND review_digest IS NULL AND receipt_path IS NULL AND completed_at IS NULL)
    OR
    (state = 'DISPATCHED' AND dispatched_at IS NOT NULL AND verdict IS NULL
     AND review_digest IS NULL AND receipt_path IS NULL AND completed_at IS NULL)
    OR
    (state = 'COMPLETED' AND verdict IN ('PASS', 'REJECTED')
     AND review_digest IS NOT NULL AND receipt_path IS NOT NULL
     AND completed_at IS NOT NULL)),
  PRIMARY KEY (run_id, review_node_id, candidate_sha)
);"""


SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS runs (
  run_id             TEXT PRIMARY KEY,
  plan_digest        TEXT NOT NULL,
  -- The plan's title, written at run creation so the console does not have
  -- to re-hash plan files to name a run (§16.3 item 61). NULL on a ledger
  -- written before the column and on a run of a plan that predates the
  -- field: both read as "no stored name", never as a name invented here.
  plan_name          TEXT,
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
  scheduler_start_epoch REAL,
  -- The test-acceptance contract this run was created under. See
  -- `_RUNS_ADDED_COLUMNS` for why NULL is the legacy pin and why nothing
  -- rewrites this column after `create_run`.
  test_strength_contract TEXT,
  -- Resumes that have reopened a review-budget block in this run. NULL is
  -- zero. See `RESUME_REVIEW_REFRESH_CEILING` for the bound it feeds and
  -- §3.6 A9 for why an unbounded version of it is forbidden.
  review_refresh_count INTEGER
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
  -- NULL for authored nodes.  A derived review row stores its one source
  -- build node explicitly, never as an incidental convention in its id.
  review_of    TEXT,
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
  -- How this node reached PENDING after leaving it (`st.PendingCause`),
  -- NULL in every other state and on a seeded PENDING that never left
  -- the frontier. The twin of `merge_cause` one state earlier: PENDING
  -- was one word carrying three facts (operator retry, resume reopen,
  -- scheduler fail_attempt), and the distinction lived only in
  -- transition prose (#103). NULL on a PENDING row written before this
  -- column means *unrecorded*, never `SCHEDULER`.
  pending_cause          TEXT,
  -- Persistent authority for a reviewable build lane.  Ordinary DAG nodes
  -- remain NULL, including rows written before this lifecycle existed.
  lane_phase             TEXT CHECK (lane_phase IS NULL OR lane_phase IN (
    'BUILDING', 'CANDIDATE_READY', 'REVIEWING', 'REPAIR_HANDOFF',
    'REPAIRING', 'WAITING_FOR_NEW_CANDIDATE', 'ACCEPTED', 'BLOCKED',
    'CANCELLED')),
  output_sha             TEXT,
  granted_extra_attempts INTEGER NOT NULL DEFAULT 0,
  -- The attempt number this node's retry-class spend is counted *from*.
  -- Written at a typed boundary and nowhere else: `run resume` raises it for
  -- every node in the run, an operator `retry` raises it for the one node it
  -- names, and in both cases to the highest attempt already classified at
  -- that instant. `attempts_spent` then counts only attempts above it, so a
  -- node is charged for what it spent since the boundary rather than for
  -- everything it ever spent (§7.5, §11.3).
  --
  -- A floor rather than a decrement, because the attempt rows are the
  -- evidence chain: refreshing a budget by deleting or rewriting what it
  -- counted would destroy the record of the failures that produced it.
  -- Moving where the count starts leaves every row exactly as it was.
  --
  -- Nullable with no default, so a ledger written before the column reads
  -- NULL, and NULL is read as 0 -- no boundary recorded, counted from the
  -- beginning of the node's life, which is the behaviour that predates it.
  retry_spend_floor      INTEGER,
  -- The retained-lane twin of retry_spend_floor, expressed as the highest
  -- durable cycle_seq recorded before the latest operator boundary.
  lane_retry_spend_floor INTEGER,
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
  -- The host whose pid namespace `pid` was taken from, and the start
  -- time of that process. A pid is not an identity: it is only
  -- meaningful on the machine that issued it, and the kernel reuses it.
  -- Recorded beside the pid so `attempt_liveness` can *decline* rather
  -- than answer wrongly. NULL on a ledger written before these columns,
  -- and when no pid was recorded -- both read as unknown, never as
  -- dead and never as alive.
  attempt_host TEXT,
  attempt_start_epoch REAL,
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
CREATE TABLE IF NOT EXISTS lane_candidates (
  run_id               TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id        TEXT NOT NULL,
  candidate_seq        INTEGER NOT NULL CHECK (candidate_seq > 0),
  candidate_sha        TEXT NOT NULL CHECK (
    length(candidate_sha) IN (40, 64)
    AND candidate_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  parent_candidate_sha TEXT CHECK (
    parent_candidate_sha IS NULL OR (
      length(parent_candidate_sha) IN (40, 64)
      AND parent_candidate_sha NOT GLOB '*[^0-9A-Fa-f]*')),
  builder_generation   INTEGER NOT NULL CHECK (builder_generation >= 0),
  created_at           TEXT NOT NULL,
  PRIMARY KEY (run_id, build_node_id, candidate_seq),
  UNIQUE (run_id, build_node_id, candidate_sha)
);
"""
    + _candidate_reviews_ddl("candidate_reviews", if_not_exists=True)
    + """
CREATE TABLE IF NOT EXISTS repair_handoffs (
  run_id                TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id         TEXT NOT NULL,
  rejected_candidate_sha TEXT NOT NULL CHECK (
    length(rejected_candidate_sha) IN (40, 64)
    AND rejected_candidate_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  findings_json         TEXT NOT NULL CHECK (
    json_valid(findings_json) AND json_type(findings_json) = 'array'),
  state                 TEXT NOT NULL CHECK (
    state IN ('PENDING', 'SUBMITTED', 'ACKNOWLEDGED', 'FAILED')),
  builder_generation    INTEGER NOT NULL CHECK (builder_generation >= 0),
  submitted_at          TEXT,
  acknowledged_at       TEXT,
  CHECK (
    (state = 'PENDING' AND submitted_at IS NULL AND acknowledged_at IS NULL)
    OR (state = 'SUBMITTED' AND submitted_at IS NOT NULL AND acknowledged_at IS NULL)
    OR (state = 'ACKNOWLEDGED' AND submitted_at IS NOT NULL
        AND acknowledged_at IS NOT NULL)
    OR (state = 'FAILED' AND acknowledged_at IS NULL)),
  PRIMARY KEY (run_id, build_node_id, rejected_candidate_sha)
);
CREATE TABLE IF NOT EXISTS legacy_review_migration_blocks (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id TEXT NOT NULL,
  reason        TEXT NOT NULL,
  detail_json   TEXT NOT NULL CHECK (
    json_valid(detail_json) AND json_type(detail_json) = 'object'),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, build_node_id)
);
CREATE TABLE IF NOT EXISTS test_gate_evidence (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  tests_node_id TEXT NOT NULL,
  -- The identity of the acceptance. Keyed on the candidate rather than the
  -- node because what is accepted is *those bytes*: a later or substituted
  -- test tree has a different sha, finds no row, and cannot inherit this.
  candidate_sha TEXT NOT NULL CHECK (
    length(candidate_sha) IN (40, 64)
    AND candidate_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  runner        TEXT NOT NULL,
  selector      TEXT NOT NULL,
  strong        INTEGER NOT NULL CHECK (strong IN (0, 1)),
  refusal       TEXT,
  evidence_json TEXT NOT NULL CHECK (
    json_valid(evidence_json) AND json_type(evidence_json) = 'object'),
  created_at    TEXT NOT NULL,
  -- Strength and its refusal are one fact with two representations, so the
  -- table refuses the pair that would let a reader disagree with itself.
  CHECK ((strong = 1 AND refusal IS NULL)
         OR (strong = 0 AND refusal IS NOT NULL)),
  PRIMARY KEY (run_id, tests_node_id, candidate_sha)
);
CREATE TABLE IF NOT EXISTS test_implementation_pairings (
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id      TEXT NOT NULL,
  tests_node_id      TEXT NOT NULL,
  -- The exact accepted test bytes this implementation was verified against.
  accepted_test_sha  TEXT NOT NULL CHECK (
    length(accepted_test_sha) IN (40, 64)
    AND accepted_test_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  implementation_sha TEXT NOT NULL CHECK (
    length(implementation_sha) IN (40, 64)
    AND implementation_sha NOT GLOB '*[^0-9A-Fa-f]*'),
  verifier_command   TEXT NOT NULL,
  selector           TEXT NOT NULL,
  executed_cases     INTEGER NOT NULL CHECK (executed_cases >= 0),
  coverage_json      TEXT NOT NULL CHECK (
    json_valid(coverage_json) AND json_type(coverage_json) = 'object'),
  created_at         TEXT NOT NULL,
  PRIMARY KEY (run_id, build_node_id, implementation_sha, tests_node_id)
);
CREATE TABLE IF NOT EXISTS legacy_test_strength_blocks (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  tests_node_id TEXT NOT NULL,
  reason        TEXT NOT NULL,
  detail_json   TEXT NOT NULL CHECK (
    json_valid(detail_json) AND json_type(detail_json) = 'object'),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, tests_node_id)
);
CREATE TABLE IF NOT EXISTS lane_retry_spend (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id TEXT NOT NULL,
  retry_class   TEXT NOT NULL CHECK (retry_class IN (
    'SEMANTIC', 'ENVIRONMENTAL', 'LAUNCHER_TRANSIENT', 'REVIEW_REJECTION',
    'TEST_REVIEW_REJECTION')),
  cycle_seq     INTEGER NOT NULL CHECK (cycle_seq > 0),
  candidate_sha TEXT CHECK (
    candidate_sha IS NULL OR (
      length(candidate_sha) IN (40, 64)
      AND candidate_sha NOT GLOB '*[^0-9A-Fa-f]*')),
  detail_json   TEXT NOT NULL CHECK (
    json_valid(detail_json) AND json_type(detail_json) = 'object'),
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, build_node_id, cycle_seq)
);
-- Every plan this run has executed under, in order, with the bytes.
--
-- The bytes are here because the plan is the one artifact a run depends on
-- that was referenced by content and not *stored* by content. `candidate_sha`,
-- `output_sha`, `base_sha` and `accepted_test_sha` are git objects;
-- `review_digest` resolves into the receipt store at `{digest}.json`, which is
-- why a finalization receipt survives an edit to the plan it finalised.
-- `runs.plan_digest` alone resolved through a *file path*, and `plan ship`
-- overwrites that file — so amending a plan destroyed the only handle the run
-- had to its own plan, and `_resume_run_selection` refused forever.
--
-- Storing the bytes is what makes that refusal unnecessary rather than
-- relaxed: resume resolves the plan from the run's own retained record instead
-- of searching mutable files, so it still cannot be handed a different plan by
-- accident. `seq` is the lineage — seq 1 is what `create_run` adopted, and each
-- amendment appends — so "which nodes merged under which plan" is answerable
-- from the ledger rather than reconstructed.
CREATE TABLE IF NOT EXISTS run_plan_versions (
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  seq         INTEGER NOT NULL CHECK (seq > 0),
  plan_digest TEXT NOT NULL CHECK (
    length(plan_digest) = 64
    AND plan_digest NOT GLOB '*[^0-9A-Fa-f]*'),
  plan_bytes  BLOB NOT NULL,
  adopted_at  TEXT NOT NULL,
  PRIMARY KEY (run_id, seq),
  UNIQUE (run_id, plan_digest)
);
CREATE TABLE IF NOT EXISTS actor_sessions (
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  build_node_id     TEXT NOT NULL,
  actor_role        TEXT NOT NULL,
  generation        INTEGER NOT NULL CHECK (generation >= 0),
  state             TEXT NOT NULL CHECK (state IN ('ACTIVE', 'CLOSED')),
  pane_id           TEXT NOT NULL,
  tab_id            TEXT,
  session_path      TEXT NOT NULL,
  correlation_token TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (run_id, build_node_id, actor_role, generation)
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
CREATE INDEX IF NOT EXISTS idx_lane_candidates_latest
  ON lane_candidates(run_id, build_node_id, candidate_seq DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_reviews_active
  ON candidate_reviews(run_id, review_node_id, state);
CREATE INDEX IF NOT EXISTS idx_repair_handoffs_state
  ON repair_handoffs(run_id, build_node_id, state);
CREATE INDEX IF NOT EXISTS idx_lane_retry_spend_class
  ON lane_retry_spend(run_id, build_node_id, retry_class, cycle_seq);
CREATE INDEX IF NOT EXISTS idx_test_gate_evidence_node
  ON test_gate_evidence(run_id, tests_node_id, strong);
CREATE INDEX IF NOT EXISTS idx_test_pairings_build
  ON test_implementation_pairings(run_id, build_node_id);
CREATE INDEX IF NOT EXISTS idx_legacy_test_strength_blocks
  ON legacy_test_strength_blocks(run_id, tests_node_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_actor_session
  ON actor_sessions(run_id, build_node_id, actor_role) WHERE state='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_legacy_review_migration_blocks
  ON legacy_review_migration_blocks(run_id, build_node_id);
"""
)

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
    ("plan_name", "TEXT"),
    # Which test-acceptance contract this run was **created** under. Written
    # once at run creation and never rewritten: a run is pinned to the rules
    # it started with, so a resumed run keeps its own admission and lifecycle
    # semantics rather than acquiring the ones the binary happens to ship.
    #
    # NULL is the legacy pin and is read as one everywhere: a ledger written
    # before this column, or a run created under `maestro-plan.v3`, accepted
    # its tests nodes on their case count. That is classified, reported, and
    # left standing — never silently upgraded, and never retroactively
    # invalidated (see `legacy_test_strength_audit`).
    ("test_strength_contract", "TEXT"),
    # How many times a resume has reopened a review-budget block in this run.
    # The bound §3.6 A9 requires, made durable: a reviewer's opinion has no
    # fixed point, so `reject -> resume -> repair -> reject` would otherwise run
    # as long as somebody keeps typing `run resume`.
    #
    # NULL is zero and is read as zero everywhere: a ledger written before this
    # column has spent none of its allowance, which is the same thing a fresh
    # run has. Counted rather than timed, because "how many times has this
    # loop gone round" is the question A9 actually asks.
    ("review_refresh_count", "INTEGER"),
)

#: Columns added to `dag_nodes` after the first ledgers were written.  Derived
#: review identity needs its source build recorded structurally so a resumed
#: scheduler never has to infer it from a display id.
_DAG_NODES_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (("review_of", "TEXT"),)

#: The same, for `node_lifecycle`. Kept as a second list rather than folded
#: into the one above because the read-only projection selects the `runs`
#: additions by name and must not be handed a column from another table.
_NODE_LIFECYCLE_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("cancel_cause", "TEXT"),
    ("merge_cause", "TEXT"),
    ("pending_cause", "TEXT"),
    (
        "lane_phase",
        (
            "TEXT CHECK (lane_phase IS NULL OR lane_phase IN "
            "('BUILDING','CANDIDATE_READY','REVIEWING','REPAIR_HANDOFF',"
            "'REPAIRING','WAITING_FOR_NEW_CANDIDATE','ACCEPTED','BLOCKED',"
            "'CANCELLED'))"
        ),
    ),
    ("retry_spend_floor", "INTEGER"),
    ("lane_retry_spend_floor", "INTEGER"),
)

#: Every table an older ledger may be missing a column from, in one place so
#: `_migrate` cannot silently cover one table and not the other.
#: The same, for `attempt_baselines`. Nullable with no default, so a ledger
#: written before the column existed reads NULL — "nobody looked" — rather
#: than `'{}'`, which would claim the tree had no ignored files at base.
_ATTEMPT_BASELINE_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("ignored_json", "TEXT"),
)

#: The same, for `attempts`. Nullable with no default, so a ledger written
#: before these columns existed reads NULL -- no host, no start epoch --
#: and `attempt_liveness` answers unknown rather than guessing the pid
#: is this attempt's own process. A foreign pid that happens to be
#: absent here must not convict a live attempt; a reused pid that
#: happens to be present must not block salvage forever.
_ATTEMPTS_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("attempt_host", "TEXT"),
    ("attempt_start_epoch", "REAL"),
)

#: A tab id was added after actor sessions had already become durable.  It is
#: deliberately nullable: a legacy row may not claim a tab it did not record,
#: and resume must prove one from Herdr's pane metadata rather than invent it.
_ACTOR_SESSION_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (("tab_id", "TEXT"),)

_ADDED_COLUMNS: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    ("runs", _RUNS_ADDED_COLUMNS),
    ("dag_nodes", _DAG_NODES_ADDED_COLUMNS),
    ("node_lifecycle", _NODE_LIFECYCLE_ADDED_COLUMNS),
    ("attempt_baselines", _ATTEMPT_BASELINE_ADDED_COLUMNS),
    ("attempts", _ATTEMPTS_ADDED_COLUMNS),
    ("actor_sessions", _ACTOR_SESSION_ADDED_COLUMNS),
)

#: Raise a node's retry-budget floor to the highest of its attempts that has
#: **already** been classified. One definition of the boundary, rendered for
#: the two scopes that may cross it — a run-wide resume and a per-node retry —
#: so the two can never come to mean different things.
#:
#: "Already classified" is what makes the two writers agree with the accounting
#: each performs around them. `resume_run` charges an inherited RUNNING attempt
#: ENVIRONMENTAL *after* writing the boundary, and an operator `retry` closes a
#: stranded RUNNING attempt the same way in the same transaction; neither row
#: is classified at the instant the floor is computed, so both land above it
#: and are charged against the refreshed budget. §9.7's exemption survives
#: intact too: an inherited attempt that declared an accepted result is closed
#: UNCLASSIFIED and still costs nothing.
#:
#: `MAX(attempt_no)` and not `COUNT(*)`: `attempts_spent` filters the rows it
#: counts by class, and a floor expressed as a count of one class could not
#: say anything about another. An attempt number orders every class at once.
_RETRY_SPEND_FLOOR_SQL = (
    "UPDATE node_lifecycle SET retry_spend_floor = ("
    "SELECT COALESCE(MAX(a.attempt_no), 0) FROM attempts a"
    " WHERE a.run_id = node_lifecycle.run_id"
    " AND a.node_id = node_lifecycle.node_id"
    " AND a.retry_class IS NOT NULL)"
)

#: Every node of one run — the resume boundary is a property of the run.
_RAISE_RUN_RETRY_SPEND_FLOOR = _RETRY_SPEND_FLOOR_SQL + " WHERE run_id=?"

#: Retained correction-loop spend has its own ledger and therefore needs its
#: own boundary. Keep all rows for guidance and evidence; only budget accounting
#: begins after this cycle sequence.
_LANE_RETRY_SPEND_FLOOR_SQL = (
    "UPDATE node_lifecycle SET lane_retry_spend_floor = ("
    "SELECT COALESCE(MAX(s.cycle_seq), 0) FROM lane_retry_spend s"
    " WHERE s.run_id = node_lifecycle.run_id"
    " AND s.build_node_id = node_lifecycle.node_id)"
)
_RAISE_RUN_LANE_RETRY_SPEND_FLOOR = _LANE_RETRY_SPEND_FLOOR_SQL + " WHERE run_id=?"
_RAISE_NODE_LANE_RETRY_SPEND_FLOOR = (
    _LANE_RETRY_SPEND_FLOOR_SQL + " WHERE run_id=? AND node_id=?"
)

#: Resume refreshes every retry ceiling a run needs to continue, so a node
#: blocked because one of them ran out returns to the frontier in the same
#: transaction. Launcher and environmental faults are infrastructure; the
#: semantic ceiling is an adjudication bound, and its inclusion here is a
#: deliberate policy change rather than a tidy-up.
#:
#: It used to be excluded, on §7.5's argument that repeated semantic failure
#: indicates a planning defect rather than bad luck, so the operator should have
#: to look before granting more. §16.3 item 16 already recorded that argument as
#: an assumption rather than a result, and the recovery as a forced retry one
#: attempt at a time.
#:
#: `run-8d1a71f463e4430f92a125a8f8b3731d` is what the exclusion cost, and the
#: ledger carries the whole sequence:
#:
#:   1987  lane-routing-chemical  RUNNING->BLOCKED  SEMANTIC_BUDGET_EXHAUSTED
#:   1988  (run)                  ->BLOCKED         declare-outcome
#:
#: One node over its ceiling stopped a run that still had work left, until a
#: human typed a grant.
#:
#: A note on that citation, because it was briefly removed from this comment as
#: unverifiable and the removal was wrong: the denial came from querying a
#: *scratch copy* of the ledger taken earlier for an unrelated migration probe,
#: and reporting its `max(id)` as the live state. The live file holds 1987 and
#: 1988 exactly as written above. Reading a snapshot and calling it the ledger
#: is the same mistake as reading a passing test and calling it the code.
#:
#: What keeps this from being an off-switch for §7.5 is that it is a *floor*,
#: not a ceiling removal. `_RAISE_RUN_RETRY_SPEND_FLOOR` moves where counting
#: starts; the configured ceiling still applies to everything spent after the
#: boundary, so a node whose plan is genuinely wrong blocks again on the next
#: resume rather than looping forever. Nothing is deleted: the attempts, the
#: spend rows and the transitions are the evidence chain and stay intact.
#:
#: `CREDENTIAL_REFUSED` and the other non-budget blocks are deliberately absent.
#: They are not budgets, no boundary can pay them off, and adding them here
#: would quietly turn "resume refreshes budgets" into "resume clears blocks".
_RESUME_REFRESHED_BLOCK_REASONS = (
    st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED,
    st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
    st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
)

#: How many times one run's review ceiling may be refreshed by a resume.
#:
#: `REVIEW_BUDGET_EXHAUSTED` is refreshed like the other classes, but unlike
#: them it needs a bound, and §3.6 A9 is the reason: *"Never gate progress on a
#: zero-finding LLM sweep with restart-on-any-finding — it has no bounded
#: termination. Bound the loop or accept graded findings."*
#:
#: The asymmetry is real rather than bureaucratic. A semantic ceiling bounds
#: itself — the gate either goes green or it does not, and the adjudicator is a
#: count of executed cases, so a node that cannot pass stops. A reviewer's
#: opinion has no fixed point, so `reject -> resume -> repair -> reject` runs as
#: long as an operator keeps typing `run resume`. A9 does not forbid refreshing
#: this budget; it forbids refreshing it without a bound. This is the bound.
#:
#: Small on purpose. A large number would satisfy every test of the mechanism
#: while leaving the loop unbounded in practice, which is the shape A9 convicts;
#: `tests/test_resume_refreshes_review_budget.py` asserts the magnitude, not
#: just the behaviour. Three is enough to carry a lane through a reviewer that
#: disagreed twice and few enough that a genuinely unconvergeable lane stops
#: for a human while the run still has budget to finish everything else.
#:
#: The count is spent only by a resume that actually reopened something, which
#: is what lets this bound and idempotence hold at once: resuming twice with no
#: work in between reopens nothing the second time and therefore costs nothing.
RESUME_REVIEW_REFRESH_CEILING = 3

#: One node — the escape boundary is a decision about that node alone, which
#: is what a `run resume` flag cannot express (§11.3).
_RAISE_NODE_RETRY_SPEND_FLOOR = _RETRY_SPEND_FLOOR_SQL + " WHERE run_id=? AND node_id=?"


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
    return tuple(
        str(row[1])
        for row in conn.execute("PRAGMA table_info({0})".format(table)).fetchall()
    )


def _migrate(conn: sqlite3.Connection) -> Tuple[str, ...]:
    """Add nullable columns and rebuild review checks an old ledger lacks.

    Every added column is nullable with no default: an existing row keeps NULL,
    and NULL is read everywhere below as "nobody recorded this", never as a
    value. So the migration cannot invent a fact about a run that predates the
    column. A pre-migration `CANCELLED` therefore carries no cause, and `run
    resume` refuses it — the safe direction, since the alternative is guessing
    that a run nobody recorded a cause for was merely paused.

    `candidate_reviews` is the exception to additive migration: SQLite cannot
    alter its state CHECK. Rebuilding it inside this transaction changes every
    unfinished legacy `DISPATCHED` row to `PUBLISHED`, deliberately removing a
    dispatch claim no pre-submission receipt can prove. Terminal evidence is
    copied byte-for-byte except `dispatched_at`, which remains NULL because old
    ledgers never recorded it.

    Concurrent openers are a supported case — `_enable_wal` says so
    explicitly. The `PRAGMA table_info` check and every migration DDL statement
    therefore run in one serialized transaction.
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
                        "ALTER TABLE {0} ADD COLUMN {1} {2}".format(table, name, kind)
                    )
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).lower():
                        raise
                    continue
                added.append("{0}.{1}".format(table, name))
        if _candidate_reviews_rebuild_needed(conn):
            _rebuild_candidate_reviews(conn)
            added.append("candidate_reviews.rebuilt")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tuple(added)


_REVIEW_MIGRATION_TABLES: Tuple[str, ...] = (
    "lane_candidates",
    "candidate_reviews",
    "repair_handoffs",
    "legacy_review_migration_blocks",
    "lane_retry_spend",
    "actor_sessions",
)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _candidate_reviews_rebuild_needed(conn: sqlite3.Connection) -> bool:
    """Whether an existing review table predates the durable dispatch state."""
    if not _has_table(conn, "candidate_reviews"):
        return False
    columns = set(_table_columns(conn, "candidate_reviews"))
    if "dispatched_at" not in columns:
        return True
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='candidate_reviews'"
    ).fetchone()
    return row is None or "PUBLISHED" not in str(row[0]).upper()


def _rebuild_candidate_reviews(conn: sqlite3.Connection) -> None:
    """Atomically replace the old immutable-state CHECK with the new lifecycle."""
    conn.execute(
        _candidate_reviews_ddl("candidate_reviews_rebuild", if_not_exists=False)
    )
    conn.execute(
        "INSERT INTO candidate_reviews_rebuild"
        " (run_id, review_node_id, candidate_sha, reviewer_generation, state,"
        "  dispatched_at, review_digest, receipt_path, findings_json, verdict,"
        "  completed_at)"
        " SELECT run_id, review_node_id, candidate_sha, reviewer_generation,"
        "  CASE state WHEN 'DISPATCHED' THEN 'PUBLISHED' ELSE state END,"
        "  NULL, review_digest, receipt_path, findings_json, verdict, completed_at"
        " FROM candidate_reviews"
    )
    conn.execute("DROP TABLE candidate_reviews")
    conn.execute("ALTER TABLE candidate_reviews_rebuild RENAME TO candidate_reviews")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_reviews_active"
        " ON candidate_reviews(run_id, review_node_id, state)"
    )


def _persistent_review_migration_needed(conn: sqlite3.Connection) -> bool:
    """Whether this opener will add persistent-review authority to an old DB."""
    if any(not _has_table(conn, table) for table in _REVIEW_MIGRATION_TABLES):
        return True
    return (
        _candidate_reviews_rebuild_needed(conn)
        or "review_of" not in _table_columns(conn, "dag_nodes")
        or "lane_phase" not in _table_columns(conn, "node_lifecycle")
        or "tab_id" not in _table_columns(conn, "actor_sessions")
    )


@contextlib.contextmanager
def _review_migration_lock(db_path: Path) -> Iterator[None]:
    """Serialize snapshots and restores across first openers of one ledger."""
    lock_path = db_path.with_name(f".{db_path.name}.review-migration.lock")
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(descriptor, "a+b", closefd=False) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _fsync_path(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sqlite_backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Write a self-contained SQLite snapshot, including WAL-visible changes."""
    backup_path = db_path.with_name(
        f"{db_path.name}.pre-review-{int(time.time() * 1_000_000)}-"
        f"{uuid.uuid4().hex}.sqlite3"
    )
    destination = sqlite3.connect(str(backup_path), isolation_level=None)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    _fsync_path(backup_path)
    _fsync_path(backup_path.parent)
    return backup_path


def _restore_sqlite_backup(db_path: Path, backup_path: Path) -> None:
    """Restore a snapshot through SQLite, never by copying a WAL database."""
    source = sqlite3.connect(str(backup_path), isolation_level=None)
    target = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        source.backup(target)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        target.close()
        source.close()
    _fsync_path(db_path)
    _fsync_path(db_path.parent)


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
            f"could not put the lifecycle database in WAL mode: it is still {mode!r}"
        )


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
            out[key[: -len("_json")]] = json.loads(value)
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
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _require_candidate_sha(sha: str, *, field_name: str = "candidate_sha") -> str:
    """Refuse abbreviated, malformed, and non-hex candidate identities."""
    if (
        not isinstance(sha, str)
        or len(sha) not in (40, 64)
        or any(char not in "0123456789abcdefABCDEF" for char in sha)
    ):
        raise LifecycleError(
            f"{field_name} must be a complete 40- or 64-character hexadecimal SHA"
        )
    return sha.lower()


def _require_generation(generation: int, *, field_name: str) -> int:
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise LifecycleError(f"{field_name} must be a non-negative integer")
    return generation


def _require_optional_tab_id(tab_id: Optional[str]) -> Optional[str]:
    """Keep a tab id exact when known; map only the empty wire default to NULL."""
    if tab_id is None or tab_id == "":
        return None
    if not isinstance(tab_id, str) or not tab_id.strip():
        raise LifecycleError("tab_id must be a non-empty string or None")
    return tab_id


def _canonical_findings(
    findings: Sequence[Mapping[str, Any]],
) -> Tuple[Tuple[Mapping[str, Any], ...], str]:
    """Validate the findings ledger as a JSON array of object records once."""
    if isinstance(findings, (str, bytes)):
        raise LifecycleError("findings must be a sequence of object records")
    try:
        normalized = tuple(dict(finding) for finding in findings)
    except (TypeError, ValueError) as error:
        raise LifecycleError("findings must be a sequence of object records") from error
    try:
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise LifecycleError("findings must be JSON serializable") from error
    return normalized, encoded


def _canonical_detail(
    detail: Optional[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], str]:
    """Encode an authority detail object without accepting a second JSON shape."""
    try:
        normalized = dict(detail or {})
    except (TypeError, ValueError) as error:
        raise LifecycleError("detail must be an object mapping") from error
    try:
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise LifecycleError("detail must be JSON serializable") from error
    return normalized, encoded


def _candidate_from_row(row: Sequence[Any]) -> st.LaneCandidate:
    return st.LaneCandidate(
        run_id=row[0],
        build_node_id=row[1],
        candidate_seq=int(row[2]),
        candidate_sha=row[3],
        parent_candidate_sha=row[4],
        builder_generation=int(row[5]),
        created_at=row[6],
    )


def _review_from_row(row: Sequence[Any]) -> st.CandidateReview:
    raw_findings = json.loads(row[8])
    if not isinstance(raw_findings, list) or any(
        not isinstance(finding, dict) for finding in raw_findings
    ):
        raise LifecycleError("candidate review findings ledger is not an object array")
    return st.CandidateReview(
        run_id=row[0],
        review_node_id=row[1],
        candidate_sha=row[2],
        reviewer_generation=int(row[3]),
        state=st.CandidateReviewState(row[4]),
        dispatched_at=row[5],
        review_digest=row[6],
        receipt_path=row[7],
        findings=tuple(dict(finding) for finding in raw_findings),
        verdict=st.ReviewVerdict(row[9]) if row[9] else None,
        completed_at=row[10],
    )


def _handoff_from_row(row: Sequence[Any]) -> st.RepairHandoff:
    raw_findings = json.loads(row[3])
    if not isinstance(raw_findings, list) or any(
        not isinstance(finding, dict) for finding in raw_findings
    ):
        raise LifecycleError("repair handoff findings ledger is not an object array")
    return st.RepairHandoff(
        run_id=row[0],
        build_node_id=row[1],
        rejected_candidate_sha=row[2],
        findings=tuple(dict(finding) for finding in raw_findings),
        state=st.RepairHandoffState(row[4]),
        builder_generation=int(row[5]),
        submitted_at=row[6],
        acknowledged_at=row[7],
    )


def _lane_retry_spend_from_row(row: Sequence[Any]) -> st.LaneRetrySpend:
    raw_detail = json.loads(row[5])
    if not isinstance(raw_detail, dict):
        raise LifecycleError("lane retry spend detail is not an object")
    return st.LaneRetrySpend(
        run_id=row[0],
        build_node_id=row[1],
        retry_class=st.LaneRetryClass(row[2]),
        cycle_seq=int(row[3]),
        candidate_sha=row[4],
        detail=dict(raw_detail),
        created_at=row[6],
    )


def _actor_session_from_row(row: Sequence[Any]) -> st.ActorSession:
    return st.ActorSession(
        run_id=row[0],
        build_node_id=row[1],
        actor_role=row[2],
        generation=int(row[3]),
        state=st.ActorSessionState(row[4]),
        pane_id=row[5],
        tab_id=row[6],
        session_path=row[7],
        correlation_token=row[8],
        updated_at=row[9],
    )


def _review_build_id(review_node_id: str) -> str:
    build_node_id, marker, suffix = review_node_id.rpartition("::review")
    if (
        not marker
        or suffix
        or not build_node_id
        or review_node_id.count("::review") != 1
    ):
        raise LifecycleError(
            "a derived review id must be exactly '<build-node-id>::review'"
        )
    return build_node_id


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
        state is st.NodeState.CANCELLED for _, state, _ in node_states
    )
    if cancel_requested or all_cancelled:
        cancelled = tuple(
            nid for nid, state, _ in node_states if state is st.NodeState.CANCELLED
        )
        # The two conditions are not interchangeable and the outcome records
        # which one fired. `cancel_requested` is the operator's stop control,
        # under which nothing was adjudicated; `all_cancelled` without it is a
        # run given up on node by node, each node individually adjudicated as
        # work the run should finish without. `run resume` reopens the first
        # and refuses the second (§7.8), and the precedence here is the same
        # precedence the arm above it already had.
        cause = requested_cause or (
            st.CancelCause.RUN_CANCEL if cancel_requested else st.CancelCause.ABANDONED
        )
        return OutcomeReport(
            outcome=st.RunOutcome.CANCELLED,
            abandoned_nodes=cancelled,
            cancel_cause=cause,
        )

    merged = [nid for nid, state, _ in node_states if state is st.NodeState.MERGED]
    cancelled = tuple(
        nid for nid, state, _ in node_states if state is st.NodeState.CANCELLED
    )
    stragglers = [
        nid
        for nid, state, _ in node_states
        if state
        not in (st.NodeState.MERGED, st.NodeState.ACCEPTED, st.NodeState.CANCELLED)
    ]
    if merged and not stragglers and acceptance_result is True:
        return OutcomeReport(
            outcome=st.RunOutcome.ACCEPTED,
            abandoned_nodes=cancelled,
            acceptance_result=acceptance_result,
        )

    blocked = tuple(
        nid for nid, state, _ in node_states if state is st.NodeState.BLOCKED
    )
    reasons = {
        nid: reason
        for nid, state, reason in node_states
        if state is st.NodeState.BLOCKED and reason is not None
    }
    return OutcomeReport(
        outcome=st.RunOutcome.BLOCKED,
        blocked_nodes=blocked,
        block_reasons=reasons,
        abandoned_nodes=cancelled,
        acceptance_result=acceptance_result,
    )


# ── the legal transition guard (§7.3) ───────────────────────────────────────


def _guard_transition(
    current: st.NodeState,
    to_state: st.NodeState,
    *,
    actor: str,
    cancel_cause: Optional[st.CancelCause] = None,
) -> None:
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
        reopening = (
            current is st.NodeState.CANCELLED
            and to_state is st.NodeState.PENDING
            and actor == "operator"
            and cancel_cause in st.REOPENABLE_CANCEL_CAUSES
        )
        if not reopening:
            raise IllegalTransition(
                f"{current.value} is absolutely terminal (§7.3); no transition leaves it, "
                f"including this attempted move to {to_state.value}"
            )
    if (
        current is st.NodeState.BLOCKED
        and to_state is not st.NodeState.BLOCKED
        and actor != "operator"
    ):
        raise IllegalTransition(
            "BLOCKED is operator-terminal (§7.3): no automatic transition leaves it, "
            f"only an operator escape may — refusing the move to {to_state.value}"
        )


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
    #: Count of terminal REJECTED rows for this node's derived reviewer.
    #: Distinct from VERIFIED transitions: a rejection proves a reviewer
    #: adjudicated an immutable candidate even when no candidate ever passed.
    review_rejections: int
    #: Count of scheduler attempt rows, retained as separate execution evidence.
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
            "block_reason": (self.block_reason.value if self.block_reason else None),
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
LATE_ENVELOPE_RECOVERY_KEY = "late_envelope_recovery"
LATE_ENVELOPE_PHASE_KEY = "late_envelope_phase"
SEALED_OUTPUT_SHA_KEY = "sealed_output_sha"
REPAIR_HANDOFF_RECOVERY_KEY = "repair_handoff_recovery"
REVIEW_BUDGET_RECOVERY_KEY = "review_budget_recovery"
#: One-shot authority to reopen a `QUIESCENCE_UNPROVEN` block over an
#: attempt that durably never crossed dispatch. Written by
#: `_write_resume_transition` against evidence, consumed by
#: `claim_undispatched_attempt`, and never a default: an attempt row
#: without it is claimed by nothing.
UNDISPATCHED_RESUME_KEY = "undispatched_resume"


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
                "baseline entry for {0!r} is not a string".format(rel)
            )
        mode, sep, blob = value.partition(" ")
        if not sep or not mode or not blob:
            raise BaselineCorrupt(
                "baseline entry for {0!r} is not '<mode> <blob>'".format(rel)
            )
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
        path = Path(db_path)
        ensure_dir(path.parent)
        existed = path.exists()
        self.db_path = str(path)
        self.review_migration_backup_path: Optional[Path] = None
        self._lock = threading.RLock()
        # One connection outlives every thread that touches it — `serialized`
        # is what makes sharing it safe (mirrors tracer.py, §7.2).
        self.conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        # busy_timeout first, before any statement that can contend with the
        # journal-mode switch (mirrors tracer.py's ordering fix).
        self.conn.execute("PRAGMA busy_timeout=5000;")
        _enable_wal(self.conn)
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        with _review_migration_lock(path):
            backup_path: Optional[Path] = None
            try:
                if existed and _persistent_review_migration_needed(self.conn):
                    backup_path = _sqlite_backup(self.conn, path)
                    self.review_migration_backup_path = backup_path
                self.conn.executescript(SCHEMA)
                _migrate(self.conn)
            except Exception as error:
                self.conn.close()
                if backup_path is not None:
                    try:
                        _restore_sqlite_backup(path, backup_path)
                    except Exception as restore_error:
                        raise ReviewSchemaMigrationFailed(
                            "persistent-review migration failed and its SQLite "
                            f"backup could not be restored: {backup_path}"
                        ) from restore_error
                    raise ReviewSchemaMigrationFailed(
                        "persistent-review migration failed; restored SQLite "
                        f"backup {backup_path}"
                    ) from error
                raise ReviewSchemaMigrationFailed(
                    "persistent-review schema setup failed before scheduling"
                ) from error

    # ── run / plan projection ────────────────────────────────────────────────

    @serialized
    def create_run(
        self,
        run_id: str,
        plan_digest: str,
        nodes: Sequence[st.PlanNode],
        plan_name: Optional[str] = None,
        test_strength_contract: Optional[st.TestStrengthContract] = None,
    ) -> None:
        """Project the plan's nodes into `dag_nodes`, seed every node PENDING,
        in one transaction (§7.1).

        `test_strength_contract` is written here and nowhere else. Pinning it
        at creation is what makes the rollout safe in both directions: a run
        created under the legacy rules keeps them for every resume, and a run
        created under `STRENGTH_V1` cannot later be resumed into the weaker
        ones by a binary that happens to be older. `None` records the legacy
        pin explicitly rather than leaving the column unwritten, so "created
        before the column" and "created under legacy rules" stay
        distinguishable in an audit.
        """
        existing = self.conn.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            raise RunAlreadyExists(f"run {run_id} already exists")
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO runs (run_id, plan_digest, created_at, last_transition_at,"
                " latest_outcome, latest_outcome_at, cancel_requested,"
                " scheduler_pid, scheduler_host, scheduler_claimed_at,"
                " scheduler_start_epoch, plan_name, test_strength_contract)"
                " VALUES (?,?,?,?,NULL,NULL,0,?,?,?,?,?,?)",
                (
                    run_id,
                    plan_digest,
                    now,
                    now,
                    os.getpid(),
                    scheduler_host(),
                    now,
                    wd.process_start_epoch(os.getpid()),
                    plan_name,
                    st.TestStrengthContract(test_strength_contract).value
                    if test_strength_contract is not None
                    else None,
                ),
            )
            for node in nodes:
                self.conn.execute(
                    "INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind, depth,"
                    " needs_json, outputs_json, specs_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        node.node_id,
                        plan_digest,
                        node.kind.value,
                        node.depth,
                        json.dumps(list(node.needs)),
                        json.dumps(list(node.outputs)),
                        json.dumps(list(node.specs)),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO node_lifecycle (run_id, node_id, state, attempt_no,"
                    " block_reason, output_sha, granted_extra_attempts, updated_at)"
                    " VALUES (?,?,?,0,NULL,NULL,0,?)",
                    (run_id, node.node_id, st.NodeState.PENDING.value, now),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def ensure_derived_review_node(
        self,
        run_id: str,
        build_node_id: str,
        *,
        depth: int,
        downstream_needs: Sequence[str] = (),
    ) -> st.NodeLifecycle:
        """Project one derived review node and its dependency edges exactly once.

        The authoring model must keep refusing ``NodeKind.REVIEW``.  This is
        therefore the sole runtime projection seam: it derives the id and
        source binding, inserts the review row and rewires the direct
        downstream edges in one transaction.  A prior projection is accepted
        only when every durable fact is identical; it is never repaired by
        overwriting a possibly ambiguous older ledger.
        """
        if depth < 0:
            raise LifecycleError("a derived review node cannot have negative depth")
        review_node_id = "{0}::review".format(build_node_id)
        downstream_ids = tuple(downstream_needs)
        if len(set(downstream_ids)) != len(downstream_ids):
            raise LifecycleError("derived review downstream nodes must be distinct")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.conn.execute(
                "SELECT plan_digest, test_strength_contract FROM runs"
                " WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise LifecycleError(f"no run row for {run_id}")
            plan_digest = str(run[0])
            contract = (
                st.DEFAULT_TEST_STRENGTH_CONTRACT
                if run[1] is None
                else st.TestStrengthContract(str(run[1]))
            )
            build = self.conn.execute(
                "SELECT plan_digest, kind FROM dag_nodes WHERE run_id=? AND node_id=?",
                (run_id, build_node_id),
            ).fetchone()
            if build is None:
                raise UnknownNode(f"{run_id}/{build_node_id} has no dag row")
            if build[0] != plan_digest:
                raise LifecycleError(
                    f"{run_id}/{build_node_id}: dag plan digest does not match its run"
                )
            if build[1] not in (st.NodeKind.AGENT.value, st.NodeKind.TESTS.value):
                # A tests node owns a derived review for the same reason an
                # agent node does, and excluding it here was half of why a
                # tests node could reach MERGED unread. A code node still
                # cannot: its acceptance is its command's exit code (§6.2),
                # there is no diff a reviewer is being asked about, and a
                # review row for one would be state nothing evaluates.
                raise LifecycleError(
                    f"{run_id}/{build_node_id}: only an agent or tests build "
                    "lane can own a derived review node"
                )
            if (
                build[1] == st.NodeKind.TESTS.value
                and contract is not st.TestStrengthContract.STRENGTH_V1
            ):
                # The store's own half of the rollout invariant. A tests node
                # in a legacy-pinned run was admitted on its case count under
                # rules that had no reviewer in them, and projecting one now
                # does not merely add a row: it rewires every direct dependant
                # to need the review instead, which reopens the dependency
                # decision of nodes that are already terminal. The scheduler
                # no longer asks for this; refusing it here means no future
                # caller can reintroduce it by accident (§19 M42).
                raise LifecycleError(
                    f"{run_id}/{build_node_id}: a tests node in a run pinned "
                    f"to the {contract.value} test-acceptance contract cannot "
                    "own a derived review node"
                )

            review = self.conn.execute(
                "SELECT plan_digest, kind, depth, needs_json, outputs_json,"
                " specs_json, review_of FROM dag_nodes WHERE run_id=? AND node_id=?",
                (run_id, review_node_id),
            ).fetchone()
            created = review is None
            if created:
                now = now_iso()
                self.conn.execute(
                    "INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind, depth,"
                    " needs_json, outputs_json, specs_json, review_of)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        review_node_id,
                        plan_digest,
                        st.NodeKind.REVIEW.value,
                        depth,
                        json.dumps([build_node_id]),
                        "[]",
                        "[]",
                        build_node_id,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO node_lifecycle (run_id, node_id, state, attempt_no,"
                    " block_reason, output_sha, granted_extra_attempts, updated_at)"
                    " VALUES (?,?,?,0,NULL,NULL,0,?)",
                    (run_id, review_node_id, st.NodeState.PENDING.value, now),
                )
            else:
                expected = (
                    plan_digest,
                    st.NodeKind.REVIEW.value,
                    depth,
                    (build_node_id,),
                    (),
                    (),
                    build_node_id,
                )
                actual = (
                    review[0],
                    review[1],
                    int(review[2]),
                    tuple(json.loads(review[3])),
                    tuple(json.loads(review[4])),
                    tuple(json.loads(review[5])),
                    review[6],
                )
                if actual != expected:
                    raise LifecycleError(
                        f"{run_id}/{review_node_id}: derived review projection mismatch"
                    )
                lifecycle = self.conn.execute(
                    "SELECT 1 FROM node_lifecycle WHERE run_id=? AND node_id=?",
                    (run_id, review_node_id),
                ).fetchone()
                if lifecycle is None:
                    raise LifecycleError(
                        f"{run_id}/{review_node_id}: derived review dag row lacks lifecycle"
                    )

            for downstream_id in downstream_ids:
                row = self.conn.execute(
                    "SELECT needs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
                    (run_id, downstream_id),
                ).fetchone()
                if row is None:
                    raise UnknownNode(f"{run_id}/{downstream_id} has no dag row")
                needs = tuple(json.loads(row[0]))
                if created:
                    if review_node_id in needs or build_node_id not in needs:
                        raise LifecycleError(
                            f"{run_id}/{downstream_id}: cannot project review dependency"
                        )
                    rewired = tuple(
                        review_node_id if need == build_node_id else need
                        for need in needs
                    )
                    self.conn.execute(
                        "UPDATE dag_nodes SET needs_json=? WHERE run_id=? AND node_id=?",
                        (json.dumps(rewired), run_id, downstream_id),
                    )
                elif review_node_id not in needs or build_node_id in needs:
                    raise LifecycleError(
                        f"{run_id}/{downstream_id}: derived review dependency mismatch"
                    )

            if created:
                now = now_iso()
                self.conn.execute(
                    "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                    " reason, actor, detail_json, created_at)"
                    " VALUES (?,?,'node',NULL,?,'derived-review-projected','scheduler',?,?)",
                    (
                        run_id,
                        review_node_id,
                        st.NodeState.PENDING.value,
                        json.dumps(
                            {
                                "review_of": build_node_id,
                                "downstream_needs": list(downstream_ids),
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                self.conn.execute(
                    "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id)
                )
            row = self.conn.execute(
                "SELECT state, attempt_no, block_reason, output_sha,"
                " granted_extra_attempts, lane_phase FROM node_lifecycle"
                " WHERE run_id=? AND node_id=?",
                (run_id, review_node_id),
            ).fetchone()
            self.conn.execute("COMMIT")
            return st.NodeLifecycle(
                node_id=review_node_id,
                state=st.NodeState(row[0]),
                attempt_no=row[1],
                block_reason=st.BlockReason(row[2]) if row[2] else None,
                output_sha=row[3],
                granted_extra_attempts=row[4],
                lane_phase=st.LanePhase(row[5]) if row[5] else None,
            )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ── reads ────────────────────────────────────────────────────────────────

    @serialized
    def get_node(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        row = self.conn.execute(
            "SELECT state, attempt_no, block_reason, output_sha,"
            " granted_extra_attempts, lane_phase, pending_cause"
            " FROM node_lifecycle WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
        (
            state,
            attempt_no,
            block_reason,
            output_sha,
            granted,
            lane_phase,
            pending_cause,
        ) = row
        return st.NodeLifecycle(
            node_id=node_id,
            state=st.NodeState(state),
            attempt_no=attempt_no,
            block_reason=st.BlockReason(block_reason) if block_reason else None,
            output_sha=output_sha,
            granted_extra_attempts=granted,
            lane_phase=st.LanePhase(lane_phase) if lane_phase else None,
            pending_cause=st.PendingCause(pending_cause) if pending_cause else None,
        )

    @serialized
    def set_lane_phase(
        self,
        run_id: str,
        build_node_id: str,
        phase: st.LanePhase,
        *,
        expected: Optional[st.LanePhase] = None,
    ) -> bool:
        """Compare-and-set a lane's durable phase.

        ``expected=None`` is an intentional initial CAS: it can only claim a
        lane that has no recorded phase.  Terminal phases cannot be reopened
        by this runtime surface; an operator escape, if one is ever added,
        must name a different authority rather than silently relaxing this
        guard.
        """
        try:
            requested = st.LanePhase(phase)
        except ValueError as error:
            raise LifecycleError(f"unknown lane phase {phase!r}") from error
        expected_phase = None
        if expected is not None:
            try:
                expected_phase = st.LanePhase(expected)
            except ValueError as error:
                raise LifecycleError(
                    f"unknown expected lane phase {expected!r}"
                ) from error
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT lane_phase FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, build_node_id),
            ).fetchone()
            if row is None:
                raise UnknownNode(f"{run_id}/{build_node_id} has no lifecycle row")
            current = st.LanePhase(row[0]) if row[0] else None
            if current is not expected_phase:
                self.conn.execute("COMMIT")
                return False
            if current in st.LANE_PHASE_TERMINAL and current is not requested:
                self.conn.execute("COMMIT")
                return False
            if current is requested:
                self.conn.execute("COMMIT")
                return True
            now = now_iso()
            self.conn.execute(
                "UPDATE node_lifecycle SET lane_phase=?, updated_at=?"
                " WHERE run_id=? AND node_id=?",
                (requested.value, now, run_id, build_node_id),
            )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,?,'node',NULL,NULL,'lane-phase','scheduler',?,?)",
                (
                    run_id,
                    build_node_id,
                    json.dumps(
                        {
                            "from": current.value if current else None,
                            "to": requested.value,
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id)
            )
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def get_attempt(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> st.AttemptRecord:
        row = self.conn.execute(
            "SELECT base_sha, state, started_at, launched_at, pid, turn_count,"
            " retry_class, extra_json, attempt_host, attempt_start_epoch"
            " FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        (
            base_sha,
            state,
            started_at,
            launched_at,
            pid,
            turn_count,
            retry_class,
            extra_json,
            attempt_host,
            attempt_start_epoch,
        ) = row
        return st.AttemptRecord(
            run_id=run_id,
            node_id=node_id,
            attempt_no=attempt_no,
            base_sha=base_sha,
            state=st.NodeState(state),
            started_at=started_at or 0.0,
            launched_at=launched_at,
            pid=pid,
            turn_count=turn_count,
            retry_class=st.RetryClass(retry_class) if retry_class else None,
            extra=json.loads(extra_json),
            attempt_host=attempt_host,
            attempt_start_epoch=attempt_start_epoch,
        )

    @serialized
    def node_outputs(self, run_id: str, node_id: str) -> Tuple[str, ...]:
        """The node's declared outputs, as stored when the run was created."""
        row = self.conn.execute(
            "SELECT outputs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no dag row")
        return tuple(json.loads(row[0]))

    # ── persistent review authority ─────────────────────────────────────────

    def _candidate(
        self, run_id: str, build_node_id: str, candidate_sha: str
    ) -> Optional[st.LaneCandidate]:
        row = self.conn.execute(
            "SELECT run_id, build_node_id, candidate_seq, candidate_sha,"
            " parent_candidate_sha, builder_generation, created_at"
            " FROM lane_candidates WHERE run_id=? AND build_node_id=?"
            " AND candidate_sha=?",
            (run_id, build_node_id, candidate_sha),
        ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def _review(
        self, run_id: str, review_node_id: str, candidate_sha: str
    ) -> Optional[st.CandidateReview]:
        row = self.conn.execute(
            "SELECT run_id, review_node_id, candidate_sha, reviewer_generation,"
            " state, dispatched_at, review_digest, receipt_path, findings_json,"
            " verdict, completed_at"
            " FROM candidate_reviews WHERE run_id=? AND review_node_id=?"
            " AND candidate_sha=?",
            (run_id, review_node_id, candidate_sha),
        ).fetchone()
        return _review_from_row(row) if row is not None else None

    def _handoff(
        self, run_id: str, build_node_id: str, rejected_candidate_sha: str
    ) -> Optional[st.RepairHandoff]:
        row = self.conn.execute(
            "SELECT run_id, build_node_id, rejected_candidate_sha, findings_json,"
            " state, builder_generation, submitted_at, acknowledged_at"
            " FROM repair_handoffs WHERE run_id=? AND build_node_id=?"
            " AND rejected_candidate_sha=?",
            (run_id, build_node_id, rejected_candidate_sha),
        ).fetchone()
        return _handoff_from_row(row) if row is not None else None

    @serialized
    def lane_candidates(
        self, run_id: str, build_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.LaneCandidate, ...]:
        """Candidates in stable publication order, optionally for one lane."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("candidate read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, candidate_seq, candidate_sha,"
            " parent_candidate_sha, builder_generation, created_at"
            " FROM lane_candidates WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params = (run_id, build_node_id)
        rows = self.conn.execute(
            sql + " ORDER BY build_node_id, candidate_seq LIMIT ?", params + (limit,)
        ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    @serialized
    def candidate(
        self, run_id: str, build_node_id: str, candidate_sha: str
    ) -> Optional[st.LaneCandidate]:
        return self._candidate(
            run_id, build_node_id, _require_candidate_sha(candidate_sha)
        )

    @serialized
    def candidate_reviews(
        self, run_id: str, review_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.CandidateReview, ...]:
        """Reviews in candidate order; each row is the one durable verdict."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("review read limit must be a positive integer")
        sql = (
            "SELECT run_id, review_node_id, candidate_sha, reviewer_generation,"
            " state, dispatched_at, review_digest, receipt_path, findings_json,"
            " verdict, completed_at"
            " FROM candidate_reviews WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if review_node_id is not None:
            sql += " AND review_node_id=?"
            params = (run_id, review_node_id)
        rows = self.conn.execute(
            sql + " ORDER BY review_node_id, rowid LIMIT ?", params + (limit,)
        ).fetchall()
        return tuple(_review_from_row(row) for row in rows)

    @serialized
    def candidate_review(
        self, run_id: str, review_node_id: str, candidate_sha: str
    ) -> Optional[st.CandidateReview]:
        return self._review(
            run_id, review_node_id, _require_candidate_sha(candidate_sha)
        )

    @serialized
    def repair_handoffs(
        self, run_id: str, build_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.RepairHandoff, ...]:
        """Repair handoffs in rejection order, optionally for one build lane."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("handoff read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, rejected_candidate_sha, findings_json,"
            " state, builder_generation, submitted_at, acknowledged_at"
            " FROM repair_handoffs WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params = (run_id, build_node_id)
        rows = self.conn.execute(
            sql + " ORDER BY build_node_id, rowid LIMIT ?", params + (limit,)
        ).fetchall()
        return tuple(_handoff_from_row(row) for row in rows)

    @serialized
    def repair_handoff(
        self, run_id: str, build_node_id: str, rejected_candidate_sha: str
    ) -> Optional[st.RepairHandoff]:
        return self._handoff(
            run_id,
            build_node_id,
            _require_candidate_sha(
                rejected_candidate_sha, field_name="rejected_candidate_sha"
            ),
        )

    # ── the test-strength ledger (§TS) ───────────────────────────────────────

    @serialized
    def test_strength_contract(self, run_id: str) -> st.TestStrengthContract:
        """The contract this run was created under. NULL reads as LEGACY.

        Read, never inferred from what the runtime currently supports. A run
        whose ledger predates the column is legacy by the fact that nobody
        pinned it, and answering anything else here is what would retroactively
        change the rules an already-terminal node was decided under.
        """
        pinned = self.pinned_test_strength_contract(run_id)
        if pinned is None:
            raise LifecycleError("no run row for {0}".format(run_id))
        return pinned

    @serialized
    def pinned_test_strength_contract(
        self, run_id: str
    ) -> Optional[st.TestStrengthContract]:
        """The pin, or `None` when there is no run to have pinned anything.

        The separate question from `test_strength_contract`, and the one a
        scheduler has to ask *before* it projects: "does this run already
        exist, and if so under which rules?" A run that does not exist yet is
        not legacy and is not `STRENGTH_V1` -- it has no pin, and answering
        with either value would be inventing a durable fact that has not been
        written. Existence and contract are one query because two would race.
        """
        row = self.conn.execute(
            "SELECT test_strength_contract FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return st.DEFAULT_TEST_STRENGTH_CONTRACT
        try:
            return st.TestStrengthContract(str(row[0]))
        except ValueError:
            raise LifecycleError(
                "run {0} is pinned to an unknown test-strength contract "
                "{1!r}; there is no default and none is guessed".format(
                    run_id, row[0])) from None

    @serialized
    def record_test_gate_evidence(
        self,
        run_id: str,
        tests_node_id: str,
        candidate_sha: str,
        *,
        runner: str,
        selector: str,
        strong: bool,
        refusal: Optional[str],
        evidence: Mapping[str, Any],
    ) -> "TestGateEvidenceRecord":
        """Record one candidate's measured gate strength, exactly once.

        Exactly-once by `(run_id, tests_node_id, candidate_sha)`: re-measuring
        the same immutable bytes must not be able to reach a different answer,
        which is B10's lesson applied to the evidence rather than to the
        verdict. A second write whose facts differ is refused rather than
        merged, because the two disagreeing measurements are the finding.
        """
        sha = _require_candidate_sha(candidate_sha)
        if bool(strong) == bool(refusal):
            raise LifecycleError(
                "test gate evidence is strong xor refused; "
                "strong={0!r} refusal={1!r}".format(strong, refusal))
        _detail, evidence_json = _canonical_detail(evidence)
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT runner, selector, strong, refusal, evidence_json,"
                " created_at FROM test_gate_evidence"
                " WHERE run_id=? AND tests_node_id=? AND candidate_sha=?",
                (run_id, tests_node_id, sha),
            ).fetchone()
            if existing is not None:
                stored = (str(existing[0]), str(existing[1]),
                          bool(existing[2]),
                          None if existing[3] is None else str(existing[3]),
                          str(existing[4]))
                offered = (runner, selector, bool(strong), refusal,
                           evidence_json)
                if stored != offered:
                    raise LifecycleError(
                        "test gate evidence for {0}@{1} already exists and "
                        "differs; the same bytes cannot be measured twice to "
                        "two answers".format(tests_node_id, sha))
                self.conn.execute("COMMIT")
                return TestGateEvidenceRecord(
                    tests_node_id=tests_node_id, candidate_sha=sha,
                    runner=stored[0], selector=stored[1], strong=stored[2],
                    refusal=stored[3], evidence=json.loads(stored[4]),
                    created_at=str(existing[5]), created=False)
            self.conn.execute(
                "INSERT INTO test_gate_evidence (run_id, tests_node_id,"
                " candidate_sha, runner, selector, strong, refusal,"
                " evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, tests_node_id, sha, runner, selector,
                 1 if strong else 0, refusal, evidence_json, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return TestGateEvidenceRecord(
            tests_node_id=tests_node_id, candidate_sha=sha, runner=runner,
            selector=selector, strong=bool(strong), refusal=refusal,
            evidence=dict(evidence), created_at=now, created=True)

    @serialized
    def test_gate_evidence(
        self, run_id: str, tests_node_id: Optional[str] = None,
        candidate_sha: Optional[str] = None, *, limit: int = 1000
    ) -> Tuple["TestGateEvidenceRecord", ...]:
        """Recorded gate-strength measurements, newest last."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("test gate evidence limit must be positive")
        sql = ("SELECT tests_node_id, candidate_sha, runner, selector, strong,"
               " refusal, evidence_json, created_at FROM test_gate_evidence"
               " WHERE run_id=?")
        params: Tuple[Any, ...] = (run_id,)
        if tests_node_id is not None:
            sql += " AND tests_node_id=?"
            params += (tests_node_id,)
        if candidate_sha is not None:
            sql += " AND candidate_sha=?"
            params += (_require_candidate_sha(candidate_sha),)
        rows = self.conn.execute(
            sql + " ORDER BY tests_node_id, created_at, rowid LIMIT ?",
            params + (limit,)).fetchall()
        return tuple(
            TestGateEvidenceRecord(
                tests_node_id=str(row[0]), candidate_sha=str(row[1]),
                runner=str(row[2]), selector=str(row[3]),
                strong=bool(row[4]),
                refusal=None if row[5] is None else str(row[5]),
                evidence=json.loads(str(row[6])), created_at=str(row[7]),
                created=False)
            for row in rows)

    @serialized
    def accepted_test_candidate(
        self, run_id: str, tests_node_id: str
    ) -> Optional["TestGateEvidenceRecord"]:
        """The one test candidate that carries **both** halves of acceptance.

        Strong measured evidence *and* a completed independent review that
        passed, joined here rather than by two calls at each reader, because a
        reader that asks only one of the two questions is exactly the gap that
        let a tests node reach MERGED on a case count. A tests node with no
        such row has no accepted candidate, and its dependants do not run.
        """
        review_node_id = "{0}::review".format(tests_node_id)
        row = self.conn.execute(
            "SELECT e.tests_node_id, e.candidate_sha, e.runner, e.selector,"
            " e.strong, e.refusal, e.evidence_json, e.created_at"
            " FROM test_gate_evidence e"
            " JOIN candidate_reviews r"
            "   ON r.run_id = e.run_id"
            "  AND r.candidate_sha = e.candidate_sha"
            "  AND r.review_node_id = ?"
            " WHERE e.run_id=? AND e.tests_node_id=? AND e.strong=1"
            "   AND r.state='COMPLETED' AND r.verdict='PASS'"
            " ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1",
            (review_node_id, run_id, tests_node_id),
        ).fetchone()
        if row is None:
            return None
        return TestGateEvidenceRecord(
            tests_node_id=str(row[0]), candidate_sha=str(row[1]),
            runner=str(row[2]), selector=str(row[3]), strong=bool(row[4]),
            refusal=None if row[5] is None else str(row[5]),
            evidence=json.loads(str(row[6])), created_at=str(row[7]),
            created=False)

    @serialized
    def record_test_pairing(
        self,
        run_id: str,
        build_node_id: str,
        tests_node_id: str,
        *,
        accepted_test_sha: str,
        implementation_sha: str,
        verifier_command: str,
        selector: str,
        executed_cases: int,
        coverage: Mapping[str, Any],
    ) -> "TestPairing":
        """Bind one implementation candidate to the exact test bytes it passed.

        The row is the merge check's authority. Without it a merge would have
        to re-derive "was this implementation verified against the accepted
        test candidate" from mutable state at merge time, and the answer would
        be yes for any test tree that happened to be present.
        """
        accepted = _require_candidate_sha(accepted_test_sha,
                                          field_name="accepted_test_sha")
        implementation = _require_candidate_sha(implementation_sha,
                                                field_name="implementation_sha")
        if not isinstance(executed_cases, int) or isinstance(executed_cases, bool):
            raise LifecycleError("executed_cases is a count")
        if executed_cases < 0:
            raise LifecycleError("executed_cases cannot be negative")
        _detail, coverage_json = _canonical_detail(coverage)
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT accepted_test_sha, verifier_command, selector,"
                " executed_cases, coverage_json, created_at"
                " FROM test_implementation_pairings"
                " WHERE run_id=? AND build_node_id=? AND implementation_sha=?"
                "   AND tests_node_id=?",
                (run_id, build_node_id, implementation, tests_node_id),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != accepted:
                    raise LifecycleError(
                        "{0}@{1} is already paired with test candidate {2}; "
                        "rebinding it to {3} would let a substituted test tree "
                        "inherit an earlier acceptance".format(
                            build_node_id, implementation, existing[0],
                            accepted))
                self.conn.execute("COMMIT")
                return TestPairing(
                    build_node_id=build_node_id, tests_node_id=tests_node_id,
                    accepted_test_sha=str(existing[0]),
                    implementation_sha=implementation,
                    verifier_command=str(existing[1]),
                    selector=str(existing[2]),
                    executed_cases=int(existing[3]),
                    coverage=json.loads(str(existing[4])),
                    created_at=str(existing[5]))
            self.conn.execute(
                "INSERT INTO test_implementation_pairings (run_id,"
                " build_node_id, tests_node_id, accepted_test_sha,"
                " implementation_sha, verifier_command, selector,"
                " executed_cases, coverage_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, build_node_id, tests_node_id, accepted,
                 implementation, verifier_command, selector,
                 int(executed_cases), coverage_json, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return TestPairing(
            build_node_id=build_node_id, tests_node_id=tests_node_id,
            accepted_test_sha=accepted, implementation_sha=implementation,
            verifier_command=verifier_command, selector=selector,
            executed_cases=int(executed_cases), coverage=dict(coverage),
            created_at=now)

    @serialized
    def test_pairings(
        self, run_id: str, build_node_id: Optional[str] = None,
        implementation_sha: Optional[str] = None, *, limit: int = 1000
    ) -> Tuple["TestPairing", ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("test pairing read limit must be positive")
        sql = ("SELECT build_node_id, tests_node_id, accepted_test_sha,"
               " implementation_sha, verifier_command, selector,"
               " executed_cases, coverage_json, created_at"
               " FROM test_implementation_pairings WHERE run_id=?")
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params += (build_node_id,)
        if implementation_sha is not None:
            sql += " AND implementation_sha=?"
            params += (_require_candidate_sha(implementation_sha,
                                              field_name="implementation_sha"),)
        rows = self.conn.execute(
            sql + " ORDER BY build_node_id, tests_node_id, rowid LIMIT ?",
            params + (limit,)).fetchall()
        return tuple(
            TestPairing(
                build_node_id=str(row[0]), tests_node_id=str(row[1]),
                accepted_test_sha=str(row[2]),
                implementation_sha=str(row[3]),
                verifier_command=str(row[4]), selector=str(row[5]),
                executed_cases=int(row[6]),
                coverage=json.loads(str(row[7])), created_at=str(row[8]))
            for row in rows)

    # ── the rollout: classify old runs, never rewrite them ───────────────────

    def _legacy_test_strength_block(
        self, run_id: str, tests_node_id: str
    ) -> Optional[str]:
        row = self.conn.execute(
            "SELECT reason FROM legacy_test_strength_blocks"
            " WHERE run_id=? AND tests_node_id=?",
            (run_id, tests_node_id),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _audit_one_tests_node(
        self, run_id: str, tests_node_id: str
    ) -> LegacyTestStrengthFinding:
        """Classify one tests node against the new contract. Read-only.

        Attribution is exact or it is absent. The candidate this node's state
        rests on is `node_lifecycle.output_sha` — an immutable git object id
        written when the attempt was sealed — and evidence is only ever looked
        up against that exact sha. Nothing here reads `extra_json`, a report
        file, a branch name, or a timestamp: inferring acceptance from mutable
        metadata is precisely the shortcut this classification exists to
        refuse.
        """
        row = self.conn.execute(
            "SELECT state, output_sha, merge_cause FROM node_lifecycle"
            " WHERE run_id=? AND node_id=?",
            (run_id, tests_node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(
                "{0} has no lifecycle row in {1}".format(tests_node_id, run_id))
        state = str(row[0])
        candidate_sha = None if row[1] is None else str(row[1])
        merge_cause = None if row[2] is None else str(row[2])
        evidence = self.conn.execute(
            "SELECT strong, refusal FROM test_gate_evidence"
            " WHERE run_id=? AND tests_node_id=? AND candidate_sha=?",
            (run_id, tests_node_id, candidate_sha),
        ).fetchone() if candidate_sha else None
        review = self.conn.execute(
            "SELECT state, verdict FROM candidate_reviews"
            " WHERE run_id=? AND review_node_id=? AND candidate_sha=?",
            (run_id, "{0}::review".format(tests_node_id), candidate_sha),
        ).fetchone() if candidate_sha else None
        reviewed = (review is not None and str(review[0]) == "COMPLETED"
                    and str(review[1]) == "PASS")
        strong = evidence is not None and bool(evidence[0])
        detail: Dict[str, Any] = {
            "state": state,
            "candidate_sha": candidate_sha,
            "merge_cause": merge_cause,
            "has_gate_evidence": evidence is not None,
            "gate_evidence_strong": strong,
            "independently_reviewed": reviewed,
        }
        if evidence is not None and evidence[1] is not None:
            detail["gate_evidence_refusal"] = str(evidence[1])
        if strong and reviewed:
            classification = "TEST_ACCEPTED"
        elif state in {st.NodeState.MERGED.value, st.NodeState.ACCEPTED.value}:
            classification = LEGACY_TEST_STRENGTH_UNPROVEN
        elif state in {st.NodeState.BLOCKED.value,
                       st.NodeState.CANCELLED.value}:
            classification = "TEST_TERMINAL_WITHOUT_MERGE"
        else:
            classification = "TEST_STRENGTH_PENDING"
        return LegacyTestStrengthFinding(
            tests_node_id=tests_node_id,
            state=state,
            candidate_sha=candidate_sha,
            classification=classification,
            blocking=self._legacy_test_strength_block(
                run_id, tests_node_id) is not None,
            detail=detail,
        )

    @serialized
    def legacy_test_strength_audit(
        self, run_id: str, *, limit: int = 1000
    ) -> Tuple[LegacyTestStrengthFinding, ...]:
        """Classify every tests node in a run. Reads only; changes nothing.

        This is the whole of what an existing run gets by default. A tests node
        that reached MERGED without evidence attributable to its exact
        candidate is reported `LEGACY_TEST_STRENGTH_UNPROVEN` and **stays
        MERGED**: the classification is informational, its dependants stay
        admitted, and no terminal row becomes nonterminal. Turning that
        classification into a fence is an explicit operator migration under an
        explicit policy, never a side effect of running a newer binary.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("legacy test strength audit limit must be positive")
        rows = self.conn.execute(
            "SELECT node_id FROM dag_nodes WHERE run_id=? AND kind=?"
            " ORDER BY node_id LIMIT ?",
            (run_id, st.NodeKind.TESTS.value, limit),
        ).fetchall()
        return tuple(self._audit_one_tests_node(run_id, str(row[0]))
                     for row in rows)

    @serialized
    def migrate_test_strength(
        self,
        run_id: str,
        *,
        apply: bool = False,
        policy: str = "classify",
        backup: bool = True,
    ) -> TestStrengthMigrationReport:
        """Migrate one run onto the test-strength contract, or report what would.

        Bounded, reversible, and evidence-preserving by construction:

        * it writes nothing at all unless `apply` is true, so the dry-run
          report is produced by the same code that would perform the change
          rather than by a second description of it;
        * it takes a SQLite backup first and names it in the report, so
          "reversible" is a file an operator can restore rather than a claim;
        * every write happens in one transaction and any failure rolls the
          whole thing back before anything is scheduled;
        * it never discards a commit, undoes a merge, erases a review or a
          receipt, reopens an implementation lane, alters a candidate
          identity, or resets a retry budget. The only rows it writes are
          `legacy_test_strength_blocks`, which is new state about unadmitted
          work, and only under the `block_unadmitted` policy.

        `policy="classify"` records the audit and blocks nothing.
        `policy="block_unadmitted"` additionally fences the dependants of
        every `LEGACY_TEST_STRENGTH_UNPROVEN` tests node **that have not yet
        been admitted** — a dependant still PENDING, with no attempt. A
        dependant already RUNNING, VERIFIED, or MERGED was admitted under the
        pinned contract and is left exactly as it is.
        """
        if policy not in ("classify", "block_unadmitted"):
            raise LifecycleError(
                "{0!r} is not a migration policy; expected classify or "
                "block_unadmitted".format(policy))
        # The lock is re-entrant, so one guarded method may call another.
        contract = self.test_strength_contract(run_id)
        # The audit is the definition of "what is unproven here"; a migration
        # is that audit plus, under one policy, a fence. Calling it rather
        # than re-deriving it is what keeps the dry-run report and the
        # read-only verb from ever disagreeing about a run.
        findings = self.legacy_test_strength_audit(run_id, limit=100_000)
        unproven = tuple(f for f in findings
                         if f.classification == LEGACY_TEST_STRENGTH_UNPROVEN)
        would_block: List[str] = []
        if policy == "block_unadmitted":
            for finding in unproven:
                for dependant in self._unadmitted_dependants(
                        run_id, finding.tests_node_id):
                    would_block.append(dependant)
        would_block = sorted(set(would_block))
        if not apply:
            return TestStrengthMigrationReport(
                run_id=run_id, contract=contract.value, applied=False,
                backup_path=None, findings=findings,
                blocked_nodes=tuple(would_block),
                reason=("dry run: {0} tests node(s), {1} unproven, {2} "
                        "unadmitted dependant(s) would be fenced".format(
                            len(findings), len(unproven), len(would_block))))
        backup_path: Optional[Path] = None
        if backup:
            backup_path = _sqlite_backup(self.conn, Path(self.db_path))
        migrated: List[str] = []
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for finding in unproven:
                if policy != "block_unadmitted":
                    continue
                dependants = self._unadmitted_dependants(
                    run_id, finding.tests_node_id)
                if not dependants:
                    continue
                _detail, detail_json = _canonical_detail({
                    "policy": policy,
                    "tests_node_id": finding.tests_node_id,
                    "candidate_sha": finding.candidate_sha,
                    "unadmitted_dependants": list(dependants),
                })
                self.conn.execute(
                    "INSERT INTO legacy_test_strength_blocks (run_id,"
                    " tests_node_id, reason, detail_json, created_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(run_id, tests_node_id) DO NOTHING",
                    (run_id, finding.tests_node_id,
                     LEGACY_TEST_STRENGTH_UNPROVEN, detail_json, now))
                migrated.append(finding.tests_node_id)
            self.conn.execute("COMMIT")
        except Exception as error:
            self.conn.execute("ROLLBACK")
            if backup_path is not None:
                _restore_sqlite_backup(Path(self.db_path), backup_path)
            raise LifecycleError(
                "test-strength migration of {0} failed and was rolled back; "
                "backup {1}".format(run_id, backup_path)) from error
        refreshed = self.legacy_test_strength_audit(run_id, limit=100_000)
        return TestStrengthMigrationReport(
            run_id=run_id, contract=contract.value, applied=True,
            backup_path=None if backup_path is None else str(backup_path),
            findings=refreshed, blocked_nodes=tuple(would_block),
            migrated_nodes=tuple(sorted(set(migrated))),
            reason="applied policy {0}".format(policy))

    def _unadmitted_dependants(
        self, run_id: str, tests_node_id: str
    ) -> Tuple[str, ...]:
        """Dependants of `tests_node_id` that no attempt has ever started.

        "Not yet admitted" is a structural fact, not a judgement: the node is
        PENDING and the ledger holds no attempt row for it. A node that ever
        ran was admitted under the pinned contract, and the rollout invariant
        forbids reaching back through it.
        """
        found: List[str] = []
        for row in self.conn.execute(
                "SELECT node_id, needs_json FROM dag_nodes WHERE run_id=?",
                (run_id,)).fetchall():
            node_id = str(row[0])
            try:
                needs = json.loads(str(row[1]))
            except ValueError:
                continue
            if tests_node_id not in (needs or []):
                continue
            state = self.conn.execute(
                "SELECT state FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, node_id)).fetchone()
            if state is None or str(state[0]) != st.NodeState.PENDING.value:
                continue
            attempted = self.conn.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND node_id=? LIMIT 1",
                (run_id, node_id)).fetchone()
            if attempted is None:
                found.append(node_id)
        return tuple(sorted(found))

    @serialized
    def legacy_test_strength_blocks(
        self, run_id: str, *, limit: int = 1000
    ) -> Tuple[LegacyTestStrengthFinding, ...]:
        """Tests nodes an operator migration explicitly fenced."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("legacy test strength block limit must be positive")
        rows = self.conn.execute(
            "SELECT tests_node_id, reason, detail_json FROM"
            " legacy_test_strength_blocks WHERE run_id=?"
            " ORDER BY tests_node_id LIMIT ?",
            (run_id, limit)).fetchall()
        return tuple(
            LegacyTestStrengthFinding(
                tests_node_id=str(row[0]),
                state="",
                candidate_sha=None,
                classification=str(row[1]),
                blocking=True,
                detail=json.loads(str(row[2])))
            for row in rows)

    def _legacy_review_migration_block(
        self, run_id: str, build_node_id: str
    ) -> Optional[str]:
        row = self.conn.execute(
            "SELECT reason FROM legacy_review_migration_blocks"
            " WHERE run_id=? AND build_node_id=?",
            (run_id, build_node_id),
        ).fetchone()
        return str(row[0]) if row is not None else None

    @serialized
    def legacy_review_migrations(
        self, run_id: str, *, limit: int = 100
    ) -> Tuple[LegacyReviewMigration, ...]:
        """Read lanes held before review scheduling by unsafe legacy evidence."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("legacy review migration read limit must be positive")
        rows = self.conn.execute(
            "SELECT build_node_id, reason FROM legacy_review_migration_blocks"
            " WHERE run_id=? ORDER BY build_node_id LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return tuple(
            LegacyReviewMigration(
                build_node_id=str(row[0]),
                migrated=False,
                blocked=True,
                reason=str(row[1]),
            )
            for row in rows
        )

    def _legacy_review_migration_failure(
        self, run_id: str, build_node_id: str, reason: str, detail: Mapping[str, Any]
    ) -> LegacyReviewMigration:
        """Record a fail-closed lane fence inside the caller's transaction."""
        _detail, detail_json = _canonical_detail(detail)
        self.conn.execute(
            "INSERT INTO legacy_review_migration_blocks"
            " (run_id, build_node_id, reason, detail_json, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(run_id, build_node_id) DO NOTHING",
            (run_id, build_node_id, reason, detail_json, now_iso()),
        )
        persisted = self._legacy_review_migration_block(run_id, build_node_id)
        return LegacyReviewMigration(
            build_node_id=build_node_id,
            migrated=False,
            blocked=True,
            reason=persisted or reason,
        )

    @serialized
    def migrate_legacy_inline_reviews(
        self,
        run_id: str,
        evidence: Sequence[LegacyReviewEvidence],
        *,
        evidence_validator: Callable[[LegacyReviewEvidence], bool],
        ancestry_validator: Optional[Callable[[str, str], bool]] = None,
    ) -> Tuple[LegacyReviewMigration, ...]:
        """Import uniquely proven terminal inline reviews before scheduling.

        Receipt/digest authentication is injected because this store deliberately
        does not own the detached receipt root.  An invalid row never degrades
        into a fresh dispatch: it persists a lane fence that `begin_review`
        checks before making any dispatch row.
        """
        if not callable(evidence_validator):
            raise LifecycleError("legacy review evidence requires a validator")
        if isinstance(evidence, (str, bytes)):
            raise LifecycleError("legacy review evidence must be a sequence")
        try:
            supplied = tuple(evidence)
        except TypeError as error:
            raise LifecycleError("legacy review evidence must be a sequence") from error
        if any(not isinstance(item, LegacyReviewEvidence) for item in supplied):
            raise LifecycleError("legacy review evidence has an untyped record")
        grouped: Dict[str, List[LegacyReviewEvidence]] = {}
        for item in supplied:
            grouped.setdefault(item.build_node_id, []).append(item)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            outcomes: List[LegacyReviewMigration] = []
            for build_node_id in sorted(grouped):
                rows = grouped[build_node_id]
                review_rows = self.conn.execute(
                    "SELECT node_id FROM dag_nodes WHERE run_id=? AND kind=?"
                    " AND review_of=? ORDER BY node_id",
                    (run_id, st.NodeKind.REVIEW.value, build_node_id),
                ).fetchall()
                if len(review_rows) != 1:
                    outcomes.append(
                        self._legacy_review_migration_failure(
                            run_id,
                            build_node_id,
                            "LEGACY_REVIEW_BINDING_INVALID",
                            {"derived_review_count": len(review_rows)},
                        )
                    )
                    continue
                review_node_id = str(review_rows[0][0])
                # A canonical candidate ledger means this lane has already
                # crossed the migration boundary.  Legacy attempt metadata is
                # then audit history only: later resumes must not compare it
                # with, or fence, the authoritative candidate/review rows.
                existing_candidates = self.lane_candidates(
                    run_id, build_node_id, limit=10_000
                )
                existing_reviews = self.candidate_reviews(
                    run_id, review_node_id, limit=10_000
                )
                if existing_candidates or existing_reviews:
                    self.conn.execute(
                        "DELETE FROM legacy_review_migration_blocks"
                        " WHERE run_id=? AND build_node_id=?",
                        (run_id, build_node_id),
                    )
                    outcomes.append(
                        LegacyReviewMigration(
                            build_node_id=build_node_id,
                            migrated=False,
                            blocked=False,
                            reason=None,
                            candidates=existing_candidates,
                            reviews=existing_reviews,
                        )
                    )
                    continue
                existing_block = self._legacy_review_migration_block(
                    run_id, build_node_id
                )
                if existing_block is not None:
                    outcomes.append(
                        LegacyReviewMigration(
                            build_node_id=build_node_id,
                            migrated=False,
                            blocked=True,
                            reason=existing_block,
                        )
                    )
                    continue
                node = self.conn.execute(
                    "SELECT output_sha FROM node_lifecycle"
                    " WHERE run_id=? AND node_id=?",
                    (run_id, build_node_id),
                ).fetchone()
                if node is None:
                    outcomes.append(
                        self._legacy_review_migration_failure(
                            run_id, build_node_id, "LEGACY_BUILD_NODE_UNKNOWN", {}
                        )
                    )
                    continue
                try:
                    if any(
                        not isinstance(item.candidate_seq, int)
                        or isinstance(item.candidate_seq, bool)
                        for item in rows
                    ):
                        raise LifecycleError("LEGACY_CANDIDATE_ORDER_INVALID")
                    ordered = tuple(sorted(rows, key=lambda item: item.candidate_seq))
                    sequences = tuple(item.candidate_seq for item in ordered)
                    if sequences != tuple(range(1, len(ordered) + 1)):
                        raise LifecycleError("LEGACY_CANDIDATE_ORDER_INVALID")
                    candidate_shas = tuple(
                        _require_candidate_sha(
                            item.candidate_sha, field_name="legacy_candidate_sha"
                        )
                        for item in ordered
                    )
                    if len(set(candidate_shas)) != len(candidate_shas):
                        raise LifecycleError("LEGACY_CANDIDATE_DUPLICATE")
                    for item in ordered:
                        _require_candidate_sha(
                            item.base_sha, field_name="legacy_base_sha"
                        )
                        _require_candidate_sha(
                            item.review_digest, field_name="legacy_review_digest"
                        )
                        if (
                            not isinstance(item.receipt_path, str)
                            or not item.receipt_path.strip()
                        ):
                            raise LifecycleError("LEGACY_RECEIPT_PATH_INVALID")
                        _require_generation(
                            item.builder_generation,
                            field_name="legacy_builder_generation",
                        )
                        _require_generation(
                            item.reviewer_generation,
                            field_name="legacy_reviewer_generation",
                        )
                        st.ReviewVerdict(item.verdict)
                        _canonical_findings(item.findings)
                        if not evidence_validator(item):
                            raise LifecycleError("LEGACY_RECEIPT_OR_DIGEST_INVALID")
                    if any(
                        ordered[index].builder_generation
                        < ordered[index - 1].builder_generation
                        for index in range(1, len(ordered))
                    ):
                        raise LifecycleError("LEGACY_BUILDER_GENERATION_REGRESSED")
                    if len(ordered) > 1 and ancestry_validator is None:
                        raise LifecycleError("LEGACY_CANDIDATE_ANCESTRY_UNPROVEN")
                    for index in range(1, len(ordered)):
                        if not ancestry_validator(
                            candidate_shas[index - 1], candidate_shas[index]
                        ):
                            raise LifecycleError("LEGACY_CANDIDATE_ANCESTRY_UNPROVEN")
                    output_sha = _require_candidate_sha(
                        node[0], field_name="legacy_build_output_sha"
                    )
                    if output_sha != candidate_shas[-1]:
                        raise LifecycleError("LEGACY_CANDIDATE_SHA_MISMATCH")
                except Exception as error:
                    reason = str(error)
                    if not reason.startswith("LEGACY_"):
                        reason = "LEGACY_REVIEW_EVIDENCE_INVALID"
                    outcomes.append(
                        self._legacy_review_migration_failure(
                            run_id, build_node_id, reason, {"error": str(error)}
                        )
                    )
                    continue

                now = now_iso()
                candidates: List[st.LaneCandidate] = []
                reviews: List[st.CandidateReview] = []
                for index, (item, candidate_sha) in enumerate(
                    zip(ordered, candidate_shas)
                ):
                    parent_sha = candidate_shas[index - 1] if index else None
                    typed_findings, findings_json = _canonical_findings(item.findings)
                    verdict = st.ReviewVerdict(item.verdict)
                    self.conn.execute(
                        "INSERT INTO lane_candidates"
                        " (run_id, build_node_id, candidate_seq, candidate_sha,"
                        "  parent_candidate_sha, builder_generation, created_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            run_id,
                            build_node_id,
                            item.candidate_seq,
                            candidate_sha,
                            parent_sha,
                            item.builder_generation,
                            now,
                        ),
                    )
                    self.conn.execute(
                        "INSERT INTO candidate_reviews"
                        " (run_id, review_node_id, candidate_sha, reviewer_generation,"
                        "  state, dispatched_at, review_digest, receipt_path,"
                        "  findings_json, verdict, completed_at)"
                        " VALUES (?,?,?,?,?,NULL,?,?,?,?,?)",
                        (
                            run_id,
                            review_node_id,
                            candidate_sha,
                            item.reviewer_generation,
                            st.CandidateReviewState.COMPLETED.value,
                            item.review_digest,
                            item.receipt_path,
                            findings_json,
                            verdict.value,
                            now,
                        ),
                    )
                    candidate = st.LaneCandidate(
                        run_id=run_id,
                        build_node_id=build_node_id,
                        candidate_seq=item.candidate_seq,
                        candidate_sha=candidate_sha,
                        parent_candidate_sha=parent_sha,
                        builder_generation=item.builder_generation,
                        created_at=now,
                    )
                    review = st.CandidateReview(
                        run_id=run_id,
                        review_node_id=review_node_id,
                        candidate_sha=candidate_sha,
                        reviewer_generation=item.reviewer_generation,
                        state=st.CandidateReviewState.COMPLETED,
                        dispatched_at=None,
                        review_digest=item.review_digest,
                        receipt_path=item.receipt_path,
                        findings=typed_findings,
                        verdict=verdict,
                        completed_at=now,
                    )
                    candidates.append(candidate)
                    reviews.append(review)
                    if verdict is st.ReviewVerdict.REJECTED:
                        self.conn.execute(
                            "INSERT INTO repair_handoffs"
                            " (run_id, build_node_id, rejected_candidate_sha,"
                            "  findings_json, state, builder_generation,"
                            "  submitted_at, acknowledged_at)"
                            " VALUES (?,?,?,?,?,?,NULL,NULL)",
                            (
                                run_id,
                                build_node_id,
                                candidate_sha,
                                findings_json,
                                st.RepairHandoffState.PENDING.value,
                                item.builder_generation,
                            ),
                        )
                outcomes.append(
                    LegacyReviewMigration(
                        build_node_id=build_node_id,
                        migrated=True,
                        blocked=False,
                        reason=None,
                        candidates=tuple(candidates),
                        reviews=tuple(reviews),
                    )
                )
            self.conn.execute("COMMIT")
            return tuple(outcomes)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def publish_candidate(
        self,
        run_id: str,
        build_node_id: str,
        candidate_sha: str,
        *,
        parent_candidate_sha: Optional[str] = None,
        builder_generation: int,
        ancestry_validator: Optional[Callable[[str, str], bool]] = None,
        repo_path: Optional[Path] = None,
    ) -> st.CandidatePublication:
        """Append one proven descendant candidate, or return its exact replay.

        A repository-aware validator receives ``(parent_sha, candidate_sha)``;
        absent an injected validator, ``repo_path`` is required for every
        non-initial candidate and git supplies that proof.  Neither a SHA's
        lexical order nor a builder's assertion is ancestry evidence.
        """
        candidate_sha = _require_candidate_sha(candidate_sha)
        builder_generation = _require_generation(
            builder_generation, field_name="builder_generation"
        )
        parent_sha = (
            _require_candidate_sha(
                parent_candidate_sha, field_name="parent_candidate_sha"
            )
            if parent_candidate_sha is not None
            else None
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            node = self.conn.execute(
                "SELECT 1 FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, build_node_id),
            ).fetchone()
            if node is None:
                raise UnknownNode(f"{run_id}/{build_node_id} has no lifecycle row")
            existing = self._candidate(run_id, build_node_id, candidate_sha)
            blocked = self._legacy_review_migration_block(run_id, build_node_id)
            if blocked is not None:
                raise LegacyReviewMigrationBlocked(run_id, build_node_id, blocked)
            if existing is not None:
                if (
                    existing.parent_candidate_sha != parent_sha
                    or existing.builder_generation != builder_generation
                ):
                    raise LifecycleError(
                        "candidate replay disagrees with the immutable publication"
                    )
                self.conn.execute("COMMIT")
                return st.CandidatePublication(candidate=existing, created=False)
            previous_row = self.conn.execute(
                "SELECT run_id, build_node_id, candidate_seq, candidate_sha,"
                " parent_candidate_sha, builder_generation, created_at"
                " FROM lane_candidates WHERE run_id=? AND build_node_id=?"
                " ORDER BY candidate_seq DESC LIMIT 1",
                (run_id, build_node_id),
            ).fetchone()
            previous = _candidate_from_row(previous_row) if previous_row else None
            if previous is None:
                if parent_sha is not None:
                    raise LifecycleError(
                        "the first lane candidate has no parent candidate SHA"
                    )
            else:
                if parent_sha != previous.candidate_sha:
                    raise LifecycleError(
                        "a candidate must name the latest published candidate as parent"
                    )
                if builder_generation < previous.builder_generation:
                    raise LifecycleError(
                        "a stale builder generation cannot publish a candidate"
                    )
                proven = False
                if ancestry_validator is not None:
                    try:
                        proven = bool(ancestry_validator(parent_sha, candidate_sha))
                    except Exception as error:
                        raise LifecycleError(
                            "candidate ancestry validator did not prove descent"
                        ) from error
                elif repo_path is not None:
                    proven = _is_ancestor(repo_path, parent_sha, candidate_sha)
                else:
                    raise LifecycleError(
                        "a descendant candidate requires an ancestry validator or repo_path"
                    )
                if not proven:
                    raise LifecycleError(
                        "candidate SHA is equal to or not a proven descendant of its parent"
                    )
            sequence = 1 if previous is None else previous.candidate_seq + 1
            now = now_iso()
            self.conn.execute(
                "INSERT INTO lane_candidates"
                " (run_id, build_node_id, candidate_seq, candidate_sha,"
                "  parent_candidate_sha, builder_generation, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    build_node_id,
                    sequence,
                    candidate_sha,
                    parent_sha,
                    builder_generation,
                    now,
                ),
            )
            created = st.LaneCandidate(
                run_id=run_id,
                build_node_id=build_node_id,
                candidate_seq=sequence,
                candidate_sha=candidate_sha,
                parent_candidate_sha=parent_sha,
                builder_generation=builder_generation,
                created_at=now,
            )
            self.conn.execute("COMMIT")
            return st.CandidatePublication(candidate=created, created=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _require_review_candidate(
        self, run_id: str, review_node_id: str, candidate_sha: str
    ) -> st.LaneCandidate:
        build_node_id = _review_build_id(review_node_id)
        candidate = self._candidate(run_id, build_node_id, candidate_sha)
        if candidate is None:
            raise LifecycleError(
                f"{run_id}/{review_node_id}: candidate was not published by {build_node_id}"
            )
        review = self.conn.execute(
            "SELECT kind, review_of FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, review_node_id),
        ).fetchone()
        if review is None:
            raise UnknownNode(f"{run_id}/{review_node_id} has no derived review row")
        if review[0] != st.NodeKind.REVIEW.value or review[1] != build_node_id:
            raise LifecycleError(
                f"{run_id}/{review_node_id}: review/source binding is not durable"
            )
        return candidate

    def _record_review_output_audit(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        reason: str,
        reviewer_generation: int,
        verdict: st.ReviewVerdict,
        review_digest: str,
        receipt_path: str,
        findings_json: str,
    ) -> None:
        """Persist ignored stale/duplicate reviewer output as typed audit evidence."""
        self.conn.execute(
            "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
            " reason, actor, detail_json, created_at)"
            " VALUES (?,?,'node',NULL,NULL,?,'scheduler',?,?)",
            (
                run_id,
                review_node_id,
                reason,
                json.dumps(
                    {
                        "candidate_sha": candidate_sha,
                        "reviewer_generation": reviewer_generation,
                        "verdict": verdict.value,
                        "review_digest": review_digest,
                        "receipt_path": receipt_path,
                        "findings": json.loads(findings_json),
                    },
                    sort_keys=True,
                ),
                now_iso(),
            ),
        )

    @serialized
    def begin_review(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        reviewer_generation: int,
    ) -> st.ReviewBegin:
        """Publish one review before any reviewer prompt is submitted."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        reviewer_generation = _require_generation(
            reviewer_generation, field_name="reviewer_generation"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            build_node_id = _review_build_id(review_node_id)
            blocked = self._legacy_review_migration_block(run_id, build_node_id)
            if blocked is not None:
                raise LegacyReviewMigrationBlocked(run_id, build_node_id, blocked)
            self._require_review_candidate(run_id, review_node_id, candidate_sha)
            existing = self._review(run_id, review_node_id, candidate_sha)
            if existing is not None:
                self.conn.execute("COMMIT")
                return st.ReviewBegin(
                    review=existing,
                    created=False,
                    should_dispatch=(
                        existing.state is st.CandidateReviewState.PUBLISHED
                    ),
                )
            self.conn.execute(
                "INSERT INTO candidate_reviews"
                " (run_id, review_node_id, candidate_sha, reviewer_generation, state,"
                "  dispatched_at, review_digest, receipt_path, findings_json, verdict,"
                "  completed_at)"
                " VALUES (?,?,?,?,?,NULL,NULL,NULL,'[]',NULL,NULL)",
                (
                    run_id,
                    review_node_id,
                    candidate_sha,
                    reviewer_generation,
                    st.CandidateReviewState.PUBLISHED.value,
                ),
            )
            created = st.CandidateReview(
                run_id=run_id,
                review_node_id=review_node_id,
                candidate_sha=candidate_sha,
                reviewer_generation=reviewer_generation,
                state=st.CandidateReviewState.PUBLISHED,
                dispatched_at=None,
                review_digest=None,
                receipt_path=None,
                findings=(),
                verdict=None,
                completed_at=None,
            )
            self.conn.execute("COMMIT")
            return st.ReviewBegin(review=created, created=True, should_dispatch=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def mark_review_dispatched(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        reviewer_generation: int,
    ) -> st.CandidateReview:
        """Record transcript-proven prompt submission exactly once."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        reviewer_generation = _require_generation(
            reviewer_generation, field_name="reviewer_generation"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._require_review_candidate(run_id, review_node_id, candidate_sha)
            existing = self._review(run_id, review_node_id, candidate_sha)
            if existing is None:
                raise LifecycleError("a review dispatch requires a published review")
            if existing.reviewer_generation != reviewer_generation:
                raise LifecycleError("review dispatch generation is stale")
            if existing.state is not st.CandidateReviewState.PUBLISHED:
                self.conn.execute("COMMIT")
                return existing
            dispatched_at = now_iso()
            changed = self.conn.execute(
                "UPDATE candidate_reviews SET state=?, dispatched_at=?"
                " WHERE run_id=? AND review_node_id=? AND candidate_sha=?"
                " AND state=? AND reviewer_generation=?",
                (
                    st.CandidateReviewState.DISPATCHED.value,
                    dispatched_at,
                    run_id,
                    review_node_id,
                    candidate_sha,
                    st.CandidateReviewState.PUBLISHED.value,
                    reviewer_generation,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError("candidate review dispatch CAS lost unexpectedly")
            dispatched = self._review(run_id, review_node_id, candidate_sha)
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,?,'node',?,?,?,'scheduler',?,?)",
                (
                    run_id,
                    review_node_id,
                    st.CandidateReviewState.PUBLISHED.value,
                    st.CandidateReviewState.DISPATCHED.value,
                    "candidate-review-dispatched",
                    json.dumps(
                        {
                            "candidate_sha": candidate_sha,
                            "reviewer_generation": reviewer_generation,
                            "dispatched_at": dispatched_at,
                        },
                        sort_keys=True,
                    ),
                    dispatched_at,
                ),
            )
            self.conn.execute("COMMIT")
            return dispatched
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def recover_review_dispatch(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        expected_reviewer_generation: int,
        reviewer_generation: int,
    ) -> st.ReviewBegin:
        """Re-publish an unfinished review for a proven replacement reviewer."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        expected_reviewer_generation = _require_generation(
            expected_reviewer_generation, field_name="expected_reviewer_generation"
        )
        reviewer_generation = _require_generation(
            reviewer_generation, field_name="reviewer_generation"
        )
        if reviewer_generation <= expected_reviewer_generation:
            raise LifecycleError(
                "a replacement reviewer generation must advance its predecessor"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            build_node_id = _review_build_id(review_node_id)
            blocked = self._legacy_review_migration_block(run_id, build_node_id)
            if blocked is not None:
                raise LegacyReviewMigrationBlocked(run_id, build_node_id, blocked)
            self._require_review_candidate(run_id, review_node_id, candidate_sha)
            existing = self._review(run_id, review_node_id, candidate_sha)
            if existing is None:
                raise LifecycleError("an unpublished review cannot be recovered")
            if (
                existing.terminal
                or existing.reviewer_generation != expected_reviewer_generation
            ):
                self.conn.execute("COMMIT")
                return st.ReviewBegin(
                    review=existing, created=False, should_dispatch=False
                )
            changed = self.conn.execute(
                "UPDATE candidate_reviews SET reviewer_generation=?, state=?,"
                " dispatched_at=NULL"
                " WHERE run_id=? AND review_node_id=? AND candidate_sha=?"
                " AND state IN (?,?) AND reviewer_generation=?",
                (
                    reviewer_generation,
                    st.CandidateReviewState.PUBLISHED.value,
                    run_id,
                    review_node_id,
                    candidate_sha,
                    st.CandidateReviewState.PUBLISHED.value,
                    st.CandidateReviewState.DISPATCHED.value,
                    expected_reviewer_generation,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError("candidate review recovery CAS lost unexpectedly")
            updated = self._review(run_id, review_node_id, candidate_sha)
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,?,'node',?,?,?,'scheduler',?,?)",
                (
                    run_id,
                    review_node_id,
                    existing.state.value,
                    st.CandidateReviewState.PUBLISHED.value,
                    "candidate-review-recovered",
                    json.dumps(
                        {
                            "candidate_sha": candidate_sha,
                            "from_generation": expected_reviewer_generation,
                            "to_generation": reviewer_generation,
                        },
                        sort_keys=True,
                    ),
                    now_iso(),
                ),
            )
            self.conn.execute("COMMIT")
            return st.ReviewBegin(review=updated, created=False, should_dispatch=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def complete_review(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        reviewer_generation: int,
        verdict: st.ReviewVerdict,
        review_digest: str,
        receipt_path: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> st.ReviewCompletion:
        """CAS the first PASS verdict; rejection must atomically create a handoff."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        reviewer_generation = _require_generation(
            reviewer_generation, field_name="reviewer_generation"
        )
        try:
            verdict = st.ReviewVerdict(verdict)
        except ValueError as error:
            raise LifecycleError(f"unknown review verdict {verdict!r}") from error
        if not isinstance(review_digest, str) or not review_digest.strip():
            raise LifecycleError("review_digest must be a non-empty string")
        if not isinstance(receipt_path, str) or not receipt_path.strip():
            raise LifecycleError("receipt_path must be a non-empty string")
        typed_findings, findings_json = _canonical_findings(findings)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._require_review_candidate(run_id, review_node_id, candidate_sha)
            existing = self._review(run_id, review_node_id, candidate_sha)
            if existing is None:
                raise LifecycleError("a review result requires a prior dispatch")
            if existing.terminal:
                self._record_review_output_audit(
                    run_id,
                    review_node_id,
                    candidate_sha,
                    reason="candidate-review-duplicate-output",
                    reviewer_generation=reviewer_generation,
                    verdict=verdict,
                    review_digest=review_digest,
                    receipt_path=receipt_path,
                    findings_json=findings_json,
                )
                self.conn.execute("COMMIT")
                return st.ReviewCompletion(review=existing, completed=False)
            if existing.reviewer_generation != reviewer_generation:
                self._record_review_output_audit(
                    run_id,
                    review_node_id,
                    candidate_sha,
                    reason="candidate-review-stale-generation",
                    reviewer_generation=reviewer_generation,
                    verdict=verdict,
                    review_digest=review_digest,
                    receipt_path=receipt_path,
                    findings_json=findings_json,
                )
                self.conn.execute("COMMIT")
                return st.ReviewCompletion(review=existing, completed=False)
            if existing.state is not st.CandidateReviewState.DISPATCHED:
                raise LifecycleError("a review result requires durable dispatch proof")
            if verdict is st.ReviewVerdict.REJECTED:
                raise LifecycleError(
                    "a rejected review must use reject_and_create_handoff atomically"
                )
            now = now_iso()
            changed = self.conn.execute(
                "UPDATE candidate_reviews SET state=?, review_digest=?, receipt_path=?,"
                " findings_json=?, verdict=?, completed_at=?"
                " WHERE run_id=? AND review_node_id=? AND candidate_sha=?"
                " AND state=? AND reviewer_generation=? AND verdict IS NULL",
                (
                    st.CandidateReviewState.COMPLETED.value,
                    review_digest,
                    receipt_path,
                    findings_json,
                    verdict.value,
                    now,
                    run_id,
                    review_node_id,
                    candidate_sha,
                    st.CandidateReviewState.DISPATCHED.value,
                    reviewer_generation,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "candidate review completion CAS lost unexpectedly"
                )
            completed = st.CandidateReview(
                run_id=run_id,
                review_node_id=review_node_id,
                candidate_sha=candidate_sha,
                reviewer_generation=reviewer_generation,
                state=st.CandidateReviewState.COMPLETED,
                dispatched_at=existing.dispatched_at,
                review_digest=review_digest,
                receipt_path=receipt_path,
                findings=typed_findings,
                verdict=verdict,
                completed_at=now,
            )
            self.conn.execute("COMMIT")
            return st.ReviewCompletion(review=completed, completed=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _bind_repair_parent_to_current_attempt(
        self, run_id: str, build_node_id: str, rejected_candidate_sha: str
    ) -> bool:
        """Bind recovery when this review belongs to an active build attempt."""
        lifecycle = self.conn.execute(
            "SELECT attempt_no FROM node_lifecycle WHERE run_id=? AND node_id=?",
            (run_id, build_node_id),
        ).fetchone()
        if lifecycle is None or int(lifecycle[0]) < 1:
            return False
        attempt_no = int(lifecycle[0])
        attempt = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, build_node_id, attempt_no),
        ).fetchone()
        if attempt is None:
            raise LifecycleError("a repair handoff requires an owning attempt row")
        try:
            payload = json.loads(attempt[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LifecycleError("owning attempt extras are not valid JSON") from exc
        if not isinstance(payload, dict):
            raise LifecycleError("owning attempt extras are not an object")
        payload[REPAIR_HANDOFF_RECOVERY_KEY] = rejected_candidate_sha
        changed = self.conn.execute(
            "UPDATE attempts SET extra_json=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (
                json.dumps(payload, sort_keys=True),
                run_id,
                build_node_id,
                attempt_no,
            ),
        ).rowcount
        if changed != 1:
            raise LifecycleError("repair handoff attempt binding CAS lost unexpectedly")
        return True

    @serialized
    def reject_and_create_handoff(
        self,
        run_id: str,
        review_node_id: str,
        candidate_sha: str,
        *,
        reviewer_generation: int,
        builder_generation: int,
        review_digest: str,
        receipt_path: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> st.RejectionHandoff:
        """Atomically record a REJECTED review and its one repair handoff."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        reviewer_generation = _require_generation(
            reviewer_generation, field_name="reviewer_generation"
        )
        builder_generation = _require_generation(
            builder_generation, field_name="builder_generation"
        )
        if not isinstance(review_digest, str) or not review_digest.strip():
            raise LifecycleError("review_digest must be a non-empty string")
        if not isinstance(receipt_path, str) or not receipt_path.strip():
            raise LifecycleError("receipt_path must be a non-empty string")
        typed_findings, findings_json = _canonical_findings(findings)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            candidate = self._require_review_candidate(
                run_id, review_node_id, candidate_sha
            )
            if builder_generation < candidate.builder_generation:
                raise LifecycleError(
                    "a stale builder generation cannot receive a repair handoff"
                )
            build_node_id = candidate.build_node_id
            existing = self._review(run_id, review_node_id, candidate_sha)
            if existing is None:
                raise LifecycleError("a rejected review requires a prior dispatch")
            if existing.terminal:
                handoff = self._handoff(run_id, build_node_id, candidate_sha)
                if existing.verdict is st.ReviewVerdict.REJECTED and handoff is None:
                    raise LifecycleError(
                        "rejected review has no repair handoff; ledger invariant broken"
                    )
                self._record_review_output_audit(
                    run_id,
                    review_node_id,
                    candidate_sha,
                    reason="candidate-review-duplicate-output",
                    reviewer_generation=reviewer_generation,
                    verdict=st.ReviewVerdict.REJECTED,
                    review_digest=review_digest,
                    receipt_path=receipt_path,
                    findings_json=findings_json,
                )
                self._bind_repair_parent_to_current_attempt(
                    run_id, build_node_id, candidate_sha
                )
                self.conn.execute("COMMIT")
                return st.RejectionHandoff(
                    review=existing, handoff=handoff, completed=False, created=False
                )
            if existing.reviewer_generation != reviewer_generation:
                self._record_review_output_audit(
                    run_id,
                    review_node_id,
                    candidate_sha,
                    reason="candidate-review-stale-generation",
                    reviewer_generation=reviewer_generation,
                    verdict=st.ReviewVerdict.REJECTED,
                    review_digest=review_digest,
                    receipt_path=receipt_path,
                    findings_json=findings_json,
                )
                self.conn.execute("COMMIT")
                return st.RejectionHandoff(
                    review=existing, handoff=None, completed=False, created=False
                )
            if existing.state is not st.CandidateReviewState.DISPATCHED:
                raise LifecycleError(
                    "a rejected review requires durable dispatch proof"
                )
            now = now_iso()
            changed = self.conn.execute(
                "UPDATE candidate_reviews SET state=?, review_digest=?, receipt_path=?,"
                " findings_json=?, verdict=?, completed_at=?"
                " WHERE run_id=? AND review_node_id=? AND candidate_sha=?"
                " AND state=? AND reviewer_generation=? AND verdict IS NULL",
                (
                    st.CandidateReviewState.COMPLETED.value,
                    review_digest,
                    receipt_path,
                    findings_json,
                    st.ReviewVerdict.REJECTED.value,
                    now,
                    run_id,
                    review_node_id,
                    candidate_sha,
                    st.CandidateReviewState.DISPATCHED.value,
                    reviewer_generation,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError("candidate review rejection CAS lost unexpectedly")
            self.conn.execute(
                "INSERT INTO repair_handoffs"
                " (run_id, build_node_id, rejected_candidate_sha, findings_json, state,"
                "  builder_generation, submitted_at, acknowledged_at)"
                " VALUES (?,?,?,?,?,?,NULL,NULL)",
                (
                    run_id,
                    build_node_id,
                    candidate_sha,
                    findings_json,
                    st.RepairHandoffState.PENDING.value,
                    builder_generation,
                ),
            )
            self._bind_repair_parent_to_current_attempt(
                run_id, build_node_id, candidate_sha
            )
            review = st.CandidateReview(
                run_id=run_id,
                review_node_id=review_node_id,
                candidate_sha=candidate_sha,
                reviewer_generation=reviewer_generation,
                state=st.CandidateReviewState.COMPLETED,
                dispatched_at=existing.dispatched_at,
                review_digest=review_digest,
                receipt_path=receipt_path,
                findings=typed_findings,
                verdict=st.ReviewVerdict.REJECTED,
                completed_at=now,
            )
            handoff = st.RepairHandoff(
                run_id=run_id,
                build_node_id=build_node_id,
                rejected_candidate_sha=candidate_sha,
                findings=typed_findings,
                state=st.RepairHandoffState.PENDING,
                builder_generation=builder_generation,
                submitted_at=None,
                acknowledged_at=None,
            )
            self.conn.execute("COMMIT")
            return st.RejectionHandoff(
                review=review, handoff=handoff, completed=True, created=True
            )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def mark_handoff_submitted(
        self,
        run_id: str,
        build_node_id: str,
        rejected_candidate_sha: str,
        *,
        builder_generation: int,
    ) -> st.HandoffSubmission:
        """Record successful prompt delivery before its acknowledgement arrives."""
        rejected_candidate_sha = _require_candidate_sha(
            rejected_candidate_sha, field_name="rejected_candidate_sha"
        )
        builder_generation = _require_generation(
            builder_generation, field_name="builder_generation"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            handoff = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            if handoff is None:
                raise LifecycleError("no repair handoff exists for this candidate")
            current_builder = self.current_actor_session(
                run_id, build_node_id, "builder"
            )
            if handoff.builder_generation != builder_generation or (
                current_builder is not None
                and current_builder.generation != builder_generation
            ):
                self.conn.execute("COMMIT")
                return st.HandoffSubmission(handoff=handoff, submitted=False)
            if handoff.state is st.RepairHandoffState.PENDING:
                now = now_iso()
                self.conn.execute(
                    "UPDATE repair_handoffs SET state=?, submitted_at=?"
                    " WHERE run_id=? AND build_node_id=? AND rejected_candidate_sha=?"
                    " AND state=? AND builder_generation=?",
                    (
                        st.RepairHandoffState.SUBMITTED.value,
                        now,
                        run_id,
                        build_node_id,
                        rejected_candidate_sha,
                        st.RepairHandoffState.PENDING.value,
                        builder_generation,
                    ),
                )
                handoff = self._handoff(run_id, build_node_id, rejected_candidate_sha)
                self.conn.execute("COMMIT")
                return st.HandoffSubmission(handoff=handoff, submitted=True)
            self.conn.execute("COMMIT")
            return st.HandoffSubmission(
                handoff=handoff,
                submitted=handoff.state
                in (
                    st.RepairHandoffState.SUBMITTED,
                    st.RepairHandoffState.ACKNOWLEDGED,
                ),
            )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def acknowledge_handoff(
        self,
        run_id: str,
        build_node_id: str,
        rejected_candidate_sha: str,
        *,
        builder_generation: int,
    ) -> st.HandoffAcknowledgement:
        """Acknowledge only the submitted handoff for this SHA and generation."""
        rejected_candidate_sha = _require_candidate_sha(
            rejected_candidate_sha, field_name="rejected_candidate_sha"
        )
        builder_generation = _require_generation(
            builder_generation, field_name="builder_generation"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            handoff = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            if handoff is None:
                raise LifecycleError("no repair handoff exists for this candidate")
            current_builder = self.current_actor_session(
                run_id, build_node_id, "builder"
            )
            if handoff.builder_generation != builder_generation or (
                current_builder is not None
                and current_builder.generation != builder_generation
            ):
                self.conn.execute("COMMIT")
                return st.HandoffAcknowledgement(handoff=handoff, acknowledged=False)
            if handoff.state is st.RepairHandoffState.ACKNOWLEDGED:
                self.conn.execute("COMMIT")
                return st.HandoffAcknowledgement(handoff=handoff, acknowledged=True)
            if handoff.state is not st.RepairHandoffState.SUBMITTED:
                self.conn.execute("COMMIT")
                return st.HandoffAcknowledgement(handoff=handoff, acknowledged=False)
            now = now_iso()
            changed = self.conn.execute(
                "UPDATE repair_handoffs SET state=?, acknowledged_at=?"
                " WHERE run_id=? AND build_node_id=? AND rejected_candidate_sha=?"
                " AND state=? AND builder_generation=?",
                (
                    st.RepairHandoffState.ACKNOWLEDGED.value,
                    now,
                    run_id,
                    build_node_id,
                    rejected_candidate_sha,
                    st.RepairHandoffState.SUBMITTED.value,
                    builder_generation,
                ),
            ).rowcount
            if changed != 1:
                raise LifecycleError(
                    "repair handoff acknowledgement CAS lost unexpectedly"
                )
            acknowledged = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            self.conn.execute("COMMIT")
            return st.HandoffAcknowledgement(handoff=acknowledged, acknowledged=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def fail_handoff(
        self,
        run_id: str,
        build_node_id: str,
        rejected_candidate_sha: str,
        *,
        builder_generation: int,
        reason: str,
    ) -> Optional[st.RepairHandoff]:
        """Persist bounded handoff failure without changing review truth."""
        rejected_candidate_sha = _require_candidate_sha(
            rejected_candidate_sha, field_name="rejected_candidate_sha"
        )
        builder_generation = _require_generation(
            builder_generation, field_name="builder_generation"
        )
        if not isinstance(reason, str) or not reason.strip():
            raise LifecycleError("handoff failure reason must be non-empty")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            handoff = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            if (
                handoff is None
                or handoff.builder_generation != builder_generation
                or handoff.state
                in (st.RepairHandoffState.ACKNOWLEDGED, st.RepairHandoffState.FAILED)
            ):
                self.conn.execute("COMMIT")
                return handoff
            self.conn.execute(
                "UPDATE repair_handoffs SET state=?"
                " WHERE run_id=? AND build_node_id=? AND rejected_candidate_sha=?"
                " AND builder_generation=?",
                (
                    st.RepairHandoffState.FAILED.value,
                    run_id,
                    build_node_id,
                    rejected_candidate_sha,
                    builder_generation,
                ),
            )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,?,'node',NULL,NULL,'repair-handoff-failed','scheduler',?,?)",
                (
                    run_id,
                    build_node_id,
                    json.dumps(
                        {
                            "candidate_sha": rejected_candidate_sha,
                            "builder_generation": builder_generation,
                            "reason": reason,
                        },
                        sort_keys=True,
                    ),
                    now_iso(),
                ),
            )
            failed = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            self.conn.execute("COMMIT")
            return failed
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def spend_lane_retry(
        self,
        run_id: str,
        build_node_id: str,
        retry_class: st.LaneRetryClass,
        *,
        cycle_seq: int,
        candidate_sha: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> st.LaneRetrySpendRecord:
        """Record one correction-loop budget debit without minting an attempt."""
        try:
            retry_class = st.LaneRetryClass(retry_class)
        except ValueError as error:
            raise LifecycleError(f"unknown lane retry class {retry_class!r}") from error
        if (
            not isinstance(cycle_seq, int)
            or isinstance(cycle_seq, bool)
            or cycle_seq < 1
        ):
            raise LifecycleError("lane retry cycle_seq must be a positive integer")
        candidate_sha = (
            _require_candidate_sha(candidate_sha) if candidate_sha is not None else None
        )
        typed_detail, detail_json = _canonical_detail(detail)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            node = self.conn.execute(
                "SELECT 1 FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, build_node_id),
            ).fetchone()
            if node is None:
                raise UnknownNode(f"{run_id}/{build_node_id} has no lifecycle row")
            existing = self.conn.execute(
                "SELECT run_id, build_node_id, retry_class, cycle_seq, candidate_sha,"
                " detail_json, created_at FROM lane_retry_spend"
                " WHERE run_id=? AND build_node_id=? AND cycle_seq=?",
                (run_id, build_node_id, cycle_seq),
            ).fetchone()
            if existing is not None:
                spend = _lane_retry_spend_from_row(existing)
                if (
                    spend.retry_class is not retry_class
                    or spend.candidate_sha != candidate_sha
                    or dict(spend.detail) != dict(typed_detail)
                ):
                    raise LifecycleError(
                        "lane retry replay disagrees with the immutable spend"
                    )
                self.conn.execute("COMMIT")
                return st.LaneRetrySpendRecord(spend=spend, created=False)
            now = now_iso()
            self.conn.execute(
                "INSERT INTO lane_retry_spend"
                " (run_id, build_node_id, retry_class, cycle_seq, candidate_sha,"
                "  detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    build_node_id,
                    retry_class.value,
                    cycle_seq,
                    candidate_sha,
                    detail_json,
                    now,
                ),
            )
            spend = st.LaneRetrySpend(
                run_id=run_id,
                build_node_id=build_node_id,
                retry_class=retry_class,
                cycle_seq=cycle_seq,
                candidate_sha=candidate_sha,
                detail=typed_detail,
                created_at=now,
            )
            self.conn.execute("COMMIT")
            return st.LaneRetrySpendRecord(spend=spend, created=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def lane_retry_spends(
        self, run_id: str, build_node_id: str, *, limit: int = 100
    ) -> Tuple[st.LaneRetrySpend, ...]:
        """The bounded durable correction-loop spend ledger for one lane."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("lane retry read limit must be a positive integer")
        rows = self.conn.execute(
            "SELECT run_id, build_node_id, retry_class, cycle_seq, candidate_sha,"
            " detail_json, created_at FROM lane_retry_spend"
            " WHERE run_id=? AND build_node_id=? ORDER BY cycle_seq LIMIT ?",
            (run_id, build_node_id, limit),
        ).fetchall()
        return tuple(_lane_retry_spend_from_row(row) for row in rows)

    @serialized
    def lane_retry_spend_floor(self, run_id: str, build_node_id: str) -> int:
        """The cycle sequence retained-lane budgets are counted from."""
        row = self.conn.execute(
            "SELECT lane_retry_spend_floor FROM node_lifecycle"
            " WHERE run_id=? AND node_id=?",
            (run_id, build_node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{build_node_id} has no lifecycle row")
        return int(row[0] or 0)

    @serialized
    def current_lane_retry_spends(
        self, run_id: str, build_node_id: str, *, limit: int = 100
    ) -> Tuple[st.LaneRetrySpend, ...]:
        """Correction-loop debits recorded after the latest operator boundary."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("lane retry read limit must be a positive integer")
        floor = self.lane_retry_spend_floor(run_id, build_node_id)
        rows = self.conn.execute(
            "SELECT run_id, build_node_id, retry_class, cycle_seq, candidate_sha,"
            " detail_json, created_at FROM lane_retry_spend"
            " WHERE run_id=? AND build_node_id=? AND cycle_seq>?"
            " ORDER BY cycle_seq LIMIT ?",
            (run_id, build_node_id, floor, limit),
        ).fetchall()
        return tuple(_lane_retry_spend_from_row(row) for row in rows)

    def _actor_session(
        self, run_id: str, build_node_id: str, actor_role: str, generation: int
    ) -> Optional[st.ActorSession]:
        row = self.conn.execute(
            "SELECT run_id, build_node_id, actor_role, generation, state, pane_id,"
            " tab_id, session_path, correlation_token, updated_at FROM actor_sessions"
            " WHERE run_id=? AND build_node_id=? AND actor_role=? AND generation=?",
            (run_id, build_node_id, actor_role, generation),
        ).fetchone()
        return _actor_session_from_row(row) if row is not None else None

    @serialized
    def actor_sessions(
        self,
        run_id: str,
        build_node_id: str,
        *,
        actor_role: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[st.ActorSession, ...]:
        """Bounded session-generation authority for resume and watchdog fencing."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("actor session read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, actor_role, generation, state, pane_id,"
            " tab_id, session_path, correlation_token, updated_at FROM actor_sessions"
            " WHERE run_id=? AND build_node_id=?"
        )
        params: Tuple[Any, ...] = (run_id, build_node_id)
        if actor_role is not None:
            sql += " AND actor_role=?"
            params = (run_id, build_node_id, actor_role)
        rows = self.conn.execute(
            sql + " ORDER BY actor_role, generation LIMIT ?", params + (limit,)
        ).fetchall()
        return tuple(_actor_session_from_row(row) for row in rows)

    @serialized
    def current_actor_session(
        self, run_id: str, build_node_id: str, actor_role: str
    ) -> Optional[st.ActorSession]:
        row = self.conn.execute(
            "SELECT run_id, build_node_id, actor_role, generation, state, pane_id,"
            " tab_id, session_path, correlation_token, updated_at FROM actor_sessions"
            " WHERE run_id=? AND build_node_id=? AND actor_role=? AND state=?",
            (run_id, build_node_id, actor_role, st.ActorSessionState.ACTIVE.value),
        ).fetchone()
        return _actor_session_from_row(row) if row is not None else None

    @serialized
    def register_actor_session(
        self,
        run_id: str,
        build_node_id: str,
        actor_role: str,
        *,
        generation: int,
        pane_id: str,
        session_path: str,
        correlation_token: str,
        tab_id: Optional[str] = None,
    ) -> st.ActorSession:
        """Register the first active generation; never replace an active peer."""
        generation = _require_generation(generation, field_name="generation")
        tab_id = _require_optional_tab_id(tab_id)
        values = {
            "actor_role": actor_role,
            "pane_id": pane_id,
            "session_path": session_path,
            "correlation_token": correlation_token,
        }
        if any(
            not isinstance(value, str) or not value.strip() for value in values.values()
        ):
            raise LifecycleError(
                "actor role, pane id, session path, and correlation token are required"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            node = self.conn.execute(
                "SELECT 1 FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, build_node_id),
            ).fetchone()
            if node is None:
                raise UnknownNode(f"{run_id}/{build_node_id} has no lifecycle row")
            existing = self._actor_session(
                run_id, build_node_id, actor_role, generation
            )
            if existing is not None:
                if (
                    existing.state is st.ActorSessionState.ACTIVE
                    and existing.pane_id == pane_id
                    and existing.tab_id == tab_id
                    and existing.session_path == session_path
                    and existing.correlation_token == correlation_token
                ):
                    self.conn.execute("COMMIT")
                    return existing
                raise LifecycleError(
                    "actor session generation already has different durable identity"
                )
            active = self.conn.execute(
                "SELECT generation FROM actor_sessions WHERE run_id=? AND build_node_id=?"
                " AND actor_role=? AND state=?",
                (run_id, build_node_id, actor_role, st.ActorSessionState.ACTIVE.value),
            ).fetchone()
            if active is not None:
                raise LifecycleError(
                    "an active actor session must be recovered, never overwritten"
                )
            now = now_iso()
            self.conn.execute(
                "INSERT INTO actor_sessions"
                " (run_id, build_node_id, actor_role, generation, state, pane_id,"
                "  tab_id, session_path, correlation_token, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    build_node_id,
                    actor_role,
                    generation,
                    st.ActorSessionState.ACTIVE.value,
                    pane_id,
                    tab_id,
                    session_path,
                    correlation_token,
                    now,
                ),
            )
            session = st.ActorSession(
                run_id=run_id,
                build_node_id=build_node_id,
                actor_role=actor_role,
                generation=generation,
                state=st.ActorSessionState.ACTIVE,
                pane_id=pane_id,
                tab_id=tab_id,
                session_path=session_path,
                correlation_token=correlation_token,
                updated_at=now,
            )
            self.conn.execute("COMMIT")
            return session
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def recover_builder_handoff(
        self,
        run_id: str,
        build_node_id: str,
        rejected_candidate_sha: str,
        *,
        expected_generation: int,
        generation: int,
        pane_id: str,
        session_path: str,
        correlation_token: str,
        tab_id: Optional[str] = None,
    ) -> st.ActorSessionRecovery:
        """Atomically replace a proven-absent builder and rebind its handoff."""
        rejected_candidate_sha = _require_candidate_sha(
            rejected_candidate_sha, field_name="rejected_candidate_sha"
        )
        expected_generation = _require_generation(
            expected_generation, field_name="expected_generation"
        )
        generation = _require_generation(generation, field_name="generation")
        if generation <= expected_generation:
            raise LifecycleError(
                "replacement builder generation must advance its predecessor"
            )
        tab_id = _require_optional_tab_id(tab_id)
        values = (pane_id, session_path, correlation_token)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise LifecycleError(
                "replacement pane id, session path, and correlation token are required"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active = self.current_actor_session(run_id, build_node_id, "builder")
            handoff = self._handoff(run_id, build_node_id, rejected_candidate_sha)
            if handoff is None:
                raise LifecycleError(
                    "builder handoff recovery requires a durable handoff row"
                )
            if active is not None and active.generation == generation:
                if (
                    active.pane_id == pane_id
                    and active.tab_id == tab_id
                    and active.session_path == session_path
                    and active.correlation_token == correlation_token
                    and handoff.builder_generation == generation
                ):
                    self.conn.execute("COMMIT")
                    return st.ActorSessionRecovery(session=active, recovered=False)
                raise LifecycleError(
                    "builder handoff recovery replay disagrees with durable identity"
                )
            if handoff.builder_generation != expected_generation:
                if active is None:
                    raise LifecycleError(
                        "builder handoff recovery lost its predecessor generation"
                    )
                self.conn.execute("COMMIT")
                return st.ActorSessionRecovery(session=active, recovered=False)
            if active is not None and active.generation != expected_generation:
                self.conn.execute("COMMIT")
                return st.ActorSessionRecovery(session=active, recovered=False)
            now = now_iso()
            if active is not None:
                closed = self.conn.execute(
                    "UPDATE actor_sessions SET state=?, updated_at=?"
                    " WHERE run_id=? AND build_node_id=? AND actor_role='builder'"
                    " AND generation=? AND state=?",
                    (
                        st.ActorSessionState.CLOSED.value,
                        now,
                        run_id,
                        build_node_id,
                        expected_generation,
                        st.ActorSessionState.ACTIVE.value,
                    ),
                ).rowcount
                if closed != 1:
                    raise LifecycleError(
                        "builder generation recovery CAS lost unexpectedly"
                    )
            else:
                predecessor = self.conn.execute(
                    "SELECT state FROM actor_sessions"
                    " WHERE run_id=? AND build_node_id=? AND actor_role='builder'"
                    " AND generation=?",
                    (run_id, build_node_id, expected_generation),
                ).fetchone()
                if (
                    predecessor is None
                    or predecessor[0] != st.ActorSessionState.CLOSED.value
                ):
                    raise LifecycleError(
                        "builder handoff recovery requires a proven-absent "
                        "predecessor generation"
                    )
            self.conn.execute(
                "INSERT INTO actor_sessions"
                " (run_id, build_node_id, actor_role, generation, state, pane_id,"
                "  tab_id, session_path, correlation_token, updated_at)"
                " VALUES (?,?,'builder',?,?,?,?,?,?,?)",
                (
                    run_id,
                    build_node_id,
                    generation,
                    st.ActorSessionState.ACTIVE.value,
                    pane_id,
                    tab_id,
                    session_path,
                    correlation_token,
                    now,
                ),
            )
            rebound = self.conn.execute(
                "UPDATE repair_handoffs SET builder_generation=?, state=?,"
                " submitted_at=NULL, acknowledged_at=NULL"
                " WHERE run_id=? AND build_node_id=? AND rejected_candidate_sha=?"
                " AND builder_generation=? AND state IN (?,?,?,?)",
                (
                    generation,
                    st.RepairHandoffState.PENDING.value,
                    run_id,
                    build_node_id,
                    rejected_candidate_sha,
                    expected_generation,
                    st.RepairHandoffState.PENDING.value,
                    st.RepairHandoffState.SUBMITTED.value,
                    st.RepairHandoffState.ACKNOWLEDGED.value,
                    st.RepairHandoffState.FAILED.value,
                ),
            ).rowcount
            if rebound != 1:
                raise LifecycleError(
                    "repair handoff generation recovery CAS lost unexpectedly"
                )
            session = st.ActorSession(
                run_id=run_id,
                build_node_id=build_node_id,
                actor_role="builder",
                generation=generation,
                state=st.ActorSessionState.ACTIVE,
                pane_id=pane_id,
                tab_id=tab_id,
                session_path=session_path,
                correlation_token=correlation_token,
                updated_at=now,
            )
            self.conn.execute("COMMIT")
            return st.ActorSessionRecovery(session=session, recovered=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def recover_actor_session(
        self,
        run_id: str,
        build_node_id: str,
        actor_role: str,
        *,
        expected_generation: int,
        generation: int,
        pane_id: str,
        session_path: str,
        correlation_token: str,
        tab_id: Optional[str] = None,
    ) -> st.ActorSessionRecovery:
        """Close one proven-dead generation and atomically claim its replacement."""
        expected_generation = _require_generation(
            expected_generation, field_name="expected_generation"
        )
        generation = _require_generation(generation, field_name="generation")
        if generation <= expected_generation:
            raise LifecycleError(
                "replacement actor generation must advance its predecessor"
            )
        tab_id = _require_optional_tab_id(tab_id)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active = self.current_actor_session(run_id, build_node_id, actor_role)
            if active is None:
                raise LifecycleError(
                    "actor session recovery requires an active generation"
                )
            if active.generation != expected_generation:
                if active.generation == generation:
                    if (
                        active.pane_id == pane_id
                        and active.tab_id == tab_id
                        and active.session_path == session_path
                        and active.correlation_token == correlation_token
                    ):
                        self.conn.execute("COMMIT")
                        return st.ActorSessionRecovery(session=active, recovered=False)
                    raise LifecycleError(
                        "actor session recovery replay disagrees with durable identity"
                    )
                self.conn.execute("COMMIT")
                return st.ActorSessionRecovery(session=active, recovered=False)
            values = (pane_id, session_path, correlation_token)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise LifecycleError(
                    "replacement pane id, session path, and correlation token are required"
                )
            now = now_iso()
            self.conn.execute(
                "UPDATE actor_sessions SET state=?, updated_at=?"
                " WHERE run_id=? AND build_node_id=? AND actor_role=?"
                " AND generation=? AND state=?",
                (
                    st.ActorSessionState.CLOSED.value,
                    now,
                    run_id,
                    build_node_id,
                    actor_role,
                    expected_generation,
                    st.ActorSessionState.ACTIVE.value,
                ),
            )
            self.conn.execute(
                "INSERT INTO actor_sessions"
                " (run_id, build_node_id, actor_role, generation, state, pane_id,"
                "  tab_id, session_path, correlation_token, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    build_node_id,
                    actor_role,
                    generation,
                    st.ActorSessionState.ACTIVE.value,
                    pane_id,
                    tab_id,
                    session_path,
                    correlation_token,
                    now,
                ),
            )
            session = st.ActorSession(
                run_id=run_id,
                build_node_id=build_node_id,
                actor_role=actor_role,
                generation=generation,
                state=st.ActorSessionState.ACTIVE,
                pane_id=pane_id,
                tab_id=tab_id,
                session_path=session_path,
                correlation_token=correlation_token,
                updated_at=now,
            )
            self.conn.execute("COMMIT")
            return st.ActorSessionRecovery(session=session, recovered=True)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def close_actor_session(
        self, run_id: str, build_node_id: str, actor_role: str, *, generation: int
    ) -> bool:
        """Close only the active generation a caller can name exactly."""
        generation = _require_generation(generation, field_name="generation")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            session = self._actor_session(run_id, build_node_id, actor_role, generation)
            if session is None:
                self.conn.execute("COMMIT")
                return False
            if session.state is st.ActorSessionState.CLOSED:
                self.conn.execute("COMMIT")
                return True
            changed = self.conn.execute(
                "UPDATE actor_sessions SET state=?, updated_at=?"
                " WHERE run_id=? AND build_node_id=? AND actor_role=?"
                " AND generation=? AND state=?",
                (
                    st.ActorSessionState.CLOSED.value,
                    now_iso(),
                    run_id,
                    build_node_id,
                    actor_role,
                    generation,
                    st.ActorSessionState.ACTIVE.value,
                ),
            ).rowcount
            self.conn.execute("COMMIT")
            return changed == 1
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def record_baseline(
        self,
        run_id: str,
        node_id: str,
        attempt_no: int,
        baseline: Mapping[str, Sequence[str]],
        ignored_at_base: Optional[Mapping[str, str]] = None,
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
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise UnknownNode(
                f"{run_id}/{node_id}#{attempt_no}: no attempt row to record a baseline on"
            )
        encoded = encode_baseline(baseline)
        digest = baseline_digest(encoded)
        existing = self.conn.execute(
            "SELECT digest FROM attempt_baselines"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if existing is not None and existing[0] != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} already recorded baseline "
                f"{existing[0]}; a second, different baseline would rewrite "
                "what the attempt started from"
            )
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
                (
                    run_id,
                    node_id,
                    attempt_no,
                    digest,
                    json.dumps(encoded, sort_keys=True, separators=(",", ":")),
                    None
                    if ignored_at_base is None
                    else json.dumps(
                        dict(ignored_at_base), sort_keys=True, separators=(",", ":")
                    ),
                    now_iso(),
                ),
            )
            self.conn.execute(
                "UPDATE attempts SET extra_json=?"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (json.dumps(payload, sort_keys=True), run_id, node_id, attempt_no),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return digest

    @serialized
    def attempt_baseline(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> Dict[str, Tuple[str, str]]:
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
            (run_id, node_id, attempt_no),
        ).fetchone()
        attempt_row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if attempt_row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        extra = json.loads(attempt_row[0] or "{}")
        stamped = (
            extra.get(ATTEMPT_BASELINE_DIGEST_KEY) if isinstance(extra, dict) else None
        )
        if row is None:
            raise BaselineUnrecorded(
                f"{run_id}/{node_id}#{attempt_no} recorded no measurement baseline"
            )
        digest, inventory_json = row
        if stamped != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} carries baseline digest "
                f"{stamped!r} but the stored baseline digests to {digest!r}"
            )
        try:
            encoded = json.loads(inventory_json)
        except json.JSONDecodeError as exc:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline is not JSON: {exc}"
            ) from exc
        if not isinstance(encoded, dict):
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline is not an object"
            )
        if baseline_digest(encoded) != digest:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} baseline does not match its "
                f"recorded digest {digest}"
            )
        return decode_baseline(encoded)

    @serialized
    def attempt_ignored_at_base(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> Optional[Dict[str, str]]:
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
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise BaselineUnrecorded(
                f"{run_id}/{node_id}#{attempt_no} recorded no measurement baseline"
            )
        if row[0] is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} ignored-at-base map is not "
                f"JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise BaselineCorrupt(
                f"{run_id}/{node_id}#{attempt_no} ignored-at-base map is not an object"
            )
        return {str(key): str(value) for key, value in payload.items()}

    @serialized
    def record_sealed_output(
        self, run_id: str, node_id: str, attempt_no: int, output_sha: str
    ) -> None:
        """Record the latest commit sealed by this attempt's private-index CAS.

        This write immediately follows ``commit_measured_delta``. Later gates
        may be replayed after a crash; the latest commit must not be recreated.
        A repair cycle may replace an earlier sealed candidate in the same
        retained attempt after that candidate has been durably rejected.
        """
        output_sha = _require_candidate_sha(
            output_sha, field_name=SEALED_OUTPUT_SHA_KEY
        )
        row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        try:
            payload = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload[SEALED_OUTPUT_SHA_KEY] = output_sha
        self.conn.execute(
            "UPDATE attempts SET extra_json=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (json.dumps(payload, sort_keys=True), run_id, node_id, attempt_no),
        )

    @serialized
    def attempt_sealed_output(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> Optional[str]:
        """Return the latest sealed commit marker, if one was recorded."""
        row = self.conn.execute(
            "SELECT extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no} has no attempt row")
        try:
            payload = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(SEALED_OUTPUT_SHA_KEY)
        if value is None:
            return None
        return _require_candidate_sha(value, field_name=SEALED_OUTPUT_SHA_KEY)

    @serialized
    def record_salvage(
        self, run_id: str, node_id: str, attempt_no: int, extra: Mapping[str, Any]
    ) -> None:
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
            (run_id, node_id, attempt_no),
        ).fetchone()
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
            (
                json.dumps(merged, sort_keys=True),
                st.NodeState.RUNNING.value,
                CLOSED_ATTEMPT_STATE.value,
                run_id,
                node_id,
                attempt_no,
            ),
        )

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
            "SELECT last_transition_at FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
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
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id)
            )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,NULL,'acceptance-start','scheduler','{}',?)",
                (run_id, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def record_budget_allowance(
        self,
        run_id: str,
        node_id: str,
        *,
        cumulative_semantic_attempts: int,
        effective_ceiling: int,
        run_ids: Sequence[str],
    ) -> None:
        """Record that an operator admitted a node whose cross-run fix-loop
        budget was already spent (§3.6 B10).

        B10 requires the escape from a refusal to exist; §1.2 requires the
        escape to leave a stored record rather than a memory of a flag
        somebody typed. Without this row the whole of "the operator allowed
        it" lives in the argv of a process that has exited, and the run's
        ledger records a node that quietly had more attempts than the ceiling
        allows with nothing saying why.

        A transition rather than a column, and the same division §11.3 settled
        for `retry --grant`: the column carries what a guard reads, the
        transition carries what an operator reads. The guard here reads the
        prior runs' attempt rows, so there is nothing to store for it; the
        magnitude it was overridden by is exactly what an operator later needs.

        Written before the run row exists, because the refusal it escapes is
        decided before anything else in the run is. The `transitions` table
        carries no foreign key for that reason among others, and the row is
        keyed on the run id the invocation is about to use, so it joins the
        rest of that run's audit trail the moment the projection lands.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state,"
                " to_state, reason, actor, detail_json, created_at)"
                " VALUES (?,?,'node',NULL,NULL,?,'operator',?,?)",
                (
                    run_id,
                    node_id,
                    NODE_BUDGET_ALLOWANCE_REASON,
                    json.dumps(
                        {
                            "cumulative_semantic_attempts": cumulative_semantic_attempts,
                            "effective_ceiling": effective_ceiling,
                            "run_ids": list(run_ids),
                        },
                        sort_keys=True,
                    ),
                    now_iso(),
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def attempts_for(
        self, run_id: str, node_id: Optional[str] = None
    ) -> Tuple[st.AttemptRecord, ...]:
        """Attempt rows, for the counting the retry policy does over them.

        The semantic ceiling is a `COUNT(*)` over rows that already exist
        rather than a counter this store maintains, so what the policy needs
        from here is the rows themselves and nothing more (§7.5).
        """
        sql = (
            "SELECT run_id, node_id, attempt_no, base_sha, state, started_at,"
            " launched_at, pid, turn_count, retry_class, extra_json,"
            " attempt_host, attempt_start_epoch"
            " FROM attempts WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if node_id is not None:
            sql += " AND node_id=?"
            params = (run_id, node_id)
        return tuple(
            st.AttemptRecord(
                run_id=r[0],
                node_id=r[1],
                attempt_no=r[2],
                base_sha=r[3],
                state=st.NodeState(r[4]),
                started_at=r[5] or 0.0,
                launched_at=r[6],
                pid=r[7],
                turn_count=r[8] or 0,
                retry_class=st.RetryClass(r[9]) if r[9] else None,
                extra=json.loads(r[10]),
                attempt_host=r[11],
                attempt_start_epoch=r[12],
            )
            for r in self.conn.execute(sql + " ORDER BY attempt_no", params).fetchall()
        )

    @serialized
    def mark_launched(
        self,
        run_id: str,
        node_id: str,
        attempt_no: int,
        pid: Optional[int],
        launched_at: Optional[float] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
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
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None:
            raise UnknownNode(
                f"{run_id}/{node_id}#{attempt_no}: no attempt row to mark launched"
            )
        payload = json.loads(row[0] or "{}")
        if extra:
            payload.update(extra)
        attempt_host = None
        attempt_start_epoch = None
        if pid is not None:
            labelled = scheduler_host()
            attempt_host = labelled or None
            attempt_start_epoch = wd.process_start_epoch(int(pid))
        self.conn.execute(
            "UPDATE attempts SET launched_at=?, pid=?, extra_json=?,"
            " attempt_host=?, attempt_start_epoch=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (
                launched_at if launched_at is not None else time.time(),
                pid,
                json.dumps(payload),
                attempt_host,
                attempt_start_epoch,
                run_id,
                node_id,
                attempt_no,
            ),
        )

    @serialized
    def record_heartbeat(
        self, attempt: st.AttemptRecord, turn_count: int, observed_at: float
    ) -> None:
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
            (
                turn_count,
                attempt.launched_at,
                attempt.run_id,
                attempt.node_id,
                attempt.attempt_no,
            ),
        )

    @serialized
    def record_plan_version(
        self, run_id: str, plan_digest: str, plan_bytes: bytes
    ) -> int:
        """Append the plan bytes this run is executing under. Returns its seq.

        Idempotent on the digest: re-recording the version already at the head
        returns its existing seq rather than appending a duplicate, so a resume
        that re-asserts what it is already running is free. A digest the run
        adopted *earlier* and has since amended away is refused, because
        re-adopting it would make the lineage a set rather than a history and
        "which nodes merged under which plan" would stop being answerable.
        """
        _require_candidate_sha(plan_digest, field_name="plan_digest")
        if not isinstance(plan_bytes, (bytes, bytearray)) or not plan_bytes:
            raise LifecycleError(
                "a plan version needs its bytes; {0} supplied none".format(run_id)
            )
        rows = self.conn.execute(
            "SELECT seq, plan_digest FROM run_plan_versions"
            " WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        if rows and str(rows[-1][1]) == plan_digest:
            return int(rows[-1][0])
        for seq, digest in rows:
            if str(digest) == plan_digest:
                raise LifecycleError(
                    "{0} already executed under {1} at version {2} and has "
                    "amended past it; the lineage is a history, not a set"
                    .format(run_id, plan_digest[:12], seq)
                )
        seq = (int(rows[-1][0]) + 1) if rows else 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO run_plan_versions"
                " (run_id, seq, plan_digest, plan_bytes, adopted_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, seq, plan_digest, bytes(plan_bytes), now_iso()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return seq

    @serialized
    def plan_versions(self, run_id: str) -> Tuple[Tuple[int, str, str], ...]:
        """This run's plan lineage as `(seq, digest, adopted_at)`, in order."""
        return tuple(
            (int(row[0]), str(row[1]), str(row[2]))
            for row in self.conn.execute(
                "SELECT seq, plan_digest, adopted_at FROM run_plan_versions"
                " WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        )

    @serialized
    def current_plan(self, run_id: str) -> Optional[Tuple[str, bytes]]:
        """The digest and bytes this run is executing under now, or None.

        `None` is a run created before plan bytes were retained. It is not an
        error and must not be treated as one: such a run resolves its plan the
        old way, by matching installed files, and keeps whatever behaviour it
        had. Inventing bytes for it would be the defaulting failure §19.2 names.
        """
        row = self.conn.execute(
            "SELECT plan_digest, plan_bytes FROM run_plan_versions"
            " WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else (str(row[0]), bytes(row[1]))

    #: States whose `dag_nodes` row an amendment may never rewrite. MERGED and
    #: ACCEPTED carry evidence measured against the spec in that row; RUNNING
    #: has a live attempt launched against it.
    _AMENDMENT_FROZEN_STATES = ("MERGED", "ACCEPTED", "RUNNING")

    @serialized
    def amend_run_plan(
        self,
        run_id: str,
        plan_digest: str,
        plan_bytes: bytes,
        updates: Mapping[str, st.PlanNode],
        additions: Sequence[st.PlanNode] = (),
        transfers: Sequence[Tuple[str, str, str]] = (),
    ) -> int:
        """Adopt an amended plan, rewriting only nodes that may be rewritten.

        **`needs_json` is not in this statement, for an existing node, at all.**
        That is the whole safety argument and it is structural rather than
        conventional: §19 M42 is a projection that rewired `needs_json` on a
        run in flight, reopening dependency decisions for nodes already
        terminal, and the only existing writer of that column is
        `ensure_derived_review_node`, which is refused outright on a legacy pin.
        An amendment cannot reach the column because no amendment statement
        names it. `plan_amendment.classify` refuses a `needs` change before
        reaching here; this makes that refusal unnecessary rather than trusted.

        The state guard is in the `WHERE` clause rather than in a Python check
        above it, so a caller that skipped the classifier still cannot land a
        write on a settled row — the row simply does not match. A statement that
        matches nothing is then a refusal rather than a silent no-op, which is
        what the rowcount assertion buys.

        A new node is an INSERT and may carry `needs`, because nothing was
        admitted in its absence.
        """
        _require_candidate_sha(plan_digest, field_name="plan_digest")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for node_id, node in sorted(updates.items()):
                changed = self.conn.execute(
                    "UPDATE dag_nodes SET outputs_json=?, specs_json=?, kind=?"
                    " WHERE run_id=? AND node_id=? AND EXISTS ("
                    "  SELECT 1 FROM node_lifecycle nl"
                    "  WHERE nl.run_id = dag_nodes.run_id"
                    "    AND nl.node_id = dag_nodes.node_id"
                    "    AND nl.state NOT IN (?,?,?))",
                    (
                        json.dumps(list(node.outputs)),
                        json.dumps(list(node.specs)),
                        node.kind.value,
                        run_id,
                        node_id,
                        *self._AMENDMENT_FROZEN_STATES,
                    ),
                ).rowcount
                if changed != 1:
                    raise IllegalTransition(
                        "{0}/{1}: an amendment may not rewrite this node — it "
                        "is absent, or its state is one of {2} and its spec is "
                        "the terms its evidence was measured against".format(
                            run_id, node_id, self._AMENDMENT_FROZEN_STATES
                        )
                    )
            # A settled node releasing a path it has finished writing. This is
            # the *only* statement in the system that touches a settled row,
            # and it is written so it can do nothing else: it sets one column,
            # it removes exactly one value from that column's JSON array, and
            # it matches only an agent-kind row in a settled state.
            #
            # Why a settled row may be touched at all: a node's `outputs` do
            # two jobs — write permission during the attempt, and an ownership
            # claim in the plan. For a MERGED node the first is spent (the
            # delta was measured against the outputs as they stood, and the
            # receipt records that measurement) and the second is a property of
            # the graph. Releasing a finished path therefore re-judges nothing,
            # and `run_plan_versions` keeps the version it merged under so the
            # evidence stays readable against its own terms.
            #
            # Why `kind='agent'` is in the WHERE clause rather than checked
            # above it: a merged *tests* node's outputs are still read after
            # the merge — `compare_test_bytes` pairs every later build lane
            # against `tuple(tests_node.outputs)`, and `_append_needed_tests`
            # reads them for a dependant's prompt — so for that kind they are
            # live state rather than a spent permission. The statement cannot
            # express the unsound case.
            for path, donor, _recipient in transfers:
                released = self.conn.execute(
                    "UPDATE dag_nodes SET outputs_json = ("
                    "  SELECT json_group_array(value) FROM json_each("
                    "    dag_nodes.outputs_json) WHERE value <> ?)"
                    " WHERE run_id=? AND node_id=? AND kind=?"
                    "   AND EXISTS ("
                    "     SELECT 1 FROM node_lifecycle nl"
                    "     WHERE nl.run_id = dag_nodes.run_id"
                    "       AND nl.node_id = dag_nodes.node_id"
                    "       AND nl.state IN (?,?))",
                    (
                        path,
                        run_id,
                        donor,
                        st.NodeKind.AGENT.value,
                        st.NodeState.MERGED.value,
                        st.NodeState.ACCEPTED.value,
                    ),
                ).rowcount
                if released != 1:
                    raise IllegalTransition(
                        "{0}/{1}: cannot release {2} — the node is absent, is "
                        "not an agent node, or is not settled. A merged tests "
                        "node's outputs are still read after the merge and are "
                        "not a spent permission".format(run_id, donor, path)
                    )
            for node in additions:
                self.conn.execute(
                    "INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind,"
                    " depth, needs_json, outputs_json, specs_json)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        node.node_id,
                        plan_digest,
                        node.kind.value,
                        node.depth,
                        json.dumps(list(node.needs)),
                        json.dumps(list(node.outputs)),
                        json.dumps(list(node.specs)),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO node_lifecycle (run_id, node_id, state,"
                    " attempt_no, updated_at) VALUES (?,?,?,0,?)",
                    (run_id, node.node_id, st.NodeState.PENDING.value, now_iso()),
                )
            rows = self.conn.execute(
                "SELECT seq, plan_digest FROM run_plan_versions"
                " WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
            if rows and str(rows[-1][1]) == plan_digest:
                self.conn.commit()
                return int(rows[-1][0])
            seq = (int(rows[-1][0]) + 1) if rows else 1
            self.conn.execute(
                "INSERT INTO run_plan_versions"
                " (run_id, seq, plan_digest, plan_bytes, adopted_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, seq, plan_digest, bytes(plan_bytes), now_iso()),
            )
            self.conn.execute(
                "UPDATE runs SET plan_digest=?, last_transition_at=?"
                " WHERE run_id=?",
                (plan_digest, now_iso(), run_id),
            )
            self.conn.execute(
                "INSERT INTO transitions"
                " (run_id, node_id, kind, from_state, to_state, reason, actor,"
                " detail_json, created_at) VALUES (?,?,'run',?,?,?,?,?,?)",
                (
                    run_id,
                    None,
                    None,
                    None,
                    "plan-amended",
                    "operator",
                    json.dumps(
                        {
                            "plan_digest": plan_digest,
                            "seq": seq,
                            "updated": sorted(updates),
                            "added": sorted(n.node_id for n in additions),
                            "transfers": [list(t) for t in transfers],
                        },
                        sort_keys=True,
                    ),
                    now_iso(),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return seq

    def _review_refresh_count(self, run_id: str) -> int:
        """Resumes that have reopened a review-budget block in this run.

        Unserialized on purpose: `resume_run` calls it from inside its own
        transaction, and taking the lock again there would deadlock. The public
        `review_refresh_count` is the one callers outside a transaction use.

        NULL is zero, for the reason every other added column reads that way: a
        ledger written before the column recorded no refreshes, which is the
        same thing a fresh run has, and inventing any other number here would
        retroactively spend an allowance nobody used.
        """
        row = self.conn.execute(
            "SELECT review_refresh_count FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise LifecycleError("no run row for {0}".format(run_id))
        return int(row[0] or 0)

    @serialized
    def review_refresh_count(self, run_id: str) -> int:
        """How much of this run's §3.6 A9 review allowance has been spent."""
        return self._review_refresh_count(run_id)

    @serialized
    def retry_spend_floor(self, run_id: str, node_id: str) -> int:
        """The attempt number this node's retry budgets are counted from.

        0 when no boundary has been crossed, and 0 for a ledger written before
        the column: NULL says nobody recorded a boundary, never that one was
        recorded at zero, and both read the same because counting from the
        beginning of the node's life is what the absence of a boundary means.
        """
        row = self.conn.execute(
            "SELECT retry_spend_floor FROM node_lifecycle WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
        return int(row[0] or 0)

    def attempts_spent(
        self, run_id: str, node_id: str, retry_class: st.RetryClass
    ) -> int:
        """How many attempts of this node failed in this class *since its last
        boundary* (§7.5, §11.3).

        Counts only rows already classified, which is what keeps §7.5's rule
        that no infra fault produces a budget decrement true of the other
        direction as well: an unclassified row contributes to nothing.

        Counts only rows above `retry_spend_floor`, which is the other half.
        Without it this number was cumulative over the node's whole life and
        nothing debited, expired, or reset it, so spend on a defect that has
        since been fixed was charged forever: three `LAUNCHER_TRANSIENT`
        attempts burned in ten seconds relaunching against a workspace herdr
        had already destroyed (#79) left the node permanently over a budget of
        two, and the next launcher failure of any kind — including one that
        was genuinely transient — blocked it on first contact with zero
        tolerance, on a debt no amount of success could pay off (#92).

        The floor moves only at a typed boundary written by an operator: a
        `run resume`, or a `retry` naming this node. It never moves because an
        attempt succeeded, because time passed, or because anything an agent
        said, so §1.2 holds — the budget a node is charged against is a
        function of the ledger's transition rows, not of any process's opinion
        about whether its earlier failures still count.
        """
        floor = self.retry_spend_floor(run_id, node_id)
        return sum(
            1
            for a in self.attempts_for(run_id, node_id)
            if a.retry_class is retry_class and a.attempt_no > floor
        )

    def close(self) -> None:
        """Close the shared connection. One connection outlives every thread
        that touched it, so closing is the caller's explicit act rather than
        a per-thread teardown."""
        with self._lock:
            self.conn.close()

    @serialized
    def latest_outcome(self, run_id: str) -> Optional[st.RunOutcome]:
        row = self.conn.execute(
            "SELECT latest_outcome FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return st.RunOutcome(row[0])

    @serialized
    def node_records(self, run_id: str) -> Tuple[wt.NodeRecord, ...]:
        rows = self.conn.execute(
            "SELECT d.node_id, d.depth, d.needs_json, l.state, d.specs_json"
            " FROM dag_nodes d JOIN node_lifecycle l"
            " ON l.run_id = d.run_id AND l.node_id = d.node_id"
            " WHERE d.run_id=?",
            (run_id,),
        ).fetchall()
        return tuple(
            wt.NodeRecord(
                node_id=node_id,
                depth=depth,
                needs=tuple(json.loads(needs)),
                state=state,
                specs=tuple(json.loads(specs)),
            )
            for node_id, depth, needs, state, specs in rows
        )

    def ready_nodes(self, run_id: str) -> Tuple[str, ...]:
        """Pending nodes whose deps are all MERGED, sorted `(depth, node_id)` (§7.1).
        The predicate is MERGED, never VERIFIED/SUCCEEDED."""
        records = self.node_records(run_id)
        merged = {r.node_id for r in records if r.state == st.NodeState.MERGED.value}
        pending = [
            r
            for r in records
            if r.state == st.NodeState.PENDING.value
            and all(dep in merged for dep in r.needs)
        ]
        return tuple(
            r.node_id for r in sorted(pending, key=lambda r: (r.depth, r.node_id))
        )

    def upstream_blocked(self, run_id: str) -> Tuple[str, ...]:
        """The derived `UPSTREAM_BLOCKED` predicate (§8.7) — never stored, so a
        rescue needs no un-cascade rule. Delegates to worktree.upstream_blocked,
        which already owns this computation."""
        return wt.upstream_blocked(self.node_records(run_id))

    # ── the audit tier (§5.3, §7.7, §7.8, §10.5, §10.6) ─────────────────────

    def _audit_query(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[Dict[str, Any], ...]:
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
    def audit_transitions(
        self, run_id: str, node_id: Optional[str] = None
    ) -> Tuple[Dict[str, Any], ...]:
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
                (
                    run_id,
                    result.node_id,
                    result.attempt_no,
                    result.subject_sha,
                    payload,
                    result.adjudication.value if result.adjudication else None,
                    now_iso(),
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def audit_results(
        self, run_id: str, node_id: Optional[str] = None
    ) -> Tuple[Dict[str, Any], ...]:
        """Result rows with their payloads, oldest first (§7.7)."""
        sql = "SELECT * FROM results WHERE run_id=?"
        params: Tuple[Any, ...] = (run_id,)
        if node_id is not None:
            sql += " AND node_id=?"
            params = (run_id, node_id)
        return self._audit_query(sql + " ORDER BY id", params)

    @serialized
    def result_adjudication(
        self, run_id: str, node_id: str, attempt_no: int
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
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return st.Adjudication(row[0])

    @serialized
    def accepted_result_payload(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> Optional[Mapping[str, Any]]:
        """The latest accepted envelope for one exact attempt.

        The adjudication enum is checked in the same query before payload data
        is exposed.  Callers may reconstruct lost envelope transport from this
        durable row; prose inside the payload never decides whether recovery
        is authorized.
        """
        row = self.conn.execute(
            "SELECT payload_json, adjudication FROM results"
            " WHERE run_id=? AND node_id=? AND attempt_no=?"
            " ORDER BY id DESC LIMIT 1",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if row is None or row[1] != st.Adjudication.ACCEPTED.value:
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, Mapping):
            raise LifecycleError(
                f"{run_id}/{node_id}#{attempt_no}: accepted result payload "
                "is not an object"
            )
        return dict(payload)

    @serialized
    def record_orphan(
        self,
        run_id: str,
        *,
        node_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
        pid: Optional[int] = None,
        handle: Optional[str] = None,
        reason: str = "",
    ) -> None:
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
                (run_id, node_id, attempt_no, pid, handle, reason or None, now_iso()),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def audit_orphans(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        """Every unreachable pane recorded for this run, oldest first (§7.8).
        `run status` reports these so an operator can kill them by hand."""
        return self._audit_query(
            "SELECT * FROM orphans WHERE run_id=? ORDER BY id", (run_id,)
        )

    # ── the generic guarded transition (§7.9) ───────────────────────────────

    @serialized
    def _transition_node(
        self,
        run_id: str,
        node_id: str,
        to_state: st.NodeState,
        *,
        actor: str,
        reason: str,
        block_reason: Optional[st.BlockReason] = None,
        output_sha: Optional[str] = None,
        new_attempt: bool = False,
        granted_extra_delta: int = 0,
        require_state: Optional[Tuple[st.NodeState, ...]] = None,
        detail: Optional[Mapping[str, Any]] = None,
        cancel_cause: Optional[st.CancelCause] = None,
        merge_cause: Optional[st.MergeCause] = None,
        pending_cause: Optional[st.PendingCause] = None,
        extra_writes: Optional[
            Callable[[st.NodeLifecycle], Sequence[Tuple[str, Tuple]]]
        ] = None,
    ) -> st.NodeLifecycle:
        """Guard, then write the lifecycle row, the audit row, and the run's
        `last_transition_at`, all inside one `BEGIN IMMEDIATE` (§7.9). Any
        caller-supplied `extra_writes` (an attempt-row update, say) runs inside
        the same transaction, so the write set stays atomic."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, attempt_no, block_reason, output_sha,"
                " granted_extra_attempts, cancel_cause, lane_phase"
                " FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            if row is None:
                raise UnknownNode(f"{run_id}/{node_id} has no lifecycle row")
            current = st.NodeState(row[0])
            current_cause = st.CancelCause(row[5]) if row[5] else None
            _guard_transition(
                current, to_state, actor=actor, cancel_cause=current_cause
            )
            if require_state is not None and current not in require_state:
                raise IllegalTransition(
                    f"{node_id}: expected state in "
                    f"{tuple(s.value for s in require_state)}, found {current.value}"
                )

            if new_attempt:
                durable_max = self.conn.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) FROM attempts"
                    " WHERE run_id=? AND node_id=?",
                    (run_id, node_id),
                ).fetchone()[0]
                new_attempt_no = max(int(row[1]), int(durable_max)) + 1
            else:
                new_attempt_no = row[1]
            new_block_reason = (
                block_reason if to_state is st.NodeState.BLOCKED else None
            )
            new_output_sha = output_sha if output_sha is not None else row[3]
            new_granted = row[4] + granted_extra_delta

            # The vocabulary type itself validates the (state, block_reason)
            # pairing (§7.3) — reused rather than re-checked here.
            lifecycle = st.NodeLifecycle(
                node_id=node_id,
                state=to_state,
                attempt_no=new_attempt_no,
                block_reason=new_block_reason,
                output_sha=new_output_sha,
                granted_extra_attempts=new_granted,
                lane_phase=st.LanePhase(row[6]) if row[6] else None,
                pending_cause=(
                    pending_cause if to_state is st.NodeState.PENDING else None
                ),
            )

            # Scoped to CANCELLED exactly as `block_reason` is scoped to
            # BLOCKED: any transition out of CANCELLED clears the cause with
            # the state that made it meaningful, so no row can carry a cause
            # for a cancellation it is no longer under.
            new_cancel_cause = (
                cancel_cause if to_state is st.NodeState.CANCELLED else None
            )
            # Scoped to MERGED exactly as the line above is scoped to
            # CANCELLED. Nothing transitions out of MERGED (§7.3), so the
            # clearing arm is unreachable by construction rather than by
            # care — it is written anyway so that the scoping rule is one
            # rule stated twice rather than a rule and an assumption, and so
            # that a later narrowing of absolute terminality cannot leave a
            # node carrying a merge cause for a merge it is no longer under.
            new_merge_cause = merge_cause if to_state is st.NodeState.MERGED else None
            # Scoped to PENDING exactly as the two lines above are scoped to
            # CANCELLED and MERGED. A seeded PENDING never left the frontier
            # and keeps NULL; a transition *to* PENDING stamps who wrote it;
            # a transition *out* of PENDING clears the cause with the state
            # that made it meaningful (#103).
            new_pending_cause = (
                pending_cause if to_state is st.NodeState.PENDING else None
            )
            now = now_iso()
            self.conn.execute(
                "UPDATE node_lifecycle SET state=?, attempt_no=?, block_reason=?,"
                " output_sha=?, granted_extra_attempts=?, cancel_cause=?,"
                " merge_cause=?, pending_cause=?, updated_at=?"
                " WHERE run_id=? AND node_id=?",
                (
                    lifecycle.state.value,
                    lifecycle.attempt_no,
                    lifecycle.block_reason.value if lifecycle.block_reason else None,
                    lifecycle.output_sha,
                    lifecycle.granted_extra_attempts,
                    new_cancel_cause.value if new_cancel_cause else None,
                    new_merge_cause.value if new_merge_cause else None,
                    new_pending_cause.value if new_pending_cause else None,
                    now,
                    run_id,
                    node_id,
                ),
            )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at) VALUES (?,?,'node',?,?,?,?,?,?)",
                (
                    run_id,
                    node_id,
                    current.value,
                    to_state.value,
                    reason,
                    actor,
                    json.dumps(detail or {}),
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?", (now, run_id)
            )
            for sql, params in extra_writes(lifecycle) if extra_writes else ():
                self.conn.execute(sql, params)
            self.conn.execute("COMMIT")
            return lifecycle
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ── scheduler-driven transitions ────────────────────────────────────────

    def start_attempt(
        self,
        run_id: str,
        node_id: str,
        base_sha: str,
        attempt_extra: Optional[Mapping[str, Any]] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """PENDING -> RUNNING, opening a new attempt row (§7.6's attempt window).

        Attempt numbers are allocated above both the lifecycle pointer and the
        durable attempts ledger. Operator recovery can legitimately restore a
        node to an older retained attempt while leaving later cancelled rows
        as audit evidence. Reusing one of those primary keys would roll this
        transaction back without changing readiness, causing the scheduler to
        redispatch the same node forever. Taking the maximum inside the same
        ``BEGIN IMMEDIATE`` transaction preserves monotonic identity and makes
        that recovery shape schedulable.

        `attempt_extra` is written into the row's `extra_json` **in the same
        transaction that creates it**, and that is the point rather than a
        convenience. The only current writer is the repair basis, whose
        `integration_head` is what `AttemptRecord.integration_head` — and
        therefore `guidance_key`, and therefore the next repair decision —
        reads. A row that existed for even one read without it would answer
        `base_sha` for its integration head, which for a repair attempt is the
        rejected commit rather than the head, and every reader would be wrong
        about a fact the row is the only record of.

        `'{}'` when nothing is passed, byte-identical to what this wrote
        before, so a row opened by an ordinary attempt is unchanged.

        `detail` lands on the audit-tier `transitions` row this write already
        creates, and is where the repair decision's *reason* goes — including
        every reason a repair was refused, which by construction leaves no
        trace on the attempt row itself because a refused repair produces an
        ordinary fresh-base attempt indistinguishable from any other. Its
        reader is `transitions()`; §5.3 forbids reading the audit tier at
        runtime and nothing does, which is exactly what a diagnosis field
        should be.
        """
        payload = (
            json.dumps(dict(attempt_extra), sort_keys=True) if attempt_extra else "{}"
        )

        def extra(lifecycle: st.NodeLifecycle):
            return [
                (
                    "INSERT INTO attempts (run_id, node_id, attempt_no, base_sha, state,"
                    " started_at, turn_count, extra_json) VALUES (?,?,?,?,?,?,0,?)",
                    (
                        run_id,
                        node_id,
                        lifecycle.attempt_no,
                        base_sha,
                        st.NodeState.RUNNING.value,
                        time.time(),
                        payload,
                    ),
                )
            ]

        lifecycle = self._transition_node(
            run_id,
            node_id,
            st.NodeState.RUNNING,
            actor="scheduler",
            reason="attempt-start",
            new_attempt=True,
            require_state=(st.NodeState.PENDING,),
            detail=detail,
            extra_writes=extra,
        )
        return lifecycle.attempt_no

    def mark_verified(
        self, run_id: str, node_id: str, output_sha: str
    ) -> st.NodeLifecycle:
        """RUNNING -> VERIFIED only after the exact derived review has passed."""
        node_row = self.conn.execute(
            "SELECT kind FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if node_row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no dag row")
        if node_row[0] == st.NodeKind.REVIEW.value:
            raise LifecycleError(
                "a derived review reaches ACCEPTED only through mark_review_accepted"
            )
        review_row = self.conn.execute(
            "SELECT node_id FROM dag_nodes WHERE run_id=? AND kind=? AND review_of=?",
            (run_id, st.NodeKind.REVIEW.value, node_id),
        ).fetchone()
        if review_row is not None:
            output_sha = _require_candidate_sha(output_sha, field_name="output_sha")
            review_node_id = review_row[0]
            candidate = self._candidate(run_id, node_id, output_sha)
            review = self._review(run_id, review_node_id, output_sha)
            review_state = self.conn.execute(
                "SELECT state FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (run_id, review_node_id),
            ).fetchone()
            if (
                candidate is None
                or review is None
                or review.verdict is not st.ReviewVerdict.PASS
                or review_state is None
                or review_state[0] != st.NodeState.ACCEPTED.value
            ):
                raise LifecycleError(
                    f"{run_id}/{node_id}: VERIFIED requires ACCEPTED PASS for its "
                    "exact published candidate"
                )

        def extra(lifecycle: st.NodeLifecycle):
            return [
                (
                    "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        st.NodeState.VERIFIED.value,
                        run_id,
                        node_id,
                        lifecycle.attempt_no,
                    ),
                )
            ]

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.VERIFIED,
            actor="scheduler",
            reason="verified",
            output_sha=output_sha,
            require_state=(st.NodeState.RUNNING,),
            extra_writes=extra,
        )

    @serialized
    def mark_review_accepted(
        self, run_id: str, review_node_id: str, candidate_sha: str
    ) -> st.NodeLifecycle:
        """Terminally accept a derived review only after its exact PASS row."""
        candidate_sha = _require_candidate_sha(candidate_sha)
        row = self.conn.execute(
            "SELECT kind FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, review_node_id),
        ).fetchone()
        if row is None:
            raise UnknownNode(f"{run_id}/{review_node_id} has no dag row")
        if row[0] != st.NodeKind.REVIEW.value:
            raise LifecycleError("only a derived review node can reach ACCEPTED")
        review = self._review(run_id, review_node_id, candidate_sha)
        if review is None or review.verdict is not st.ReviewVerdict.PASS:
            raise LifecycleError(
                "a derived review may be accepted only for its exact PASS candidate"
            )
        return self._transition_node(
            run_id,
            review_node_id,
            st.NodeState.ACCEPTED,
            actor="scheduler",
            reason="review-accepted",
            require_state=(
                st.NodeState.PENDING,
                st.NodeState.RUNNING,
                st.NodeState.VERIFIED,
            ),
        )

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
        node_row = self.conn.execute(
            "SELECT kind FROM dag_nodes WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if node_row is None:
            raise UnknownNode(f"{run_id}/{node_id} has no dag row")
        if node_row[0] == st.NodeKind.REVIEW.value:
            raise LifecycleError(
                "a derived review is terminal at ACCEPTED and cannot be merged"
            )
        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.MERGED,
            actor="scheduler",
            reason="merged",
            merge_cause=st.MergeCause.SCHEDULER,
            require_state=(st.NodeState.VERIFIED,),
        )

    def mark_blocked(
        self,
        run_id: str,
        node_id: str,
        reason: st.BlockReason,
        *,
        detail: Optional[Mapping[str, Any]] = None,
        retry_class: Optional[st.RetryClass] = None,
        attempt_extra: Optional[Mapping[str, Any]] = None,
    ) -> st.NodeLifecycle:
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
            writes = [
                (
                    "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (st.NodeState.BLOCKED.value, run_id, node_id, lifecycle.attempt_no),
                )
            ]
            if retry_class is not None:
                writes.append(
                    (
                        "UPDATE attempts SET retry_class=? WHERE run_id=? AND node_id=?"
                        " AND attempt_no=?",
                        (retry_class.value, run_id, node_id, lifecycle.attempt_no),
                    )
                )
            if attempt_extra:
                # The blocking attempt's marker matters as much as a retrying
                # one's: without it the budget-exhausting attempt leaves no
                # stored evidence, and after `retry --force` the count restarts
                # one short — which turns a granted single attempt into an
                # unbounded loop for as long as the operator keeps forcing.
                row = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, lifecycle.attempt_no),
                ).fetchone()
                try:
                    merged = dict(json.loads(row[0]) if row and row[0] else {})
                except (TypeError, ValueError):
                    merged = {}
                merged.update(dict(attempt_extra))
                writes.append(
                    (
                        "UPDATE attempts SET extra_json=?"
                        " WHERE run_id=? AND node_id=? AND attempt_no=?",
                        (
                            json.dumps(merged, sort_keys=True),
                            run_id,
                            node_id,
                            lifecycle.attempt_no,
                        ),
                    )
                )
            return writes

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.BLOCKED,
            actor="scheduler",
            reason=f"blocked:{reason.value}",
            block_reason=reason,
            require_state=(st.NodeState.RUNNING, st.NodeState.VERIFIED),
            detail=detail,
            extra_writes=extra,
        )

    def fail_attempt(
        self,
        run_id: str,
        node_id: str,
        retry_class: st.RetryClass,
        detail: Optional[Mapping[str, Any]] = None,
        attempt_extra: Optional[Mapping[str, Any]] = None,
    ) -> st.NodeLifecycle:
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
            writes = [
                (
                    "UPDATE attempts SET retry_class=?, state=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        retry_class.value,
                        CLOSED_ATTEMPT_STATE.value,
                        run_id,
                        node_id,
                        lifecycle.attempt_no,
                    ),
                )
            ]
            if attempt_extra:
                row = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, lifecycle.attempt_no),
                ).fetchone()
                try:
                    merged = dict(json.loads(row[0]) if row and row[0] else {})
                except (TypeError, ValueError):
                    merged = {}
                merged.update(dict(attempt_extra))
                writes.append(
                    (
                        "UPDATE attempts SET extra_json=?"
                        " WHERE run_id=? AND node_id=? AND attempt_no=?",
                        (
                            json.dumps(merged, sort_keys=True),
                            run_id,
                            node_id,
                            lifecycle.attempt_no,
                        ),
                    )
                )
            return writes

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="scheduler",
            reason=f"retry:{retry_class.value}",
            require_state=(st.NodeState.RUNNING,),
            pending_cause=st.PendingCause.SCHEDULER,
            detail=detail,
            extra_writes=extra,
        )

    # ── run-level: cancellation, outcome, resume ────────────────────────────

    @serialized
    def adoptable_attempts(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        """Completed, unmerged work a discard would lose (§7.3).

        A VERIFIED node is adoptable. An accepted result is adoptable unless
        the lane's latest immutable candidate has a terminal REJECTED review.
        Candidate review rows, never attempt extras, supply that exclusion.
        """
        nodes = self.conn.execute(
            "SELECT node_id, state, attempt_no FROM node_lifecycle WHERE run_id=?",
            (run_id,),
        ).fetchall()
        found: List[Dict[str, Any]] = []
        for node_id, state, attempt_no in nodes:
            current = st.NodeState(state)
            if current in st.ABSOLUTELY_TERMINAL:
                continue
            if current is st.NodeState.VERIFIED:
                found.append(
                    {
                        "node_id": node_id,
                        "state": current.value,
                        "attempt_no": attempt_no,
                        "why": "verified",
                    }
                )
                continue
            row = self.conn.execute(
                "SELECT adjudication FROM results"
                " WHERE run_id=? AND node_id=? AND attempt_no=?"
                " ORDER BY id DESC LIMIT 1",
                (run_id, node_id, attempt_no),
            ).fetchone()
            if not row or row[0] != st.Adjudication.ACCEPTED.value:
                continue
            latest_review = self.conn.execute(
                "SELECT cr.verdict FROM lane_candidates lc"
                " LEFT JOIN candidate_reviews cr"
                " ON cr.run_id=lc.run_id"
                " AND cr.review_node_id=? AND cr.candidate_sha=lc.candidate_sha"
                " WHERE lc.run_id=? AND lc.build_node_id=?"
                " ORDER BY lc.candidate_seq DESC LIMIT 1",
                (f"{node_id}::review", run_id, node_id),
            ).fetchone()
            if latest_review and latest_review[0] == st.ReviewVerdict.REJECTED.value:
                continue
            found.append(
                {
                    "node_id": node_id,
                    "state": current.value,
                    "attempt_no": attempt_no,
                    "why": "accepted-unmerged",
                }
            )
        return tuple(found)

    @serialized
    def cancel_run(
        self, run_id: str, *, cause: st.CancelCause = st.CancelCause.RUN_CANCEL
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
                "SELECT node_id, state FROM node_lifecycle WHERE run_id=?", (run_id,)
            ).fetchall()
            now = now_iso()
            cancelled = []
            for node_id, state in rows:
                current = st.NodeState(state)
                if current in st.ABSOLUTELY_TERMINAL:
                    continue
                self.conn.execute(
                    "UPDATE node_lifecycle SET state=?, block_reason=NULL,"
                    " cancel_cause=?, lane_phase=CASE WHEN lane_phase IS NULL"
                    " THEN NULL ELSE ? END, updated_at=?"
                    " WHERE run_id=? AND node_id=?",
                    (
                        st.NodeState.CANCELLED.value,
                        cause.value,
                        st.LanePhase.CANCELLED.value,
                        now,
                        run_id,
                        node_id,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                    " reason, actor, detail_json, created_at)"
                    " VALUES (?,?,'node',?,?, 'run-cancel','operator','{}',?)",
                    (run_id, node_id, current.value, st.NodeState.CANCELLED.value, now),
                )
                # §7.8's "its result is rejected because its attempt is no
                # longer running" is only true if cancellation writes it. The
                # scheduler never blocks on the kill, so the surviving pane may
                # still report — and §7.7 reads this column to refuse it.
                self.conn.execute(
                    "UPDATE attempts SET state=? WHERE run_id=? AND node_id=? AND state=?",
                    (
                        CLOSED_ATTEMPT_STATE.value,
                        run_id,
                        node_id,
                        st.NodeState.RUNNING.value,
                    ),
                )
                cancelled.append(node_id)
            self.conn.execute(
                "UPDATE runs SET last_transition_at=?, cancel_requested=1 WHERE run_id=?",
                (now, run_id),
            )
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
    def declare_outcome(
        self,
        run_id: str,
        *,
        stuck: bool = False,
        acceptance_result: Optional[bool] = None,
        cancel_cause: Optional[st.CancelCause] = None,
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
                "SELECT cancel_requested FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise UnknownNode(f"run {run_id} does not exist")
            rows = self.conn.execute(
                "SELECT node_id, state, block_reason FROM node_lifecycle WHERE run_id=?",
                (run_id,),
            ).fetchall()
            node_states = [
                (
                    node_id,
                    st.NodeState(state),
                    st.BlockReason(reason) if reason else None,
                )
                for node_id, state, reason in rows
            ]
            report = total_run_outcome(
                node_states,
                stuck=stuck,
                cancel_requested=bool(run_row[0]),
                acceptance_result=acceptance_result,
                requested_cause=cancel_cause,
            )
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
                (
                    report.outcome.value,
                    now,
                    now,
                    report.cancel_cause.value if report.cancel_cause else None,
                    run_id,
                ),
            )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,?, 'declare-outcome','scheduler',?,?)",
                (
                    run_id,
                    report.outcome.value,
                    json.dumps(
                        {
                            "blocked_nodes": list(report.blocked_nodes),
                            "abandoned_nodes": list(report.abandoned_nodes),
                            "acceptance_result": report.acceptance_result,
                            "cancel_cause": (
                                report.cancel_cause.value
                                if report.cancel_cause
                                else None
                            ),
                        }
                    ),
                    now,
                ),
            )
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
                (
                    os.getpid(),
                    scheduler_host(),
                    now,
                    wd.process_start_epoch(os.getpid()),
                    run_id,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def _write_resume_transition(
        self,
        run_id: str,
        *,
        late_envelope_attempts: Iterable[Tuple[str, int]] = (),
        undispatched_attempts: Iterable[Tuple[str, int]] = (),
    ) -> None:
        """Atomically claim a dead run and write its resume boundary.

        `cancel_requested` is cleared here because the operator withdrew the
        stop request. `cancel_cause` remains historical until the next declared
        outcome rewrites it.

        The ownership check and claim share this `BEGIN IMMEDIATE`
        transaction. Two concurrent `run resume` processes therefore cannot
        both observe the old scheduler as dead: the first writes its own pid
        before releasing the database lock, and the second then refuses that
        live owner. Re-entry by the already-recorded process is idempotent and
        remains legal. A recorded owner on another host is unknown, never dead.

        Every node's retry-budget floor is raised in the same transaction to
        the highest attempt already classified. Infrastructure-budget blocks
        return to PENDING directly. A review-budget block returns only after
        an explicit grant marked its retained attempt and runtime preflight
        supplied absence-proven recovery authority for that exact generation.
        Credential, quiescence, and other adjudicated blocks remain blocked.
        The inherited-attempt accounting below remains separate: a newly
        classified inherited attempt is still charged against the refreshed
        floor, while an UNCLASSIFIED attempt costs nothing.

        `late_envelope_attempts` is absence-proven recovery authority supplied
        by the runtime preflight. A successful result row alone is not proof
        that the retained worktree or declaration still exists.

        `undispatched_attempts` is the same shape for the opposite state: a
        `QUIESCENCE_UNPROVEN` block over a generation that both this ledger
        and the runtime preflight agree never crossed dispatch. Those return
        to PENDING carrying a one-shot marker the scheduler consumes by
        reclaiming that exact attempt. A quiescence block over an attempt that
        did dispatch, or one either half cannot account for, is untouched here
        and stays blocked -- which is what it is for.
        """
        proven_late_envelopes = set(late_envelope_attempts)
        proven_undispatched = set(undispatched_attempts)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            claim = self.conn.execute(
                "SELECT scheduler_pid, scheduler_host FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if claim is None:
                raise UnknownNode(f"run {run_id} does not exist")
            prior_pid = int(claim[0]) if claim[0] is not None else None
            prior_host = claim[1]
            if prior_pid is not None:
                prior = RunRecord(
                    run_id=run_id,
                    plan_digest="",
                    created_at="",
                    last_transition_at="",
                    latest_outcome=None,
                    latest_outcome_at=None,
                    cancel_requested=False,
                    scheduler_pid=prior_pid,
                    scheduler_host=prior_host,
                )
                alive = scheduler_liveness(prior)
                if alive is True and prior_pid != os.getpid():
                    raise SchedulerStillAlive(
                        f"{run_id}: scheduler pid {prior_pid} is still alive on "
                        f"{prior_host or scheduler_host()}; resume refused "
                        "without changing the live run"
                    )
                if alive is None:
                    raise SchedulerLivenessUnknown(
                        f"{run_id}: scheduler pid {prior_pid} was recorded on "
                        f"{prior_host or 'an unknown host'}; resume cannot prove "
                        "that owner is dead"
                    )
            now = now_iso()
            self.conn.execute(
                "UPDATE runs SET last_transition_at=?, cancel_requested=0,"
                " scheduler_pid=?, scheduler_host=?, scheduler_claimed_at=?,"
                " scheduler_start_epoch=? WHERE run_id=?",
                (
                    now,
                    os.getpid(),
                    scheduler_host(),
                    now,
                    wd.process_start_epoch(os.getpid()),
                    run_id,
                ),
            )
            self.conn.execute(_RAISE_RUN_RETRY_SPEND_FLOOR, (run_id,))
            self.conn.execute(_RAISE_RUN_LANE_RETRY_SPEND_FLOOR, (run_id,))
            # A9's bound, read before the loop and spent after it. `spent` is
            # set only by a node this resume actually reopened through the
            # refresh route, so a resume that reopens nothing costs nothing and
            # repeating it is free.
            review_refresh_used = self._review_refresh_count(run_id)
            review_refresh_available = (
                review_refresh_used < RESUME_REVIEW_REFRESH_CEILING
            )
            review_refresh_spent = False
            refreshed_reasons = tuple(
                reason.value
                for reason in (
                    *_RESUME_REFRESHED_BLOCK_REASONS,
                    st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                )
            )
            placeholders = ",".join("?" for _ in refreshed_reasons)
            refreshed_nodes = self.conn.execute(
                "SELECT node_id, attempt_no, block_reason FROM node_lifecycle"
                " WHERE run_id=? AND state=?"
                f" AND block_reason IN ({placeholders}) ORDER BY node_id",
                (
                    run_id,
                    st.NodeState.BLOCKED.value,
                    *refreshed_reasons,
                ),
            ).fetchall()
            for node_id, attempt_no, block_reason in refreshed_nodes:
                late_envelope = (str(node_id), int(attempt_no)) in proven_late_envelopes
                review_budget = (
                    block_reason is not None
                    and st.BlockReason(block_reason)
                    is st.BlockReason.REVIEW_BUDGET_EXHAUSTED
                )
                attempt = None
                payload: Dict[str, Any] = {}
                if late_envelope or review_budget:
                    attempt = self.conn.execute(
                        "SELECT extra_json FROM attempts"
                        " WHERE run_id=? AND node_id=? AND attempt_no=?",
                        (run_id, node_id, attempt_no),
                    ).fetchone()
                    if attempt is None:
                        raise UnknownNode(
                            f"{run_id}/{node_id}#{attempt_no}: attempt row is absent"
                        )
                    payload = json.loads(attempt[0] or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                if review_budget:
                    # Two distinct routes out of a review-budget block, and
                    # they answer different questions.
                    #
                    # The recovery route corrects the record: a verdict that
                    # arrived after the block was written. It is not a fresh
                    # allowance, so the A9 ceiling does not apply to it and
                    # never consumes a unit of it.
                    #
                    # The refresh route is a fresh allowance, and it is the one
                    # A9 bounds. It is available only while the run has units
                    # left; past that the node stays blocked for an operator to
                    # look at, which is the termination A9 asks for.
                    recovery = (
                        payload.get(REVIEW_BUDGET_RECOVERY_KEY) is True
                        and late_envelope
                    )
                    if not recovery:
                        if not review_refresh_available:
                            continue
                        review_refresh_spent = True
                phase = (
                    self._late_envelope_resume_phase(
                        run_id, str(node_id), payload
                    ).value
                    if late_envelope
                    else None
                )
                if late_envelope:
                    payload.pop(REVIEW_BUDGET_RECOVERY_KEY, None)
                    payload[LATE_ENVELOPE_RECOVERY_KEY] = True
                    self.conn.execute(
                        "UPDATE attempts SET state=?, extra_json=?"
                        " WHERE run_id=? AND node_id=? AND attempt_no=?",
                        (
                            CLOSED_ATTEMPT_STATE.value,
                            json.dumps(payload, sort_keys=True),
                            run_id,
                            node_id,
                            attempt_no,
                        ),
                    )
                self.conn.execute(
                    "UPDATE node_lifecycle SET state=?, block_reason=NULL,"
                    " pending_cause=?, lane_phase=?, updated_at=?"
                    " WHERE run_id=? AND node_id=? AND state=?",
                    (
                        st.NodeState.PENDING.value,
                        st.PendingCause.OPERATOR_RESUME.value,
                        phase,
                        now,
                        run_id,
                        node_id,
                        st.NodeState.BLOCKED.value,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO transitions"
                    " (run_id, node_id, kind, from_state, to_state, reason,"
                    " actor, detail_json, created_at)"
                    " VALUES (?,?,'node',?,?,?,?,?,?)",
                    (
                        run_id,
                        node_id,
                        st.NodeState.BLOCKED.value,
                        st.NodeState.PENDING.value,
                        "resume:retry-budget",
                        "operator",
                        "{}",
                        now,
                    ),
                )
            if review_refresh_spent:
                # One unit per resume, not per node: the allowance is the
                # operator's decision to go round again, and a run that reopens
                # four review-blocked lanes in one resume made that decision
                # once. Written in the same transaction as the reopens it paid
                # for, so a crash between the two cannot hand out a free round.
                self.conn.execute(
                    "UPDATE runs SET review_refresh_count=? WHERE run_id=?",
                    (review_refresh_used + 1, run_id),
                )
            for node_id, attempt_no in sorted(proven_undispatched):
                attempt = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, attempt_no),
                ).fetchone()
                if attempt is None:
                    raise UnknownNode(
                        f"{run_id}/{node_id}#{attempt_no}: attempt row is absent"
                    )
                payload = json.loads(attempt[0] or "{}")
                if not isinstance(payload, dict):
                    payload = {}
                payload[UNDISPATCHED_RESUME_KEY] = True
                self.conn.execute(
                    "UPDATE attempts SET extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        json.dumps(payload, sort_keys=True),
                        run_id,
                        node_id,
                        attempt_no,
                    ),
                )
                # Guarded on the exact state *and* the exact block reason, so
                # a node that moved between the eligibility read and this
                # write is left alone rather than reopened on a stale premise.
                reopened = self.conn.execute(
                    "UPDATE node_lifecycle SET state=?, block_reason=NULL,"
                    " pending_cause=?, updated_at=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?"
                    "   AND state=? AND block_reason=?",
                    (
                        st.NodeState.PENDING.value,
                        st.PendingCause.OPERATOR_RESUME.value,
                        now,
                        run_id,
                        node_id,
                        attempt_no,
                        st.NodeState.BLOCKED.value,
                        st.BlockReason.QUIESCENCE_UNPROVEN.value,
                    ),
                )
                if reopened.rowcount != 1:
                    raise LifecycleError(
                        f"{run_id}/{node_id}#{attempt_no}: no QUIESCENCE_UNPROVEN "
                        "block on that generation to reopen"
                    )
                self.conn.execute(
                    "INSERT INTO transitions"
                    " (run_id, node_id, kind, from_state, to_state, reason,"
                    " actor, detail_json, created_at)"
                    " VALUES (?,?,'node',?,?,?,?,?,?)",
                    (
                        run_id,
                        node_id,
                        st.NodeState.BLOCKED.value,
                        st.NodeState.PENDING.value,
                        "resume:undispatched-quiescence",
                        "operator",
                        json.dumps({"attempt_no": attempt_no}, sort_keys=True),
                        now,
                    ),
                )
            self.conn.execute(
                "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
                " reason, actor, detail_json, created_at)"
                " VALUES (?,NULL,'run',NULL,NULL,'resume','operator','{}',?)",
                (run_id, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def quiescence_blocked_attempts(self, run_id: str) -> Tuple[Tuple[str, int], ...]:
        """Blocked attempts `run resume` may recover after agent absence."""
        rows = self.conn.execute(
            "SELECT node_id, attempt_no FROM node_lifecycle "
            "WHERE run_id=? AND state=? AND block_reason=? ORDER BY node_id",
            (
                run_id,
                st.NodeState.BLOCKED.value,
                st.BlockReason.QUIESCENCE_UNPROVEN.value,
            ),
        ).fetchall()
        return tuple((str(row[0]), int(row[1])) for row in rows)

    @serialized
    def undispatched_quiescence_attempts(
        self, run_id: str
    ) -> Tuple[Tuple[str, int], ...]:
        """Quiescence blocks whose attempt provably never crossed dispatch.

        A `QUIESCENCE_UNPROVEN` block says "this attempt's owned execution
        could not be shown absent". That is an adjudication about a writer,
        and where a writer could exist it stays. But the runtime's quiescer
        answers the same way about an attempt that never created anything at
        all -- it resolves a handle from a map only a successful launch
        populates -- and that answer is a statement about the map, not about
        the world. This is the durable half of telling the two apart, and it
        is a conjunction of absences rather than one flag, because any single
        one of them could be absent for an unrelated reason:

        * the attempt row was never launched -- no `launched_at`, no pid, no
          host, no process start epoch, no turn, and no transcript path. The
          launcher writes all of those in one `mark_launched`, so their joint
          absence is the launch not having happened rather than a field
          having been missed;
        * no actor session was ever bound for that generation, and none is
          ACTIVE for the node at all -- a pane bound to this node is a writer
          whatever the attempt row says;
        * the attempt produced nothing durable: no result row, no lane
          candidate at or beyond that generation, no repair handoff, and no
          orphan (an orphan row *is* a recorded abandoned pid);
        * it is the node's newest attempt. Reopening a superseded generation
          would relaunch work a later one already replaced.

        The filesystem half -- no submitted prompt, no envelope, no session
        directory, and a worktree still identical to its recorded baseline --
        belongs to the runtime preflight, which owns those paths. Both halves
        must agree before `resume_run` is given any authority here, and an
        attempt that fails either one stays blocked.
        """
        rows = self.conn.execute(
            "SELECT nl.node_id, nl.attempt_no, a.extra_json"
            " FROM node_lifecycle nl"
            " JOIN attempts a ON a.run_id=nl.run_id AND a.node_id=nl.node_id"
            "  AND a.attempt_no=nl.attempt_no"
            " WHERE nl.run_id=? AND nl.state=? AND nl.block_reason=?"
            "   AND a.launched_at IS NULL AND a.pid IS NULL"
            "   AND a.attempt_host IS NULL AND a.attempt_start_epoch IS NULL"
            "   AND a.turn_count=0"
            "   AND a.attempt_no=(SELECT MAX(m.attempt_no) FROM attempts m"
            "                      WHERE m.run_id=nl.run_id AND m.node_id=nl.node_id)"
            "   AND NOT EXISTS (SELECT 1 FROM actor_sessions s"
            "                    WHERE s.run_id=nl.run_id"
            "                      AND s.build_node_id=nl.node_id"
            "                      AND (s.generation>=nl.attempt_no OR s.state=?))"
            "   AND NOT EXISTS (SELECT 1 FROM results r"
            "                    WHERE r.run_id=nl.run_id AND r.node_id=nl.node_id"
            "                      AND r.attempt_no=nl.attempt_no)"
            "   AND NOT EXISTS (SELECT 1 FROM lane_candidates c"
            "                    WHERE c.run_id=nl.run_id"
            "                      AND c.build_node_id=nl.node_id"
            "                      AND c.builder_generation>=nl.attempt_no)"
            "   AND NOT EXISTS (SELECT 1 FROM repair_handoffs h"
            "                    WHERE h.run_id=nl.run_id"
            "                      AND h.build_node_id=nl.node_id"
            "                      AND h.builder_generation>=nl.attempt_no)"
            "   AND NOT EXISTS (SELECT 1 FROM orphans o"
            "                    WHERE o.run_id=nl.run_id AND o.node_id=nl.node_id"
            "                      AND o.attempt_no=nl.attempt_no)"
            " ORDER BY nl.node_id",
            (
                run_id,
                st.NodeState.BLOCKED.value,
                st.BlockReason.QUIESCENCE_UNPROVEN.value,
                st.ActorSessionState.ACTIVE.value,
            ),
        ).fetchall()
        eligible = []
        for node_id, attempt_no, extra_json in rows:
            payload = json.loads(extra_json or "{}")
            if not isinstance(payload, dict):
                continue
            # A transcript path is written by the same call that sets
            # `launched_at`, and a sealed output by a measurement that only a
            # dispatched attempt reaches. Either one present means the SQL
            # above is reading a row it does not understand, so it declines.
            if payload.get(wd.SESSION_PATH_KEY) is not None:
                continue
            if payload.get(SEALED_OUTPUT_SHA_KEY) is not None:
                continue
            eligible.append((str(node_id), int(attempt_no)))
        return tuple(eligible)

    @serialized
    def claim_undispatched_attempt(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> int:
        """Take back the *same* generation a stale quiescence block held.

        The counterpart of `claim_late_envelope_attempt`, for the opposite
        state: that one adopts an attempt whose work is complete and must not
        be relaunched, this one reopens an attempt whose work never started
        and therefore must be. Both consume a one-shot marker written against
        evidence at the resume boundary; neither invents authority of its own,
        and an attempt row that does not carry the marker is refused here
        rather than reopened on the caller's say-so.

        The recorded measurement baseline goes with the claim. §8.3's bracket
        opens on the *provisioned* tree, and this attempt is about to be
        provisioned again before it is measured; a before-side taken by a
        bracket that never closed is not the before-side of the one about to
        open. Keeping it would either bind the new measurement to a stale
        tree or -- when provision moves at all -- collide with
        `record_baseline`'s refusal to rewrite what an attempt started from.
        The row is dropped in the same transaction that reopens the attempt,
        so no reader ever sees the attempt RUNNING with a foreign baseline.

        No attempt number is allocated: `start_attempt` mints generations and
        this deliberately does not, which is what makes the recovery
        same-attempt rather than a fresh try wearing a recovery's name.
        """
        state_row = self.conn.execute(
            "SELECT state, extra_json FROM attempts"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (run_id, node_id, attempt_no),
        ).fetchone()
        if state_row is None:
            raise UnknownNode(f"{run_id}/{node_id}#{attempt_no}: attempt row is absent")
        payload = json.loads(state_row[1] or "{}")
        if (
            not isinstance(payload, dict)
            or payload.pop(UNDISPATCHED_RESUME_KEY, None) is not True
        ):
            raise LifecycleError(
                f"{run_id}/{node_id}#{attempt_no}: no undispatched-resume "
                "authority on the attempt row; a quiescence block is reopened "
                "against evidence recorded at the resume boundary, never on "
                "request"
            )
        payload.pop(ATTEMPT_BASELINE_DIGEST_KEY, None)
        encoded = json.dumps(payload, sort_keys=True)

        def extra(lifecycle: st.NodeLifecycle):
            return [
                (
                    "UPDATE attempts SET state=?, retry_class=NULL, started_at=?,"
                    " extra_json=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        st.NodeState.RUNNING.value,
                        time.time(),
                        encoded,
                        run_id,
                        node_id,
                        attempt_no,
                    ),
                ),
                (
                    "DELETE FROM attempt_baselines"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, attempt_no),
                ),
            ]

        lifecycle = self._transition_node(
            run_id,
            node_id,
            st.NodeState.RUNNING,
            actor="scheduler",
            reason="attempt-start",
            require_state=(st.NodeState.PENDING,),
            detail={"repair": "undispatched-quiescence"},
            extra_writes=extra,
        )
        if lifecycle.attempt_no != attempt_no:
            raise LifecycleError(
                f"{run_id}/{node_id}: lifecycle points at attempt "
                f"{lifecycle.attempt_no}, not the {attempt_no} being reclaimed"
            )
        return attempt_no

    @serialized
    def retry_budget_blocked_attempts(self, run_id: str) -> Tuple[Tuple[str, int], ...]:
        """Budget-blocked generations eligible for proof-backed recovery.

        Infrastructure blocks are always eligible at a resume boundary.
        Review-budget blocks are eligible only after an operator grant marked
        the exact retained attempt; a bare resume cannot erase adjudication.
        """
        reasons = tuple(
            reason.value
            for reason in (
                *_RESUME_REFRESHED_BLOCK_REASONS,
                st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            )
        )
        placeholders = ",".join("?" for _ in reasons)
        rows = self.conn.execute(
            "SELECT nl.node_id, nl.attempt_no, nl.block_reason, a.extra_json"
            " FROM node_lifecycle nl"
            " LEFT JOIN attempts a ON a.run_id=nl.run_id"
            " AND a.node_id=nl.node_id AND a.attempt_no=nl.attempt_no"
            " WHERE nl.run_id=? AND nl.state=?"
            f" AND nl.block_reason IN ({placeholders}) ORDER BY nl.node_id",
            (run_id, st.NodeState.BLOCKED.value, *reasons),
        ).fetchall()
        eligible = []
        for node_id, attempt_no, block_reason, extra_json in rows:
            if block_reason == st.BlockReason.REVIEW_BUDGET_EXHAUSTED.value:
                payload = json.loads(extra_json or "{}")
                if (
                    not isinstance(payload, dict)
                    or payload.get(REVIEW_BUDGET_RECOVERY_KEY) is not True
                ):
                    continue
            eligible.append((str(node_id), int(attempt_no)))
        return tuple(eligible)

    @serialized
    def running_attempts(self, run_id: str) -> Tuple[Tuple[str, int], ...]:
        """Current RUNNING generations a resumed scheduler may recover."""
        rows = self.conn.execute(
            "SELECT node_id, attempt_no FROM node_lifecycle "
            "WHERE run_id=? AND state=? ORDER BY node_id",
            (run_id, st.NodeState.RUNNING.value),
        ).fetchall()
        return tuple((str(row[0]), int(row[1])) for row in rows)

    def _late_envelope_resume_phase(
        self, run_id: str, node_id: str, payload: Mapping[str, Any]
    ) -> st.LanePhase:
        """Restore the retained lane phase instead of restarting at BUILDING."""
        stored = payload.get(LATE_ENVELOPE_PHASE_KEY)
        if isinstance(stored, str):
            try:
                phase = st.LanePhase(stored)
            except ValueError:
                phase = None
            if phase is not None and phase is not st.LanePhase.BLOCKED:
                return phase
        if isinstance(payload.get(REPAIR_HANDOFF_RECOVERY_KEY), str):
            return st.LanePhase.REPAIRING
        handoff = self.conn.execute(
            "SELECT state FROM repair_handoffs"
            " WHERE run_id=? AND build_node_id=?"
            " ORDER BY rowid DESC LIMIT 1",
            (run_id, node_id),
        ).fetchone()
        if handoff is not None:
            state = st.RepairHandoffState(str(handoff[0]))
            if state is st.RepairHandoffState.PENDING:
                return st.LanePhase.REPAIR_HANDOFF
            if state is st.RepairHandoffState.SUBMITTED:
                return st.LanePhase.REPAIRING
            if state is st.RepairHandoffState.ACKNOWLEDGED:
                return st.LanePhase.WAITING_FOR_NEW_CANDIDATE
        return st.LanePhase.BUILDING

    @serialized
    def prepare_late_envelope_recovery(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> st.NodeLifecycle:
        """Return a completed generation to the frontier without retrying it.

        A quiescence block terminalizes both the node and its lane phase.
        Recovery reopens those two authorities atomically: leaving the lane
        ``BLOCKED`` makes the recovered worker lose its first phase CAS and
        strand the node ``RUNNING`` after its future returns.
        """

        def extra(lifecycle: st.NodeLifecycle):
            if lifecycle.attempt_no != attempt_no:
                raise IllegalTransition(
                    f"{run_id}/{node_id}: attempt {attempt_no} is no longer current"
                )
            row = self.conn.execute(
                "SELECT extra_json FROM attempts"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (run_id, node_id, attempt_no),
            ).fetchone()
            if row is None:
                raise UnknownNode(
                    f"{run_id}/{node_id}#{attempt_no}: attempt row is absent"
                )
            payload = json.loads(row[0] or "{}")
            if not isinstance(payload, dict):
                payload = {}
            phase = self._late_envelope_resume_phase(run_id, node_id, payload)
            payload[LATE_ENVELOPE_RECOVERY_KEY] = True
            return [
                (
                    "UPDATE attempts SET extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (json.dumps(payload, sort_keys=True), run_id, node_id, attempt_no),
                ),
                (
                    "UPDATE node_lifecycle SET lane_phase=?"
                    " WHERE run_id=? AND node_id=? AND lane_phase IS NOT NULL",
                    (phase.value, run_id, node_id),
                ),
            ]

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="operator",
            reason="resume:late-envelope",
            pending_cause=st.PendingCause.OPERATOR_RESUME,
            require_state=(st.NodeState.BLOCKED,),
            extra_writes=extra,
        )

    @serialized
    def claim_late_envelope_attempt(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> st.NodeLifecycle:
        """PENDING -> RUNNING and consume the one-shot recovery marker."""

        def extra(lifecycle: st.NodeLifecycle):
            if lifecycle.attempt_no != attempt_no:
                raise IllegalTransition(
                    f"{run_id}/{node_id}: attempt {attempt_no} is no longer current"
                )
            row = self.conn.execute(
                "SELECT extra_json FROM attempts"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (run_id, node_id, attempt_no),
            ).fetchone()
            if row is None:
                raise UnknownNode(
                    f"{run_id}/{node_id}#{attempt_no}: attempt row is absent"
                )
            payload = json.loads(row[0] or "{}")
            if (
                not isinstance(payload, dict)
                or payload.pop(LATE_ENVELOPE_RECOVERY_KEY, None) is not True
            ):
                raise IllegalTransition(
                    f"{run_id}/{node_id}#{attempt_no}: late recovery is not pending"
                )
            return [
                (
                    "UPDATE attempts SET state=?, started_at=?, launched_at=NULL,"
                    " pid=NULL, attempt_host=NULL, attempt_start_epoch=NULL,"
                    " turn_count=0, extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        st.NodeState.RUNNING.value,
                        time.time(),
                        json.dumps(payload, sort_keys=True),
                        run_id,
                        node_id,
                        attempt_no,
                    ),
                )
            ]

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.RUNNING,
            actor="scheduler",
            reason="attempt-recover:late-envelope",
            require_state=(st.NodeState.PENDING,),
            extra_writes=extra,
        )

    @serialized
    def claim_repair_handoff_attempt(
        self,
        run_id: str,
        build_node_id: str,
        review_node_id: str,
        attempt_no: int,
        rejected_candidate_sha: str,
    ) -> st.NodeLifecycle:
        """Resume a persisted repair handoff without minting another attempt.

        This recovery is legal only when all three durable authorities agree:
        the current attempt already has an accepted result, the named immutable
        candidate has a terminal rejection, and its handoff remains recoverable.
        The guarded node PENDING -> RUNNING transition and closed blocking
        attempt-row reopening are one transaction, so a second scheduler cannot
        claim a sibling attempt.
        """

        def extra(lifecycle: st.NodeLifecycle):
            if lifecycle.attempt_no != attempt_no:
                raise IllegalTransition(
                    f"{run_id}/{build_node_id}: attempt {attempt_no} "
                    "is no longer current"
                )
            attempt = self.conn.execute(
                "SELECT state, retry_class, extra_json FROM attempts"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (run_id, build_node_id, attempt_no),
            ).fetchone()
            if attempt is None:
                raise UnknownNode(
                    f"{run_id}/{build_node_id}#{attempt_no}: attempt row is absent"
                )
            if attempt[0] not in (
                st.NodeState.CANCELLED.value,
                st.NodeState.BLOCKED.value,
            ):
                raise IllegalTransition(
                    f"{run_id}/{build_node_id}#{attempt_no}: "
                    f"attempt is {attempt[0]}, not CANCELLED or BLOCKED"
                )
            if attempt[1] not in (
                None,
                st.RetryClass.ENVIRONMENTAL.value,
                st.RetryClass.LAUNCHER_TRANSIENT.value,
            ):
                raise IllegalTransition(
                    f"{run_id}/{build_node_id}#{attempt_no}: "
                    f"{attempt[1]} failure cannot resume a repair handoff"
                )
            result = self.conn.execute(
                "SELECT adjudication FROM results"
                " WHERE run_id=? AND node_id=? AND attempt_no=?"
                " ORDER BY id DESC LIMIT 1",
                (run_id, build_node_id, attempt_no),
            ).fetchone()
            if result is None or result[0] != st.Adjudication.ACCEPTED.value:
                raise IllegalTransition(
                    f"{run_id}/{build_node_id}#{attempt_no}: "
                    "accepted result evidence is absent"
                )
            candidate = self.conn.execute(
                "SELECT 1 FROM lane_candidates"
                " WHERE run_id=? AND build_node_id=? AND candidate_sha=?",
                (run_id, build_node_id, rejected_candidate_sha),
            ).fetchone()
            review = self.conn.execute(
                "SELECT state, verdict FROM candidate_reviews"
                " WHERE run_id=? AND review_node_id=? AND candidate_sha=?",
                (run_id, review_node_id, rejected_candidate_sha),
            ).fetchone()
            handoff = self.conn.execute(
                "SELECT state FROM repair_handoffs"
                " WHERE run_id=? AND build_node_id=?"
                " AND rejected_candidate_sha=?",
                (run_id, build_node_id, rejected_candidate_sha),
            ).fetchone()
            if (
                candidate is None
                or review is None
                or review[0] != st.CandidateReviewState.COMPLETED.value
                or review[1] != st.ReviewVerdict.REJECTED.value
                or handoff is None
                or handoff[0] == st.RepairHandoffState.FAILED.value
            ):
                raise IllegalTransition(
                    f"{run_id}/{build_node_id}#{attempt_no}: "
                    "rejected candidate handoff evidence is incomplete"
                )
            try:
                payload = json.loads(attempt[2] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload[REPAIR_HANDOFF_RECOVERY_KEY] = rejected_candidate_sha
            return [
                (
                    "UPDATE attempts SET state=?, started_at=?, launched_at=NULL,"
                    " pid=NULL, attempt_host=NULL, attempt_start_epoch=NULL,"
                    " turn_count=0, retry_class=NULL, extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        st.NodeState.RUNNING.value,
                        time.time(),
                        json.dumps(payload, sort_keys=True),
                        run_id,
                        build_node_id,
                        attempt_no,
                    ),
                )
            ]

        return self._transition_node(
            run_id,
            build_node_id,
            st.NodeState.RUNNING,
            actor="scheduler",
            reason="attempt-recover:repair-handoff",
            require_state=(st.NodeState.PENDING,),
            detail={"candidate_sha": rejected_candidate_sha},
            extra_writes=extra,
        )

    @serialized
    def _running_node_ids(self, run_id: str) -> Tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT node_id FROM node_lifecycle WHERE run_id=? AND state=?",
            (run_id, st.NodeState.RUNNING.value),
        ).fetchall()
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
            "SELECT cancel_cause FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
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
            (run_id, st.NodeState.CANCELLED.value, st.CancelCause.RUN_CANCEL.value),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def _reopen_run_cancelled_node(self, run_id: str, node_id: str) -> st.NodeLifecycle:
        """CANCELLED(RUN_CANCEL) -> PENDING, the resume's half of the stop.

        `attempt_no` is carried forward unchanged and no retry class is
        debited: `cancel_run` closed the attempt row without classifying it,
        so a node stopped mid-attempt returns to the frontier with the budget
        it had. Nothing about it was adjudicated, so there is nothing to
        charge it for.
        """
        reopened = self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="operator",
            reason="resume:run-cancel",
            pending_cause=st.PendingCause.OPERATOR_RESUME,
            require_state=(st.NodeState.CANCELLED,),
        )
        if reopened.lane_phase is st.LanePhase.CANCELLED:
            repair = self.conn.execute(
                "SELECT cr.verdict, rh.state FROM lane_candidates lc"
                " JOIN candidate_reviews cr ON cr.run_id=lc.run_id"
                " AND cr.review_node_id=lc.build_node_id || '::review'"
                " AND cr.candidate_sha=lc.candidate_sha"
                " JOIN repair_handoffs rh ON rh.run_id=lc.run_id"
                " AND rh.build_node_id=lc.build_node_id"
                " AND rh.rejected_candidate_sha=lc.candidate_sha"
                " WHERE lc.run_id=? AND lc.build_node_id=?"
                " ORDER BY lc.candidate_seq DESC LIMIT 1",
                (run_id, node_id),
            ).fetchone()
            phase = (
                st.LanePhase.REPAIR_HANDOFF.value
                if repair is not None
                and repair[0] == st.ReviewVerdict.REJECTED.value
                and repair[1] != st.RepairHandoffState.FAILED.value
                else None
            )
            self.conn.execute(
                "UPDATE node_lifecycle SET lane_phase=?"
                " WHERE run_id=? AND node_id=? AND lane_phase=?",
                (phase, run_id, node_id, st.LanePhase.CANCELLED.value),
            )
            return self.get_node(run_id, node_id)
        return reopened

    def resume_run(
        self,
        run_id: str,
        *,
        late_envelope_attempts: Sequence[Tuple[str, int]] = (),
        undispatched_attempts: Sequence[Tuple[str, int]] = (),
    ) -> Tuple[str, ...]:
        """Legal against BLOCKED, STUCK, NULL, and a run the operator stopped
        with `run cancel`; refused against ACCEPTED and against a run given up
        on node by node (§7.3). Writes the resume transition — refreshing
        `last_transition_at` — BEFORE touching any inherited RUNNING attempt,
        so the backstop measures silence from the resume, not from the dead
        run's last act (§7.8, §11.2). An inherited RUNNING attempt with no
        independently validated success envelope returns to PENDING for a new
        generation: the resumed process owns none of its pane and cannot adopt
        work whose completion is unknown.

        A caller may name generations whose successful envelope, worktree
        identity, baseline, and agent absence were already validated. Those
        generations also return to PENDING, but carry a one-shot recovery
        marker; the scheduler claims the same attempt and runs its normal
        inventory, gate, review, and merge path without relaunching the builder.
        This is artifact precedence at the resume boundary: completed work is
        not discarded because the supervisor died before observing it.

        A generation with an ACCEPTED result row but without that full recovery
        proof is closed UNCLASSIFIED rather than charged ENVIRONMENTAL.
        `attempts_spent` counts only classified rows, so the infrastructure
        failure costs no budget, but the work is not adopted on a partial
        evidence chain. Everything else is charged ENVIRONMENTAL exactly as
        before.

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
        requested_recoveries = set(late_envelope_attempts)
        requested_undispatched = set(undispatched_attempts)
        # Re-derived here rather than trusted from the caller. The runtime
        # preflight owns the filesystem half of the proof and this owns the
        # durable half; a generation the ledger does not itself find eligible
        # is refused however convincing the caller's evidence was.
        unknown_undispatched = requested_undispatched - set(
            self.undispatched_quiescence_attempts(run_id)
        )
        if unknown_undispatched:
            named = ", ".join(
                "{0}#{1}".format(node_id, attempt_no)
                for node_id, attempt_no in sorted(unknown_undispatched)
            )
            raise LifecycleError(
                f"{run_id}: {named} is not a quiescence block this ledger can "
                "show never crossed dispatch; resume left the run unchanged"
            )
        recoverable_attempts = set(self.running_attempts(run_id))
        recoverable_attempts.update(self.retry_budget_blocked_attempts(run_id))
        unknown_recoveries = requested_recoveries - recoverable_attempts
        if unknown_recoveries:
            raise ResumeRefused(
                f"{run_id}: late-envelope recovery does not name current "
                "RUNNING or retry-budget-blocked attempts: "
                f"{sorted(unknown_recoveries)!r}"
            )
        outcome = self.latest_outcome(run_id)
        cause = self.run_cancel_cause(run_id)
        if outcome is st.RunOutcome.ACCEPTED:
            raise ResumeRefused(
                f"{run_id}: resume is refused against a declared "
                f"{outcome.value} run (§7.3) — it is not reopenable"
            )
        if (
            outcome is st.RunOutcome.CANCELLED
            and cause not in st.REOPENABLE_CANCEL_CAUSES
        ):
            if cause is st.CancelCause.ABANDONED:
                why = (
                    "was given up on node by node, and each of those nodes "
                    "was adjudicated as work the run should finish without"
                )
            elif cause is st.CancelCause.DISCARDED:
                why = (
                    "was discarded — `run cancel --discard` is the verb "
                    "that ends a run for good, and `run pause` is the one "
                    "that stops a run you mean to come back to"
                )
            else:
                why = (
                    "records no cause at all — its ledger predates the "
                    "column, and reading an unrecorded cancellation as a "
                    "pause is the guess that reopens an adjudicated run"
                )
            raise ResumeRefused(
                f"{run_id}: resume is refused against a run declared CANCELLED "
                f"with cause {cause.value if cause else 'unrecorded'} (§7.3) — "
                f"only a run stopped by an operator's `run cancel` is "
                f"reopenable; this one {why}"
            )
        self._write_resume_transition(
            run_id,
            late_envelope_attempts=requested_recoveries,
            undispatched_attempts=requested_undispatched,
        )
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
                    run_id,
                    node_id=node_id,
                    attempt_no=inherited.attempt_no,
                    pid=inherited.pid,
                    handle=str(inherited.extra.get("pane"))
                    if inherited.extra.get("pane")
                    else None,
                    reason="resume: inherited a RUNNING attempt this process does not own",
                )
            key = (node_id, node.attempt_no)
            if key in requested_recoveries:
                self._release_late_envelope_attempt(run_id, node_id, node.attempt_no)
            elif (
                self.result_adjudication(run_id, node_id, node.attempt_no)
                is st.Adjudication.ACCEPTED
            ):
                # A declared result is durable output from this exact
                # generation, not permission to create another attempt.
                # Re-enter completion through the same late-envelope path so
                # candidate/review/handoff ledgers decide the persisted phase.
                self._release_late_envelope_attempt(run_id, node_id, node.attempt_no)
            else:
                self.fail_attempt(run_id, node_id, st.RetryClass.ENVIRONMENTAL)
            reclaimed.append(node_id)
        return tuple(reclaimed)

    def _release_unclassified_attempt(
        self, run_id: str, node_id: str
    ) -> st.NodeLifecycle:
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
            return [
                (
                    "UPDATE attempts SET state=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (CLOSED_ATTEMPT_STATE.value, run_id, node_id, lifecycle.attempt_no),
                )
            ]

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="scheduler",
            reason="resume:result-declared",
            pending_cause=st.PendingCause.SCHEDULER,
            require_state=(st.NodeState.RUNNING,),
            extra_writes=extra,
        )

    def _release_late_envelope_attempt(
        self, run_id: str, node_id: str, attempt_no: int
    ) -> st.NodeLifecycle:
        """RUNNING -> PENDING while retaining this declared generation."""

        def extra(lifecycle: st.NodeLifecycle):
            if lifecycle.attempt_no != attempt_no:
                raise IllegalTransition(
                    f"{run_id}/{node_id}: attempt {attempt_no} is no longer current"
                )
            row = self.conn.execute(
                "SELECT extra_json FROM attempts"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (run_id, node_id, attempt_no),
            ).fetchone()
            if row is None:
                raise UnknownNode(
                    f"{run_id}/{node_id}#{attempt_no}: attempt row is absent"
                )
            payload = json.loads(row[0] or "{}")
            if not isinstance(payload, dict):
                payload = {}
            payload[LATE_ENVELOPE_RECOVERY_KEY] = True
            return [
                (
                    "UPDATE attempts SET state=?, extra_json=?"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        CLOSED_ATTEMPT_STATE.value,
                        json.dumps(payload, sort_keys=True),
                        run_id,
                        node_id,
                        attempt_no,
                    ),
                )
            ]

        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="operator",
            reason="resume:late-envelope",
            pending_cause=st.PendingCause.OPERATOR_RESUME,
            require_state=(st.NodeState.RUNNING,),
            extra_writes=extra,
        )

    # ── operator escapes (§11.3) ────────────────────────────────────────────

    def _require_escape_legal(self, run_id: str) -> None:
        outcome = self.latest_outcome(run_id)
        if outcome not in (st.RunOutcome.BLOCKED, st.RunOutcome.STUCK):
            raise EscapeRefused(
                f"{run_id}: escapes are legal only against a run declared BLOCKED or "
                f"STUCK (§7.3, §11.2); latest outcome is "
                f"{outcome.value if outcome else 'NULL'} — an escape against an "
                "undeclared run would race a scheduler that may still be alive"
            )

    @serialized
    def _scheduler_claim(self, run_id: str) -> Tuple[Optional[int], Optional[str]]:
        row = self.conn.execute(
            "SELECT scheduler_pid, scheduler_host FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
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
            run_id=run_id,
            plan_digest="",
            created_at="",
            last_transition_at="",
            latest_outcome=None,
            latest_outcome_at=None,
            cancel_requested=False,
            scheduler_pid=pid,
            scheduler_host=recorded_host,
        )
        alive = scheduler_liveness(record)
        if alive is True:
            raise SchedulerStillAlive(
                f"{run_id}: scheduler pid {pid} is still alive on "
                f"{recorded_host or scheduler_host()}; an escape against a "
                "RUNNING node would race a scheduler that is still there"
            )
        if alive is None:
            if not pid or pid <= 0:
                condition = "no scheduler pid is recorded"
            else:
                condition = (
                    f"scheduler pid {pid} was recorded on {recorded_host}, "
                    "not this host"
                )
            raise SchedulerLivenessUnknown(
                f"{run_id}: scheduler liveness is unknown ({condition}); "
                "refusing rather than guessing the scheduler is dead"
            )

    def _close_running_attempt(
        self, run_id: str, node_id: str, *, retry_class: Optional[st.RetryClass] = None
    ):
        """Close the live attempt row in the same transaction as the escape.

        The row stays; only its state (and optional classification) change.
        Leaving it RUNNING collides the next attempt with §10.3's partial
        unique index and makes §7.7 adjudicate a late arrival ACCEPTED.
        """

        def extra(lifecycle: st.NodeLifecycle):
            if retry_class is None:
                return [
                    (
                        "UPDATE attempts SET state=? "
                        "WHERE run_id=? AND node_id=? AND state=?",
                        (
                            CLOSED_ATTEMPT_STATE.value,
                            run_id,
                            node_id,
                            st.NodeState.RUNNING.value,
                        ),
                    )
                ]
            return [
                (
                    "UPDATE attempts SET retry_class=?, state=? "
                    "WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (
                        retry_class.value,
                        CLOSED_ATTEMPT_STATE.value,
                        run_id,
                        node_id,
                        lifecycle.attempt_no,
                    ),
                )
            ]

        return extra

    def _prepare_stranded_running(
        self, run_id: str, node_id: str, *, retry_class: Optional[st.RetryClass] = None
    ):
        """If the node is RUNNING, demand a dead scheduler and close the attempt.

        Returns `(extra_writes, detail)` for `_transition_node`. BLOCKED is
        unchanged. Other states fall through to `require_state`.
        """
        current = self.get_node(run_id, node_id).state
        if current is not st.NodeState.RUNNING:
            return None, None
        self._require_scheduler_dead(run_id)
        return (
            self._close_running_attempt(run_id, node_id, retry_class=retry_class),
            {"scheduler_liveness": False},
        )

    def retry(
        self, run_id: str, node_id: str, *, force: bool = False, grant: int = 0
    ) -> st.NodeLifecycle:
        """Issue an operator retry or grant against one blocked generation.

        Most retries move ``BLOCKED`` to ``PENDING`` and begin a new attempt.
        A positive grant against ``REVIEW_BUDGET_EXHAUSTED`` is different:
        the rejected candidate, repair handoff, worktree, and actor generation
        belong to the current attempt. The grant therefore leaves that attempt
        blocked and marks it for proof-backed recovery by ``run resume``.
        Runtime preflight must still prove the retained worktree and actor
        absence before the same attempt may return to the frontier.

        ``grant`` sizes the durable semantic and review-rejection budgets.
        Semantic spend is counted from classified attempts; review spend is
        counted independently in ``lane_retry_spends``. Both are cumulative
        across candidate bases for this run and node.

        The escape's identity is unchanged: any positive grant is
        ``Escape.RETRY_FORCE``. Its magnitude is recorded in transition detail.

        Retry-class budgets are refreshed here without a magnitude. Launcher
        and environmental failures are infrastructure tolerance, so the named
        node gets a fresh configured allowance rather than an operator-sized
        adjudication budget.
        """
        if grant < 0:
            raise EscapeRefused(f"{node_id}: retry grant must be positive, got {grant}")
        if grant and force:
            # Not a style preference: `--force` *is* a grant of one, so
            # accepting both would leave the total silently ambiguous between
            # 1, `grant`, and `grant + 1` — the exact arithmetic an operator
            # sizing a grant cannot afford to guess at.
            raise EscapeRefused(
                f"{node_id}: --force is a grant of one; pass one of "
                "--force or --grant, never both"
            )
        delta = grant if grant else (1 if force else 0)
        self._require_escape_legal(run_id)
        current = self.get_node(run_id, node_id)
        retained_review = (
            delta > 0
            and current.state is st.NodeState.BLOCKED
            and current.block_reason is st.BlockReason.REVIEW_BUDGET_EXHAUSTED
        )
        if retained_review:

            def retain_attempt(lifecycle: st.NodeLifecycle):
                row = self.conn.execute(
                    "SELECT extra_json FROM attempts"
                    " WHERE run_id=? AND node_id=? AND attempt_no=?",
                    (run_id, node_id, lifecycle.attempt_no),
                ).fetchone()
                if row is None:
                    raise UnknownNode(
                        f"{run_id}/{node_id}#{lifecycle.attempt_no}: attempt row is absent"
                    )
                payload = json.loads(row[0] or "{}")
                if not isinstance(payload, dict):
                    payload = {}
                if payload.get(REVIEW_BUDGET_RECOVERY_KEY) is True:
                    raise EscapeRefused(
                        f"{node_id}: review-budget recovery is already granted"
                    )
                payload[REVIEW_BUDGET_RECOVERY_KEY] = True
                return [
                    (
                        "UPDATE attempts SET extra_json=?"
                        " WHERE run_id=? AND node_id=? AND attempt_no=?",
                        (
                            json.dumps(payload, sort_keys=True),
                            run_id,
                            node_id,
                            lifecycle.attempt_no,
                        ),
                    ),
                    (_RAISE_NODE_RETRY_SPEND_FLOOR, (run_id, node_id)),
                    (_RAISE_NODE_LANE_RETRY_SPEND_FLOOR, (run_id, node_id)),
                ]

            self._transition_node(
                run_id,
                node_id,
                st.NodeState.BLOCKED,
                actor="operator",
                reason=st.Escape.RETRY_FORCE.value,
                block_reason=st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                require_state=(st.NodeState.BLOCKED,),
                granted_extra_delta=delta,
                detail={
                    "granted_extra_delta": delta,
                    "retained_attempt_recovery_requested": True,
                },
                extra_writes=retain_attempt,
            )
            return self.get_node(run_id, node_id)

        stranded, detail = self._prepare_stranded_running(
            run_id, node_id, retry_class=st.RetryClass.ENVIRONMENTAL
        )

        def extra(lifecycle: st.NodeLifecycle):
            # BLOCKED is a terminal lane phase, not merely a presentation of
            # the node state. An operator retry starts a new correction cycle;
            # leaving BLOCKED here makes the next scheduler generation lose
            # its first BUILDING CAS and strands the node in RUNNING.
            reset_phase = (
                "UPDATE node_lifecycle SET lane_phase=NULL"
                " WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            )
            # The floor first, the stranded attempt's classification second,
            # and the order is the semantics rather than a style choice: the
            # floor is the highest attempt *already* classified, so a row this
            # same transaction is about to classify must not be in it. Written
            # the other way round, the escape would forgive the very failure
            # it is being invoked over.
            return [
                reset_phase,
                (_RAISE_NODE_RETRY_SPEND_FLOOR, (run_id, node_id)),
                (_RAISE_NODE_LANE_RETRY_SPEND_FLOOR, (run_id, node_id)),
            ] + list(stranded(lifecycle) if stranded else ())

        reason = st.Escape.RETRY_FORCE.value if delta else st.Escape.RETRY.value
        detail = dict(detail or {})
        if delta:
            detail["granted_extra_delta"] = delta
        self._transition_node(
            run_id,
            node_id,
            st.NodeState.PENDING,
            actor="operator",
            reason=reason,
            require_state=(st.NodeState.BLOCKED, st.NodeState.RUNNING),
            granted_extra_delta=delta,
            pending_cause=st.PendingCause.OPERATOR_RETRY,
            detail=detail or None,
            extra_writes=extra,
        )
        return self.get_node(run_id, node_id)

    def _merge_evidence(self, run_id: str, node_id: str) -> MergeEvidence:
        """Count operator-visible evidence from authoritative durable rows."""
        verified = self.conn.execute(
            "SELECT COUNT(*) FROM transitions"
            " WHERE run_id=? AND node_id=? AND kind='node' AND to_state=?",
            (run_id, node_id, st.NodeState.VERIFIED.value),
        ).fetchone()
        attempts = self.attempts_for(run_id, node_id)
        rejected = self.conn.execute(
            "SELECT COUNT(*) FROM candidate_reviews"
            " WHERE run_id=? AND review_node_id=? AND state=? AND verdict=?",
            (
                run_id,
                f"{node_id}::review",
                st.CandidateReviewState.COMPLETED.value,
                st.ReviewVerdict.REJECTED.value,
            ),
        ).fetchone()
        return MergeEvidence(
            verified_transitions=int(verified[0]) if verified else 0,
            review_rejections=int(rejected[0]) if rejected else 0,
            attempts_recorded=len(attempts),
            block_reason=self.get_node(run_id, node_id).block_reason,
        )

    def skip(
        self, run_id: str, node_id: str, *, accept_sha: str, repo_path
    ) -> st.NodeLifecycle:
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
            (run_id, node_id),
        ).fetchone()
        if latest_attempt is None:
            raise SkipAncestryRefused(
                f"{node_id}: no attempt base exists; skip cannot prove output identity"
            )
        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        if branch.returncode != 0:
            raise SkipAncestryRefused(
                f"{node_id}: {repo_path} has no checked-out branch; "
                "skip does not bypass worktree identity (§11.3)"
            )
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
                f"digest: git -C {repo_path} rev-parse {accept_sha}"
            )
        if not wt.is_valid_output_commit(
            repo, accept_sha, expected_base=str(latest_attempt[0])
        ):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not a valid output commit descending "
                f"from attempt base {latest_attempt[0]} in {repo_path}; "
                "skip does not bypass identity (§11.3)"
            )
        if not _is_ancestor(repo_path, accept_sha):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is not an ancestor of HEAD in {repo_path}; "
                "skip does not bypass the ancestry proof (§11.3)"
            )
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", accept_sha],
            capture_output=True,
            text=True,
        )
        if (
            head.returncode != 0
            or resolved.returncode != 0
            or (resolved.stdout.strip() != head.stdout.strip())
        ):
            raise SkipAncestryRefused(
                f"{node_id}: {accept_sha} is an older ancestor of HEAD in {repo_path}; "
                "skip accepts only the current HEAD (§11.3)"
            )
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise SkipAncestryRefused(
                f"{node_id}: {repo_path} is not a clean worktree at {accept_sha}; "
                "skip does not bypass cleanliness (§11.3)"
            )
        detail = dict(detail or {})
        detail[MERGE_EVIDENCE_KEY] = evidence.as_detail()
        return self._transition_node(
            run_id,
            node_id,
            st.NodeState.MERGED,
            actor="operator",
            reason=st.Escape.SKIP.value,
            require_state=(st.NodeState.BLOCKED, st.NodeState.RUNNING),
            output_sha=accept_sha,
            merge_cause=st.MergeCause.OPERATOR_ACCEPTED,
            detail=detail,
            extra_writes=extra,
        )

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
            run_id,
            node_id,
            st.NodeState.CANCELLED,
            actor="operator",
            reason=st.Escape.ABANDON.value,
            cancel_cause=st.CancelCause.ABANDONED,
            extra_writes=extra,
        )


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
    #: The validated authored plan name persisted at run creation. Resume
    #: reuses it for visible Herdr placement instead of re-deriving a label
    #: from argparse state.
    plan_name: Optional[str] = None


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
    #: How this node reached PENDING after leaving it (#103), and `None`
    #: both for a node that is not PENDING, for a seeded PENDING that never
    #: left the frontier, and for a PENDING row written before the column
    #: existed. `st.pending_cause_label` is the one derivation — a reader
    #: must not guess `SCHEDULER` from a NULL (RC1).
    pending_cause: Optional[st.PendingCause] = None
    #: The durable build/review-loop phase, absent on ordinary nodes and on
    #: ledgers written before persistent review authority existed.
    lane_phase: Optional[st.LanePhase] = None

    @property
    def merge_provenance(self) -> Optional[str]:
        """`SCHEDULER`, `OPERATOR_ACCEPTED`, `UNRECORDED`, or `None`."""
        return st.merge_cause_label(self.state, self.merge_cause)

    @property
    def pending_provenance(self) -> Optional[str]:
        """`SCHEDULER`, `OPERATOR_RETRY`, `OPERATOR_RESUME`, or `None`."""
        return st.pending_cause_label(self.state, self.pending_cause)


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
    "EMPTY",
    "PENDING",
    "RUNNING",
    "BLOCKED",
    "QUIESCENT",
    "CANCELLING",
    "CANCELLED",
    "MERGED",
    "ABANDONED",
)


def scheduler_liveness(
    record: RunRecord,
    *,
    is_alive: Callable[[int], bool] = wd.process_is_alive,
    host: Optional[str] = None,
) -> Optional[bool]:
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
    record: RunRecord,
    *,
    is_alive: Callable[[int], bool] = wd.process_is_alive,
    start_epoch: Callable[[int], Optional[float]] = wd.process_start_epoch,
    host: Optional[str] = None,
) -> Optional[int]:
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


def attempt_liveness(
    attempt: st.AttemptRecord,
    *,
    is_alive: Callable[[int], bool] = wd.process_is_alive,
    start_epoch: Callable[[int], Optional[float]] = wd.process_start_epoch,
    host: Optional[str] = None,
) -> Optional[bool]:
    """Is this attempt's own process still there? `None` = cannot be said.

    Three answers, and the third is the one that keeps this honest:

    * `True` — a pid was recorded on this host with a start epoch, and the
      live process at that pid started at that instant. Identity is proven.
    * `False` — a pid was recorded on this host with a start epoch, and no
      such process exists. Absence on the host that issued the pid is a
      structural fact about the process table, read with `os.kill(pid, 0)`.
    * `None` — no pid was recorded; or no host or no start epoch was
      recorded (a ledger older than the columns); or the pid was recorded
      on another host, whose pid namespace this machine cannot be asked
      about; or a process exists at the pid but its start epoch does not
      equal the recorded one (kernel reuse, #37). Callers must treat
      `None` as "unknown" and never as "dead": a watchdog stall is a
      lifecycle transition, and §1.2 forbids one caused by an unproven
      pid. Salvage treats `None` as refuse-closed, with a distinct code.
    """
    pid = attempt.pid
    if not pid or pid <= 0:
        return None
    recorded_host = attempt.attempt_host
    recorded_epoch = attempt.attempt_start_epoch
    if not recorded_host or recorded_epoch is None:
        return None
    current_host = host if host is not None else scheduler_host()
    if not _same_scheduler_host(recorded_host, current_host):
        return None
    if not is_alive(int(pid)):
        return False
    started = start_epoch(int(pid))
    if started is None or started != recorded_epoch:
        return None
    return True


def derive_run_state(
    record: RunRecord,
    nodes: Sequence[NodeRow],
    *,
    is_alive: Callable[[int], bool] = wd.process_is_alive,
    host: Optional[str] = None,
) -> str:
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
        all_merge_complete = all(
            state in (st.NodeState.MERGED, st.NodeState.ACCEPTED) for state in states
        )
        if all_merge_complete and not record.cancel_requested:
            return "MERGED"
        if record.cancel_requested or record.latest_outcome is st.RunOutcome.CANCELLED:
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


#: The derived states that say the run has stopped. Four are terminal on the
#: node rows alone; ABANDONED is the scheduler-death case, which
#: `derive_run_state` has already established from the process table.
STOPPED_RUN_STATES: Tuple[str, ...] = (
    "EMPTY",
    "MERGED",
    "CANCELLED",
    "QUIESCENT",
    "ABANDONED",
)

#: The derived states that establish the opposite from the node rows alone: a
#: node is RUNNING this instant, or a cancellation has not yet reached every
#: node. Neither needs a liveness probe to be true.
LIVE_RUN_STATES: Tuple[str, ...] = ("RUNNING", "CANCELLING")


def run_in_flight(
    record: RunRecord,
    nodes: Sequence[NodeRow],
    *,
    is_alive: Callable[[int], bool] = wd.process_is_alive,
    host: Optional[str] = None,
) -> Optional[bool]:
    """Is this run still going? `True` / `False` / `None` = cannot be said.

    `derive_run_state` answers *what shape* a run is in, and a reader wanting
    only "has it stopped" was left to partition nine strings for itself. Two of
    them do not partition: BLOCKED and PENDING both mean "work is left and
    nothing is executing it this instant", which is the shape of a run whose
    scheduler declared and exited *and* of a live one between two polls. What
    separates them is not in the node rows at all — it is whether a scheduler
    process is still behind the run — so this composes the two facts `run
    status` already reports side by side rather than adding a third derivation
    of either (§10.6).

    The tri-state is `scheduler_liveness`'s, and for its reason: no pid
    recorded, or a pid recorded on another host, means the process table this
    machine can read cannot answer, and a caller must render that as unknown.
    Collapsing `None` onto either boolean is how a report comes to assert that
    a run ended when no row ever said so — the defect this function exists to
    let a reader avoid, not one it may commit itself.

    Every input is a typed row or the process table (§1.2). No write, no
    prose; `LifecycleReader` opens the database `mode=ro`.
    """
    state = derive_run_state(record, nodes, is_alive=is_alive, host=host)
    if state in STOPPED_RUN_STATES:
        return False
    if state in LIVE_RUN_STATES:
        return True
    return scheduler_liveness(record, is_alive=is_alive, host=host)


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

    def current_plan(self, run_id: str) -> Optional[Tuple[str, bytes]]:
        """The digest and bytes this run executes under, or None.

        The read half of `LifecycleStore.current_plan`, here because resume
        resolves the plan before any writer handle exists and §10.6 keeps the
        query in this module rather than in the CLI.

        `None` is a run created before plan bytes were retained. Resume then
        falls back to matching installed files, which is exactly what it did
        before — absence is a legacy run, never an error.
        """
        try:
            row = self.conn.execute(
                "SELECT plan_digest, plan_bytes FROM run_plan_versions"
                " WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # A ledger written before the table existed. Same answer as a run
            # that never recorded one: fall back, do not fail.
            return None
        return None if row is None else (str(row[0]), bytes(row[1]))

    @classmethod
    def open(cls, db_path) -> "LifecycleReader":
        path = Path(db_path)
        if not path.is_file():
            raise LedgerUnavailable(f"no lifecycle database at {path}")
        located = path.resolve().as_uri()[len("file://") :]
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
        optional = tuple(name for name, _ in _RUNS_ADDED_COLUMNS if name in available)
        sql = (
            "SELECT run_id, plan_digest, created_at, last_transition_at,"
            " latest_outcome, latest_outcome_at, cancel_requested"
            + "".join(", " + name for name in optional)
            + " FROM runs"
        )
        params: Tuple[Any, ...] = ()
        if plan_digest is not None:
            sql += " WHERE plan_digest=?"
            params = (plan_digest,)
        return tuple(
            RunRecord(
                run_id=row["run_id"],
                plan_digest=row["plan_digest"],
                created_at=row["created_at"],
                last_transition_at=row["last_transition_at"],
                latest_outcome=(
                    st.RunOutcome(row["latest_outcome"])
                    if row["latest_outcome"]
                    else None
                ),
                latest_outcome_at=row["latest_outcome_at"],
                cancel_requested=bool(row["cancel_requested"]),
                cancel_cause=(
                    st.CancelCause(row["cancel_cause"])
                    if "cancel_cause" in optional and row["cancel_cause"]
                    else None
                ),
                scheduler_pid=(
                    row["scheduler_pid"] if "scheduler_pid" in optional else None
                ),
                scheduler_host=(
                    row["scheduler_host"] if "scheduler_host" in optional else None
                ),
                scheduler_claimed_at=(
                    row["scheduler_claimed_at"]
                    if "scheduler_claimed_at" in optional
                    else None
                ),
                scheduler_start_epoch=(
                    row["scheduler_start_epoch"]
                    if "scheduler_start_epoch" in optional
                    else None
                ),
                plan_name=(row["plan_name"] if "plan_name" in optional else None),
            )
            for row in self._rows(
                sql + " ORDER BY created_at DESC, run_id DESC", params
            )
        )

    def run(self, run_id: str) -> Optional[RunRecord]:
        found = [record for record in self.runs() if record.run_id == run_id]
        return found[0] if found else None

    def nodes(self, run_id: str) -> Tuple[NodeRow, ...]:
        # `mode=ro`, so this reader cannot migrate a ledger that predates
        # `merge_cause` / `pending_cause` and must not refuse to read one
        # either — the same rule `runs()` follows for the scheduler-ownership
        # columns. An absent column is simply not selected and reads back
        # `None`, which `st.merge_cause_label` renders as `UNRECORDED` for a
        # MERGED row rather than guessing `SCHEDULER`, and which
        # `st.pending_cause_label` leaves as `None` rather than guessing
        # `SCHEDULER` for a PENDING row.
        available = set(_table_columns(self.conn, "node_lifecycle"))
        merge_cause_sql = (
            " l.merge_cause," if "merge_cause" in available else " NULL AS merge_cause,"
        )
        pending_cause_sql = (
            " l.pending_cause,"
            if "pending_cause" in available
            else " NULL AS pending_cause,"
        )
        lane_phase_sql = (
            " l.lane_phase," if "lane_phase" in available else " NULL AS lane_phase,"
        )
        rows = self._rows(
            "SELECT d.node_id, d.kind, d.depth, d.needs_json, l.state,"
            " l.attempt_no, l.block_reason, l.output_sha,"
            + merge_cause_sql
            + pending_cause_sql
            + lane_phase_sql
            + " l.granted_extra_attempts, l.updated_at"
            " FROM dag_nodes d JOIN node_lifecycle l"
            " ON l.run_id = d.run_id AND l.node_id = d.node_id"
            " WHERE d.run_id=? ORDER BY d.depth, d.node_id",
            (run_id,),
        )
        return tuple(
            NodeRow(
                node_id=row["node_id"],
                kind=row["kind"],
                depth=row["depth"],
                needs=tuple(json.loads(row["needs_json"])),
                state=st.NodeState(row["state"]),
                attempt_no=row["attempt_no"],
                block_reason=(
                    st.BlockReason(row["block_reason"]) if row["block_reason"] else None
                ),
                output_sha=row["output_sha"],
                merge_cause=(
                    st.MergeCause(row["merge_cause"]) if row["merge_cause"] else None
                ),
                pending_cause=(
                    st.PendingCause(row["pending_cause"])
                    if row["pending_cause"]
                    else None
                ),
                lane_phase=(
                    st.LanePhase(row["lane_phase"]) if row["lane_phase"] else None
                ),
                granted_extra_attempts=row["granted_extra_attempts"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def attempts(self, run_id: str) -> Tuple[st.AttemptRecord, ...]:
        # `mode=ro`, so this reader cannot migrate a ledger that predates
        # the attempt-identity columns and must not refuse to read one
        # either — the same rule `runs()` follows for the scheduler-
        # ownership columns. An absent column is simply not selected and
        # reads back `None`, which `attempt_liveness` already treats as
        # "cannot be said".
        available = set(_table_columns(self.conn, "attempts"))
        optional = tuple(
            name for name, _ in _ATTEMPTS_ADDED_COLUMNS if name in available
        )
        rows = self._rows(
            "SELECT node_id, attempt_no, base_sha, state, started_at,"
            " launched_at, pid, turn_count, retry_class, extra_json"
            + "".join(", " + name for name in optional)
            + " FROM attempts WHERE run_id=? ORDER BY node_id, attempt_no",
            (run_id,),
        )
        return tuple(
            st.AttemptRecord(
                run_id=run_id,
                node_id=row["node_id"],
                attempt_no=row["attempt_no"],
                base_sha=row["base_sha"],
                state=st.NodeState(row["state"]),
                started_at=row["started_at"] or 0.0,
                launched_at=row["launched_at"],
                pid=row["pid"],
                turn_count=row["turn_count"] or 0,
                retry_class=(
                    st.RetryClass(row["retry_class"]) if row["retry_class"] else None
                ),
                extra=json.loads(row["extra_json"]),
                attempt_host=(
                    row["attempt_host"] if "attempt_host" in optional else None
                ),
                attempt_start_epoch=(
                    row["attempt_start_epoch"]
                    if "attempt_start_epoch" in optional
                    else None
                ),
            )
            for row in rows
        )

    def _has_table(self, table: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def _columns(self, table: str) -> Tuple[str, ...]:
        """The columns a table actually has. A read-only handle cannot run
        `_migrate`, so an older ledger is read for what it holds rather than
        for what this binary would have created."""
        return tuple(
            str(row[1])
            for row in self.conn.execute(
                "PRAGMA table_info({0})".format(table)
            ).fetchall()
        )

    def lane_candidates(
        self, run_id: str, build_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.LaneCandidate, ...]:
        if not self._has_table("lane_candidates"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("candidate read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, candidate_seq, candidate_sha,"
            " parent_candidate_sha, builder_generation, created_at"
            " FROM lane_candidates WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params = (run_id, build_node_id)
        rows = self._rows(
            sql + " ORDER BY build_node_id, candidate_seq LIMIT ?", params + (limit,)
        )
        return tuple(_candidate_from_row(row) for row in rows)

    def candidate_reviews(
        self, run_id: str, review_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.CandidateReview, ...]:
        if not self._has_table("candidate_reviews"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("review read limit must be a positive integer")
        dispatched_at = (
            "dispatched_at"
            if "dispatched_at" in _table_columns(self.conn, "candidate_reviews")
            else "NULL AS dispatched_at"
        )
        sql = (
            "SELECT run_id, review_node_id, candidate_sha, reviewer_generation,"
            f" state, {dispatched_at}, review_digest, receipt_path, findings_json,"
            " verdict, completed_at"
            " FROM candidate_reviews WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if review_node_id is not None:
            sql += " AND review_node_id=?"
            params = (run_id, review_node_id)
        rows = self._rows(
            sql + " ORDER BY review_node_id, rowid LIMIT ?", params + (limit,)
        )
        return tuple(_review_from_row(row) for row in rows)

    def repair_handoffs(
        self, run_id: str, build_node_id: Optional[str] = None, *, limit: int = 100
    ) -> Tuple[st.RepairHandoff, ...]:
        if not self._has_table("repair_handoffs"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("handoff read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, rejected_candidate_sha, findings_json,"
            " state, builder_generation, submitted_at, acknowledged_at"
            " FROM repair_handoffs WHERE run_id=?"
        )
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params = (run_id, build_node_id)
        rows = self._rows(
            sql + " ORDER BY build_node_id, rowid LIMIT ?", params + (limit,)
        )
        return tuple(_handoff_from_row(row) for row in rows)

    def legacy_review_migrations(
        self, run_id: str, *, limit: int = 100
    ) -> Tuple[LegacyReviewMigration, ...]:
        """Lanes held before scheduling because legacy review evidence is unsafe."""
        if not self._has_table("legacy_review_migration_blocks"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError(
                "legacy review migration read limit must be a positive integer"
            )
        rows = self._rows(
            "SELECT build_node_id, reason FROM legacy_review_migration_blocks"
            " WHERE run_id=? ORDER BY build_node_id LIMIT ?",
            (run_id, limit),
        )
        return tuple(
            LegacyReviewMigration(
                build_node_id=row["build_node_id"],
                migrated=False,
                blocked=True,
                reason=row["reason"],
            )
            for row in rows
        )

    def pinned_test_strength_contract(
        self, run_id: str
    ) -> Optional[st.TestStrengthContract]:
        """The pin, or `None` when the ledger holds no row for this run.

        A ledger predating the column is a different answer from a ledger with
        no run: the first pinned nothing because nothing could pin, which is
        the legacy contract; the second has nothing to say at all.
        """
        rows = self._rows("SELECT run_id FROM runs WHERE run_id=?", (run_id,))
        if not rows:
            return None
        return self.test_strength_contract(run_id)

    def test_strength_contract(self, run_id: str) -> st.TestStrengthContract:
        """The contract this run was created under. NULL, and a ledger with no
        column at all, both read as the legacy pin."""
        if "test_strength_contract" not in self._columns("runs"):
            return st.DEFAULT_TEST_STRENGTH_CONTRACT
        rows = self._rows(
            "SELECT test_strength_contract FROM runs WHERE run_id=?", (run_id,))
        if not rows or rows[0]["test_strength_contract"] is None:
            return st.DEFAULT_TEST_STRENGTH_CONTRACT
        try:
            return st.TestStrengthContract(str(rows[0]["test_strength_contract"]))
        except ValueError:
            raise LifecycleError(
                "run {0} is pinned to an unknown test-strength contract".format(
                    run_id)) from None

    def test_gate_evidence(
        self, run_id: str, tests_node_id: Optional[str] = None, *,
        limit: int = 1000
    ) -> Tuple[TestGateEvidenceRecord, ...]:
        """Recorded gate-strength measurements. Empty on a ledger without the
        table, which is a run that predates the contract, never a run whose
        tests were measured and found strong."""
        if not self._has_table("test_gate_evidence"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("test gate evidence limit must be positive")
        sql = ("SELECT tests_node_id, candidate_sha, runner, selector, strong,"
               " refusal, evidence_json, created_at FROM test_gate_evidence"
               " WHERE run_id=?")
        params: Tuple[Any, ...] = (run_id,)
        if tests_node_id is not None:
            sql += " AND tests_node_id=?"
            params += (tests_node_id,)
        rows = self._rows(
            sql + " ORDER BY tests_node_id, created_at, rowid LIMIT ?",
            params + (limit,))
        return tuple(
            TestGateEvidenceRecord(
                tests_node_id=row["tests_node_id"],
                candidate_sha=row["candidate_sha"],
                runner=row["runner"], selector=row["selector"],
                strong=bool(row["strong"]),
                refusal=row["refusal"],
                evidence=json.loads(row["evidence_json"]),
                created_at=row["created_at"], created=False)
            for row in rows)

    def test_pairings(
        self, run_id: str, build_node_id: Optional[str] = None, *,
        limit: int = 1000
    ) -> Tuple[TestPairing, ...]:
        if not self._has_table("test_implementation_pairings"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("test pairing read limit must be positive")
        sql = ("SELECT build_node_id, tests_node_id, accepted_test_sha,"
               " implementation_sha, verifier_command, selector,"
               " executed_cases, coverage_json, created_at"
               " FROM test_implementation_pairings WHERE run_id=?")
        params: Tuple[Any, ...] = (run_id,)
        if build_node_id is not None:
            sql += " AND build_node_id=?"
            params += (build_node_id,)
        rows = self._rows(
            sql + " ORDER BY build_node_id, tests_node_id, rowid LIMIT ?",
            params + (limit,))
        return tuple(
            TestPairing(
                build_node_id=row["build_node_id"],
                tests_node_id=row["tests_node_id"],
                accepted_test_sha=row["accepted_test_sha"],
                implementation_sha=row["implementation_sha"],
                verifier_command=row["verifier_command"],
                selector=row["selector"],
                executed_cases=int(row["executed_cases"]),
                coverage=json.loads(row["coverage_json"]),
                created_at=row["created_at"])
            for row in rows)

    def legacy_test_strength_blocks(
        self, run_id: str, *, limit: int = 1000
    ) -> Tuple[LegacyTestStrengthFinding, ...]:
        if not self._has_table("legacy_test_strength_blocks"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("legacy test strength block limit must be positive")
        rows = self._rows(
            "SELECT tests_node_id, reason, detail_json FROM"
            " legacy_test_strength_blocks WHERE run_id=?"
            " ORDER BY tests_node_id LIMIT ?", (run_id, limit))
        return tuple(
            LegacyTestStrengthFinding(
                tests_node_id=row["tests_node_id"], state="",
                candidate_sha=None, classification=row["reason"],
                blocking=True, detail=json.loads(row["detail_json"]))
            for row in rows)

    def lane_retry_spends(
        self, run_id: str, build_node_id: str, *, limit: int = 100
    ) -> Tuple[st.LaneRetrySpend, ...]:
        if not self._has_table("lane_retry_spend"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("lane retry read limit must be a positive integer")
        rows = self._rows(
            "SELECT run_id, build_node_id, retry_class, cycle_seq, candidate_sha,"
            " detail_json, created_at FROM lane_retry_spend"
            " WHERE run_id=? AND build_node_id=? ORDER BY cycle_seq LIMIT ?",
            (run_id, build_node_id, limit),
        )
        return tuple(_lane_retry_spend_from_row(row) for row in rows)

    def actor_sessions(
        self,
        run_id: str,
        build_node_id: str,
        *,
        actor_role: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[st.ActorSession, ...]:
        if not self._has_table("actor_sessions"):
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise LifecycleError("actor session read limit must be a positive integer")
        sql = (
            "SELECT run_id, build_node_id, actor_role, generation, state, pane_id,"
            " session_path, correlation_token, updated_at FROM actor_sessions"
            " WHERE run_id=? AND build_node_id=?"
        )
        params: Tuple[Any, ...] = (run_id, build_node_id)
        if actor_role is not None:
            sql += " AND actor_role=?"
            params = (run_id, build_node_id, actor_role)
        rows = self._rows(
            sql + " ORDER BY actor_role, generation LIMIT ?", params + (limit,)
        )
        return tuple(_actor_session_from_row(row) for row in rows)

    def attempts_by_run_for_plan(
        self, plan_digest: str, exclude_run_id: Optional[str] = None
    ) -> Dict[str, Tuple[st.AttemptRecord, ...]]:
        """Every run of one plan, and the attempt rows each of them holds.

        A node's identity across runs is `(plan_digest, node_id)` and nothing
        else. The ledger has never heard of a plan *name* — `runs.plan_digest`
        and `dag_nodes.plan_digest` are the whole of what it stores (§7.1) — so
        scoping by digest is what makes "the same node" a fact rather than a
        guess about two runs that happened to reuse a lane id. Re-shipping a
        plan mints a new digest and legitimately starts the count again: the
        bytes the node is judged against changed.

        `exclude_run_id` is always the run about to execute. On `run start`
        that id is fresh and excludes nothing; on `run resume` it is the run
        being re-entered, whose own review spend `Scheduler._review_ceiling_
        reached` already owns, and counting it here as well would charge the
        same rejection to two budgets.

        Read-only by construction: this reader opens `mode=ro`, and the
        cross-run budget is decided before the run's own ledger exists.
        """
        return {
            record.run_id: self.attempts(record.run_id)
            for record in self.runs(plan_digest)
            if record.run_id != exclude_run_id
        }

    def granted_extra_attempts_for_plan(
        self, plan_digest: str, exclude_run_id: Optional[str] = None
    ) -> Dict[str, int]:
        """Per node, the operator grants standing on this plan's prior runs.

        `retry --force` and `retry --grant N` raise
        `node_lifecycle.granted_extra_attempts`, which is keyed
        `(run_id, node_id)` — so a new run mints a row at zero and an operator
        who deliberately widened a node's allowance in run A would find the
        node refused at the start of run B, by a rule reading the very
        rejections that grant was given to absorb. Summing them here is what
        keeps a deliberate operator act from being silently revoked by the
        next `run start`.
        """
        granted: Dict[str, int] = {}
        for record in self.runs(plan_digest):
            if record.run_id == exclude_run_id:
                continue
            for row in self.nodes(record.run_id):
                granted[row.node_id] = granted.get(row.node_id, 0) + (
                    row.granted_extra_attempts or 0
                )
        return granted

    def transitions(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        return tuple(
            _audit_dict(row)
            for row in self._rows(
                "SELECT * FROM transitions WHERE run_id=? ORDER BY id", (run_id,)
            )
        )

    def results(self, run_id: str) -> Tuple[Dict[str, Any], ...]:
        return tuple(
            _audit_dict(row)
            for row in self._rows(
                "SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,)
            )
        )
