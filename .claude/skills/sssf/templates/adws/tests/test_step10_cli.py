"""Every §11 operator verb exists and dispatches behavior or typed refusal."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import enforcement
from adw_modules import finalization
from adw_modules import launcher
from adw_modules import plan_validate as pv
from adw_modules import receipt_crypto
from adw_modules import scheduler_types


class OperatorCliTest(unittest.TestCase):
    def test_all_public_verbs_exist_in_the_real_parser(self):
        parser = maestro.build_parser()
        enforcement.assert_verbs(maestro.parser_verbs(parser))

    @contextlib.contextmanager
    def _repository_cwd(self, repo):
        previous = Path.cwd()
        os.chdir(repo)
        try:
            yield
        finally:
            os.chdir(previous)

    def _named_plan_configuration(self, root, *, state_root="../maestro-state"):
        repo = root / "repo"
        plan_file = repo / "plans" / "named" / "maestro-plan.v1"
        plan_file.parent.mkdir(parents=True)
        stored = b'{"plan":"stored bytes"}\n'
        plan_file.write_bytes(stored)
        (repo / "adws").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        state = (repo / state_root).resolve()
        route_dir = state / repo.name / "route-receipts"
        route_dir.mkdir(parents=True)
        for route in ("omp", "claude"):
            (route_dir / (route + ".json")).write_text("{}", encoding="utf-8")
        signing_seed = receipt_crypto.generate_seed()
        route_seed = receipt_crypto.generate_seed()
        environment = {
            "MAESTRO_TEST_VERIFY_KEY": receipt_crypto.seed_to_public_key(
                signing_seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": signing_seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY": receipt_crypto.seed_to_public_key(
                route_seed).hex(),
        }
        config = {
            "schema": "maestro-config.v1",
            "plans_dir": "plans",
            "state_root": state_root,
            "keys": {
                "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
            },
            "executables": binaries,
            "route_receipts": {
                "omp": "route-receipts/omp.json",
                "claude": "route-receipts/claude.json",
            },
            "reviewer": {
                "route": "claude",
                "model": "review-model",
                "effort": "high",
                "finalization_timeout_s": 60,
                "turn_timeout_s": 20,
                "poll_interval_s": 1,
            },
            "execution": {
                "route": "omp",
                "model": "execution-model",
                "effort": "medium",
                "profile": "test",
                "concurrency": 2,
                "node_timeout_s": 120,
                "turn_timeout_s": 30,
                "final_acceptance_timeout_s": 45,
                "backstop_t_s": 600,
                "semantic_ceiling": 3,
            },
        }
        (repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8")
        return {
            "environment": environment,
            "plan_file": plan_file,
            "repo": repo,
            "state": state / repo.name,
            "stored": stored,
        }

    def test_named_plan_validate_binds_repository_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._named_plan_configuration(Path(tmp))
            with mock.patch.dict(
                    os.environ, fixture["environment"], clear=False), \
                    self._repository_cwd(fixture["repo"]), \
                    mock.patch.object(
                        maestro, "_plan_validate", return_value=0) as validate:
                self.assertEqual(maestro.main(["plan", "validate", "named"]), 0)
        args = validate.call_args.args[0]
        self.assertEqual(Path(args.plan_file).resolve(),
                         fixture["plan_file"].resolve())
        self.assertEqual(Path(args.repo).resolve(), fixture["repo"].resolve())
        self.assertEqual(Path(args.data_dir).resolve(),
                         (fixture["state"] / "data").resolve())
        self.assertEqual(Path(args.receipt_dir).resolve(),
                         (fixture["state"] / "receipts").resolve())
        self.assertEqual(args.verify_key, [fixture["environment"][
            "MAESTRO_TEST_VERIFY_KEY"]])
        self.assertEqual(args.signing_seed, fixture["environment"][
            "MAESTRO_TEST_SIGNING_SEED"])
        self.assertEqual(Path(args.herdr).resolve(),
                         (fixture["repo"].parent / "herdr").resolve())
        self.assertEqual(Path(args.omp).resolve(),
                         (fixture["repo"].parent / "omp").resolve())
        self.assertEqual(Path(args.claude).resolve(),
                         (fixture["repo"].parent / "claude").resolve())

    def test_named_plan_finalize_derives_reviewer_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._named_plan_configuration(Path(tmp))
            digest = maestro.plan_digest.digest_of(fixture["stored"])
            with mock.patch.dict(
                    os.environ, fixture["environment"], clear=False), \
                    self._repository_cwd(fixture["repo"]), \
                    mock.patch.object(
                        maestro, "_plan_finalize", return_value=0) as finalize:
                self.assertEqual(maestro.main(["plan", "finalize", "named"]), 0)
        args = finalize.call_args.args[0]
        root = fixture["state"] / "finalization" / digest
        self.assertEqual(Path(args.plan_file).resolve(),
                         fixture["plan_file"].resolve())
        self.assertEqual(args.reviewer_route, "claude")
        self.assertEqual(args.reviewer_model, "review-model")
        self.assertEqual(args.reviewer_effort, "high")
        self.assertIsNone(args.reviewer_profile)
        self.assertEqual(args.reviewer_session_dir, str(root / "session"))
        self.assertEqual(args.reviewer_report_file, str(root / "report.json"))
        self.assertEqual(args.finalization_timeout_s, 60.0)

    def test_named_run_start_derives_digest_and_external_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._named_plan_configuration(Path(tmp))
            run_uuid = SimpleNamespace(hex="0123456789abcdef")
            with mock.patch.dict(
                    os.environ, fixture["environment"], clear=False), \
                    self._repository_cwd(fixture["repo"]), \
                    mock.patch.object(maestro.uuid, "uuid4",
                                      return_value=run_uuid), \
                    mock.patch.object(
                        maestro, "_run_start", return_value=0) as start:
                self.assertEqual(maestro.main(["run", "start", "named"]), 0)
        args = start.call_args.args[0]
        run_root = fixture["state"] / "runs" / ("run-" + run_uuid.hex)
        self.assertEqual(Path(args.plan_file).resolve(),
                         fixture["plan_file"].resolve())
        self.assertEqual(args.digest, maestro.plan_digest.digest_of(
            fixture["stored"]))
        self.assertEqual(args.run_id, "run-" + run_uuid.hex)
        self.assertEqual(args.db, str(fixture["state"] / "lifecycle.sqlite3"))
        self.assertEqual(args.integration_path, str(run_root / "integration"))
        self.assertEqual(args.worktrees_root, str(run_root / "worktrees"))
        self.assertEqual(args.scratch_root, str(run_root / "scratch"))
        self.assertEqual(args.agent_route, "omp")
        self.assertEqual(args.agent_profile, "test")
        for path in (args.db, args.data_dir, args.receipt_dir):
            self.assertFalse(maestro._path_is_within(
                Path(path), fixture["repo"]))
            self.assertFalse(maestro._path_is_within(
                Path(path), Path(args.integration_path)))

    def test_named_plan_configuration_fails_closed_for_bad_env_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._named_plan_configuration(root)
            with mock.patch.dict(os.environ, {}, clear=True), \
                    self._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro.main(["plan", "validate", "named"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["outcome"],
                             "MAESTRO_ENVIRONMENT_REQUIRED")

            malformed_repo = root / "malformed-config"
            (malformed_repo / "adws").mkdir(parents=True)
            (malformed_repo / "adws" / "maestro.config.yaml").write_text(
                "[", encoding="utf-8")
            with self._repository_cwd(malformed_repo), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro.main(["plan", "validate", "named"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["outcome"],
                             "MAESTRO_CONFIGURATION_INVALID")

            invalid = self._named_plan_configuration(
                root / "invalid", state_root=".maestro-state")
            with mock.patch.dict(
                    os.environ, invalid["environment"], clear=False), \
                    self._repository_cwd(invalid["repo"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro.main(["plan", "validate", "named"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["outcome"],
                             "MAESTRO_CONFIGURATION_INVALID")

            with mock.patch.dict(
                    os.environ, fixture["environment"], clear=False), \
                    self._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro.main(["plan", "validate", "missing"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["outcome"],
                             "MAESTRO_CONFIGURATION_INVALID")

            with mock.patch.dict(
                    os.environ, fixture["environment"], clear=False), \
                    self._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro.main([
                    "plan", "validate", "named", "--repo", "override"])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())["outcome"],
                             "MAESTRO_CONFIGURATION_INVALID")

    def test_plan_validate_malformed_bytes_is_authoring_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text("{}")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro.main(["plan", "validate", str(plan), "--repo", tmp])
            self.assertEqual(code, 2)
            self.assertIn("AUTHORING_BLOCKED", output.getvalue())

    def _receipt_environment(self, tmp: Path):
        repo = tmp / "repo"
        repo.mkdir()
        data_dir = tmp / "data"
        data_dir.mkdir()
        receipt_dir = tmp / "receipts"
        digest = "a" * 64
        seed = receipt_crypto.generate_seed()
        store = finalization.ReceiptStore(
            receipt_dir, repo_paths=(repo,), data_dir=data_dir,
            verify_keys=[receipt_crypto.seed_to_public_key(seed)],
            signing_seed=seed)
        receipt = finalization.Receipt(
            plan_digest=digest, rubric_version="test.v1",
            verdict=finalization.Verdict.PASS, cells=(),
            reviewer=finalization.ReviewerIdentity(
                route="test", model="test", session_id="test"),
            created_at_epoch=0.0)
        plan_file = tmp / "plan.json"
        plan_file.write_text("{}")
        return {
            "data_dir": data_dir,
            "digest": digest,
            "plan_file": plan_file,
            "receipt": receipt,
            "receipt_dir": receipt_dir,
            "repo": repo,
            "seed": seed,
            "store": store,
        }

    def _validation_arguments(self, environment, *verification):
        return [
            "plan", "validate", str(environment["plan_file"]),
            "--repo", str(environment["repo"]),
            "--receipt-dir", str(environment["receipt_dir"]),
            "--data-dir", str(environment["data_dir"]),
            *verification,
        ]

    def _validate_receipt_gate(self, digest):
        def validate(_stored, _repo, *, receipts, collector):
            if receipts.has_receipt(digest):
                return pv.ValidationResult(
                    pv.Outcome.FINALIZATION_ELIGIBLE, digest, ())
            return pv.ValidationResult(pv.Outcome.AUTHORING_BLOCKED, None, ())
        return validate

    def _run_receipt_gate(self, argv, digest):
        output = io.StringIO()
        with mock.patch.object(
                maestro.pv, "validate_plan",
                side_effect=self._validate_receipt_gate(digest)):
            with contextlib.redirect_stdout(output):
                code = maestro.main(argv)
        return code, json.loads(output.getvalue())

    def test_plan_validate_rejects_a_forged_receipt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            (environment["receipt_dir"] / (environment["digest"] + ".json")
             ).write_text("{}")
            code, payload = self._run_receipt_gate(
                self._validation_arguments(
                    environment, "--verify-key",
                    receipt_crypto.seed_to_public_key(
                        environment["seed"]).hex()),
                environment["digest"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_validate_accepts_a_valid_signed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            code, payload = self._run_receipt_gate(
                self._validation_arguments(
                    environment, "--verify-key",
                    receipt_crypto.seed_to_public_key(
                        environment["seed"]).hex()),
                environment["digest"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "FINALIZATION_ELIGIBLE")

    def test_plan_validate_rejects_a_receipt_under_the_wrong_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            wrong_key = receipt_crypto.seed_to_public_key(
                receipt_crypto.generate_seed())
            code, payload = self._run_receipt_gate(
                self._validation_arguments(
                    environment, "--verify-key", wrong_key.hex()),
                environment["digest"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_validate_fails_closed_without_verifier_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            with mock.patch.dict(
                    os.environ,
                    {"MAESTRO_RECEIPT_DIR": str(environment["receipt_dir"])},
                    clear=False):
                code, payload = self._run_receipt_gate(
                    ["plan", "validate", str(environment["plan_file"]),
                     "--repo", str(environment["repo"])],
                    environment["digest"])
        self.assertEqual(code, 3)
        self.assertEqual(
            payload["outcome"], "RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED")

    def _receipt_access_arguments(self, environment):
        return [
            "--repo", str(environment["repo"]),
            "--receipt-dir", str(environment["receipt_dir"]),
            "--data-dir", str(environment["data_dir"]),
            "--verify-key", receipt_crypto.seed_to_public_key(
                environment["seed"]).hex(),
        ]

    def _run_receipt_access(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = maestro.main(argv)
        return code, json.loads(output.getvalue())

    def test_plan_show_emits_only_the_verified_receipt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])

            code, receipt = self._run_receipt_access([
                "plan", "show", environment["digest"],
                *self._receipt_access_arguments(environment)])

        self.assertEqual(code, 0)
        self.assertEqual(receipt, json.loads(
            environment["receipt"].to_bytes().decode("utf-8")))


    def test_plan_list_returns_only_verified_digest_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            (environment["receipt_dir"] / "not-a-receipt.json").write_text(
                "untrusted", encoding="utf-8")

            code, digests = self._run_receipt_access([
                "plan", "list", *self._receipt_access_arguments(environment)])

        self.assertEqual(code, 0)
        self.assertEqual(digests, [environment["digest"]])
    def test_plan_show_refuses_traversal_and_mismatched_verified_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            source = environment["store"].path_for(environment["digest"])
            source.rename(environment["store"].path_for("b" * 64))
            Path(str(source) + ".sig").rename(
                environment["store"].signature_path_for("b" * 64))

            traversal_code, traversal = self._run_receipt_access([
                "plan", "show", "../untrusted",
                *self._receipt_access_arguments(environment)])
            mismatch_code, mismatch = self._run_receipt_access([
                "plan", "show", "b" * 64,
                *self._receipt_access_arguments(environment)])

        self.assertEqual(traversal_code, 3)
        self.assertEqual(traversal["outcome"], "RECEIPT_VERIFICATION_FAILED")
        self.assertEqual(mismatch_code, 3)
        self.assertEqual(mismatch["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_list_refuses_tampering_instead_of_listing_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            environment["store"].write(environment["receipt"])
            (environment["receipt_dir"] / ("b" * 64 + ".json")).write_text(
                "untrusted", encoding="utf-8")

            code, failure = self._run_receipt_access([
                "plan", "list", *self._receipt_access_arguments(environment)])

        self.assertEqual(code, 3)
        self.assertEqual(failure["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_show_maps_non_utf8_signature_to_typed_verification_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            path = environment["store"].write(environment["receipt"])
            Path(str(path) + ".sig").write_bytes(b"\xff")

            code, failure = self._run_receipt_access([
                "plan", "show", environment["digest"],
                *self._receipt_access_arguments(environment)])

        self.assertEqual(code, 3)
        self.assertEqual(failure["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_show_maps_signed_schema_failure_to_typed_verification_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._receipt_environment(Path(tmp))
            data = b'{"not":"a receipt"}'
            environment["store"].path_for(environment["digest"]).write_bytes(data)
            environment["store"].signature_path_for(environment["digest"]).write_text(
                receipt_crypto.sign(environment["seed"], data).hex() + "\n",
                encoding="ascii")

            code, failure = self._run_receipt_access([
                "plan", "show", environment["digest"],
                *self._receipt_access_arguments(environment)])

        self.assertEqual(code, 3)
        self.assertEqual(failure["outcome"], "RECEIPT_VERIFICATION_FAILED")

    def test_plan_show_missing_store_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            data = root / "data"
            missing = root / "missing" / "receipts"
            repo.mkdir()
            data.mkdir()

            code, failure = self._run_receipt_access([
                "plan", "show", "a" * 64, "--repo", str(repo),
                "--receipt-dir", str(missing), "--data-dir", str(data),
                "--verify-key", receipt_crypto.seed_to_public_key(
                    receipt_crypto.generate_seed()).hex()])

            self.assertEqual(code, 3)
            self.assertEqual(failure, {
                "detail": "a" * 64,
                "outcome": "FINALIZED_PLAN_NOT_FOUND",
            })
            self.assertFalse(missing.exists())

    def test_plan_receipt_access_has_a_stable_missing_configuration_refusal(self):
        code, refusal = self._run_receipt_access(["plan", "list"])

        self.assertEqual(code, 3)
        self.assertEqual(refusal, {
            "detail": (
                "--receipt-dir, --data-dir, and at least one --verify-key "
                "are required for receipt access"),
            "outcome": "RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED",
        })

    def test_plan_finalize_refuses_a_signing_seed_absent_from_verify_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            data = root / "data"
            repo.mkdir()
            data.mkdir()
            signer = receipt_crypto.generate_seed()
            args = SimpleNamespace(
                receipt_dir=str(root / "receipts"), data_dir=str(data),
                repo=str(repo),
                verify_key=[receipt_crypto.seed_to_public_key(
                    receipt_crypto.generate_seed()).hex()],
                signing_seed=signer.hex())

            with self.assertRaises(maestro._PlanReceiptConfigurationError):
                maestro._finalization_store(args)

    def test_plan_finalize_runs_validation_adapter_and_emits_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_file = root / "plan.json"
            plan_file.write_text("{}", encoding="utf-8")
            digest = "a" * 64
            receipt = finalization.Receipt(
                plan_digest=digest, rubric_version="test.v1",
                verdict=finalization.Verdict.PASS, cells=(),
                reviewer=finalization.ReviewerIdentity(
                    route="omp", model="test", session_id="session"),
                created_at_epoch=0.0)
            args = mock.Mock(
                plan_file=str(plan_file), repo=str(root),
                receipt_dir=str(root / "receipts"), data_dir=str(root / "data"),
                verify_key=[receipt_crypto.seed_to_public_key(
                    receipt_crypto.generate_seed()).hex()],
                signing_seed=receipt_crypto.generate_seed().hex())
            validation = pv.ValidationResult(
                pv.Outcome.FINALIZATION_ELIGIBLE, digest, ())
            outcome = finalization.FinalizationOutcome(
                finalization.Verdict.PASS, receipt, replayed=False)
            with mock.patch.object(
                    maestro.pv, "validate_plan", return_value=validation
            ), mock.patch.object(
                    maestro.plan_model, "parse_bytes", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro.plan_finalization, "review_objects", return_value=()
            ), mock.patch.object(
                    maestro, "_finalization_store", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_reviewer_window_factory", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro.finalization, "finalize", return_value=outcome
            ) as finalize, contextlib.redirect_stdout(io.StringIO()) as output:
                code = maestro._plan_finalize(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "digest": digest,
            "outcome": "FINALIZED",
            "replayed": False,
            "verdict": "PASS",
        })
        self.assertEqual(finalize.call_args.kwargs["plan_digest"], digest)

    def test_run_resume_refuses_before_claiming_execution(self):
        output = io.StringIO()
        with mock.patch.object(maestro.lc, "LifecycleStore") as store:
            with contextlib.redirect_stdout(output):
                code = maestro.main(["run", "resume", "run-1", "--db", "state.db"])

        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["outcome"],
                         "RUN_CONFIGURATION_REQUIRED")
        store.return_value.resume_run.assert_not_called()

    def test_run_refuses_retry_while_unproven_herdr_handle_is_reclaimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir()
            scratch = root / "scratch"
            scratch.mkdir()
            token = "run-1-agent-1"
            handle = launcher.LaunchHandle(
                token, "pane-1", "agent-1", root,
                transcript_path=scratch / "session.jsonl")

            class FailingRoute:
                def __init__(self):
                    self.handles = {}
                    self.cancel_calls = 0

                def launch(self, _spec):
                    self.handles[token] = handle
                    return handle

                def poll(self, _handle):
                    return launcher.PollResult(launcher.PollState.RUNNING)

                def cancel(self, _handle, _deadline):
                    self.cancel_calls += 1
                    raise launcher.HarnessQuiescenceError(
                        "HERDR_QUIESCENCE_UNPROVEN:" + token)

                def reclaim(self, requested):
                    found = self.handles.get(requested)
                    return (found,) if found is not None else ()

            route = FailingRoute()
            node = SimpleNamespace(
                kind=scheduler_types.NodeKind.AGENT, node_id="agent")
            plan = SimpleNamespace(
                agent_nodes=(node,),
                merge_policy=SimpleNamespace(
                    integration_branch="main",
                    integration_gate=SimpleNamespace(runner="none", argv=())),
                node_by_id=lambda: {
                    "agent": SimpleNamespace(instruction="do the work")},
                to_plan_nodes=lambda: ())
            attempt = SimpleNamespace(path=root, scratch=scratch)
            record = SimpleNamespace(node_id="agent", attempt_no=1)

            class RetryingScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    self.deps = deps

                def run(self):
                    try:
                        self.deps.run_node(
                            attempt, node, record, "", lambda _pid: None,
                            lambda: True)
                    except launcher.HarnessQuiescenceError:
                        self.deps.quiesce_attempt(record, "retry")
                    raise AssertionError("unproven Herdr handle was released")

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(Path(tmp) / "state.db"),
                run_id="run-1", integration_path=str(root), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch-root"), digest="a" * 64,
                agent_route="omp", agent_model="model", agent_effort="high",
                agent_profile="profile")
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro, "_runtime_launcher", return_value=route
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore"
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", RetryingScheduler
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)

        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["outcome"],
                         "HARNESSQUIESCENCEERROR")
        self.assertEqual(route.cancel_calls, 2)
        self.assertEqual(route.reclaim(token), (handle,))

    def test_run_refuses_retry_while_code_process_group_is_unproven(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir()

            class LiveProcess:
                pid = 4096
                returncode = None

                def __init__(self, *_args, **_kwargs):
                    pass

                def poll(self):
                    return None

            node = SimpleNamespace(
                kind=scheduler_types.NodeKind.CODE, node_id="code",
                command=("unreachable",))
            plan = SimpleNamespace(
                agent_nodes=(),
                merge_policy=SimpleNamespace(
                    integration_branch="main",
                    integration_gate=SimpleNamespace(runner="none", argv=())),
                to_plan_nodes=lambda: ())
            attempt = SimpleNamespace(path=root, scratch=root / "scratch")
            record = SimpleNamespace(node_id="code", attempt_no=1)

            class RetryingScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    self.deps = deps

                def run(self):
                    try:
                        self.deps.run_node(
                            attempt, node, record, "", lambda _pid: None,
                            lambda: True)
                    except RuntimeError:
                        self.deps.quiesce_attempt(record, "retry")
                    raise AssertionError("unproven process group was released")

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(Path(tmp) / "state.db"),
                run_id="run-1", integration_path=str(root), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch-root"), digest="a" * 64)
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore"
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", RetryingScheduler
            ), mock.patch.object(
                    maestro.subprocess, "Popen", LiveProcess
            ), mock.patch.object(
                    maestro.launcher, "quiesce_process_group"
            ) as quiesce, mock.patch.object(
                    maestro.launcher, "_process_group_absent", return_value=False
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)

        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["outcome"],
                         "RUNTIMEERROR")
        self.assertEqual(quiesce.call_count, 2)

    def test_run_path_preflight_refuses_data_directory_aliases_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            data_dir = root / "data"
            receipt_dir = root / "receipts"
            worktrees_root = root / "worktrees"
            scratch_root = root / "scratch"
            for path in (repo, data_dir, receipt_dir, worktrees_root, scratch_root):
                path.mkdir()
            alias = root / "data-alias"
            alias.symlink_to(data_dir, target_is_directory=True)

            def args_for(db):
                return SimpleNamespace(
                    db=str(db), repo=str(repo), data_dir=str(data_dir),
                    receipt_dir=str(receipt_dir),
                    integration_path=str(root / "integration"),
                    worktrees_root=str(worktrees_root),
                    scratch_root=str(scratch_root))

            with self.assertRaises(ValueError):
                maestro._validate_run_paths(args_for(alias / "state.db"), object())

            data_database = data_dir / "state.db"
            data_database.touch()
            hardlinked_alias = root / "state.db"
            os.link(data_database, hardlinked_alias)
            with self.assertRaises(ValueError):
                maestro._validate_run_paths(args_for(hardlinked_alias), object())

    def test_unconfigured_run_is_typed_refusal_not_success_stub(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = maestro.main(["run", "start", "a" * 64])
        self.assertEqual(code, 3)
        self.assertEqual(
            output.getvalue().splitlines(),
            ['{"detail": "repository, finalized receipt, launcher roster, '
             'and liveness bounds are required", '
             '"outcome": "RUN_CONFIGURATION_REQUIRED"}'])

    def test_pre_baseline_missing_handle_is_vacuous_later_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = SimpleNamespace(node_id="code", attempt_no=1)
            captured = {}

            class CapturingScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    self.deps = deps

                def run(self):
                    self.deps.quiesce_attempt(record, "pre-baseline")
                    captured["pre_baseline"] = True
                    try:
                        self.deps.quiesce_attempt(record, "pre-inventory")
                    except RuntimeError as exc:
                        captured["later"] = str(exc)
                    return SimpleNamespace(
                        outcome=scheduler_types.RunOutcome.BLOCKED,
                        merged=(), blocked=())

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(root / "state.db"),
                run_id="run-1", integration_path=str(root), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch-root"), digest="a" * 64)
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan",
                    return_value=SimpleNamespace(
                        agent_nodes=(),
                        merge_policy=SimpleNamespace(
                            integration_branch="main",
                            integration_gate=SimpleNamespace(runner="none", argv=())),
                        to_plan_nodes=lambda: ())
            ), mock.patch.object(
                    maestro, "_validate_run_paths"
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore"
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", CapturingScheduler
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)

        self.assertEqual(code, 0)
        self.assertTrue(captured.get("pre_baseline"))
        self.assertIn("PROCESS_GROUP_UNTRACKED:pre-inventory", captured["later"])

    def test_real_scheduler_crosses_the_cli_pre_baseline_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            integration = root / "integration"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "maestro@example.invalid"],
                cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Maestro Test"],
                cwd=repo, check=True)
            (repo / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "integration/run-1",
                 str(integration), "HEAD"],
                cwd=repo, check=True)

            node = scheduler_types.PlanNode(
                node_id="code", kind=scheduler_types.NodeKind.CODE, depth=0,
                outputs=("out.txt",),
                command=(
                    sys.executable, "-c",
                    "from pathlib import Path; Path('out.txt').write_text('done\\n')"),
                expects_changes=True)
            plan = SimpleNamespace(
                agent_nodes=(),
                merge_policy=SimpleNamespace(
                    integration_branch="integration/run-1",
                    integration_gate=SimpleNamespace(runner="fixture", argv=("gate",))),
                to_plan_nodes=lambda: (node,))
            green = maestro.worktree.GateResult(
                label="gate", scope="node", selector="code",
                command=("gate",), exit_code=0, green=True,
                counts={"passed": 1, "failed": 0, "skipped": 0, "errored": 0})

            def run_gate(_attempt, _node, _phase, _cancel_requested):
                return green

            def integration_gate(_path, _specs, _cancel_requested):
                return green

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(root / "state.db"),
                run_id="run-1", integration_path=str(integration), repo=str(repo),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch"), digest="a" * 64)
            config = scheduler_types.SchedulerConfig(
                concurrency=1, node_timeout_s=30, turn_timeout_s=10,
                final_acceptance_timeout_s=30, backstop_t_s=120,
                semantic_ceiling=1)
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=config
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro, "_validate_run_paths"
            ), mock.patch.object(
                    maestro, "_scheduler_gate_deps",
                    return_value=(run_gate, integration_gate)
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)

            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["outcome"], "ACCEPTED")
            self.assertEqual((integration / "out.txt").read_text(), "done\n")

    def test_code_and_agent_share_one_launch_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            scratch = root / "scratch"
            scratch.mkdir(parents=True)
            captured = {}

            class FinishedProcess:
                pid = 7
                returncode = 0

                def __init__(self, *_args, **kwargs):
                    captured["popen_env"] = kwargs.get("env")

                def poll(self):
                    return 0

            class RecordingRoute:
                def launch(self, spec):
                    captured["spec_env"] = spec.environment
                    return launcher.LaunchHandle(
                        spec.correlation_token, "pane-1", "agent-1", root,
                        transcript_path=scratch / "session.jsonl")

                def poll(self, _handle):
                    return launcher.PollResult(launcher.PollState.EXITED, 0, "ok")

                def cancel(self, _handle, _deadline):
                    return None

                def reclaim(self, _token):
                    return ()

            route = RecordingRoute()
            shared = {"XDG_CACHE_HOME": str(scratch / "xdg")}
            code_node = SimpleNamespace(
                kind=scheduler_types.NodeKind.CODE, node_id="code",
                command=("true",))
            agent_node = SimpleNamespace(
                kind=scheduler_types.NodeKind.AGENT, node_id="agent")
            plan = SimpleNamespace(
                agent_nodes=(agent_node,),
                merge_policy=SimpleNamespace(
                    integration_branch="main",
                    integration_gate=SimpleNamespace(runner="none", argv=())),
                node_by_id=lambda: {
                    "agent": SimpleNamespace(instruction="do the work")},
                to_plan_nodes=lambda: ())
            attempt = SimpleNamespace(path=root, scratch=scratch)
            record = SimpleNamespace(node_id="code", attempt_no=1)

            class DualScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    self.deps = deps

                def run(self):
                    self.deps.run_node(
                        attempt, code_node, record, "", lambda _pid: None,
                        lambda: False)
                    agent_record = SimpleNamespace(node_id="agent", attempt_no=1)
                    self.deps.run_node(
                        attempt, agent_node, agent_record, "", lambda _pid: None,
                        lambda: False)
                    return SimpleNamespace(
                        outcome=scheduler_types.RunOutcome.ACCEPTED,
                        merged=(), blocked=())

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(Path(tmp) / "state.db"),
                run_id="run-1", integration_path=str(root), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch-root"), digest="a" * 64,
                agent_route="omp", agent_model="model", agent_effort="high",
                agent_profile="profile")
            store = mock.Mock()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro, "_runtime_launcher", return_value=route
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore", return_value=store
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", DualScheduler
            ), mock.patch.object(
                    maestro.subprocess, "Popen", FinishedProcess
            ), mock.patch.object(
                    maestro.worktree, "launch_env", return_value=shared
            ) as launch_env:
                code = maestro._run_start(args)

        self.assertEqual(code, 0)
        self.assertIs(captured["popen_env"], shared)
        self.assertIs(captured["spec_env"], shared)
        self.assertEqual(launch_env.call_count, 2)
        store.mark_launched.assert_called()
        extra = store.mark_launched.call_args.kwargs["extra"]
        self.assertEqual(extra["session_path"], str(scratch / "session.jsonl"))

    def test_missing_transcript_path_fails_closed_before_arming(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            scratch = root / "scratch"
            scratch.mkdir(parents=True)
            armed = []
            cancelled = []

            class NoTranscriptRoute:
                def launch(self, spec):
                    return launcher.LaunchHandle(
                        spec.correlation_token, "pane-1", "agent-1", root)

                def cancel(self, handle, _deadline):
                    cancelled.append(handle.correlation_token)

                def reclaim(self, _token):
                    return ()

            node = SimpleNamespace(
                kind=scheduler_types.NodeKind.AGENT, node_id="agent")
            plan = SimpleNamespace(
                agent_nodes=(node,),
                merge_policy=SimpleNamespace(
                    integration_branch="main",
                    integration_gate=SimpleNamespace(runner="none", argv=())),
                node_by_id=lambda: {
                    "agent": SimpleNamespace(instruction="do the work")},
                to_plan_nodes=lambda: ())
            attempt = SimpleNamespace(path=root, scratch=scratch)
            record = SimpleNamespace(node_id="agent", attempt_no=1)
            store = mock.Mock()

            class ArmingScheduler:
                def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                    self.deps = deps

                def run(self):
                    try:
                        self.deps.run_node(
                            attempt, node, record, "", armed.append, lambda: False)
                    finally:
                        self.deps.quiesce_attempt(record, "pre-inventory")
                    raise AssertionError("missing transcript armed the attempt")

            args = SimpleNamespace(
                plan_file=str(root / "plan.json"), db=str(Path(tmp) / "state.db"),
                run_id="run-1", integration_path=str(root), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch-root"), digest="a" * 64,
                agent_route="omp", agent_model="model", agent_effort="high",
                agent_profile="profile")
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro, "_runtime_launcher", return_value=NoTranscriptRoute()
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore", return_value=store
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", ArmingScheduler
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)

        self.assertEqual(code, 3)
        self.assertIn("SESSION_PATH_MISSING", output.getvalue())
        self.assertEqual(armed, [])
        store.mark_launched.assert_not_called()
        self.assertEqual(cancelled, ["run-1-agent-1"])

    def test_sqlite3_error_is_one_json_typed_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "not-a-db"
            bogus.write_text("not sqlite")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro.main(
                    ["run", "status", "run-1", "--db", str(bogus)])
        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(set(payload), {"detail", "outcome"})
        self.assertTrue(payload["outcome"].endswith("ERROR")
                        or "DATABASE" in payload["outcome"])


if __name__ == "__main__":
    unittest.main()
