"""End-to-end contract tests for the workspace operator CLI."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Tuple
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import coordinator_store as coordinator_store  # noqa: E402
from adw_modules import finalization  # noqa: E402
from adw_modules import receipt_crypto  # noqa: E402
from adw_modules import workspace_canonical  # noqa: E402
from adw_modules import workspace_digest  # noqa: E402
from adw_modules import workspace_model  # noqa: E402
from adw_modules import workspace_runtime  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git",) + args, cwd=str(cwd), capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError("git {0}: {1}".format(" ".join(args), result.stderr))
    return result.stdout.strip()


def _repository(root: Path) -> Tuple[Path, str]:
    repository = root / "api"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "workspace-cli@example.invalid")
    _git(repository, "config", "user.name", "Workspace CLI")
    (repository / "README").write_text("api\n", encoding="utf-8")
    _git(repository, "add", "README")
    _git(repository, "commit", "-qm", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _workspace(base_commit: str, plan_digest: str, *, publication_mode: str = "none"):
    return workspace_model.parse_mapping({
        "schema_version": "maestro-workspace.v1",
        "workspace_id": "release",
        "publication_mode": publication_mode,
        "repositories": [{
            "repository_id": "api",
            "mode": "write",
            "path": "api",
            "base_commit": base_commit,
            "plan_path": "plans/api.json",
            "plan_digest": plan_digest,
            "target_branch": "main",
            "run_argv": ["maestro"],
        }],
    })


class WorkspaceCliContract(unittest.TestCase):
    def _invoke(self, argv):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = maestro.main(argv)
        self.assertEqual(errors.getvalue(), "")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, output.getvalue())
        payload = json.loads(lines[0])
        self.assertIsInstance(payload, dict)
        self.assertEqual(output.getvalue(), json.dumps(payload, sort_keys=True) + "\n")
        return code, payload

    def _environment(self, root: Path, *, publication_mode: str = "none") -> Dict[str, object]:
        repository, base_commit = _repository(root)
        data_dir = root / "data"
        data_dir.mkdir()
        plan_digest = hashlib.sha256(b"participant plan").hexdigest()
        workspace = _workspace(base_commit, plan_digest, publication_mode=publication_mode)
        manifest = root / "workspace.json"
        manifest.write_bytes(workspace_canonical.canonicalize_workspace(workspace))
        seed = bytes(range(32))
        verify_key = receipt_crypto.seed_to_public_key(seed)
        return {
            "repository": repository,
            "base_commit": base_commit,
            "data_dir": data_dir,
            "plan_digest": plan_digest,
            "workspace": workspace,
            "manifest": manifest,
            "seed": seed,
            "verify_key": verify_key,
            "plan_receipts": root / "plan-receipts",
            "workspace_receipts": root / "workspace-receipts",
            "db": root / "coordinator.sqlite",
            "state_root": root / "state",
        }

    @staticmethod
    def _key_arguments(environment: Dict[str, object]):
        return ["--verify-key", environment["verify_key"].hex()]

    def _finalize_arguments(self, environment: Dict[str, object]):
        return [
            "workspace", "finalize", "--manifest-file", str(environment["manifest"]),
            "--plan-receipt-dir", str(environment["plan_receipts"]),
            "--workspace-receipt-dir", str(environment["workspace_receipts"]),
            "--data-dir", str(environment["data_dir"]),
            *self._key_arguments(environment),
            "--signing-seed", environment["seed"].hex(),
        ]

    def _execution_arguments(self, verb: str, environment: Dict[str, object], run_id: str):
        return [
            "workspace", verb, "--manifest-file", str(environment["manifest"]),
            "--workspace-receipt-dir", str(environment["workspace_receipts"]),
            "--data-dir", str(environment["data_dir"]),
            *self._key_arguments(environment),
            "--db", str(environment["db"]), "--state-root", str(environment["state_root"]),
            "--run-id", run_id, "--max-workers", "1", "--lease-owner", "test-operator",
            "--lease-stale-after-s", "10", "--participant-timeout-s", "10",
            "--cancellation-timeout-s", "1", "--poll-interval-s", "0.01",
        ]

    def _write_plan_receipt(self, environment: Dict[str, object], verdict):
        store = finalization.ReceiptStore(
            environment["plan_receipts"],
            repo_paths=(environment["repository"],),
            data_dir=environment["data_dir"],
            verify_keys=(environment["verify_key"],), signing_seed=environment["seed"])
        store.write(finalization.Receipt(
            plan_digest=environment["plan_digest"], rubric_version="test", verdict=verdict,
            cells=(), reviewer=finalization.ReviewerIdentity("route", "model", "session"),
            created_at_epoch=0.0))

    def _finalize(self, environment: Dict[str, object]):
        return self._invoke(self._finalize_arguments(environment))

    def test_parser_preserves_existing_verbs_and_orders_workspace_verbs(self):
        self.assertEqual(maestro.parser_verbs(maestro.build_parser()), (
            "bootstrap", "plan author",
            "plan validate", "plan finalize",
            "plan gate", "plan review", "plan ship",
            "plan show", "plan list",
            "plan set-aside", "plan set-aside-log",
            "deliver",
            "run start", "run status", "run list", "run pause", "run cancel",
            "run resume", "run convergence",
            "retry", "skip", "abandon",
            "attempt salvage",
            "workspace author", "workspace validate", "workspace finalize",
            "workspace start",
            "workspace status", "workspace cancel", "workspace resume",
            "workspace publish", "workspace rollback",
        ))

    def test_validate_requires_canonical_bytes_and_projects_digest_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            manifest = environment["manifest"]
            parsed = json.loads(manifest.read_text(encoding="utf-8"))
            manifest.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

            code, refusal = self._invoke([
                "workspace", "validate", "--manifest-file", str(manifest)])

            self.assertEqual(code, 2)
            self.assertEqual(refusal["outcome"], "WORKSPACE_NOT_CANONICAL")
            manifest.write_bytes(workspace_canonical.canonicalize_workspace(environment["workspace"]))
            code, validated = self._invoke([
                "workspace", "validate", "--manifest-file", str(manifest)])

            self.assertEqual(code, 0)
            self.assertEqual(validated["outcome"], "VALID")
            self.assertEqual(validated["digest"], workspace_digest.digest_of(manifest.read_bytes()))
            self.assertEqual(validated["participants"], [{
                "base_commit": environment["base_commit"], "mode": "write",
                "plan_digest": environment["plan_digest"], "repository_id": "api",
                "target_branch": "main",
            }])

    def test_finalize_signs_once_replays_and_refuses_nonpassing_child_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            self._write_plan_receipt(environment, finalization.Verdict.PASS)

            code, finalized = self._finalize(environment)
            replay_code, replayed = self._finalize(environment)

            self.assertEqual(code, 0)
            self.assertEqual(replay_code, 0)
            self.assertEqual(finalized, replayed)
            self.assertEqual(finalized["outcome"], "FINALIZED")
            self.assertTrue((environment["workspace_receipts"] /
                             (finalized["digest"] + ".json")).is_file())

        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            self._write_plan_receipt(environment, finalization.Verdict.FAIL)

            code, refusal = self._finalize(environment)

            self.assertEqual(code, 2)
            self.assertEqual(refusal["outcome"], "AUTHORIZATION_ERROR")

    def test_start_and_resume_refuse_missing_receipt_store_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            receipt_root = environment["workspace_receipts"]

            for verb in ("start", "resume"):
                with self.subTest(verb=verb):
                    code, failure = self._invoke(
                        self._execution_arguments(verb, environment, "release-1"))
                    self.assertEqual(code, 2)
                    self.assertEqual(failure["outcome"], "FILE_NOT_FOUND")
                    self.assertFalse(receipt_root.exists())

    def test_finalize_checks_every_repeated_plan_digest_participant_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first").mkdir()
            (root / "second").mkdir()
            data_dir = root / "data"
            data_dir.mkdir()
            digest = "a" * 64
            plan = workspace_model.parse_mapping({
                "schema_version": "maestro-workspace.v1",
                "workspace_id": "release",
                "repositories": [{
                    "repository_id": "first", "mode": "write", "path": "first",
                    "base_commit": "b" * 40, "plan_path": "first.json",
                    "plan_digest": digest, "target_branch": "main",
                    "run_argv": ["maestro"],
                }, {
                    "repository_id": "second", "mode": "write", "path": "second",
                    "base_commit": "c" * 40, "plan_path": "second.json",
                    "plan_digest": digest, "target_branch": "main",
                    "run_argv": ["maestro"],
                }],
            })
            manifest = root / "workspace.json"
            manifest.write_bytes(workspace_canonical.canonicalize_workspace(plan))
            seed = bytes(range(32))

            code, failure = self._invoke([
                "workspace", "finalize", "--manifest-file", str(manifest),
                "--plan-receipt-dir", str(root / "second" / "receipts"),
                "--workspace-receipt-dir", str(root / "workspace-receipts"),
                "--data-dir", str(data_dir), "--verify-key",
                receipt_crypto.seed_to_public_key(seed).hex(), "--signing-seed",
                seed.hex(),
            ])

        self.assertEqual(code, 2)
        self.assertEqual(failure["outcome"], "RECEIPT_STORE_LOCATION_ERROR")

    def test_finalize_refuses_plan_receipts_inside_a_read_only_participant_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read_only = root / "read-only"
            writer = root / "writer"
            read_only.mkdir()
            writer.mkdir()
            (root / "read-only-alias").symlink_to(read_only, target_is_directory=True)
            data_dir = root / "data"
            data_dir.mkdir()
            plan = workspace_model.parse_mapping({
                "schema_version": "maestro-workspace.v1",
                "workspace_id": "release",
                "repositories": [{
                    "repository_id": "read-only", "mode": "read_only",
                    "path": "read-only-alias", "base_commit": "b" * 40,
                }, {
                    "repository_id": "writer", "mode": "write", "path": "writer",
                    "base_commit": "c" * 40, "plan_path": "writer.json",
                    "plan_digest": "a" * 64, "target_branch": "main",
                    "run_argv": ["maestro"],
                }],
            })
            manifest = root / "workspace.json"
            manifest.write_bytes(workspace_canonical.canonicalize_workspace(plan))
            seed = bytes(range(32))

            code, failure = self._invoke([
                "workspace", "finalize", "--manifest-file", str(manifest),
                "--plan-receipt-dir", str(root / "read-only-alias" / "receipts"),
                "--workspace-receipt-dir", str(root / "workspace-receipts"),
                "--data-dir", str(data_dir), "--verify-key",
                receipt_crypto.seed_to_public_key(seed).hex(), "--signing-seed",
                seed.hex(),
            ])

        self.assertEqual(code, 2)
        self.assertEqual(failure["outcome"], "RECEIPT_STORE_LOCATION_ERROR")

    def test_start_then_resume_use_one_durable_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            self._write_plan_receipt(environment, finalization.Verdict.PASS)
            self.assertEqual(self._finalize(environment)[0], 0)
            calls = []

            class Coordinator:
                def __init__(self, **kwargs):
                    calls.append(kwargs)

                def run(self):
                    store = calls[-1]["store"]
                    try:
                        store.create_run(calls[-1]["run_id"],
                                         calls[-1]["workspace_digest"],
                                         calls[-1]["plan"])
                    except coordinator_store.RunAlreadyExists:
                        pass
                    return workspace_model.WorkspaceOutcome.ACCEPTED

            with mock.patch.object(maestro, "WorkspaceCoordinator", Coordinator):
                start_code, started = self._invoke(
                    self._execution_arguments("start", environment, "release-1"))
                repeated_code, repeated = self._invoke(
                    self._execution_arguments("start", environment, "release-1"))
                resume_code, resumed = self._invoke(
                    self._execution_arguments("resume", environment, "release-1"))

            self.assertEqual(start_code, 0)
            self.assertEqual(repeated_code, 2)
            self.assertEqual(repeated["outcome"], "RUN_ALREADY_EXISTS")
            self.assertEqual(resume_code, 0)
            self.assertEqual(started["run_id"], "release-1")
            self.assertEqual(resumed["run_id"], "release-1")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["run_id"], calls[1]["run_id"])
            self.assertEqual(calls[0]["workspace_digest"], calls[1]["workspace_digest"])
            store = coordinator_store.CoordinatorStore(environment["db"])
            try:
                self.assertEqual(store.get_run("release-1").workspace_digest,
                                 calls[0]["workspace_digest"])
            finally:
                store.close()

    def test_status_and_cancel_report_and_persist_authoritative_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            digest = workspace_digest.digest_of(environment["manifest"].read_bytes())
            store = coordinator_store.CoordinatorStore(environment["db"])
            try:
                store.create_run("release-1", digest, environment["workspace"])
                self.assertTrue(store.acquire_lease(
                    "release-1", "status-fixture", 0.0, 60.0))
                store.bind_repository_paths(
                    "release-1",
                    {"api": workspace_runtime.repository_binding(
                        environment["repository"])},
                    lease_owner="status-fixture")
                self.assertTrue(store.release_lease("release-1", "status-fixture"))
            finally:
                store.close()

            status_code, status = self._invoke([
                "workspace", "status", "--db", str(environment["db"]),
                "--run-id", "release-1"])
            cancel_code, cancelled = self._invoke([
                "workspace", "cancel", "--db", str(environment["db"]),
                "--run-id", "release-1", "--actor", "release-manager"])
            after_code, after = self._invoke([
                "workspace", "status", "--db", str(environment["db"]),
                "--run-id", "release-1"])

            self.assertEqual(status_code, 0)
            self.assertFalse(status["run"]["cancel_requested"])
            self.assertEqual(status["repositories"][0]["state"], "pending")
            self.assertEqual(
                status["repositories"][0]["resolved_path"],
                str(environment["repository"].resolve()))
            self.assertEqual(cancel_code, 0)
            self.assertEqual(cancelled["outcome"], "CANCELLATION_REQUESTED")
            self.assertEqual(after_code, 0)
            self.assertTrue(after["run"]["cancel_requested"])

    def test_publish_delegates_with_paths_from_persisted_manifest_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory), publication_mode="local_refs")
            digest = workspace_digest.digest_of(environment["manifest"].read_bytes())
            store = coordinator_store.CoordinatorStore(environment["db"])
            try:
                store.create_run("release-1", digest, environment["workspace"])
                self.assertTrue(store.acquire_lease(
                    "release-1", "fixture-owner", 0.0, 60.0))
                store.bind_repository_paths(
                    "release-1",
                    {"api": workspace_runtime.repository_binding(
                        environment["repository"])},
                    lease_owner="fixture-owner")
                store.claim_repository(
                    "release-1", "api", "child-1", "candidate",
                    lease_owner="fixture-owner")
                store.transition_repository(
                    "release-1", "api", workspace_model.RepositoryState.ACCEPTED,
                    accepted_sha=environment["base_commit"],
                    lease_owner="fixture-owner")
                store.declare_outcome(
                    "release-1", workspace_model.WorkspaceOutcome.ACCEPTED,
                    lease_owner="fixture-owner")
                self.assertTrue(store.release_lease(
                    "release-1", "fixture-owner"))
            finally:
                store.close()
            calls = []

            class Publisher:
                def __init__(self, **kwargs):
                    calls.append(kwargs)

                def publish(self, run_id):
                    target = coordinator_store.PublicationTarget(
                        repository_id="api", expected_base_sha=environment["base_commit"],
                        target_branch="main", candidate_branch="candidate",
                        accepted_sha=environment["base_commit"],
                        remote_url="https://git.example/api.git",
                        remote_repository="org/api",
                        state=workspace_model.PublicationState.PUBLISHED)
                    intent = coordinator_store.PublicationIntentRecord(
                        run_id=run_id, state=workspace_model.PublicationState.PUBLISHED,
                        targets=(target,), prepared_at="now", updated_at="now")
                    return SimpleNamespace(
                        run_id=run_id, outcome=workspace_model.WorkspaceOutcome.PUBLISHED,
                        intent=intent, reason=None, steps=())

            with mock.patch.object(maestro, "WorkspacePublisher", Publisher):
                code, published = self._invoke([
                    "workspace", "publish", "--manifest-dir", str(Path(directory)),
                    "--db", str(environment["db"]), "--run-id", "release-1",
                    "--actor", "release-manager"])

            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["repository_paths"],
                             {"api": environment["repository"].resolve()})
            self.assertEqual(calls[0]["actor"], "release-manager")
            self.assertEqual(published["outcome"], "published")
            self.assertEqual(published["publication"]["targets"][0]["repository_id"], "api")
            self.assertEqual(
                published["publication"]["targets"][0]["remote_url"],
                "https://git.example/api.git")
            self.assertEqual(
                published["publication"]["targets"][0]["remote_repository"],
                "org/api")

    def test_rollback_delegates_with_paths_from_persisted_manifest_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory), publication_mode="local_refs")
            digest = workspace_digest.digest_of(environment["manifest"].read_bytes())
            store = coordinator_store.CoordinatorStore(environment["db"])
            try:
                store.create_run("release-1", digest, environment["workspace"])
            finally:
                store.close()
            calls = []

            class Publisher:
                def __init__(self, **kwargs):
                    calls.append(kwargs)

                def rollback(self, run_id):
                    target = coordinator_store.PublicationTarget(
                        repository_id="api", expected_base_sha=environment["base_commit"],
                        target_branch="main", candidate_branch="candidate",
                        accepted_sha=environment["base_commit"],
                        remote_url=None, remote_repository=None,
                        state=workspace_model.PublicationState.ROLLED_BACK)
                    intent = coordinator_store.PublicationIntentRecord(
                        run_id=run_id, state=workspace_model.PublicationState.ROLLED_BACK,
                        targets=(target,), prepared_at="now", updated_at="now")
                    return SimpleNamespace(
                        run_id=run_id, outcome=workspace_model.WorkspaceOutcome.ACCEPTED,
                        intent=intent, reason=None, steps=())

            with mock.patch.object(maestro, "WorkspacePublisher", Publisher):
                code, rolled_back = self._invoke([
                    "workspace", "rollback", "--manifest-dir", str(Path(directory)),
                    "--db", str(environment["db"]), "--run-id", "release-1",
                    "--actor", "release-manager"])

            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["repository_paths"],
                             {"api": environment["repository"].resolve()})
            self.assertEqual(calls[0]["actor"], "release-manager")
            self.assertEqual(rolled_back["outcome"], "accepted")
            self.assertEqual(
                rolled_back["publication"]["targets"][0]["repository_id"], "api")

    def test_status_refuses_unavailable_database_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_database = Path(directory) / "typo" / "coordinator.sqlite"

            code, refusal = self._invoke([
                "workspace", "status", "--db", str(missing_database),
                "--run-id", "release-1"])

            self.assertEqual(code, 2)
            self.assertEqual(refusal, {
                "detail": "coordinator database is not an existing regular file: {0}".format(
                    missing_database),
                "outcome": "COORDINATOR_DATABASE_UNAVAILABLE",
            })
            self.assertFalse(missing_database.parent.exists())

        with tempfile.TemporaryDirectory() as directory:
            database_directory = Path(directory) / "coordinator.sqlite"
            database_directory.mkdir()

            code, refusal = self._invoke([
                "workspace", "status", "--db", str(database_directory),
                "--run-id", "release-1"])

            self.assertEqual(code, 2)
            self.assertEqual(refusal, {
                "detail": "coordinator database is not an existing regular file: {0}".format(
                    database_directory),
                "outcome": "COORDINATOR_DATABASE_UNAVAILABLE",
            })
            self.assertTrue(database_directory.is_dir())
            self.assertEqual(tuple(database_directory.iterdir()), ())

    def test_operational_workspace_failures_emit_one_internal_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            environment["db"].write_bytes(b"not a SQLite database")

            code, failure = self._invoke([
                "workspace", "status", "--db", str(environment["db"]),
                "--run-id", "release-1"])

            self.assertEqual(code, 3)
            self.assertEqual(set(failure), {"detail", "outcome"})
            self.assertEqual(failure["outcome"], "INTERNAL_ERROR")
            self.assertTrue(failure["detail"])

        with tempfile.TemporaryDirectory() as directory:
            environment = self._environment(Path(directory))
            digest = workspace_digest.digest_of(environment["manifest"].read_bytes())
            store = coordinator_store.CoordinatorStore(environment["db"])
            try:
                store.create_run("release-1", digest, environment["workspace"])
            finally:
                store.close()

            for exception in (
                    RuntimeError("launcher failure"),
                    subprocess.SubprocessError("subprocess failure")):
                with self.subTest(exception=type(exception).__name__):
                    class Publisher:
                        def __init__(self, **kwargs):
                            pass

                        def publish(self, run_id):
                            raise exception

                    with mock.patch.object(maestro, "WorkspacePublisher", Publisher):
                        code, failure = self._invoke([
                            "workspace", "publish",
                            "--manifest-dir", str(Path(directory)),
                            "--db", str(environment["db"]), "--run-id", "release-1",
                            "--actor", "release-manager"])

                    self.assertEqual(code, 3)
                    self.assertEqual(failure, {
                        "detail": str(exception), "outcome": "INTERNAL_ERROR"})

    def test_path_binding_refusal_has_a_stable_machine_outcome(self):
        self.assertEqual(
            maestro._workspace_error_code(
                coordinator_store.RepositoryPathMismatch("path mismatch")),
            "REPOSITORY_PATH_MISMATCH")



if __name__ == "__main__":
    unittest.main()
