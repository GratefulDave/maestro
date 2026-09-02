"""Artifact-factory ledger. `lane_state.stage` is the only mutable lane authority."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import git_publication as gitpub
from . import scheduler_types as st
from .utils import now_iso

LANE_STAGE_CHECK = ", ".join(f"'{stage.value}'" for stage in st.LaneStage)
LANE_KIND_CHECK = ", ".join(f"'{kind.value}'" for kind in st.LANE_ARTIFACT_KINDS)
RUN_KIND_CHECK = ", ".join(f"'{kind.value}'" for kind in st.RUN_ARTIFACT_KINDS)
V1_LANE_ARTIFACT_KINDS = (
    "LANE_PLAN",
    "TEST_DRAFT",
    "TEST_REVIEW",
    "SEALED_TEST_BUNDLE",
    "BUILDER_OUTPUT",
    "CODE_REVIEW",
    "INTEGRATION_MERGE",
    "BASE_INVALIDATION",
    "USER_WAIT",
    "USER_DECISION",
)
V1_LANE_KIND_CHECK = ", ".join(f"'{kind}'" for kind in V1_LANE_ARTIFACT_KINDS)
LANE_ARTIFACT_COLUMNS = (
    "artifact_id",
    "run_id",
    "lane_id",
    "sequence",
    "completed_stage",
    "artifact_kind",
    "plan_revision",
    "spec_digest",
    "lane_projection_digest",
    "input_digest",
    "output_digest",
    "artifact_ref",
    "payload_json",
    "created_at",
)
_LANE_ARTIFACTS_V1_BACKUP = "lane_artifacts__v1_backup"


def _lane_artifacts_ddl(kind_check: str) -> str:
    return f"""CREATE TABLE lane_artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  lane_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  completed_stage TEXT NOT NULL,
  artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ({kind_check})),
  plan_revision INTEGER NOT NULL,
  spec_digest TEXT NOT NULL,
  lane_projection_digest TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  output_digest TEXT NOT NULL,
  artifact_ref TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, lane_id, sequence),
  UNIQUE (run_id, lane_id, plan_revision, completed_stage, input_digest),
  FOREIGN KEY (run_id, plan_revision)
    REFERENCES plan_revisions(run_id, plan_revision) DEFERRABLE INITIALLY DEFERRED
)"""


def _schema_script(lane_kind_check: str) -> str:
    return f"""
CREATE TABLE ledger_meta (
  schema_version TEXT PRIMARY KEY
);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  runtime_state_root TEXT NOT NULL,
  runtime_state_fingerprint TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  plan_revision INTEGER NOT NULL,
  integration_ref TEXT NOT NULL,
  integration_initial_sha TEXT NOT NULL,
  target_repository_root TEXT NOT NULL,
  target_git_common_dir TEXT NOT NULL,
  target_worktree_git_dir TEXT NOT NULL,
  target_object_format TEXT NOT NULL,
  target_repository_fingerprint TEXT NOT NULL,
  target_sync_journal_fingerprint TEXT NOT NULL,
  target_initial_main_sha TEXT NOT NULL,
  target_main_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (run_id, plan_revision)
    REFERENCES plan_revisions(run_id, plan_revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE plan_revisions (
  run_id TEXT NOT NULL,
  plan_revision INTEGER NOT NULL,
  plan_digest TEXT NOT NULL,
  parent_revision INTEGER,
  plan_artifact_ref TEXT NOT NULL,
  amendment_artifact_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, plan_revision),
  UNIQUE (run_id, plan_digest),
  FOREIGN KEY (run_id, parent_revision)
    REFERENCES plan_revisions(run_id, plan_revision) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (run_id, amendment_artifact_id)
    REFERENCES run_artifacts(run_id, artifact_id) DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    (parent_revision IS NULL AND amendment_artifact_id IS NULL)
    OR (parent_revision IS NOT NULL AND amendment_artifact_id IS NOT NULL)
  )
);
CREATE TABLE dag_lanes (
  run_id TEXT NOT NULL,
  plan_revision INTEGER NOT NULL,
  lane_id TEXT NOT NULL,
  needs_json TEXT NOT NULL,
  spec_digest TEXT NOT NULL,
  declared_outputs_json TEXT NOT NULL,
  lane_projection_digest TEXT NOT NULL,
  public_acceptance_json TEXT NOT NULL,
  PRIMARY KEY (run_id, plan_revision, lane_id),
  FOREIGN KEY (run_id, plan_revision)
    REFERENCES plan_revisions(run_id, plan_revision)
);
CREATE TABLE lane_state (
  run_id TEXT NOT NULL,
  lane_id TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ({LANE_STAGE_CHECK})),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, lane_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
{_lane_artifacts_ddl(lane_kind_check)};
CREATE TABLE run_artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ({RUN_KIND_CHECK})),
  plan_revision INTEGER NOT NULL,
  input_digest TEXT NOT NULL,
  output_digest TEXT NOT NULL,
  artifact_ref TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (run_id, sequence),
  UNIQUE (run_id, artifact_kind, input_digest),
  UNIQUE (run_id, artifact_id),
  FOREIGN KEY (run_id, plan_revision)
    REFERENCES plan_revisions(run_id, plan_revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE transitions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  lane_id TEXT,
  from_stage TEXT,
  to_stage TEXT,
  artifact_id TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


LANE_ARTIFACTS_SQL = _lane_artifacts_ddl(LANE_KIND_CHECK)
V1_LANE_ARTIFACTS_SQL = _lane_artifacts_ddl(V1_LANE_KIND_CHECK)
SCHEMA = _schema_script(LANE_KIND_CHECK)
V1_SCHEMA = _schema_script(V1_LANE_KIND_CHECK)


class ArtifactStoreError(st.KernelError):
    code = "ARTIFACT_STORE"


class LedgerSchemaUnsupported(ArtifactStoreError):
    code = "LEDGER_SCHEMA_UNSUPPORTED"


class StaleStageInput(ArtifactStoreError):
    code = "STALE_STAGE_INPUT"


class ArtifactCollision(ArtifactStoreError):
    code = "ARTIFACT_COLLISION"


class RunAlreadyExists(ArtifactStoreError):
    code = "RUN_ALREADY_EXISTS"


class UnknownRun(ArtifactStoreError):
    code = "UNKNOWN_RUN"


class UnknownLane(ArtifactStoreError):
    code = "UNKNOWN_LANE"


class StageCasConflict(ArtifactStoreError):
    code = "STAGE_CAS_CONFLICT"


class AmendmentRefused(ArtifactStoreError):
    code = "AMENDMENT_REFUSED"


class PublicationRefused(ArtifactStoreError):
    code = "PUBLICATION_REFUSED"


class ResumeBlocked(ArtifactStoreError):
    code = "RESUME_BLOCKED"


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    lane_id: Optional[str]
    sequence: int
    kind: st.ArtifactKind
    plan_revision: int
    input_digest: str
    output_digest: str
    artifact_ref: str
    payload: Mapping[str, Any]
    replayed: bool = False


def serialized(method):
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return guarded


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith("sqlite_")
    }


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


def _table_create_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _dumps(value: Any) -> str:
    return st.canonical_bytes(value).decode("utf-8")


def _loads(text: str) -> Any:
    return json.loads(text)


class ArtifactStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA defer_foreign_keys=ON;")
        names = _tables(self.conn)
        if not names:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT INTO ledger_meta(schema_version) VALUES (?)",
                (st.LEDGER_SCHEMA_VERSION,),
            )
            return
        if "ledger_meta" not in names:
            self._refuse_schema()
        versions = [
            row[0]
            for row in self.conn.execute("SELECT schema_version FROM ledger_meta")
        ]
        if len(versions) != 1:
            self._refuse_schema()
        version = versions[0]
        if version == st.LEDGER_SCHEMA_VERSION:
            return
        if version == st.LEDGER_SCHEMA_VERSION_V1:
            self._migrate_v1_to_v2()
            return
        self._refuse_schema()

    def _refuse_schema(self) -> None:
        self.conn.close()
        raise LedgerSchemaUnsupported(
            "preserve the ledger read-only and start a new run/database"
        )

    def _require_supported_v1_lane_artifacts(self) -> None:
        names = _tables(self.conn)
        if "lane_artifacts" not in names or _LANE_ARTIFACTS_V1_BACKUP in names:
            self._refuse_schema()
        sql = _table_create_sql(self.conn, "lane_artifacts")
        if sql is None or _normalize_sql(sql) != _normalize_sql(V1_LANE_ARTIFACTS_SQL):
            self._refuse_schema()
        cols = tuple(
            row[1] for row in self.conn.execute("PRAGMA table_info(lane_artifacts)")
        )
        if cols != LANE_ARTIFACT_COLUMNS:
            self._refuse_schema()
        extra_indexes = list(
            self.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='lane_artifacts' AND sql IS NOT NULL"
            )
        )
        if extra_indexes:
            self._refuse_schema()

    def _rebuild_lane_artifacts_current_check(self) -> None:
        cols = ", ".join(LANE_ARTIFACT_COLUMNS)
        self.conn.execute(
            f"ALTER TABLE lane_artifacts RENAME TO {_LANE_ARTIFACTS_V1_BACKUP}"
        )
        self.conn.execute(LANE_ARTIFACTS_SQL)
        self.conn.execute(
            f"INSERT INTO lane_artifacts ({cols}) "
            f"SELECT {cols} FROM {_LANE_ARTIFACTS_V1_BACKUP}"
        )
        self.conn.execute(f"DROP TABLE {_LANE_ARTIFACTS_V1_BACKUP}")

    def _require_post_migration_integrity(self) -> None:
        fk_violations = list(self.conn.execute("PRAGMA foreign_key_check"))
        if fk_violations:
            raise ArtifactStoreError("foreign_key_check")
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ArtifactStoreError("integrity_check")
        sql = _table_create_sql(self.conn, "lane_artifacts")
        if sql is None or _normalize_sql(sql) != _normalize_sql(LANE_ARTIFACTS_SQL):
            raise ArtifactStoreError("lane_artifacts ddl")
        version = self.conn.execute(
            "SELECT schema_version FROM ledger_meta"
        ).fetchone()
        if version is None or version[0] != st.LEDGER_SCHEMA_VERSION:
            raise ArtifactStoreError("schema_version")

    def _migrate_v1_to_v2(self) -> None:
        self._require_supported_v1_lane_artifacts()
        self._begin()
        try:
            self._rebuild_lane_artifacts_current_check()
            cursor = self.conn.execute(
                "UPDATE ledger_meta SET schema_version=? WHERE schema_version=?",
                (st.LEDGER_SCHEMA_VERSION, st.LEDGER_SCHEMA_VERSION_V1),
            )
            if cursor.rowcount != 1:
                raise ArtifactStoreError("ledger_meta schema_version stamp")
            self._require_post_migration_integrity()
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _run(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise UnknownRun(run_id)
        return row

    def _projection(self, run_id: str, plan_revision: int, lane_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM dag_lanes WHERE run_id=? AND plan_revision=? AND lane_id=?",
            (run_id, plan_revision, lane_id),
        ).fetchone()
        if row is None:
            raise UnknownLane(f"{run_id}:{lane_id}@{plan_revision}")
        return row

    def _active_lanes(self, run_id: str, plan_revision: int) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.conn.execute(
                "SELECT * FROM dag_lanes WHERE run_id=? AND plan_revision=? "
                "ORDER BY lane_id",
                (run_id, plan_revision),
            )
        )

    def lane_stage(self, run_id: str, lane_id: str) -> st.LaneStage:
        row = self.conn.execute(
            "SELECT stage FROM lane_state WHERE run_id=? AND lane_id=?",
            (run_id, lane_id),
        ).fetchone()
        if row is None:
            raise UnknownLane(f"{run_id}:{lane_id}")
        return st.LaneStage(row[0])

    def _cas_stage(
        self,
        run_id: str,
        lane_id: str,
        expected: st.LaneStage,
        next_stage: st.LaneStage,
        now: str,
    ) -> None:
        cursor = self.conn.execute(
            "UPDATE lane_state SET stage=?, updated_at=? "
            "WHERE run_id=? AND lane_id=? AND stage=?",
            (next_stage.value, now, run_id, lane_id, expected.value),
        )
        if cursor.rowcount != 1:
            raise StageCasConflict(f"{run_id}:{lane_id}")

    def _audit(
        self,
        *,
        run_id: str,
        lane_id: Optional[str],
        from_stage: Optional[str],
        to_stage: Optional[str],
        artifact_id: Optional[str],
        reason: str,
        now: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO transitions(run_id, lane_id, from_stage, to_stage, "
            "artifact_id, reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, lane_id, from_stage, to_stage, artifact_id, reason, now),
        )

    def _touch_run(self, run_id: str, now: str) -> None:
        self.conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))

    def _next_lane_sequence(self, run_id: str, lane_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=?",
            (run_id, lane_id),
        ).fetchone()
        return int(row[0]) + 1

    def _next_run_sequence(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM run_artifacts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return int(row[0]) + 1

    def _latest_lane_artifact(
        self,
        run_id: str,
        lane_id: str,
        kind: st.ArtifactKind | None = None,
        *,
        verdict: st.ReviewerVerdict | None = None,
        plan_revision: int | None = None,
        projection_digest: str | None = None,
    ) -> Optional[sqlite3.Row]:
        sql = "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=?"
        params: list[Any] = [run_id, lane_id]
        if kind is not None:
            sql += " AND artifact_kind=?"
            params.append(kind.value)
        sql += " ORDER BY sequence DESC"
        for row in self.conn.execute(sql, params):
            if plan_revision is not None and row["plan_revision"] != plan_revision:
                continue
            if (
                projection_digest is not None
                and row["lane_projection_digest"] != projection_digest
            ):
                continue
            if verdict is None:
                return row
            payload = _loads(row["payload_json"])
            if payload.get("verdict") == verdict.value:
                return row
        return None



    def integration_merge_payloads(
        self, run_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        rows = self.conn.execute(
            "SELECT a.payload_json FROM lane_artifacts AS a "
            "JOIN transitions AS t "
            "ON t.run_id = a.run_id AND t.lane_id = a.lane_id "
            "AND t.artifact_id = a.artifact_id AND t.reason = 'complete_stage' "
            "WHERE a.run_id=? AND a.artifact_kind=? "
            "ORDER BY t.id ASC",
            (run_id, st.ArtifactKind.INTEGRATION_MERGE.value),
        )
        return tuple(_loads(row[0]) for row in rows)


    def _artifact_by_id(self, artifact_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM lane_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM run_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ArtifactStoreError(f"unknown artifact {artifact_id}")
        return row

    def _completion_row(
        self,
        run_id: str,
        lane_id: str,
        plan_revision: int,
        completed_stage: st.LaneStage,
        input_digest: str,
    ) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND plan_revision=? AND completed_stage=? AND input_digest=?",
            (
                run_id,
                lane_id,
                plan_revision,
                completed_stage.value,
                input_digest,
            ),
        ).fetchone()

    def _amendment_payload(self, run_id: str, plan_revision: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT amendment_artifact_id FROM plan_revisions "
            "WHERE run_id=? AND plan_revision=?",
            (run_id, plan_revision),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        artifact = self.conn.execute(
            "SELECT payload_json FROM run_artifacts WHERE run_id=? AND artifact_id=?",
            (run_id, row[0]),
        ).fetchone()
        if artifact is None:
            raise AmendmentRefused("amendment artifact missing")
        return _loads(artifact[0])

    def _require_retained(
        self,
        amendment: Optional[Mapping[str, Any]],
        *,
        lane_id: str,
        plan_revision: int,
        projection_digest: str,
        stage: st.LaneStage,
        input_digest: str,
        artifact_ids: Sequence[str],
    ) -> None:
        if amendment is None:
            raise StaleStageInput("older-revision input without PLAN_AMENDMENT")
        retained = amendment.get("retained_inputs") or []
        for entry in retained:
            if (
                entry.get("lane_id") == lane_id
                and entry.get("plan_revision") == plan_revision
                and entry.get("lane_projection_digest") == projection_digest
                and entry.get("stage") == stage.value
                and entry.get("input_digest") == input_digest
                and list(entry.get("artifact_ids") or []) == list(artifact_ids)
            ):
                return
        raise StaleStageInput(f"{lane_id}:{input_digest}")

    def _lane_ids_ordered(self, run: sqlite3.Row) -> tuple[str, ...]:
        return tuple(
            row["lane_id"]
            for row in self.conn.execute(
                "SELECT lane_id FROM dag_lanes WHERE run_id=? AND plan_revision=? "
                "ORDER BY lane_id",
                (run["run_id"], run["plan_revision"]),
            )
        )

    def _needs(self, projection: sqlite3.Row) -> tuple[str, ...]:
        return tuple(_loads(projection["needs_json"]))

    def _outputs(self, projection: sqlite3.Row) -> tuple[str, ...]:
        return tuple(_loads(projection["declared_outputs_json"]))

    def _kind_matching_digest(self, row: sqlite3.Row) -> str | None:
        needs = tuple(_loads(row["needs_json"]))
        outputs = tuple(_loads(row["declared_outputs_json"]))
        for kind in (None, st.LANE_KIND_TESTS, st.LANE_KIND_BUILD):
            if (
                st.lane_projection_digest(
                    row["spec_digest"], needs, outputs, lane_kind=kind
                )
                == row["lane_projection_digest"]
            ):
                return kind
        raise st.CanonicalIdentityError("lane_projection_digest mismatch")

    def _lane_kind_from_amendment(
        self, run_id: str, lane_id: str, plan_revision: int
    ) -> str | None:
        payload = self._amendment_payload(run_id, plan_revision)
        if not payload:
            return None
        for entry in payload.get("projection") or ():
            if not isinstance(entry, Mapping):
                continue
            if entry.get("lane_id") != lane_id:
                continue
            if "lane_kind" in entry:
                return st.normalize_lane_kind(entry.get("lane_kind"))
        return None

    def _lane_kind(
        self,
        run_id: str,
        lane_id: str,
        artifact: st.LaneArtifact | None = None,
    ) -> str | None:
        if artifact is not None and artifact.kind is st.ArtifactKind.LANE_PLAN:
            return st.normalize_lane_kind(artifact.payload.get("lane_kind"))
        run = self._run(run_id)
        revision = run["plan_revision"]
        plan = self._latest_lane_artifact(
            run_id,
            lane_id,
            st.ArtifactKind.LANE_PLAN,
            plan_revision=revision,
        )
        if plan is not None:
            return st.normalize_lane_kind(_loads(plan["payload_json"]).get("lane_kind"))
        kind = self._lane_kind_from_amendment(run_id, lane_id, revision)
        if kind is not None:
            return kind
        try:
            return self._kind_matching_digest(
                self._projection(run_id, revision, lane_id)
            )
        except UnknownLane:
            return None

    def _current_sealed_bundle(
        self, run_id: str, lane_id: str
    ) -> sqlite3.Row | None:
        revision = self._run(run_id)["plan_revision"]
        return self._latest_lane_artifact(
            run_id,
            lane_id,
            st.ArtifactKind.SEALED_TEST_BUNDLE,
            plan_revision=revision,
        )


    def _sealed_bundle(self, run_id: str, lane_id: str) -> sqlite3.Row | None:
        revision = self._run(run_id)["plan_revision"]
        if self._lane_kind(run_id, lane_id) == st.LANE_KIND_BUILD:
            needs = self._needs(self._projection(run_id, revision, lane_id))
            for dep in needs:
                if self._lane_kind(run_id, dep) == st.LANE_KIND_TESTS:
                    return self._current_sealed_bundle(run_id, dep)
            return None
        return self._current_sealed_bundle(run_id, lane_id)

    def _dependency_receipts(
        self, run_id: str, needs: Sequence[str]
    ) -> list[sqlite3.Row]:
        receipts = []
        revision = self._run(run_id)["plan_revision"]
        for dep in needs:
            if self.lane_stage(run_id, dep) is not st.LaneStage.MERGED:
                raise st.IllegalStageEdge(f"needs {dep} is not MERGED")
            if self._lane_kind(run_id, dep) == st.LANE_KIND_TESTS:
                row = self._current_sealed_bundle(run_id, dep)
                if row is None:
                    raise st.IllegalStageEdge(
                        f"needs {dep} has no SEALED_TEST_BUNDLE"
                    )
            else:
                row = self._latest_lane_artifact(
                    run_id,
                    dep,
                    st.ArtifactKind.INTEGRATION_MERGE,
                    plan_revision=revision,
                )
                if row is None:
                    raise st.IllegalStageEdge(f"needs {dep} has no INTEGRATION_MERGE")
            receipts.append(row)
        return receipts




    def _reconstruct_stage_digest(
        self,
        run: sqlite3.Row,
        projection: sqlite3.Row,
        stage: st.LaneStage,
        artifact: Optional[st.LaneArtifact] = None,
        observed: Optional[Mapping[str, Any]] = None,
    ) -> str:
        run_id = run["run_id"]
        lane_id = projection["lane_id"]
        payload = (
            dict(artifact.payload) if artifact is not None else dict(observed or {})
        )
        common = dict(
            run_id=run_id,
            lane_id=lane_id,
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=projection["spec_digest"],
            projection_digest=projection["lane_projection_digest"],
        )
        if stage is st.LaneStage.PLANNED:
            return st.planned_input_digest(
                **common,
                plan_artifact_ref=self.conn.execute(
                    "SELECT plan_artifact_ref FROM plan_revisions "
                    "WHERE run_id=? AND plan_revision=?",
                    (run_id, run["plan_revision"]),
                ).fetchone()[0],
                needs=self._needs(projection),
                declared_outputs=self._outputs(projection),
            )
        if stage is st.LaneStage.WRITING_TESTS:
            plan = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.LANE_PLAN
            )
            if plan is None:
                raise StaleStageInput("missing LANE_PLAN")
            review = self._latest_lane_artifact(
                run_id,
                lane_id,
                st.ArtifactKind.TEST_REVIEW,
                verdict=st.ReviewerVerdict.REVISE,
            )
            review_id = (
                review["artifact_id"] if review is not None else st.NO_TEST_REVIEW
            )
            invalidation = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.TEST_INVALIDATION
            )
            draft = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.TEST_DRAFT
            )
            invalidation_id = st.active_test_invalidation_id(
                invalidation_id=(
                    invalidation["artifact_id"] if invalidation is not None else None
                ),
                invalidation_sequence=(
                    int(invalidation["sequence"]) if invalidation is not None else None
                ),
                draft_sequence=int(draft["sequence"]) if draft is not None else None,
            )
            merges = self.integration_merge_payloads(run_id)
            return st.writing_tests_input_digest(
                **common,
                lane_plan_id=plan["artifact_id"],
                test_review_id=review_id,
                integration_head=gitpub.durable_integration_tip(
                    run["integration_initial_sha"], merges
                ),
                test_invalidation_id=invalidation_id,
            )

        if stage is st.LaneStage.REVIEWING_TESTS:
            plan = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.LANE_PLAN
            )
            draft = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.TEST_DRAFT
            )
            if plan is None or draft is None:
                raise StaleStageInput("missing LANE_PLAN or TEST_DRAFT")
            return st.reviewing_tests_input_digest(
                **common,
                lane_plan_id=plan["artifact_id"],
                test_draft_id=draft["artifact_id"],
            )
        if stage is st.LaneStage.TESTS_SEALED:
            plan = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.LANE_PLAN
            )
            draft = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.TEST_DRAFT
            )
            review = self._latest_lane_artifact(
                run_id,
                lane_id,
                st.ArtifactKind.TEST_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            if plan is None or draft is None or review is None:
                raise StaleStageInput("missing sealed-test inputs")
            return st.tests_sealed_input_digest(
                **common,
                lane_plan_id=plan["artifact_id"],
                test_draft_id=draft["artifact_id"],
                test_review_id=review["artifact_id"],
            )
        if stage is st.LaneStage.BUILDING:
            return self._building_digest(run, projection, payload)
        if stage is st.LaneStage.REVIEWING_CODE:
            plan = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.LANE_PLAN
            )
            sealed = self._sealed_bundle(run_id, lane_id)
            builder = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            if plan is None or sealed is None or builder is None:
                raise StaleStageInput("missing REVIEWING_CODE inputs")
            builder_payload = _loads(builder["payload_json"])
            return st.reviewing_code_input_digest(
                **common,
                lane_plan_id=plan["artifact_id"],
                sealed_bundle_id=sealed["artifact_id"],
                builder_output_id=builder["artifact_id"],
                builder_base_sha=builder_payload["builder_base_sha"],
                candidate_ref=builder_payload["candidate_ref"],
                candidate_sha=builder_payload["candidate_sha"],
            )
        if stage is st.LaneStage.READY_TO_MERGE:
            builder = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = self._latest_lane_artifact(
                run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            if builder is None or review is None:
                raise StaleStageInput("missing READY_TO_MERGE inputs")
            builder_payload = _loads(builder["payload_json"])
            if (
                artifact is not None
                and artifact.kind is st.ArtifactKind.BASE_INVALIDATION
            ):
                return st.base_invalidation_input_digest(
                    **common,
                    builder_output_id=builder["artifact_id"],
                    code_review_id=review["artifact_id"],
                    stale_builder_base_sha=builder_payload["builder_base_sha"],
                    stale_candidate_sha=builder_payload["candidate_sha"],
                    integration_head=payload["integration_head"],
                )
            return st.ready_to_merge_input_digest(
                **common,
                builder_output_id=builder["artifact_id"],
                code_review_id=review["artifact_id"],
                builder_base_sha=builder_payload["builder_base_sha"],
                candidate_ref=builder_payload["candidate_ref"],
                candidate_sha=builder_payload["candidate_sha"],
                integration_head=payload["integration_head"],
            )
        raise st.IllegalStageEdge(f"no input reconstruction for {stage.value}")

    def _building_digest(
        self, run: sqlite3.Row, projection: sqlite3.Row, payload: Mapping[str, Any]
    ) -> str:
        run_id = run["run_id"]
        lane_id = projection["lane_id"]
        plan = self._latest_lane_artifact(run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        sealed = self._sealed_bundle(run_id, lane_id)
        if plan is None or sealed is None:
            raise StaleStageInput("missing BUILDING plan/sealed bundle")
        receipts = self._dependency_receipts(run_id, self._needs(projection))
        entry = st.BuildingEntryKind(payload["entry_kind"])
        builder_base_sha = st.require_git_sha(
            payload["builder_base_sha"], name="builder_base_sha"
        )
        ids = [plan["artifact_id"], sealed["artifact_id"]]
        ids.extend(
            row["artifact_id"]
            for row in receipts
            if row["artifact_id"] not in ids
        )
        if entry is st.BuildingEntryKind.INITIAL:
            prior_builder = st.NO_PRIOR_BUILDER
            code_review = st.NO_CODE_REVIEW
            base_invalidation = st.NO_BASE_INVALIDATION
        elif entry is st.BuildingEntryKind.CODE_REVISE:
            prior = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = self._latest_lane_artifact(
                run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.REVISE,
            )
            if prior is None or review is None:
                raise StaleStageInput("CODE_REVISE missing prior artifacts")
            prior_builder = prior["artifact_id"]
            code_review = review["artifact_id"]
            base_invalidation = st.NO_BASE_INVALIDATION
            ids.extend([prior_builder, code_review])
        else:
            invalidation = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.BASE_INVALIDATION
            )
            prior = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = self._latest_lane_artifact(
                run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            if invalidation is None or prior is None or review is None:
                raise StaleStageInput("BASE_INVALIDATION variant missing artifacts")
            prior_builder = prior["artifact_id"]
            code_review = review["artifact_id"]
            base_invalidation = invalidation["artifact_id"]
            ids.extend([prior_builder, code_review, base_invalidation])
        return st.building_input_digest(
            run_id=run_id,
            lane_id=lane_id,
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=projection["spec_digest"],
            projection_digest=projection["lane_projection_digest"],
            input_artifact_ids=ids,
            entry_kind=entry,
            builder_base_sha=builder_base_sha,
            prior_builder=prior_builder,
            code_review=code_review,
            base_invalidation=base_invalidation,
        )

    def _lane_envelope_id(
        self,
        run_id: str,
        lane_id: str,
        artifact: st.LaneArtifact,
        completed_stage: st.LaneStage,
        payload: Mapping[str, Any],
    ) -> str:
        expected_output = st.digest_canonical(payload)
        if artifact.output_digest != expected_output:
            raise st.CanonicalIdentityError("output_digest mismatch")
        return st.digest_canonical(
            {
                "artifact_kind": artifact.kind.value,
                "artifact_ref": artifact.artifact_ref,
                "completed_stage": completed_stage.value,
                "input_digest": artifact.input_digest,
                "lane_id": lane_id,
                "lane_projection_digest": artifact.lane_projection_digest,
                "output_digest": artifact.output_digest,
                "payload": payload,
                "plan_revision": artifact.plan_revision,
                "run_id": run_id,
                "schema_version": st.CANONICAL_SCHEMA_VERSION,
                "spec_digest": artifact.spec_digest,
            }
        )

    def _run_envelope_id(
        self, run_id: str, artifact: st.RunArtifact, payload: Mapping[str, Any]
    ) -> str:
        expected_output = st.digest_canonical(payload)
        if artifact.output_digest != expected_output:
            raise st.CanonicalIdentityError("output_digest mismatch")
        return st.digest_canonical(
            {
                "artifact_kind": artifact.kind.value,
                "artifact_ref": artifact.artifact_ref,
                "input_digest": artifact.input_digest,
                "output_digest": artifact.output_digest,
                "payload": payload,
                "plan_revision": artifact.plan_revision,
                "run_id": run_id,
                "schema_version": st.CANONICAL_SCHEMA_VERSION,
            }
        )

    def _insert_lane_artifact(
        self,
        *,
        run_id: str,
        lane_id: str,
        artifact: st.LaneArtifact,
        completed_stage: st.LaneStage,
        now: str,
    ) -> ArtifactRecord:
        payload = st.json_ready(artifact.payload)
        artifact_id = self._lane_envelope_id(
            run_id, lane_id, artifact, completed_stage, payload
        )
        existing = self._completion_row(
            run_id,
            lane_id,
            artifact.plan_revision,
            completed_stage,
            artifact.input_digest,
        )
        if existing is not None:
            if existing["artifact_id"] != artifact_id:
                raise ArtifactCollision(artifact.input_digest)
            return ArtifactRecord(
                artifact_id=existing["artifact_id"],
                run_id=run_id,
                lane_id=lane_id,
                sequence=existing["sequence"],
                kind=st.ArtifactKind(existing["artifact_kind"]),
                plan_revision=existing["plan_revision"],
                input_digest=existing["input_digest"],
                output_digest=existing["output_digest"],
                artifact_ref=existing["artifact_ref"],
                payload=_loads(existing["payload_json"]),
                replayed=True,
            )
        sequence = self._next_lane_sequence(run_id, lane_id)
        try:
            self.conn.execute(
                "INSERT INTO lane_artifacts(artifact_id, run_id, lane_id, sequence, "
                "completed_stage, artifact_kind, plan_revision, spec_digest, "
                "lane_projection_digest, input_digest, output_digest, artifact_ref, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    run_id,
                    lane_id,
                    sequence,
                    completed_stage.value,
                    artifact.kind.value,
                    artifact.plan_revision,
                    artifact.spec_digest,
                    artifact.lane_projection_digest,
                    artifact.input_digest,
                    artifact.output_digest,
                    artifact.artifact_ref,
                    _dumps(payload),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ArtifactCollision(artifact.input_digest) from exc
        return ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            lane_id=lane_id,
            sequence=sequence,
            kind=artifact.kind,
            plan_revision=artifact.plan_revision,
            input_digest=artifact.input_digest,
            output_digest=artifact.output_digest,
            artifact_ref=artifact.artifact_ref,
            payload=payload,
            replayed=False,
        )

    def _insert_run_artifact(
        self, run_id: str, artifact: st.RunArtifact, now: str
    ) -> ArtifactRecord:
        payload = st.json_ready(artifact.payload)
        artifact_id = self._run_envelope_id(run_id, artifact, payload)
        existing = self.conn.execute(
            "SELECT * FROM run_artifacts WHERE run_id=? AND artifact_kind=? "
            "AND input_digest=?",
            (run_id, artifact.kind.value, artifact.input_digest),
        ).fetchone()
        if existing is not None:
            if existing["artifact_id"] != artifact_id:
                raise ArtifactCollision(artifact.input_digest)
            return ArtifactRecord(
                artifact_id=existing["artifact_id"],
                run_id=run_id,
                lane_id=None,
                sequence=existing["sequence"],
                kind=st.ArtifactKind(existing["artifact_kind"]),
                plan_revision=existing["plan_revision"],
                input_digest=existing["input_digest"],
                output_digest=existing["output_digest"],
                artifact_ref=existing["artifact_ref"],
                payload=_loads(existing["payload_json"]),
                replayed=True,
            )
        sequence = self._next_run_sequence(run_id)
        try:
            self.conn.execute(
                "INSERT INTO run_artifacts(artifact_id, run_id, sequence, "
                "artifact_kind, plan_revision, input_digest, output_digest, "
                "artifact_ref, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    run_id,
                    sequence,
                    artifact.kind.value,
                    artifact.plan_revision,
                    artifact.input_digest,
                    artifact.output_digest,
                    artifact.artifact_ref,
                    _dumps(payload),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ArtifactCollision(artifact.input_digest) from exc
        return ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            lane_id=None,
            sequence=sequence,
            kind=artifact.kind,
            plan_revision=artifact.plan_revision,
            input_digest=artifact.input_digest,
            output_digest=artifact.output_digest,
            artifact_ref=artifact.artifact_ref,
            payload=payload,
            replayed=False,
        )

    def _insert_projection(
        self, run_id: str, plan: st.CompiledPlan, now: str | None = None
    ) -> None:
        del now
        for lane in plan.lanes:
            self.conn.execute(
                "INSERT INTO dag_lanes(run_id, plan_revision, lane_id, needs_json, "
                "spec_digest, declared_outputs_json, lane_projection_digest, "
                "public_acceptance_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    plan.plan_revision,
                    lane.lane_id,
                    _dumps(list(lane.needs)),
                    lane.spec_digest,
                    _dumps(list(lane.declared_outputs)),
                    lane.lane_projection_digest,
                    _dumps(list(lane.public_acceptance)),
                ),
            )


    @serialized
    def create_run(
        self,
        run_id: str,
        compiled_plan: st.CompiledPlan,
        binding: st.RunBinding,
    ) -> None:
        if compiled_plan.plan_revision != 1:
            raise st.CanonicalIdentityError("initial plan_revision must be 1")
        if binding.integration_ref != st.integration_ref(run_id):
            raise st.CanonicalIdentityError("integration_ref")
        now = now_iso()
        self._begin()
        try:
            existing = self.conn.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                raise RunAlreadyExists(run_id)
            self.conn.execute(
                "INSERT INTO runs(run_id, runtime_state_root, "
                "runtime_state_fingerprint, plan_digest, plan_revision, "
                "integration_ref, integration_initial_sha, target_repository_root, "
                "target_git_common_dir, target_worktree_git_dir, target_object_format, "
                "target_repository_fingerprint, target_sync_journal_fingerprint, "
                "target_initial_main_sha, target_main_ref, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    binding.runtime_state_root,
                    binding.runtime_state_fingerprint,
                    compiled_plan.plan_digest,
                    compiled_plan.plan_revision,
                    binding.integration_ref,
                    binding.integration_initial_sha,
                    binding.target_repository_root,
                    binding.target_git_common_dir,
                    binding.target_worktree_git_dir,
                    binding.target_object_format,
                    binding.target_repository_fingerprint,
                    binding.target_sync_journal_fingerprint,
                    binding.target_initial_main_sha,
                    binding.target_main_ref,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "INSERT INTO plan_revisions(run_id, plan_revision, plan_digest, "
                "parent_revision, plan_artifact_ref, amendment_artifact_id, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    compiled_plan.plan_revision,
                    compiled_plan.plan_digest,
                    None,
                    compiled_plan.plan_artifact_ref,
                    None,
                    now,
                ),
            )
            self._insert_projection(run_id, compiled_plan)
            for lane in compiled_plan.lanes:
                self.conn.execute(
                    "INSERT INTO lane_state(run_id, lane_id, stage, updated_at) "
                    "VALUES (?,?,?,?)",
                    (run_id, lane.lane_id, st.LaneStage.PLANNED.value, now),
                )
            self._audit(
                run_id=run_id,
                lane_id=None,
                from_stage=None,
                to_stage=st.LaneStage.PLANNED.value,
                artifact_id=None,
                reason="create_run",
                now=now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def retarget_integration_initial_sha(
        self, run_id: str, expected_sha: str, new_sha: str
    ) -> None:
        now = now_iso()
        self._begin()
        try:
            cursor = self.conn.execute(
                "UPDATE runs SET integration_initial_sha=?, updated_at=? "
                "WHERE run_id=? AND integration_initial_sha=?",
                (new_sha, now, run_id, expected_sha),
            )
            if cursor.rowcount != 1:
                raise StageCasConflict(f"{run_id}:integration_initial_sha")
            self._audit(
                run_id=run_id,
                lane_id=None,
                from_stage=None,
                to_stage=None,
                artifact_id=None,
                reason="legacy_integration_retarget",
                now=now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise


    def _validate_projection_match(
        self, artifact: st.LaneArtifact, projection: sqlite3.Row
    ) -> None:
        if artifact.spec_digest != projection["spec_digest"]:
            raise StaleStageInput("spec_digest")
        if artifact.lane_projection_digest != projection["lane_projection_digest"]:
            raise StaleStageInput("lane_projection_digest")

    @serialized
    def complete_stage(
        self,
        run_id: str,
        lane_id: str,
        expected_stage: st.LaneStage,
        expected_input_digest: str,
        artifact: st.LaneArtifact,
        next_stage: st.LaneStage,
    ) -> ArtifactRecord:
        now = now_iso()
        self._begin()
        try:
            run = self._run(run_id)
            projection = self._projection(run_id, run["plan_revision"], lane_id)
            current = self.lane_stage(run_id, lane_id)
            completed = st.completed_stage_for(artifact.kind, artifact.payload)
            self._validate_projection_match(artifact, projection)
            if artifact.input_digest != expected_input_digest:
                raise StaleStageInput("artifact input_digest")
            if current is next_stage:
                payload = st.json_ready(artifact.payload)
                artifact_id = self._lane_envelope_id(
                    run_id, lane_id, artifact, completed, payload
                )
                existing = self._completion_row(
                    run_id,
                    lane_id,
                    artifact.plan_revision,
                    completed,
                    artifact.input_digest,
                )
                if existing is None:
                    raise StageCasConflict("stage already advanced")
                if existing["artifact_id"] != artifact_id:
                    raise ArtifactCollision(artifact.input_digest)
                self.conn.execute("COMMIT")
                return ArtifactRecord(
                    artifact_id=existing["artifact_id"],
                    run_id=run_id,
                    lane_id=lane_id,
                    sequence=existing["sequence"],
                    kind=st.ArtifactKind(existing["artifact_kind"]),
                    plan_revision=existing["plan_revision"],
                    input_digest=existing["input_digest"],
                    output_digest=existing["output_digest"],
                    artifact_ref=existing["artifact_ref"],
                    payload=_loads(existing["payload_json"]),
                    replayed=True,
                )
            if current is not expected_stage:
                raise StageCasConflict(f"{current.value} != {expected_stage.value}")
            reconstructed = self._reconstruct_stage_digest(
                run, projection, expected_stage, artifact
            )
            if reconstructed != expected_input_digest:
                raise StaleStageInput("expected_input_digest")
            if artifact.plan_revision != run["plan_revision"]:
                amendment = self._amendment_payload(run_id, run["plan_revision"])
                self._require_retained(
                    amendment,
                    lane_id=lane_id,
                    plan_revision=artifact.plan_revision,
                    projection_digest=artifact.lane_projection_digest,
                    stage=expected_stage,
                    input_digest=expected_input_digest,
                    artifact_ids=list(artifact.payload.get("input_artifact_ids") or []),
                )
            legal_next = st.next_stage_for(
                expected_stage,
                artifact.kind,
                artifact.verdict,
                lane_kind=self._lane_kind(run_id, lane_id, artifact),
            )
            if legal_next is not next_stage:
                raise st.IllegalStageEdge(f"{expected_stage.value}->{next_stage.value}")
            record = self._insert_lane_artifact(
                run_id=run_id,
                lane_id=lane_id,
                artifact=artifact,
                completed_stage=completed,
                now=now,
            )
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            self._cas_stage(run_id, lane_id, expected_stage, next_stage, now)
            self._audit(
                run_id=run_id,
                lane_id=lane_id,
                from_stage=expected_stage.value,
                to_stage=next_stage.value,
                artifact_id=record.artifact_id,
                reason="complete_stage",
                now=now,
            )
            self._touch_run(run_id, now)
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _predecessor(self, run_id: str, lane_id: str) -> tuple[str, int]:
        row = self.conn.execute(
            "SELECT artifact_id, sequence FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? ORDER BY sequence DESC LIMIT 1",
            (run_id, lane_id),
        ).fetchone()
        if row is None:
            return st.NO_PREDECESSOR, 0
        return row[0], int(row[1])

    def _pause_predecessor(self, run_id: str, lane_id: str) -> tuple[str, int]:
        row = self.conn.execute(
            "SELECT artifact_id, sequence FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind!=? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id, lane_id, st.ArtifactKind.USER_WAIT.value),
        ).fetchone()
        if row is None:
            return st.NO_PREDECESSOR, 0
        return row[0], int(row[1])

    @serialized
    def pause_lane(
        self,
        run_id: str,
        lane_id: str,
        expected_stage: st.LaneStage,
        expected_input_digest: str,
        *,
        observed: Optional[Mapping[str, Any]] = None,
        reason: st.WaitReason = st.WaitReason.PAUSE,
    ) -> ArtifactRecord:
        if expected_stage not in st.PAUSEABLE_STAGES:
            raise st.IllegalStageEdge(expected_stage.value)
        if reason not in st.RESUMABLE_WAIT_REASONS:
            raise st.IllegalStageEdge(reason.value)
        now = now_iso()
        self._begin()
        try:
            run = self._run(run_id)
            projection = self._projection(run_id, run["plan_revision"], lane_id)
            current = self.lane_stage(run_id, lane_id)
            reconstructed = self._reconstruct_stage_digest(
                run, projection, expected_stage, observed=observed
            )
            if reconstructed != expected_input_digest:
                raise StaleStageInput("pause input")
            predecessor_id, predecessor_seq = self._pause_predecessor(run_id, lane_id)
            wait_digest = st.user_wait_input_digest(
                predecessor_artifact_id=predecessor_id,
                predecessor_sequence=predecessor_seq,
                wait_reason=reason,
                resume_stage=expected_stage,
                resume_input_digest=expected_input_digest,
                run_id=run_id,
                lane_id=lane_id,
                plan_revision=run["plan_revision"],
            )
            payload = {
                "input_digest": wait_digest,
                "predecessor_artifact_id": predecessor_id,
                "predecessor_sequence": predecessor_seq,
                "resume_input_digest": expected_input_digest,
                "resume_stage": expected_stage.value,
                "wait_reason": reason.value,
            }
            artifact = st.LaneArtifact(
                kind=st.ArtifactKind.USER_WAIT,
                plan_revision=run["plan_revision"],
                spec_digest=projection["spec_digest"],
                lane_projection_digest=projection["lane_projection_digest"],
                input_digest=wait_digest,
                output_digest=st.digest_canonical(payload),
                artifact_ref=f"user-wait:{run_id}:{lane_id}:{wait_digest}",
                payload=payload,
            )
            if current is st.LaneStage.WAITING_FOR_USER:
                record = self._insert_lane_artifact(
                    run_id=run_id,
                    lane_id=lane_id,
                    artifact=artifact,
                    completed_stage=expected_stage,
                    now=now,
                )
                if not record.replayed:
                    raise StageCasConflict("waiting with different pause")
                self.conn.execute("COMMIT")
                return record
            if current is not expected_stage:
                raise StageCasConflict(current.value)
            record = self._insert_lane_artifact(
                run_id=run_id,
                lane_id=lane_id,
                artifact=artifact,
                completed_stage=expected_stage,
                now=now,
            )
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            self._cas_stage(
                run_id, lane_id, expected_stage, st.LaneStage.WAITING_FOR_USER, now
            )
            self._audit(
                run_id=run_id,
                lane_id=lane_id,
                from_stage=expected_stage.value,
                to_stage=st.LaneStage.WAITING_FOR_USER.value,
                artifact_id=record.artifact_id,
                reason="pause_lane",
                now=now,
            )
            self._touch_run(run_id, now)
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @serialized
    def resume_lane(
        self,
        run_id: str,
        lane_id: str,
        decision_payload: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        now = now_iso()
        decision: dict[str, Any] = st.json_ready(
            dict(decision_payload or {"action": "RESUME"})
        )
        self._begin()
        try:
            run = self._run(run_id)
            projection = self._projection(run_id, run["plan_revision"], lane_id)
            current = self.lane_stage(run_id, lane_id)
            wait = self._latest_lane_artifact(
                run_id, lane_id, st.ArtifactKind.USER_WAIT
            )
            if wait is None:
                raise ResumeBlocked("no USER_WAIT")
            wait_payload = _loads(wait["payload_json"])
            if (
                wait_payload.get("wait_reason")
                == st.WaitReason.AMENDMENT_REQUIRED.value
            ):
                if current is not st.LaneStage.WAITING_FOR_USER:
                    raise StageCasConflict(current.value)
                self.conn.execute("COMMIT")
                return ArtifactRecord(
                    artifact_id=wait["artifact_id"],
                    run_id=run_id,
                    lane_id=lane_id,
                    sequence=wait["sequence"],
                    kind=st.ArtifactKind.USER_WAIT,
                    plan_revision=wait["plan_revision"],
                    input_digest=wait["input_digest"],
                    output_digest=wait["output_digest"],
                    artifact_ref=wait["artifact_ref"],
                    payload=wait_payload,
                    replayed=True,
                )
            resume_stage = st.LaneStage(wait_payload["resume_stage"])
            if wait_payload.get("input_invalidated"):
                observed = decision.get("observed") or {}
                resume_input = self._reconstruct_stage_digest(
                    run, projection, resume_stage, observed=observed
                )
            else:
                resume_input = wait_payload["resume_input_digest"]
            decision_digest = st.user_decision_input_digest(
                user_wait_artifact_id=wait["artifact_id"],
                action=str(decision.get("action", "RESUME")),
                decision_payload=decision,
            )
            payload = {
                "action": decision.get("action", "RESUME"),
                "decision_payload": decision,
                "input_digest": decision_digest,
                "resume_input_digest": resume_input,
                "resume_stage": resume_stage.value,
                "user_wait_artifact_id": wait["artifact_id"],
            }
            artifact = st.LaneArtifact(
                kind=st.ArtifactKind.USER_DECISION,
                plan_revision=run["plan_revision"],
                spec_digest=projection["spec_digest"],
                lane_projection_digest=projection["lane_projection_digest"],
                input_digest=decision_digest,
                output_digest=st.digest_canonical(payload),
                artifact_ref=f"user-decision:{wait['artifact_id']}",
                payload=payload,
            )
            if current is resume_stage:
                record = self._insert_lane_artifact(
                    run_id=run_id,
                    lane_id=lane_id,
                    artifact=artifact,
                    completed_stage=st.LaneStage.WAITING_FOR_USER,
                    now=now,
                )
                if not record.replayed:
                    raise StageCasConflict("already resumed with different decision")
                self.conn.execute("COMMIT")
                return record
            if current is not st.LaneStage.WAITING_FOR_USER:
                raise StageCasConflict(current.value)
            record = self._insert_lane_artifact(
                run_id=run_id,
                lane_id=lane_id,
                artifact=artifact,
                completed_stage=st.LaneStage.WAITING_FOR_USER,
                now=now,
            )
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            self._cas_stage(
                run_id, lane_id, st.LaneStage.WAITING_FOR_USER, resume_stage, now
            )
            self._audit(
                run_id=run_id,
                lane_id=lane_id,
                from_stage=st.LaneStage.WAITING_FOR_USER.value,
                to_stage=resume_stage.value,
                artifact_id=record.artifact_id,
                reason="resume_lane",
                now=now,
            )
            self._touch_run(run_id, now)
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _final_review_lanes(self, run: sqlite3.Row, integration_sha: str) -> list[dict]:
        rows = []
        for lane in self._active_lanes(run["run_id"], run["plan_revision"]):
            plan = self._latest_lane_artifact(
                run["run_id"], lane["lane_id"], st.ArtifactKind.LANE_PLAN
            )
            sealed = self._sealed_bundle(run["run_id"], lane["lane_id"])
            if plan is None or sealed is None:
                raise st.IllegalStageEdge("final review missing contracts")
            rows.append(
                {
                    "lane_id": lane["lane_id"],
                    "public_contract_artifact_id": plan["artifact_id"],
                    "sealed_test_bundle_artifact_id": sealed["artifact_id"],
                    "spec_digest": lane["spec_digest"],
                }
            )
        rows.sort(key=lambda item: item["lane_id"])
        del integration_sha
        return rows

    def active_final_review_fingerprint(self, run_id: str, integration_sha: str) -> str:
        run = self._run(run_id)
        return st.final_review_input_fingerprint(
            integration_sha=st.require_git_sha(integration_sha, name="integration_sha"),
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            lanes=self._final_review_lanes(run, integration_sha),
        )

    def _all_merged(self, run_id: str) -> bool:
        stages = [
            st.LaneStage(row[0])
            for row in self.conn.execute(
                "SELECT stage FROM lane_state WHERE run_id=?", (run_id,)
            )
        ]
        return bool(stages) and all(stage is st.LaneStage.MERGED for stage in stages)

    @serialized
    def complete_final_review(
        self,
        run_id: str,
        review_input_fingerprint: str,
        integration_sha: str,
        observed_target_main_sha: str,
        artifact: st.RunArtifact,
        affected_lanes: Sequence[str],
    ) -> ArtifactRecord:
        now = now_iso()
        self._begin()
        try:
            run = self._run(run_id)
            if not self._all_merged(run_id):
                raise st.IllegalStageEdge("lanes are not all MERGED")
            fingerprint = st.final_review_input_fingerprint(
                integration_sha=st.require_git_sha(
                    integration_sha, name="integration_sha"
                ),
                plan_revision=run["plan_revision"],
                plan_digest=run["plan_digest"],
                lanes=self._final_review_lanes(run, integration_sha),
            )
            if fingerprint != review_input_fingerprint:
                raise StaleStageInput("final-review fingerprint")
            if artifact.kind is not st.ArtifactKind.FINAL_INTEGRATION_REVIEW:
                raise st.CanonicalIdentityError("FINAL_INTEGRATION_REVIEW required")
            if artifact.input_digest != fingerprint:
                raise StaleStageInput("artifact fingerprint")
            if artifact.payload.get("integration_sha") != integration_sha:
                raise st.CanonicalIdentityError("integration_sha")
            if (
                artifact.payload.get("observed_target_main_sha")
                != observed_target_main_sha
            ):
                raise st.CanonicalIdentityError("observed_target_main_sha")
            unique_lanes = tuple(dict.fromkeys(affected_lanes))
            if artifact.verdict is st.ReviewerVerdict.PASS:
                if unique_lanes:
                    raise st.IllegalStageEdge("PASS names no affected lanes")
            else:
                if not unique_lanes:
                    raise st.IllegalStageEdge("REVISE requires affected lanes")
                active = set(self._lane_ids_ordered(run))
                for lane_id in unique_lanes:
                    if lane_id not in active:
                        raise UnknownLane(lane_id)
                    if self.lane_stage(run_id, lane_id) is not st.LaneStage.MERGED:
                        raise st.IllegalStageEdge(f"{lane_id} is not MERGED")
            record = self._insert_run_artifact(run_id, artifact, now)
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            if artifact.verdict is st.ReviewerVerdict.REVISE:
                for lane_id in unique_lanes:
                    projection = self._projection(run_id, run["plan_revision"], lane_id)
                    predecessor_id, predecessor_seq = self._predecessor(run_id, lane_id)
                    wait_digest = st.user_wait_input_digest(
                        predecessor_artifact_id=predecessor_id,
                        predecessor_sequence=predecessor_seq,
                        wait_reason=st.WaitReason.AMENDMENT_REQUIRED,
                        resume_stage=st.LaneStage.MERGED,
                        resume_input_digest=fingerprint,
                        run_id=run_id,
                        lane_id=lane_id,
                        plan_revision=run["plan_revision"],
                    )
                    payload = {
                        "final_review_artifact_id": record.artifact_id,
                        "input_digest": wait_digest,
                        "predecessor_artifact_id": predecessor_id,
                        "predecessor_sequence": predecessor_seq,
                        "resume_input_digest": fingerprint,
                        "resume_stage": st.LaneStage.MERGED.value,
                        "wait_reason": st.WaitReason.AMENDMENT_REQUIRED.value,
                    }
                    wait_artifact = st.LaneArtifact(
                        kind=st.ArtifactKind.USER_WAIT,
                        plan_revision=run["plan_revision"],
                        spec_digest=projection["spec_digest"],
                        lane_projection_digest=projection["lane_projection_digest"],
                        input_digest=wait_digest,
                        output_digest=st.digest_canonical(payload),
                        artifact_ref=f"amendment-wait:{record.artifact_id}:{lane_id}",
                        payload=payload,
                    )
                    inserted = self._insert_lane_artifact(
                        run_id=run_id,
                        lane_id=lane_id,
                        artifact=wait_artifact,
                        completed_stage=st.LaneStage.MERGED,
                        now=now,
                    )
                    self._cas_stage(
                        run_id,
                        lane_id,
                        st.LaneStage.MERGED,
                        st.LaneStage.WAITING_FOR_USER,
                        now,
                    )
                    self._audit(
                        run_id=run_id,
                        lane_id=lane_id,
                        from_stage=st.LaneStage.MERGED.value,
                        to_stage=st.LaneStage.WAITING_FOR_USER.value,
                        artifact_id=inserted.artifact_id,
                        reason="complete_final_review",
                        now=now,
                    )
            self._audit(
                run_id=run_id,
                lane_id=None,
                from_stage=None,
                to_stage=None,
                artifact_id=record.artifact_id,
                reason="complete_final_review",
                now=now,
            )
            self._touch_run(run_id, now)
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _has_publication(self, run_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM run_artifacts WHERE run_id=? AND artifact_kind=?",
            (run_id, st.ArtifactKind.MAIN_PUBLICATION.value),
        ).fetchone()
        return row is not None

    def _active_final_review(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM run_artifacts WHERE run_id=? AND artifact_kind=? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW.value),
        ).fetchone()

    def _wait_reason(self, run_id: str, lane_id: str) -> Optional[st.WaitReason]:
        wait = self._latest_lane_artifact(run_id, lane_id, st.ArtifactKind.USER_WAIT)
        if wait is None:
            return None
        decision = self._latest_lane_artifact(
            run_id, lane_id, st.ArtifactKind.USER_DECISION
        )
        if decision is not None and decision["sequence"] > wait["sequence"]:
            return None
        payload = _loads(wait["payload_json"])
        return st.WaitReason(payload["wait_reason"])

    @serialized
    def apply_amendment(
        self,
        run_id: str,
        expected_plan_revision: int,
        amended_projection: st.CompiledPlan,
        artifact: st.RunArtifact,
        resets: Sequence[st.LaneReset],
    ) -> ArtifactRecord:
        now = now_iso()
        self._begin()
        try:
            run = self._run(run_id)
            if self._has_publication(run_id):
                raise AmendmentRefused("published runs are immutable")
            if run["plan_revision"] != expected_plan_revision:
                raise AmendmentRefused("expected_plan_revision")
            if amended_projection.plan_revision != expected_plan_revision + 1:
                raise AmendmentRefused("new plan_revision")
            if artifact.kind is not st.ArtifactKind.PLAN_AMENDMENT:
                raise st.CanonicalIdentityError("PLAN_AMENDMENT required")
            old_lanes = {
                row["lane_id"]: row
                for row in self._active_lanes(run_id, expected_plan_revision)
            }
            new_lanes = {lane.lane_id: lane for lane in amended_projection.lanes}
            removed = set(old_lanes) - set(new_lanes)
            if removed:
                raise AmendmentRefused("removing a lane is refused")
            named = []
            review = self._active_final_review(run_id)
            review_payload = _loads(review["payload_json"]) if review else None
            if (
                review_payload
                and review_payload.get("verdict") == st.ReviewerVerdict.REVISE.value
            ):
                named = list(review_payload.get("affected_lanes") or [])
            named_set = set(named)
            changed = {}
            for lane_id, new_lane in new_lanes.items():
                old = old_lanes.get(lane_id)
                if old is None:
                    changed[lane_id] = True
                    continue
                topology_changed = _loads(old["needs_json"]) != list(
                    new_lane.needs
                ) or _loads(old["declared_outputs_json"]) != list(
                    new_lane.declared_outputs
                )
                stage = self.lane_stage(run_id, lane_id)
                wait_reason = (
                    self._wait_reason(run_id, lane_id)
                    if stage is st.LaneStage.WAITING_FOR_USER
                    else None
                )
                merged_for_topology = stage is st.LaneStage.MERGED or (
                    lane_id in named_set
                    and wait_reason is st.WaitReason.AMENDMENT_REQUIRED
                )
                if merged_for_topology and topology_changed:
                    raise AmendmentRefused("merged needs/output changes are refused")
                changed[lane_id] = (
                    old["lane_projection_digest"] != new_lane.lane_projection_digest
                )
            for lane_id in named:
                new_lane = new_lanes.get(lane_id)
                old = old_lanes.get(lane_id)
                if old is None or new_lane is None:
                    raise AmendmentRefused("AMENDMENT_DOES_NOT_ADDRESS_REVIEW")
                if old["spec_digest"] == new_lane.spec_digest:
                    raise AmendmentRefused("AMENDMENT_DOES_NOT_ADDRESS_REVIEW")
            computed: list[st.LaneReset] = []
            for lane_id, new_lane in new_lanes.items():
                if lane_id not in old_lanes:
                    computed.append(
                        st.LaneReset(
                            lane_id,
                            st.LaneStage.PLANNED,
                            st.LaneStage.PLANNED,
                        )
                    )
                    continue
                current = self.lane_stage(run_id, lane_id)
                wait_reason = (
                    self._wait_reason(run_id, lane_id)
                    if current is st.LaneStage.WAITING_FOR_USER
                    else None
                )
                target = st.amendment_reset_stage(
                    current,
                    changed=changed[lane_id],
                    wait_reason=wait_reason,
                    lane_kind=new_lane.lane_kind,
                )
                computed.append(st.LaneReset(lane_id, current, target))
            computed_sorted = tuple(sorted(computed, key=lambda item: item.lane_id))
            given = tuple(sorted(resets, key=lambda item: item.lane_id))
            if given != computed_sorted:
                raise AmendmentRefused("resets do not match policy")
            retained = artifact.payload.get("retained_inputs") or []
            invalidated = artifact.payload.get("invalidated_inputs") or []
            retained_keys = {
                (
                    entry["lane_id"],
                    entry["plan_revision"],
                    entry["input_digest"],
                )
                for entry in retained
            }
            for entry in invalidated:
                key = (
                    entry["lane_id"],
                    entry["plan_revision"],
                    entry["input_digest"],
                )
                if key in retained_keys:
                    raise AmendmentRefused("input in both retained and invalidated")
            record = self._insert_run_artifact(run_id, artifact, now)
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            self.conn.execute(
                "INSERT INTO plan_revisions(run_id, plan_revision, plan_digest, "
                "parent_revision, plan_artifact_ref, amendment_artifact_id, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    amended_projection.plan_revision,
                    amended_projection.plan_digest,
                    expected_plan_revision,
                    amended_projection.plan_artifact_ref,
                    record.artifact_id,
                    now,
                ),
            )
            self._insert_projection(run_id, amended_projection)
            self.conn.execute(
                "UPDATE runs SET plan_digest=?, plan_revision=?, updated_at=? "
                "WHERE run_id=?",
                (
                    amended_projection.plan_digest,
                    amended_projection.plan_revision,
                    now,
                    run_id,
                ),
            )
            for reset in computed_sorted:
                if reset.lane_id not in old_lanes:
                    self.conn.execute(
                        "INSERT INTO lane_state(run_id, lane_id, stage, updated_at) "
                        "VALUES (?,?,?,?)",
                        (
                            run_id,
                            reset.lane_id,
                            st.LaneStage.PLANNED.value,
                            now,
                        ),
                    )
                    self._audit(
                        run_id=run_id,
                        lane_id=reset.lane_id,
                        from_stage=None,
                        to_stage=st.LaneStage.PLANNED.value,
                        artifact_id=record.artifact_id,
                        reason="apply_amendment",
                        now=now,
                    )
                    continue
                if reset.from_stage is reset.to_stage:
                    if (
                        reset.to_stage is st.LaneStage.WAITING_FOR_USER
                        and self._wait_reason(run_id, reset.lane_id)
                        is st.WaitReason.PAUSE
                    ):
                        self._replace_pause_wait(
                            run_id,
                            reset.lane_id,
                            amended_projection,
                            record.artifact_id,
                            now,
                        )
                    continue
                self._cas_stage(
                    run_id, reset.lane_id, reset.from_stage, reset.to_stage, now
                )
                self._audit(
                    run_id=run_id,
                    lane_id=reset.lane_id,
                    from_stage=reset.from_stage.value,
                    to_stage=reset.to_stage.value,
                    artifact_id=record.artifact_id,
                    reason="apply_amendment",
                    now=now,
                )
            self._audit(
                run_id=run_id,
                lane_id=None,
                from_stage=None,
                to_stage=None,
                artifact_id=record.artifact_id,
                reason="apply_amendment",
                now=now,
            )
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _replace_pause_wait(
        self,
        run_id: str,
        lane_id: str,
        plan: st.CompiledPlan,
        amendment_id: str,
        now: str,
    ) -> None:
        run = self._run(run_id)
        projection = self._projection(run_id, plan.plan_revision, lane_id)
        old = self._latest_lane_artifact(run_id, lane_id, st.ArtifactKind.USER_WAIT)
        old_payload = _loads(old["payload_json"]) if old is not None else {}
        predecessor_id, predecessor_seq = self._predecessor(run_id, lane_id)
        resume_stage = st.LaneStage(
            old_payload.get("resume_stage", st.LaneStage.BUILDING.value)
        )
        wait_digest = st.user_wait_input_digest(
            predecessor_artifact_id=predecessor_id,
            predecessor_sequence=predecessor_seq,
            wait_reason=st.WaitReason.PAUSE,
            resume_stage=resume_stage,
            resume_input_digest=amendment_id,
            run_id=run_id,
            lane_id=lane_id,
            plan_revision=run["plan_revision"],
        )
        payload = {
            "amendment_artifact_id": amendment_id,
            "input_digest": wait_digest,
            "input_invalidated": True,
            "invalidated_input_digest": old_payload.get("resume_input_digest"),
            "predecessor_artifact_id": predecessor_id,
            "predecessor_sequence": predecessor_seq,
            "resume_input_digest": amendment_id,
            "resume_stage": resume_stage.value,
            "wait_reason": st.WaitReason.PAUSE.value,
        }
        artifact = st.LaneArtifact(
            kind=st.ArtifactKind.USER_WAIT,
            plan_revision=run["plan_revision"],
            spec_digest=projection["spec_digest"],
            lane_projection_digest=projection["lane_projection_digest"],
            input_digest=wait_digest,
            output_digest=st.digest_canonical(payload),
            artifact_ref=f"pause-rewrite:{amendment_id}:{lane_id}",
            payload=payload,
        )
        self._insert_lane_artifact(
            run_id=run_id,
            lane_id=lane_id,
            artifact=artifact,
            completed_stage=st.LaneStage.WAITING_FOR_USER,
            now=now,
        )

    @serialized
    def complete_publication(
        self,
        run_id: str,
        review_input_fingerprint: str,
        receipt_ref: str,
        receipt_object: str,
        expected_before_sha: str,
        published_sha: str,
        artifact: st.RunArtifact,
    ) -> ArtifactRecord:
        now = now_iso()
        self._begin()
        try:
            run = self._run(run_id)
            review = self._active_final_review(run_id)
            if review is None:
                raise PublicationRefused("no final review")
            review_payload = _loads(review["payload_json"])
            if review_payload.get("verdict") != st.ReviewerVerdict.PASS.value:
                raise PublicationRefused("final review is not PASS")
            if review["input_digest"] != review_input_fingerprint:
                raise StaleStageInput("publication fingerprint")
            if artifact.kind is not st.ArtifactKind.MAIN_PUBLICATION:
                raise st.CanonicalIdentityError("MAIN_PUBLICATION required")
            if artifact.input_digest != review_input_fingerprint:
                raise StaleStageInput("publication artifact fingerprint")
            payload = artifact.payload
            if payload.get("receipt_ref") != receipt_ref:
                raise PublicationRefused("receipt_ref")
            if payload.get("receipt_object") != receipt_object:
                raise PublicationRefused("receipt_object")
            if payload.get("expected_before_sha") != expected_before_sha:
                raise PublicationRefused("expected_before_sha")
            if payload.get("published_sha") != published_sha:
                raise PublicationRefused("published_sha")
            if not self._all_merged(run_id):
                raise PublicationRefused("lanes are not all MERGED")
            del run
            record = self._insert_run_artifact(run_id, artifact, now)
            if record.replayed:
                self.conn.execute("COMMIT")
                return record
            self._audit(
                run_id=run_id,
                lane_id=None,
                from_stage=None,
                to_stage=None,
                artifact_id=record.artifact_id,
                reason="complete_publication",
                now=now,
            )
            self._touch_run(run_id, now)
            self.conn.execute("COMMIT")
            return record
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def ready_lane_ids(self, run_id: str) -> tuple[str, ...]:
        run = self._run(run_id)
        ready = []
        for lane in self._active_lanes(run_id, run["plan_revision"]):
            stage = self.lane_stage(run_id, lane["lane_id"])
            if stage in (
                st.LaneStage.MERGED,
                st.LaneStage.WAITING_FOR_USER,
            ):
                continue
            needs = self._needs(lane)
            if all(
                self.lane_stage(run_id, dep) is st.LaneStage.MERGED for dep in needs
            ):
                ready.append(lane["lane_id"])
        return tuple(ready)

    def derive_run_status(self, run_id: str, integration_sha: str) -> st.RunStatus:
        run = self._run(run_id)
        stages = [
            st.LaneStage(row[0])
            for row in self.conn.execute(
                "SELECT stage FROM lane_state WHERE run_id=?", (run_id,)
            )
        ]
        fingerprint = None
        try:
            fingerprint = st.final_review_input_fingerprint(
                integration_sha=st.require_git_sha(
                    integration_sha, name="integration_sha"
                ),
                plan_revision=run["plan_revision"],
                plan_digest=run["plan_digest"],
                lanes=self._final_review_lanes(run, integration_sha),
            )
        except (st.IllegalStageEdge, st.CanonicalIdentityError, ArtifactStoreError):
            fingerprint = None
        publication = False
        passing = False
        if fingerprint is not None:
            pub = self.conn.execute(
                "SELECT 1 FROM run_artifacts WHERE run_id=? AND artifact_kind=? "
                "AND input_digest=?",
                (
                    run_id,
                    st.ArtifactKind.MAIN_PUBLICATION.value,
                    fingerprint,
                ),
            ).fetchone()
            publication = pub is not None
            review = self.conn.execute(
                "SELECT payload_json FROM run_artifacts WHERE run_id=? "
                "AND artifact_kind=? AND input_digest=?",
                (
                    run_id,
                    st.ArtifactKind.FINAL_INTEGRATION_REVIEW.value,
                    fingerprint,
                ),
            ).fetchone()
            passing = bool(
                review
                and _loads(review[0]).get("verdict") == st.ReviewerVerdict.PASS.value
            )
        return st.derive_run_status(
            stages=stages,
            publication_for_active_fingerprint=publication,
            passing_final_review_for_active_fingerprint=passing,
        )

    def active_projection(self, run_id: str) -> tuple[st.LaneProjection, ...]:
        run = self._run(run_id)
        lanes = []
        for row in self._active_lanes(run_id, run["plan_revision"]):
            lanes.append(
                st.LaneProjection(
                    lane_id=row["lane_id"],
                    needs=tuple(_loads(row["needs_json"])),
                    spec_digest=row["spec_digest"],
                    declared_outputs=tuple(_loads(row["declared_outputs_json"])),
                    lane_projection_digest=row["lane_projection_digest"],
                    public_acceptance=tuple(_loads(row["public_acceptance_json"])),
                    lane_kind=self._kind_matching_digest(row),
                )
            )
        return tuple(lanes)


    def get_lane_artifact(self, artifact_id: str) -> ArtifactRecord:
        row = self.conn.execute(
            "SELECT * FROM lane_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise ArtifactStoreError(artifact_id)
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            lane_id=row["lane_id"],
            sequence=row["sequence"],
            kind=st.ArtifactKind(row["artifact_kind"]),
            plan_revision=row["plan_revision"],
            input_digest=row["input_digest"],
            output_digest=row["output_digest"],
            artifact_ref=row["artifact_ref"],
            payload=_loads(row["payload_json"]),
        )

    def schema_tables(self) -> set[str]:
        return _tables(self.conn)
