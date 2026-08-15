"""Executable contract for the Maestro multi-repository coordinator store."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import shutil
import subprocess
import sys
import tempfile
import sqlite3
import unittest
from unittest import mock
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import coordinator_store as cs  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import workspace_model as wm  # noqa: E402
from adw_modules import workspace_runtime as wr  # noqa: E402


BASE_A = "a" * 40
BASE_B = "b" * 40
PLAN_A = "1" * 64
PLAN_B = "2" * 64
OWNER = "test-coordinator"


def workspace() -> wm.WorkspacePlan:
    """A dependency pair plus a read-only participant."""
    return wm.WorkspacePlan(
        schema_version=wm.SCHEMA_V1,
        workspace_id="release-2026-08",
        repositories=(
            wm.RepositorySpec(
                repository_id="api",
                mode=wm.RepositoryMode.WRITE,
                path="services/api",
                base_commit=BASE_A,
                plan_path="plans/api.json",
                plan_digest=PLAN_A,
                target_branch="main",
                run_argv=("maestro", "run"),
            ),
            wm.RepositorySpec(
                repository_id="web",
                mode=wm.RepositoryMode.WRITE,
                path="services/web",
                base_commit=BASE_B,
                needs=("api",),
                plan_path="plans/web.json",
                plan_digest=PLAN_B,
                target_branch="main",
                run_argv=("maestro", "run"),
            ),
            wm.RepositorySpec(
                repository_id="audit",
                mode=wm.RepositoryMode.READ_ONLY,
                path="tools/audit",
                base_commit=BASE_A,
            ),
        ),
        publication_mode=wm.PublicationMode.LOCAL_REFS,
        integration_gates=(
            pm.Gate(runner="pytest", argv=("tests/integration",),
                    cwd=".", min_cases=1),
        ),
    )


def workspace_with_pending_publication_target() -> wm.WorkspacePlan:
    plan = workspace()
    docs = wm.RepositorySpec(
        repository_id="docs",
        mode=wm.RepositoryMode.WRITE,
        path="docs/site",
        base_commit=BASE_A,
        plan_path="plans/docs.json",
        plan_digest="3" * 64,
        target_branch="main",
        run_argv=("maestro", "run"),
    )
    return wm.WorkspacePlan(
        schema_version=plan.schema_version,
        workspace_id=plan.workspace_id,
        repositories=plan.repositories[:2] + (docs,) + plan.repositories[2:],
        publication_mode=plan.publication_mode,
        integration_gates=plan.integration_gates,
    )


def new_store(tmp_root: Path) -> cs.CoordinatorStore:
    return cs.CoordinatorStore(tmp_root / "coordinator.db")


def create_run(store: cs.CoordinatorStore, run_id: str = "workspace-run"
               ) -> wm.WorkspacePlan:
    plan = workspace()
    store.create_run(run_id, "d" * 64, plan)
    if not store.acquire_lease(run_id, OWNER, now=0.0, stale_after_s=1_000_000.0):
        raise AssertionError("test coordinator could not acquire its lease")
    return plan


def accept_every_repository(store: cs.CoordinatorStore, plan: wm.WorkspacePlan,
                            run_id: str = "workspace-run") -> None:
    for spec in plan.repositories:
        candidate = ("maestro/{0}".format(spec.repository_id)
                     if spec.mode is wm.RepositoryMode.WRITE else None)
        store.claim_repository(
            run_id, spec.repository_id, "child-{0}".format(spec.repository_id), candidate,
            lease_owner=OWNER)
        store.transition_repository(
            run_id, spec.repository_id, wm.RepositoryState.ACCEPTED,
            accepted_sha="c" * 40, lease_owner=OWNER)


class CoordinatorStoreRefusalTests(unittest.TestCase):

    def test_unknown_run_is_refused(self):
        """The authoritative store never invents a workspace run."""
        with tempfile.TemporaryDirectory() as tmp:
            store = cs.CoordinatorStore(Path(tmp) / "coordinator.db")
            with self.assertRaises(cs.UnknownRun):
                store.get_run("missing")
            store.close()


class CoordinatorStoreRecoveryTests(unittest.TestCase):

    def test_reopen_recovers_workspace_and_exact_repository_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            store = cs.CoordinatorStore(db_path)
            plan = create_run(store)
            claimed = store.claim_repository(
                "workspace-run", "api", "child-api", "maestro/api", lease_owner=OWNER)
            self.assertEqual(claimed.state, wm.RepositoryState.RUNNING)
            store.close()

            reopened = cs.CoordinatorStore(db_path)
            run = reopened.get_run("workspace-run")
            self.assertEqual(run.workspace_id, plan.workspace_id)
            self.assertEqual(run.workspace_digest, "d" * 64)
            self.assertEqual(run.workspace, plan)
            repositories = reopened.list_repositories("workspace-run")
            self.assertEqual(tuple(record.spec for record in repositories),
                             plan.repositories)
            self.assertEqual(tuple(record.repository_id for record in repositories),
                             ("api", "web", "audit"))
            self.assertEqual(repositories[0].child_run_id, "child-api")
            self.assertEqual(repositories[0].candidate_branch, "maestro/api")
            self.assertEqual(repositories[0].state, wm.RepositoryState.RUNNING)
            reopened.close()

    def test_duplicate_run_is_refused_without_replacing_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            plan = create_run(store)
            with self.assertRaises(cs.RunAlreadyExists):
                store.create_run("workspace-run", "e" * 64, workspace())
            self.assertEqual(store.get_run("workspace-run").workspace, plan)
            store.close()


class RepositoryPathBindingTests(unittest.TestCase):

    def test_schema_migration_adds_nullable_binding_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            store = cs.CoordinatorStore(db_path)
            plan = create_run(store)
            store.close()

            legacy = sqlite3.connect(str(db_path))
            legacy.execute("PRAGMA foreign_keys=OFF")
            legacy.execute(
                "ALTER TABLE workspace_repositories"
                " RENAME TO legacy_workspace_repositories")
            legacy.execute("""
                CREATE TABLE workspace_repositories (
                  run_id TEXT NOT NULL REFERENCES workspace_runs(run_id),
                  repository_id TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  spec_json TEXT NOT NULL,
                  needs_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  child_run_id TEXT,
                  candidate_branch TEXT,
                  accepted_sha TEXT,
                  block_reason TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (run_id, repository_id),
                  UNIQUE (run_id, position)
                )
            """)
            legacy.execute("""
                INSERT INTO workspace_repositories (
                  run_id, repository_id, position, spec_json, needs_json, state,
                  child_run_id, candidate_branch, accepted_sha, block_reason, updated_at
                )
                SELECT run_id, repository_id, position, spec_json, needs_json, state,
                       child_run_id, candidate_branch, accepted_sha, block_reason, updated_at
                FROM legacy_workspace_repositories
            """)
            legacy.execute("DROP TABLE legacy_workspace_repositories")
            legacy.commit()
            legacy.close()

            reopened = cs.CoordinatorStore(db_path)
            records = reopened.list_repositories("workspace-run")
            self.assertEqual(tuple(record.spec for record in records), plan.repositories)
            self.assertEqual(
                tuple(record.resolved_path for record in records), (None, None, None))
            self.assertTrue(reopened.acquire_lease(
                "workspace-run", OWNER, 0.0, 60.0))
            with self.assertRaises(cs.RepositoryPathMismatch):
                reopened.bind_repository_paths(
                    "workspace-run",
                    {
                        spec.repository_id: cs.RepositoryPathBinding(
                            str((Path(tmp) / spec.repository_id).resolve()),
                            str((Path(tmp) / "common" / spec.repository_id).resolve()),
                            "identity-{0}".format(spec.repository_id))
                        for spec in plan.repositories
                    },
                    lease_owner=OWNER)
            reopened.close()

    def test_first_bind_replay_and_mismatch_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = new_store(root)
            create_run(store)
            paths = {
                "api": root / "repositories" / "api",
                "web": root / "repositories" / "web",
                "audit": root / "tools" / "audit",
            }
            bindings = {
                repository_id: cs.RepositoryPathBinding(
                    resolved_path=str(path.resolve()),
                    git_common_dir=str((root / "common" / repository_id).resolve()),
                    repository_identity="identity-{0}".format(repository_id))
                for repository_id, path in paths.items()
            }

            bound = store.bind_repository_paths(
                "workspace-run", bindings, lease_owner=OWNER)
            expected = {
                repository_id: binding.resolved_path
                for repository_id, binding in bindings.items()
            }
            self.assertEqual(
                {record.repository_id: record.resolved_path for record in bound}, expected)
            self.assertEqual(
                len([entry for entry in store.audit_transitions("workspace-run")
                     if entry.reason == "repository-paths-bound"]), 1)

            replayed = store.bind_repository_paths(
                "workspace-run", bindings, lease_owner=OWNER)
            self.assertEqual(replayed, bound)

            audit_before = store.audit_transitions("workspace-run")
            moved = dict(bindings)
            moved["api"] = cs.RepositoryPathBinding(
                resolved_path=str((root / "other-clone" / "api").resolve()),
                git_common_dir=bindings["api"].git_common_dir,
                repository_identity=bindings["api"].repository_identity)
            with self.assertRaises(cs.RepositoryPathMismatch):
                store.bind_repository_paths(
                    "workspace-run", moved, lease_owner=OWNER)
            self.assertEqual(store.audit_transitions("workspace-run"), audit_before)
            self.assertEqual(
                store.get_repository("workspace-run", "api").resolved_path,
                expected["api"])
            store.close()

    def test_same_path_clone_replacement_refuses_identity_replay(self):
        """A path and base SHA cannot substitute for its bound Git common directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            subprocess.run(("git", "init", "-q", "-b", "main"), cwd=str(source),
                           check=True)
            subprocess.run(("git", "config", "user.name", "Binding"), cwd=str(source),
                           check=True)
            subprocess.run(
                ("git", "config", "user.email", "binding@example.invalid"),
                cwd=str(source), check=True)
            (source / "README").write_text("source\n", encoding="utf-8")
            subprocess.run(("git", "add", "README"), cwd=str(source), check=True)
            subprocess.run(("git", "commit", "-qm", "source"), cwd=str(source),
                           check=True)
            api = root / "api"
            subprocess.run(("git", "clone", "-q", str(source), str(api)), check=True)

            plan = wm.WorkspacePlan(
                schema_version=wm.SCHEMA_V1,
                workspace_id="identity-replay",
                repositories=(wm.RepositorySpec(
                    repository_id="api", mode=wm.RepositoryMode.READ_ONLY,
                    path="api", base_commit=BASE_A),),
            )
            store = new_store(root)
            store.create_run("identity-run", "d" * 64, plan)
            self.assertTrue(store.acquire_lease(
                "identity-run", OWNER, now=0.0, stale_after_s=100.0))
            original = wr.repository_binding(api)
            store.bind_repository_paths(
                "identity-run", {"api": original}, lease_owner=OWNER)

            shutil.rmtree(str(api))
            subprocess.run(("git", "clone", "-q", str(source), str(api)), check=True)
            replacement = wr.repository_binding(api)
            self.assertEqual(original.resolved_path, replacement.resolved_path)
            self.assertNotEqual(
                original.repository_identity, replacement.repository_identity)
            with self.assertRaises(cs.RepositoryPathMismatch):
                store.bind_repository_paths(
                    "identity-run", {"api": replacement}, lease_owner=OWNER)
            store.close()

class RepositoryLifecycleTests(unittest.TestCase):

    def test_claim_race_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)

            def claim(owner: str) -> str:
                try:
                    record = store.claim_repository(
                        "workspace-run", "api", owner, "maestro/{0}".format(owner),
                        lease_owner=OWNER)
                    return record.child_run_id or ""
                except cs.IllegalTransition:
                    return "refused"

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(claim, ("child-one", "child-two")))

            winners = tuple(result for result in results if result != "refused")
            self.assertEqual(len(winners), 1)
            record = store.get_repository("workspace-run", "api")
            self.assertEqual(record.state, wm.RepositoryState.RUNNING)
            self.assertEqual(record.child_run_id, winners[0])
            store.close()

    def test_terminal_transitions_are_strict_and_accepted_requires_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)
            with self.assertRaises(cs.IllegalTransition):
                store.transition_repository(
                    "workspace-run", "api", wm.RepositoryState.ACCEPTED,
                    accepted_sha="c" * 40, lease_owner=OWNER)
            with self.assertRaises(cs.UnknownRepository):
                store.get_repository("workspace-run", "unknown")

            store.claim_repository(
                "workspace-run", "api", "child-api", "maestro/api", lease_owner=OWNER)
            with self.assertRaises(cs.IllegalTransition):
                store.transition_repository(
                    "workspace-run", "api", wm.RepositoryState.ACCEPTED,
                    lease_owner=OWNER)
            accepted = store.transition_repository(
                "workspace-run", "api", wm.RepositoryState.ACCEPTED,
                accepted_sha="c" * 40, lease_owner=OWNER)
            self.assertEqual(accepted.state, wm.RepositoryState.ACCEPTED)
            self.assertEqual(accepted.accepted_sha, "c" * 40)
            with self.assertRaises(cs.IllegalTransition):
                store.transition_repository(
                    "workspace-run", "api", wm.RepositoryState.BLOCKED,
                    reason="too late", lease_owner=OWNER)
            store.close()

    def test_block_pending_descendants_uses_persisted_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)
            store.claim_repository(
                "workspace-run", "api", "child-api", "maestro/api", lease_owner=OWNER)
            store.transition_repository(
                "workspace-run", "api", wm.RepositoryState.BLOCKED,
                reason="agent failed", lease_owner=OWNER)
            self.assertEqual(
                store.block_pending_descendants("workspace-run", lease_owner=OWNER), ("web",))
            self.assertEqual(store.get_repository("workspace-run", "web").state,
                             wm.RepositoryState.BLOCKED)
            self.assertEqual(store.get_repository("workspace-run", "audit").state,
                             wm.RepositoryState.PENDING)
            store.close()


class CancellationAndGateTests(unittest.TestCase):

    def test_cancellation_request_is_durable_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            store = cs.CoordinatorStore(db_path)
            create_run(store)
            self.assertFalse(store.cancellation_requested("workspace-run"))
            requested = store.request_cancellation("workspace-run", actor="operator")
            self.assertTrue(requested.cancel_requested)
            self.assertTrue(store.cancellation_requested("workspace-run"))
            store.close()

            reopened = cs.CoordinatorStore(db_path)
            self.assertTrue(reopened.cancellation_requested("workspace-run"))
            reopened.close()

    def test_gate_results_are_durable_and_ordered_by_workspace_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            store = cs.CoordinatorStore(db_path)
            create_run(store)
            recorded = store.record_gate(
                "workspace-run", 0, passed=True, detail={"cases": 3},
                lease_owner=OWNER)
            self.assertTrue(recorded.passed)
            self.assertEqual(recorded.detail, {"cases": 3})
            store.close()

            reopened = cs.CoordinatorStore(db_path)
            gates = reopened.list_gates("workspace-run")
            self.assertEqual(len(gates), 1)
            self.assertEqual(gates[0].gate_index, 0)
            self.assertTrue(gates[0].passed)
            self.assertEqual(gates[0].detail, {"cases": 3})
            reopened.close()

    def test_gate_audit_keeps_the_authoritative_gate_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)
            store.record_gate(
                "workspace-run", 0, passed=True,
                detail={"gate_index": 999, "cases": 3}, lease_owner=OWNER)

            gate_audit = next(
                entry for entry in store.audit_transitions("workspace-run")
                if entry.reason == "gate-recorded")
            self.assertEqual(gate_audit.detail, {"gate_index": 0, "cases": 3})
            store.close()


class PublicationAndAuditTests(unittest.TestCase):

    def test_publication_vector_keeps_workspace_order_and_legal_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            plan = create_run(store)
            with self.assertRaises(cs.PublicationRefused):
                store.prepare_publication(
                    "workspace-run", remote_identities={},
                    lease_owner=OWNER, lease_now=0.0)

            accept_every_repository(store, plan)
            store.record_gate("workspace-run", 0, passed=True, lease_owner=OWNER)
            store.declare_outcome(
                "workspace-run", wm.WorkspaceOutcome.ACCEPTED, lease_owner=OWNER)
            prepared = store.prepare_publication(
                "workspace-run", remote_identities={},
                lease_owner=OWNER, lease_now=0.0)
            self.assertEqual(prepared.state, wm.PublicationState.PREPARED)
            self.assertEqual(
                tuple(target.repository_id for target in prepared.targets),
                ("api", "web"))
            self.assertEqual(
                tuple(target.target_branch for target in prepared.targets),
                ("main", "main"))
            self.assertEqual(
                tuple(target.candidate_branch for target in prepared.targets),
                ("maestro/api", "maestro/web"))
            self.assertEqual(
                tuple(target.expected_base_sha for target in prepared.targets),
                (BASE_A, BASE_B))
            store.close()
            store = cs.CoordinatorStore(Path(tmp) / "coordinator.db")
            recovered = store.get_publication_intent("workspace-run")
            self.assertEqual(
                tuple(target.expected_base_sha for target in recovered.targets),
                (BASE_A, BASE_B))
            with self.assertRaises(cs.DuplicatePublicationIntent):
                store.prepare_publication(
                    "workspace-run", remote_identities={},
                    lease_owner=OWNER, lease_now=0.0)
            with self.assertRaises(cs.PublicationRefused):
                store.record_publication_step(
                    "workspace-run", "api", wm.PublicationState.PREPARED,
                    lease_owner=OWNER, lease_now=0.0)

            first = store.record_publication_step(
                "workspace-run", "api", wm.PublicationState.PUBLISHED,
                lease_owner=OWNER, lease_now=0.0,
                detail={"ref": "refs/heads/main"})
            self.assertEqual(first.from_state, wm.PublicationState.PENDING)
            self.assertEqual(first.to_state, wm.PublicationState.PUBLISHED)
            self.assertEqual(store.get_publication_intent("workspace-run").state,
                             wm.PublicationState.PREPARED)
            store.record_publication_step(
                "workspace-run", "web", wm.PublicationState.PUBLISHED,
                lease_owner=OWNER, lease_now=0.0)
            self.assertEqual(store.get_publication_intent("workspace-run").state,
                             wm.PublicationState.PUBLISHED)
            steps = store.list_publication_steps("workspace-run")
            self.assertEqual(
                tuple((step.repository_id, step.to_state) for step in steps),
                (("api", wm.PublicationState.PUBLISHED),
                 ("web", wm.PublicationState.PUBLISHED)))
            with self.assertRaises(cs.PublicationRefused):
                store.record_publication_step(
                    "workspace-run", "api", wm.PublicationState.ROLLED_BACK,
                    lease_owner=OWNER, lease_now=0.0)
            store.close()

    def test_publication_mutation_refuses_missing_or_stale_lease_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)

            with self.assertRaises(cs.LeaseOwnershipError):
                store.prepare_publication(
                    "workspace-run", remote_identities={},
                    lease_owner="other-owner", lease_now=0.0)
            with self.assertRaises(cs.LeaseOwnershipError):
                store.prepare_publication(
                    "workspace-run", remote_identities={},
                    lease_owner=OWNER, lease_now=1_000_000.0)
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent("workspace-run")
            store.close()

    def test_failed_publication_requires_every_target_to_roll_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            plan = workspace_with_pending_publication_target()
            store.create_run("workspace-run", "d" * 64, plan)
            self.assertTrue(store.acquire_lease(
                "workspace-run", OWNER, now=0.0, stale_after_s=1_000_000.0))
            accept_every_repository(store, plan)
            store.record_gate("workspace-run", 0, passed=True, lease_owner=OWNER)
            store.declare_outcome(
                "workspace-run", wm.WorkspaceOutcome.ACCEPTED, lease_owner=OWNER)
            store.prepare_publication(
                "workspace-run", remote_identities={},
                lease_owner=OWNER, lease_now=0.0)

            store.record_publication_step(
                "workspace-run", "api", wm.PublicationState.PUBLISHED,
                lease_owner=OWNER, lease_now=0.0)
            store.record_publication_step(
                "workspace-run", "web", wm.PublicationState.FAILED,
                lease_owner=OWNER, lease_now=0.0)
            store.record_publication_step(
                "workspace-run", "api", wm.PublicationState.ROLLED_BACK,
                lease_owner=OWNER, lease_now=0.0)
            self.assertEqual(store.get_publication_intent("workspace-run").state,
                             wm.PublicationState.FAILED)
            store.record_publication_step(
                "workspace-run", "docs", wm.PublicationState.ROLLED_BACK,
                lease_owner=OWNER, lease_now=0.0)
            self.assertEqual(store.get_publication_intent("workspace-run").state,
                             wm.PublicationState.FAILED)
            store.record_publication_step(
                "workspace-run", "web", wm.PublicationState.ROLLED_BACK,
                lease_owner=OWNER, lease_now=0.0)
            recovered = store.get_publication_intent("workspace-run")
            self.assertEqual(recovered.state, wm.PublicationState.ROLLED_BACK)
            self.assertTrue(all(target.state is wm.PublicationState.ROLLED_BACK
                                for target in recovered.targets))
            store.close()

    def test_audit_is_append_only_and_describes_each_repository_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            create_run(store)
            store.claim_repository(
                "workspace-run", "api", "child-api", "maestro/api", lease_owner=OWNER)
            store.transition_repository(
                "workspace-run", "api", wm.RepositoryState.ACCEPTED,
                accepted_sha="c" * 40, lease_owner=OWNER)

            repository_audit = tuple(
                row for row in store.audit_transitions("workspace-run")
                if row.kind == "repository")
            self.assertEqual(len(repository_audit), 2)
            self.assertEqual(
                tuple((row.repository_id, row.kind, row.from_state, row.to_state,
                       row.reason) for row in repository_audit),
                (("api", "repository", wm.RepositoryState.PENDING,
                  wm.RepositoryState.RUNNING, "claim"),
                 ("api", "repository", wm.RepositoryState.RUNNING,
                  wm.RepositoryState.ACCEPTED, "accepted")))
            self.assertLess(
                repository_audit[0].transition_id, repository_audit[1].transition_id)
            self.assertEqual(repository_audit[1].detail["accepted_sha"], "c" * 40)
            store.close()



class CoordinatorStoreAtomicityTests(unittest.TestCase):

    def test_stale_lease_cannot_change_authority_or_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            first = cs.CoordinatorStore(db_path)
            first.create_run("workspace-run", "d" * 64, workspace())
            self.assertTrue(first.acquire_lease(
                "workspace-run", "coordinator-a", now=0.0, stale_after_s=10.0))

            second = cs.CoordinatorStore(db_path)
            self.assertTrue(second.acquire_lease(
                "workspace-run", "coordinator-b", now=10.0, stale_after_s=10.0))
            audit_before = second.audit_transitions("workspace-run")
            attempts = (
                lambda owner: first.claim_repository(
                    "workspace-run", "api", "child-api", "maestro/api",
                    lease_owner=owner),
                lambda owner: first.transition_repository(
                    "workspace-run", "api", wm.RepositoryState.BLOCKED,
                    reason="not-owner", lease_owner=owner),
                lambda owner: first.block_pending_descendants(
                    "workspace-run", lease_owner=owner),
                lambda owner: first.record_gate(
                    "workspace-run", 0, passed=True, lease_owner=owner),
                lambda owner: first.declare_outcome(
                    "workspace-run", wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED,
                    lease_owner=owner),
            )
            for owner in ("wrong-owner", "coordinator-a"):
                for attempt in attempts:
                    with self.assertRaises(cs.LeaseOwnershipError):
                        attempt(owner)

            self.assertEqual(
                second.get_repository("workspace-run", "api").state,
                wm.RepositoryState.PENDING)
            self.assertEqual(second.list_gates("workspace-run"), ())
            self.assertIsNone(second.get_run("workspace-run").outcome)
            self.assertEqual(second.audit_transitions("workspace-run"), audit_before)
            first.close()
            second.close()

    def test_two_stores_preserve_single_claim_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            first = cs.CoordinatorStore(db_path)
            first.create_run("workspace-run", "d" * 64, workspace())
            self.assertTrue(first.acquire_lease(
                "workspace-run", "shared-owner", now=0.0, stale_after_s=100.0))
            second = cs.CoordinatorStore(db_path)
            start = Barrier(2)

            def claim(store: cs.CoordinatorStore, child_run_id: str) -> str:
                start.wait()
                try:
                    return store.claim_repository(
                        "workspace-run", "api", child_run_id,
                        "maestro/{0}".format(child_run_id),
                        lease_owner="shared-owner").child_run_id or ""
                except cs.IllegalTransition:
                    return "refused"

            with ThreadPoolExecutor(max_workers=2) as pool:
                left = pool.submit(claim, first, "child-one")
                right = pool.submit(claim, second, "child-two")
                results = (left.result(), right.result())

            winners = tuple(result for result in results if result != "refused")
            self.assertEqual(len(winners), 1)
            self.assertEqual(
                second.get_repository("workspace-run", "api").child_run_id, winners[0])
            claims = tuple(
                row for row in second.audit_transitions("workspace-run")
                if row.kind == "repository" and row.reason == "claim")
            self.assertEqual(len(claims), 1)
            first.close()
            second.close()

    def test_base_exception_rolls_back_persisted_state_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.db"
            store = cs.CoordinatorStore(db_path)
            create_run(store)
            audit_before = store.audit_transitions("workspace-run")

            with mock.patch.object(
                    store, "_append_transition", side_effect=BaseException("abort")):
                with self.assertRaises(BaseException):
                    store.claim_repository(
                        "workspace-run", "api", "child-api", "maestro/api",
                        lease_owner=OWNER)

            observer = cs.CoordinatorStore(db_path)
            self.assertEqual(
                observer.get_repository("workspace-run", "api").state,
                wm.RepositoryState.PENDING)
            self.assertEqual(observer.audit_transitions("workspace-run"), audit_before)
            observer.close()
            store.close()


class LeaseTests(unittest.TestCase):

    def test_lease_contention_expiry_takeover_and_wrong_owner_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("workspace-run", "d" * 64, workspace())
            self.assertTrue(store.acquire_lease(
                "workspace-run", "scheduler-a", now=100.0, stale_after_s=10.0))
            self.assertFalse(store.acquire_lease(
                "workspace-run", "scheduler-b", now=109.0, stale_after_s=10.0))
            self.assertFalse(store.heartbeat_lease(
                "workspace-run", "scheduler-b", now=109.0, stale_after_s=10.0))
            self.assertFalse(store.release_lease("workspace-run", "scheduler-b"))
            self.assertTrue(store.heartbeat_lease(
                "workspace-run", "scheduler-a", now=109.0, stale_after_s=10.0))
            self.assertFalse(store.acquire_lease(
                "workspace-run", "scheduler-b", now=118.0, stale_after_s=10.0))
            self.assertTrue(store.acquire_lease(
                "workspace-run", "scheduler-b", now=119.0, stale_after_s=10.0))
            held = store.get_run("workspace-run")
            self.assertEqual(held.lease_owner, "scheduler-b")
            self.assertEqual(held.lease_expires_at, 129.0)
            self.assertFalse(store.release_lease("workspace-run", "scheduler-a"))
            self.assertTrue(store.release_lease("workspace-run", "scheduler-b"))
            self.assertIsNone(store.get_run("workspace-run").lease_owner)
            store.close()

    def test_heartbeat_renews_expiry_without_appending_audit(self):
        """Routine renewal changes lease liveness but is not an ownership transition."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("workspace-run", "d" * 64, workspace())
            self.assertTrue(store.acquire_lease(
                "workspace-run", "scheduler-a", now=100.0, stale_after_s=10.0))
            audit_before = store.audit_transitions("workspace-run")

            self.assertTrue(store.heartbeat_lease(
                "workspace-run", "scheduler-a", now=105.0, stale_after_s=10.0))

            renewed = store.get_run("workspace-run")
            self.assertEqual(renewed.lease_owner, "scheduler-a")
            self.assertEqual(renewed.lease_expires_at, 115.0)
            self.assertEqual(store.audit_transitions("workspace-run"), audit_before)
            store.close()
