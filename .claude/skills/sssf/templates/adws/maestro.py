#!/usr/bin/env python3
"""Maestro public operator workflow (§11)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from adw_modules import coordinator as coordinator
from adw_modules import coordinator_store
from adw_modules import finalization
from adw_modules import finalization_window
from adw_modules import launcher
from adw_modules import lifecycle as lc
from adw_modules import participant
from adw_modules import plan_finalization
from adw_modules import plan_model
from adw_modules import plan_validate as pv
from adw_modules import publication
from adw_modules import receipt_crypto
from adw_modules import route_receipts
from adw_modules import scheduler
from adw_modules import scheduler_types
from adw_modules import watchdog
from adw_modules import worktree
from adw_modules import workspace_canonical
from adw_modules import workspace_digest
from adw_modules import workspace_model
from adw_modules import workspace_runtime
from adw_modules import workspace_receipt

WorkspaceCoordinator = coordinator.WorkspaceCoordinator
SubprocessParticipantRunner = participant.SubprocessParticipantRunner
WorkspacePublisher = publication.WorkspacePublisher


class _PlanReceiptConfigurationError(RuntimeError):
    """Receipt verification cannot be configured safely."""


class _PlanReceiptVerificationError(RuntimeError):
    """A receipt presented for authorization cannot be verified."""


class _RunPathConfigurationError(ValueError):
    """Run authority storage is located in a participant-writable boundary."""


def _refusal(outcome: str, detail: str) -> int:
    print(json.dumps({"detail": detail, "outcome": outcome}, sort_keys=True))
    return 3


class _VerifiedReceipts:
    """The plan validator's receipt index, backed by signed receipts."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._store: Optional[finalization.ReceiptStore] = None

    def _receipt_store(self) -> finalization.ReceiptStore:
        if self._store is None:
            self._store = _plan_receipt_store(
                self._args,
                missing_detail=(
                    "--receipt-dir, --data-dir, and at least one --verify-key "
                    "are required when validating a superseded plan"))
        return self._store

    def has_receipt(self, digest: str) -> bool:
        try:
            receipt = self._receipt_store().load(digest)
        except FileNotFoundError:
            return False
        except (finalization.ReceiptInvalid, finalization.SignatureMissing,
                finalization.SignatureInvalid, UnicodeError, ValueError,
                KeyError) as exc:
            raise _PlanReceiptVerificationError(str(exc)) from exc
        if receipt.plan_digest != digest:
            raise _PlanReceiptVerificationError(
                "the receipt stored for {0} names {1}".format(
                    digest, receipt.plan_digest))
        return True


def _plan_validate(args: argparse.Namespace) -> int:
    try:
        result = pv.validate_plan(
            Path(args.plan_file).read_bytes(), args.repo,
            receipts=_VerifiedReceipts(args),
            collector=pv.SubprocessCollector())
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    payload = {
        "outcome": result.outcome.value,
        "digest": result.digest,
        "blockers": [
            {"obligation": row.obligation.value, "pointer": row.pointer,
             "message": row.message}
            for row in result.blockers
        ],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.eligible else 2


def _finalization_store(args: argparse.Namespace) -> finalization.ReceiptStore:
    if (not args.receipt_dir or not args.data_dir or not args.verify_key
            or not args.signing_seed):
        raise _PlanReceiptConfigurationError(
            "--receipt-dir, --data-dir, --verify-key, and --signing-seed "
            "are required to finalize a plan")
    try:
        verify_keys = tuple(bytes.fromhex(value) for value in args.verify_key)
        signing_seed = bytes.fromhex(args.signing_seed)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "finalization key material must be hexadecimal") from exc
    if (not verify_keys or any(len(key) != receipt_crypto.PUBLIC_KEY_SIZE
                               for key in verify_keys)
            or len(signing_seed) != receipt_crypto.SEED_SIZE):
        raise _PlanReceiptConfigurationError(
            "finalization keys must be Ed25519 public keys and a 32-byte seed")
    try:
        return finalization.ReceiptStore(
            args.receipt_dir, repo_paths=(args.repo,), data_dir=args.data_dir,
            verify_keys=verify_keys, signing_seed=signing_seed)
    except (finalization.ReceiptStoreLocationError,
            finalization.SigningKeyUnavailable) as exc:
        raise _PlanReceiptConfigurationError(str(exc)) from exc


def _reviewer_window_factory(args: argparse.Namespace):
    """Build the one real reviewer window for an eligible plan."""
    required = (
        args.herdr, args.omp, args.claude, args.reviewer_route,
        args.reviewer_model, args.reviewer_effort, args.reviewer_session_dir,
        args.reviewer_report_file, args.route_verify_key, args.route_receipt,
    )
    if not all(required):
        raise _PlanReceiptConfigurationError(
            "Herdr reviewer route, verified route receipt, session, report, "
            "and liveness configuration are required to finalize a plan")
    try:
        route_keys = tuple(bytes.fromhex(value) for value in args.route_verify_key)
        receipt_paths = dict(
            entry.split("=", 1) for entry in args.route_receipt)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "--route-receipt must be ROUTE=PATH and route keys hexadecimal") from exc
    if (not route_keys or any(len(key) != receipt_crypto.PUBLIC_KEY_SIZE
                              for key in route_keys)):
        raise _PlanReceiptConfigurationError(
            "--route-verify-key must contain Ed25519 public keys")
    admitted = route_receipts.load_admitted_routes(
        {route: Path(path) for route, path in receipt_paths.items()},
        verify_keys=route_keys)
    runner = launcher.HerdrLauncher(
        herdr_path=Path(args.herdr), omp_path=Path(args.omp),
        claude_path=Path(args.claude), admitted_routes=admitted)
    report_path = Path(args.reviewer_report_file)
    prompt_path = report_path.with_suffix(".prompt.json")
    session_dir = Path(args.reviewer_session_dir)

    def factory(matrix: finalization.ApplicabilityMatrix):
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(json.dumps({
            "matrix": [
                {"check_id": cell.check_id, "object_id": cell.object_id,
                 "canary": cell.canary.value if cell.canary else None}
                for cell in matrix.cells
            ],
            "plan_digest": matrix.plan_digest,
            "report_path": str(report_path.resolve()),
        }, sort_keys=True), encoding="utf-8")
        handle = None

        def launch_reviewer():
            nonlocal handle
            handle = runner.launch(launcher.LaunchSpec(
                correlation_token="finalize-" + matrix.plan_digest,
                worktree=Path(args.repo), prompt_path=prompt_path,
                envelope_path=report_path, route=args.reviewer_route,
                model=args.reviewer_model, effort=args.reviewer_effort,
                profile=args.reviewer_profile, session_dir=session_dir))
            return finalization_window.ReviewerSession(
                route=args.reviewer_route, model=args.reviewer_model,
                session_id=handle.pane_id, session_dir=str(session_dir),
                harness_owned_group=handle.process_group is not None,
                pid=handle.process_group)

        def poll_report():
            if not report_path.is_file():
                return None
            return json.loads(report_path.read_text(encoding="utf-8"))

        def kill_reviewer(_session):
            if handle is not None:
                runner.cancel(handle, finalization_window.time.monotonic() + 1.0)

        return finalization_window.FinalizationWindow(
            config=finalization_window.FinalizationConfig(
                finalization_timeout_s=args.finalization_timeout_s,
                turn_timeout_s=args.reviewer_turn_timeout_s,
                poll_interval_s=args.reviewer_poll_interval_s),
            launch=launch_reviewer, poll_report=poll_report,
            record_reviewer_session=lambda _session: None,
            record_stall=lambda _session, _signal, _elapsed: None,
            kill=kill_reviewer)

    return factory


def _plan_finalize(args: argparse.Namespace) -> int:
    try:
        stored = Path(args.plan_file).read_bytes()
        validation = pv.validate_plan(
            stored, args.repo, receipts=_VerifiedReceipts(args),
            collector=pv.SubprocessCollector())
        if not validation.eligible:
            print(json.dumps({
                "outcome": validation.outcome.value,
                "digest": validation.digest,
                "blockers": [
                    {"obligation": row.obligation.value, "pointer": row.pointer,
                     "message": row.message}
                    for row in validation.blockers
                ],
            }, sort_keys=True))
            return 2
        plan = plan_model.parse_bytes(stored)
        outcome = finalization.finalize(
            plan_digest=validation.digest,
            objects=plan_finalization.review_objects(plan),
            rubric=finalization.DEFAULT_RUBRIC,
            store=_finalization_store(args),
            validate=lambda: (),
            window_factory=lambda matrix: _reviewer_window_factory(args)(matrix),
            occupancy_reader=lambda _session: None)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("FINALIZATION_CONFIGURATION_REQUIRED", str(exc))
    except (_PlanReceiptVerificationError, finalization.SignatureMissing,
            finalization.SignatureInvalid, finalization.ReceiptInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (finalization.ReceiptStoreLocationError,
            receipt_crypto.KeyMaterialError,
            route_receipts.ReceiptInvalid, ValueError, OSError) as exc:
        return _refusal("FINALIZATION_FAILED", str(exc))
    print(json.dumps({
        "outcome": "FINALIZED",
        "digest": outcome.receipt.plan_digest,
        "verdict": outcome.verdict.value,
        "replayed": outcome.replayed,
    }, sort_keys=True))
    return 0


def _receipt_root(args: argparse.Namespace) -> Optional[Path]:
    raw = getattr(args, "receipt_dir", None) or os.environ.get(
        "MAESTRO_RECEIPT_DIR")
    return Path(raw) if raw else None


def _plan_receipt_store(
        args: argparse.Namespace, *, missing_detail: str
) -> finalization.ReceiptStore:
    root = _receipt_root(args)
    verify_key_values = getattr(args, "verify_key", None)
    data_dir = getattr(args, "data_dir", None)
    if root is None or not data_dir or not verify_key_values:
        raise _PlanReceiptConfigurationError(missing_detail)
    try:
        verify_keys = tuple(bytes.fromhex(value) for value in verify_key_values)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "--verify-key must be hexadecimal Ed25519 public key material"
        ) from exc
    if any(len(key) != receipt_crypto.PUBLIC_KEY_SIZE for key in verify_keys):
        raise _PlanReceiptConfigurationError(
            "--verify-key must contain exactly {0} bytes".format(
                receipt_crypto.PUBLIC_KEY_SIZE))
    try:
        return finalization.ReceiptStore(
            root, repo_paths=(getattr(args, "repo", "."),),
            data_dir=data_dir, verify_keys=verify_keys, create=False)
    except finalization.ReceiptStoreLocationError as exc:
        raise _PlanReceiptConfigurationError(str(exc)) from exc


def _plan_receipt_verification_refusal(exc: Exception) -> int:
    return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))


def _plan_show(args: argparse.Namespace) -> int:
    try:
        store = _plan_receipt_store(
            args,
            missing_detail=(
                "--receipt-dir, --data-dir, and at least one --verify-key "
                "are required for receipt access"))
        receipt = store.load(args.digest)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except FileNotFoundError:
        return _refusal("FINALIZED_PLAN_NOT_FOUND", args.digest)
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid, UnicodeError, ValueError,
            KeyError) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(receipt.to_bytes().decode("utf-8"))
    return 0


def _plan_list(args: argparse.Namespace) -> int:
    try:
        store = _plan_receipt_store(
            args,
            missing_detail=(
                "--receipt-dir, --data-dir, and at least one --verify-key "
                "are required for receipt access"))
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    digests = sorted(
        path.stem for path in store.root.glob("*.json")
        if len(path.stem) == 64 and all(
            character in "0123456789abcdef" for character in path.stem))
    try:
        for digest in digests:
            store.load(digest)
    except (FileNotFoundError, finalization.ReceiptInvalid,
            finalization.SignatureMissing, finalization.SignatureInvalid,
            UnicodeError, ValueError, KeyError) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(json.dumps(digests))
    return 0


def _store(args: argparse.Namespace) -> Optional[lc.LifecycleStore]:
    return lc.LifecycleStore(args.db) if getattr(args, "db", None) else None


def _run_start(args: argparse.Namespace) -> int:
    if not getattr(args, "plan_file", None):
        return _refusal(
            "RUN_CONFIGURATION_REQUIRED",
            "repository, finalized receipt, launcher roster, and liveness bounds are required")
    try:
        return _execute_run(args, resuming=False)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RUN_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
            lc.LifecycleError) as exc:
        return _refusal(type(exc).__name__.upper(), str(exc))
    except Exception as exc:
        return _refusal("RUN_EXECUTION_FAILED", str(exc))


def _run_configuration(args: argparse.Namespace) -> scheduler_types.SchedulerConfig:
    required = ("plan_file", "repo", "receipt_dir", "data_dir", "verify_key",
                "digest", "db", "run_id", "integration_path", "worktrees_root",
                "scratch_root", "concurrency", "node_timeout_s",
                "turn_timeout_s", "final_acceptance_timeout_s",
                "backstop_t_s", "semantic_ceiling")
    missing = [name for name in required if not getattr(args, name, None)]
    if missing:
        raise _PlanReceiptConfigurationError(
            "missing run configuration: {}".format(", ".join(missing)))
    return scheduler_types.SchedulerConfig(
        concurrency=args.concurrency, node_timeout_s=args.node_timeout_s,
        turn_timeout_s=args.turn_timeout_s,
        final_acceptance_timeout_s=args.final_acceptance_timeout_s,
        backstop_t_s=args.backstop_t_s, semantic_ceiling=args.semantic_ceiling)


def _paths_share_inode(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev, right_stat.st_ino)


def _is_within_run_boundary(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        pass
    else:
        return True
    ancestor = path
    while ancestor != ancestor.parent:
        if _paths_share_inode(ancestor, boundary):
            return True
        ancestor = ancestor.parent
    return _paths_share_inode(ancestor, boundary)


def _validate_run_paths(args: argparse.Namespace, _plan: plan_model.Plan) -> None:
    """Refuse lifecycle authority writable through a run participant boundary.

    The verified plan is paired with this preflight at the call site. Every
    path below is a process-visible write boundary: repository and integration
    worktrees, attempt worktrees and scratch, and the data and receipt stores
    that agents can otherwise modify.
    """
    database = Path(args.db).resolve()
    try:
        database_stat = database.stat()
    except FileNotFoundError:
        database_stat = None
    except OSError as exc:
        raise _RunPathConfigurationError(
            "lifecycle database cannot be statted: {}".format(database)) from exc
    if database_stat is not None and database_stat.st_nlink != 1:
        raise _RunPathConfigurationError(
            "lifecycle database has no single canonical inode: {}".format(database))

    boundaries = (
        ("repository", Path(args.repo).resolve()),
        ("integration worktree", Path(args.integration_path).resolve()),
        ("attempt worktrees", Path(args.worktrees_root).resolve()),
        ("SSSF data directory", Path(args.data_dir).resolve()),
        ("receipt store", Path(args.receipt_dir).resolve()),
        ("attempt scratch", Path(args.scratch_root).resolve()),
    )
    for label, boundary in boundaries:
        if _is_within_run_boundary(database, boundary):
            raise _RunPathConfigurationError(
                "lifecycle database is inside the {}: {}".format(label, boundary))


def _load_runnable_plan(args: argparse.Namespace) -> plan_model.Plan:
    stored = Path(args.plan_file).read_bytes()
    receipts = _VerifiedReceipts(args)
    validation = pv.validate_plan(
        stored, args.repo, receipts=receipts, collector=pv.SubprocessCollector())
    if not validation.eligible or validation.digest != args.digest:
        raise ValueError("RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE")
    receipt = receipts._receipt_store().load(args.digest)
    if receipt.verdict is not finalization.Verdict.PASS:
        raise ValueError("RUN_RECEIPT_NOT_PASS")
    return plan_model.parse_bytes(stored)


def _runtime_launcher(args: argparse.Namespace) -> launcher.HerdrLauncher:
    required = (args.herdr, args.omp, args.claude, args.agent_route,
                args.agent_model, args.agent_effort, args.route_receipt,
                args.route_verify_key)
    if not all(required):
        raise _PlanReceiptConfigurationError(
            "Herdr launcher route, verified route receipt, and agent model "
            "configuration are required for agent nodes")
    try:
        keys = tuple(bytes.fromhex(value) for value in args.route_verify_key)
        paths = dict(item.split("=", 1) for item in args.route_receipt)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "route receipts are ROUTE=PATH and route keys are hexadecimal") from exc
    admitted = route_receipts.load_admitted_routes(
        {route: Path(path) for route, path in paths.items()}, verify_keys=keys)
    return launcher.HerdrLauncher(
        herdr_path=Path(args.herdr), omp_path=Path(args.omp),
        claude_path=Path(args.claude), admitted_routes=admitted)


def _execute_run(args: argparse.Namespace, *, resuming: bool) -> int:
    from threading import RLock

    config = _run_configuration(args)
    plan = _load_runnable_plan(args)
    _validate_run_paths(args, plan)
    route_runner = _runtime_launcher(args) if plan.agent_nodes else None
    store = lc.LifecycleStore(args.db)
    handles = {}
    proven_absent = set()
    handles_lock = RLock()
    try:
        if resuming:
            store.resume_run(args.run_id)
        elif not Path(args.integration_path).exists():
            subprocess.run(
                ("git", "-C", str(args.repo), "worktree", "add",
                 str(args.integration_path), plan.merge_policy.integration_branch),
                check=True, capture_output=True, text=True)

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            key = (node.node_id, record.attempt_no)
            launch_environment = worktree.launch_env(attempt.scratch)
            if node.kind is scheduler_types.NodeKind.CODE:
                process = subprocess.Popen(
                    node.command, cwd=attempt.path, env=launch_environment,
                    start_new_session=True)
                with handles_lock:
                    handles[key] = process
                    proven_absent.discard(key)
                on_launch(process.pid)
                while process.poll() is None:
                    if cancel_requested():
                        quiesce_attempt(record, "cancel")
                        process.wait(timeout=1)
                        return scheduler.NodeExecution(
                            exit_code=process.returncode or 1)
                    time.sleep(0.05)
                return scheduler.NodeExecution(exit_code=process.returncode or 0)
            assert route_runner is not None
            prompt = attempt.scratch / "agent-prompt.txt"
            envelope = attempt.scratch / "agent-envelope.json"
            prompt.write_text(
                plan.node_by_id()[node.node_id].instruction +
                ("\n\nRetry guidance:\n" + retry_prompt if retry_prompt else ""),
                encoding="utf-8")
            handle = route_runner.launch(launcher.LaunchSpec(
                correlation_token="{}-{}-{}".format(
                    args.run_id, node.node_id, record.attempt_no),
                worktree=attempt.path, prompt_path=prompt,
                envelope_path=envelope, route=args.agent_route,
                model=args.agent_model, effort=args.agent_effort,
                profile=args.agent_profile,
                session_dir=attempt.scratch / "session",
                environment=launch_environment))
            with handles_lock:
                handles[key] = handle
                proven_absent.discard(key)
            if handle.transcript_path is None:
                raise RuntimeError("SESSION_PATH_MISSING:{}#{}".format(
                    node.node_id, record.attempt_no))
            store.mark_launched(
                args.run_id, node.node_id, record.attempt_no,
                handle.process_group,
                extra={watchdog.SESSION_PATH_KEY: str(handle.transcript_path)})
            on_launch(handle.process_group)
            while True:
                state = route_runner.poll(handle)
                if state.state is launcher.PollState.EXITED:
                    parsed = False
                    try:
                        json.loads(envelope.read_text(encoding="utf-8"))
                        parsed = True
                    except (OSError, ValueError, UnicodeError):
                        pass
                    return scheduler.NodeExecution(
                        envelope_parsed=parsed, exit_code=state.exit_code or 1,
                        launched_pid=handle.process_group)
                if cancel_requested():
                    quiesce_attempt(record, "cancel")
                    return scheduler.NodeExecution(
                        envelope_parsed=False, exit_code=1,
                        launched_pid=handle.process_group)
                time.sleep(0.05)

        def quiesce_attempt(record, phase):
            key = (record.node_id, record.attempt_no)
            with handles_lock:
                if key in proven_absent:
                    return
                handle = handles.get(key)
                if handle is None:
                    if phase == "pre-baseline":
                        return
                    raise RuntimeError(
                        "PROCESS_GROUP_UNTRACKED:{}:{}#{}".format(
                            phase, record.node_id, record.attempt_no))
                if isinstance(handle, subprocess.Popen):
                    process_group = handle.pid
                    launcher.quiesce_process_group(
                        process_group, time.monotonic() + 1.0)
                    if not launcher._process_group_absent(process_group):
                        raise RuntimeError(
                            "PROCESS_GROUP_STILL_OWNED:{}:{}".format(
                                phase, process_group))
                else:
                    if route_runner is None:
                        raise RuntimeError(
                            "PROCESS_GROUP_UNTRACKED:{}:{}#{}".format(
                                phase, record.node_id, record.attempt_no))
                    route_runner.cancel(handle, time.monotonic() + 1.0)
                    if route_runner.reclaim(handle.correlation_token):
                        raise RuntimeError(
                            "PROCESS_GROUP_STILL_OWNED:{}:{}".format(
                                phase, handle.correlation_token))
                handles.pop(key)
                proven_absent.add(key)

        run_gate, run_integration_gate = _scheduler_gate_deps(plan)
        deps = scheduler.SchedulerDeps(
            store=store, repo=Path(args.repo),
            integration_path=Path(args.integration_path),
            integration_branch=plan.merge_policy.integration_branch,
            worktrees_root=Path(args.worktrees_root),
            scratch_root=Path(args.scratch_root), run_node=run_node,
            run_gate=run_gate, run_integration_gate=run_integration_gate,
            quiesce_attempt=quiesce_attempt,
            kill_attempt=lambda record: quiesce_attempt(record, "watchdog-kill"))
        report = scheduler.Scheduler(
            args.run_id, plan.to_plan_nodes(), config, deps,
            plan_digest=args.digest).run()
    finally:
        store.close()
    print(json.dumps({"outcome": report.outcome.value,
                      "run_id": args.run_id,
                      "merged": list(report.merged),
                      "blocked": [
                          {"node_id": node, "reason": reason.value}
                          for node, reason in report.blocked]}, sort_keys=True))
    return 0


def _scheduler_gate_deps(plan: plan_model.Plan):
    """Adapt the frozen plan gates without dropping cancellation ownership."""
    integration_gate = plan.merge_policy.integration_gate

    def run_gate(attempt, node, phase, cancel_requested):
        return worktree.run_node_gate(
            attempt, node.gate_command, node.gate_selector, cancel_requested,
            label="{}-{}".format(node.node_id, phase))

    def run_integration_gate(integration_path, specs, cancel_requested):
        command = (integration_gate.runner,) + tuple(integration_gate.argv)
        return worktree.run_integration_gate(
            integration_path, command, Path(integration_path).parent / ".maestro",
            cancel_requested, label="integration-gate")

    return run_gate, run_integration_gate


def _run_status(args: argparse.Namespace) -> int:
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        rows = [store.get_node(args.run_id, row.node_id)
                for row in store.node_records(args.run_id)]
        print(json.dumps([
            {"node_id": row.node_id, "state": row.state.value,
             "block_reason": row.block_reason.value if row.block_reason else None}
            for row in rows
        ], sort_keys=True))
        return 0
    finally:
        store.close()


def _run_cancel(args: argparse.Namespace) -> int:
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        store.cancel_run(args.run_id)
        print(json.dumps({"outcome": "CANCELLED", "run_id": args.run_id}))
        return 0
    finally:
        store.close()


def _run_resume(args: argparse.Namespace) -> int:
    try:
        return _execute_run(args, resuming=True)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RUN_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
            lc.LifecycleError) as exc:
        return _refusal(type(exc).__name__.upper(), str(exc))
    except Exception as exc:
        return _refusal("RUN_EXECUTION_FAILED", str(exc))


def _escape(args: argparse.Namespace) -> int:
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        if args.command == "retry":
            row = store.retry(args.run_id, args.node_id, force=args.force)
        elif args.command == "skip":
            row = store.skip(args.run_id, args.node_id,
                             accept_sha=args.accept_sha, repo_path=args.repo)
        else:
            row = store.abandon(args.run_id, args.node_id)
        print(json.dumps({"node_id": row.node_id, "state": row.state.value}))
        return 0
    finally:
        store.close()


class _WorkspaceNotCanonical(RuntimeError):
    """The manifest parses but is not the immutable byte representation."""


class _WorkspaceConfigurationError(RuntimeError):
    """Operator-supplied workspace configuration cannot be used safely."""


def _workspace_emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _workspace_refusal(code: str, detail: str) -> int:
    _workspace_emit({"detail": detail, "outcome": code})
    return 2


def _workspace_participants(plan: workspace_model.WorkspacePlan) -> list:
    return [{
        "base_commit": spec.base_commit,
        "mode": spec.mode.value,
        "plan_digest": spec.plan_digest,
        "repository_id": spec.repository_id,
        "target_branch": spec.target_branch,
    } for spec in plan.repositories]


def _load_workspace_manifest(args: argparse.Namespace
                             ) -> Tuple[Path, workspace_model.WorkspacePlan, str]:
    manifest_file = Path(args.manifest_file).resolve()
    stored = manifest_file.read_bytes()
    plan = workspace_model.parse_bytes(stored)
    if stored != workspace_canonical.canonicalize_workspace(plan):
        raise _WorkspaceNotCanonical(
            "workspace manifest must contain canonical WorkspacePlan bytes")
    return manifest_file.parent, plan, workspace_digest.digest_of(stored)


def _hex_material(value: str, label: str) -> bytes:
    try:
        material = bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise _WorkspaceConfigurationError(
            "{0} must be hexadecimal".format(label)) from exc
    if len(material) != receipt_crypto.PUBLIC_KEY_SIZE:
        raise _WorkspaceConfigurationError(
            "{0} must contain exactly {1} bytes".format(
                label, receipt_crypto.PUBLIC_KEY_SIZE))
    return material


def _verify_keys(args: argparse.Namespace) -> Tuple[bytes, ...]:
    keys = tuple(_hex_material(value, "verify key") for value in args.verify_key)
    if not keys:
        raise _WorkspaceConfigurationError("at least one verify key is required")
    return keys


def _participant_boundaries(manifest_dir: Path,
                            plan: workspace_model.WorkspacePlan) -> Tuple[Path, ...]:
    return tuple((manifest_dir / spec.path).resolve() for spec in plan.repositories)


def _workspace_receipt_store(args: argparse.Namespace, manifest_dir: Path,
                             plan: workspace_model.WorkspacePlan,
                             verify_keys: Tuple[bytes, ...], *,
                             signing_seed: Optional[bytes] = None
                             ) -> workspace_receipt.WorkspaceReceiptStore:
    return workspace_receipt.WorkspaceReceiptStore(
        args.workspace_receipt_dir,
        participant_repos=_participant_boundaries(manifest_dir, plan),
        data_dir=args.data_dir,
        verify_keys=verify_keys,
        signing_seed=signing_seed,
        create=signing_seed is not None,
    )


def _plan_receipt_loader(args: argparse.Namespace, manifest_dir: Path,
                         plan: workspace_model.WorkspacePlan,
                         verify_keys: Tuple[bytes, ...]):
    store = finalization.ReceiptStore(
        args.plan_receipt_dir,
        repo_paths=_participant_boundaries(manifest_dir, plan),
        data_dir=args.data_dir,
        verify_keys=verify_keys,
        create=False,
    )
    stores = {
        spec.plan_digest: store
        for spec in plan.repositories
        if spec.mode is workspace_model.RepositoryMode.WRITE
    }

    def load(plan_digest: str):
        store = stores.get(plan_digest)
        if store is None:
            raise FileNotFoundError(
                "no participant receipt store for {0}".format(plan_digest))
        return store.load(plan_digest)

    return load


def _coordinator_config(args: argparse.Namespace) -> coordinator.CoordinatorConfig:
    try:
        return coordinator.CoordinatorConfig(
            max_workers=args.max_workers,
            lease_owner=args.lease_owner,
            lease_stale_after_s=args.lease_stale_after_s,
            participant_timeout_s=args.participant_timeout_s,
            cancellation_timeout_s=args.cancellation_timeout_s,
            poll_interval_s=args.poll_interval_s,
        )
    except ValueError as exc:
        raise _WorkspaceConfigurationError(str(exc)) from exc


def _publication_projection(intent, steps) -> Dict[str, Any]:
    return {
        "state": None if intent is None else intent.state.value,
        "targets": [] if intent is None else [{
            "accepted_sha": target.accepted_sha,
            "candidate_branch": target.candidate_branch,
            "expected_base_sha": target.expected_base_sha,
            "remote_repository": target.remote_repository,
            "remote_url": target.remote_url,
            "repository_id": target.repository_id,
            "state": target.state.value,
            "target_branch": target.target_branch,
        } for target in intent.targets],
        "steps": [{
            "detail": dict(step.detail),
            "from_state": step.from_state.value,
            "repository_id": step.repository_id,
            "step_id": step.step_id,
            "to_state": step.to_state.value,
        } for step in steps],
    }


def _workspace_validate(args: argparse.Namespace) -> int:
    _manifest_dir, plan, digest = _load_workspace_manifest(args)
    _workspace_emit({
        "digest": digest,
        "outcome": "VALID",
        "participants": _workspace_participants(plan),
    })
    return 0


def _workspace_finalize(args: argparse.Namespace) -> int:
    manifest_dir, plan, digest = _load_workspace_manifest(args)
    verify_keys = _verify_keys(args)
    signing_seed = _hex_material(args.signing_seed, "signing seed")
    store = _workspace_receipt_store(
        args, manifest_dir, plan, verify_keys, signing_seed=signing_seed)
    receipt = workspace_receipt.finalize(
        digest, plan, _plan_receipt_loader(args, manifest_dir, plan, verify_keys), store)
    _workspace_emit({
        "digest": receipt.workspace_digest,
        "outcome": "FINALIZED",
        "participants": [participant.to_mapping() for participant in receipt.participants],
    })
    return 0


def _workspace_execute(args: argparse.Namespace, *, resuming: bool) -> int:
    manifest_dir, plan, digest = _load_workspace_manifest(args)
    verify_keys = _verify_keys(args)
    receipt = _workspace_receipt_store(args, manifest_dir, plan, verify_keys).load(digest)
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        if resuming:
            persisted = store.get_run(args.run_id)
            if (persisted.workspace_digest != digest or
                    persisted.workspace != plan):
                raise coordinator.CoordinatorError(
                    "stored run does not match the exact signed workspace manifest")
        else:
            try:
                store.get_run(args.run_id)
            except coordinator_store.UnknownRun:
                pass
            else:
                raise coordinator_store.RunAlreadyExists(
                    "workspace run {0} already exists; use workspace resume".format(
                        args.run_id))
        outcome = WorkspaceCoordinator(
            run_id=args.run_id,
            plan=plan,
            workspace_digest=digest,
            receipt=receipt,
            store=store,
            manifest_dir=manifest_dir,
            state_root=Path(args.state_root),
            participant_runner=SubprocessParticipantRunner(),
            config=_coordinator_config(args),
        ).run()
        _workspace_emit({
            "digest": digest,
            "outcome": outcome.value,
            "participants": _workspace_participants(plan),
            "run_id": args.run_id,
        })
        return 0 if outcome is workspace_model.WorkspaceOutcome.ACCEPTED else 2
    finally:
        store.close()


def _workspace_start(args: argparse.Namespace) -> int:
    return _workspace_execute(args, resuming=False)


def _workspace_resume(args: argparse.Namespace) -> int:
    return _workspace_execute(args, resuming=True)


def _workspace_status(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore.open_existing(args.db)
    try:
        run = store.get_run(args.run_id)
        repositories = store.list_repositories(args.run_id)
        gates = store.list_gates(args.run_id)
        steps = store.list_publication_steps(args.run_id)
        try:
            intent = store.get_publication_intent(args.run_id)
        except coordinator_store.PublicationRefused:
            intent = None
        _workspace_emit({
            "outcome": "STATUS",
            "publication": _publication_projection(intent, steps),
            "repositories": [{
                "accepted_sha": record.accepted_sha,
                "block_reason": record.block_reason,
                "candidate_branch": record.candidate_branch,
                "child_run_id": record.child_run_id,
                "repository_id": record.repository_id,
                "resolved_path": record.resolved_path,
                "state": record.state.value,
            } for record in repositories],
            "repository_vector": _workspace_participants(run.workspace),
            "run": {
                "cancel_requested": run.cancel_requested,
                "lease_expires_at": run.lease_expires_at,
                "lease_owner": run.lease_owner,
                "outcome": None if run.outcome is None else run.outcome.value,
                "run_id": run.run_id,
                "workspace_digest": run.workspace_digest,
                "workspace_id": run.workspace_id,
            },
            "gates": [{
                "detail": dict(gate.detail),
                "gate_index": gate.gate_index,
                "passed": gate.passed,
            } for gate in gates],
        })
        return 0
    finally:
        store.close()


def _workspace_cancel(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.request_cancellation(args.run_id, actor=args.actor)
        _workspace_emit({
            "cancel_requested": run.cancel_requested,
            "outcome": "CANCELLATION_REQUESTED",
            "run_id": run.run_id,
        })
        return 0
    finally:
        store.close()


def _workspace_publish(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.get_run(args.run_id)
        paths = workspace_runtime.resolve_repository_paths(
            Path(args.manifest_dir), run.workspace)
        result = WorkspacePublisher(
            store=store, repository_paths=paths, actor=args.actor).publish(args.run_id)
        _workspace_emit({
            "outcome": result.outcome.value,
            "publication": _publication_projection(result.intent, result.steps),
            "reason": result.reason,
            "run_id": result.run_id,
        })
        return 0 if result.outcome is workspace_model.WorkspaceOutcome.PUBLISHED else 2
    finally:
        store.close()


def _workspace_rollback(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.get_run(args.run_id)
        paths = workspace_runtime.resolve_repository_paths(
            Path(args.manifest_dir), run.workspace)
        result = WorkspacePublisher(
            store=store, repository_paths=paths, actor=args.actor).rollback(args.run_id)
        _workspace_emit({
            "outcome": result.outcome.value,
            "publication": _publication_projection(result.intent, result.steps),
            "reason": result.reason,
            "run_id": result.run_id,
        })
        return 0 if result.outcome is workspace_model.WorkspaceOutcome.ACCEPTED else 2
    finally:
        store.close()


_WORKSPACE_ERROR_CODES = (
    (_WorkspaceNotCanonical, "WORKSPACE_NOT_CANONICAL"),
    (_WorkspaceConfigurationError, "WORKSPACE_CONFIGURATION_ERROR"),
    (workspace_model.WorkspaceParseError, "WORKSPACE_PARSE_ERROR"),
    (workspace_receipt.AuthorizationError, "AUTHORIZATION_ERROR"),
    (workspace_receipt.ReceiptStoreLocationError, "RECEIPT_STORE_LOCATION_ERROR"),
    (workspace_receipt.ReceiptExists, "RECEIPT_EXISTS"),
    (workspace_receipt.ReceiptInvalid, "RECEIPT_INVALID"),
    (workspace_receipt.SignatureMissing, "SIGNATURE_MISSING"),
    (workspace_receipt.SignatureInvalid, "SIGNATURE_INVALID"),
    (workspace_receipt.SigningKeyUnavailable, "SIGNING_KEY_UNAVAILABLE"),
    (finalization.ReceiptStoreLocationError, "RECEIPT_STORE_LOCATION_ERROR"),
    (finalization.ReceiptExists, "RECEIPT_EXISTS"),
    (finalization.ReceiptInvalid, "RECEIPT_INVALID"),
    (finalization.SignatureMissing, "SIGNATURE_MISSING"),
    (finalization.SignatureInvalid, "SIGNATURE_INVALID"),
    (finalization.SigningKeyUnavailable, "SIGNING_KEY_UNAVAILABLE"),
    (receipt_crypto.KeyMaterialError, "KEY_MATERIAL_ERROR"),
    (coordinator_store.CoordinatorDatabaseUnavailable,
     "COORDINATOR_DATABASE_UNAVAILABLE"),
    (coordinator_store.RunAlreadyExists, "RUN_ALREADY_EXISTS"),
    (coordinator_store.UnknownRun, "UNKNOWN_RUN"),
    (coordinator_store.UnknownRepository, "UNKNOWN_REPOSITORY"),
    (coordinator_store.GateAlreadyRecorded, "GATE_ALREADY_RECORDED"),
    (coordinator_store.DuplicatePublicationIntent, "DUPLICATE_PUBLICATION_INTENT"),
    (coordinator_store.PublicationRefused, "PUBLICATION_REFUSED"),
    (coordinator_store.IllegalTransition, "ILLEGAL_TRANSITION"),
    (coordinator_store.RepositoryPathMismatch, "REPOSITORY_PATH_MISMATCH"),
    (coordinator_store.CoordinatorStoreError, "COORDINATOR_STORE_ERROR"),
    (coordinator.LeaseUnavailable, "LEASE_UNAVAILABLE"),
    (coordinator.CoordinatorError, "COORDINATOR_ERROR"),
    (workspace_runtime.WorkspaceRuntimeError, "WORKSPACE_RUNTIME_ERROR"),
    (participant.ParticipantProtocolError, "PARTICIPANT_PROTOCOL_ERROR"),
    (publication.PublicationError, "PUBLICATION_ERROR"),
    (FileNotFoundError, "FILE_NOT_FOUND"),
    (OSError, "FILE_SYSTEM_ERROR"),
)


def _workspace_internal_error(exc: Exception) -> int:
    _workspace_emit({"detail": str(exc), "outcome": "INTERNAL_ERROR"})
    return 3


def _workspace_error_code(exc: BaseException) -> str:
    for error_type, code in _WORKSPACE_ERROR_CODES:
        if isinstance(exc, error_type):
            return code
    raise TypeError("unhandled workspace exception")


_WORKSPACE_TYPED_ERRORS = tuple(item[0] for item in _WORKSPACE_ERROR_CODES)


def _add_run_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan-file")
    _add_plan_receipt_access(parser)
    parser.add_argument("--db")
    parser.add_argument("--run-id")
    parser.add_argument("--integration-path")
    parser.add_argument("--worktrees-root")
    parser.add_argument("--scratch-root")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--node-timeout-s", type=float)
    parser.add_argument("--turn-timeout-s", type=float)
    parser.add_argument("--final-acceptance-timeout-s", type=float)
    parser.add_argument("--backstop-t-s", type=float)
    parser.add_argument("--semantic-ceiling", type=int)
    parser.add_argument("--herdr")
    parser.add_argument("--omp")
    parser.add_argument("--claude")
    parser.add_argument("--agent-route")
    parser.add_argument("--agent-model")
    parser.add_argument("--agent-effort")
    parser.add_argument("--agent-profile")
    parser.add_argument("--route-receipt", action="append")
    parser.add_argument("--route-verify-key", action="append")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db")


def _add_workspace_manifest_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest-file", required=True)


def _add_workspace_receipt_access(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace-receipt-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--verify-key", action="append", required=True)

def _add_plan_receipt_access(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".")
    parser.add_argument("--receipt-dir")
    parser.add_argument("--data-dir")
    parser.add_argument("--verify-key", action="append")



def _add_workspace_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-workers", type=int, required=True)
    parser.add_argument("--lease-owner", required=True)
    parser.add_argument("--lease-stale-after-s", type=float, required=True)
    parser.add_argument("--participant-timeout-s", type=float, required=True)
    parser.add_argument("--cancellation-timeout-s", type=float, required=True)
    parser.add_argument("--poll-interval-s", type=float, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro")
    root = parser.add_subparsers(dest="command", required=True)

    plan = root.add_parser("plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    validate = plan_sub.add_parser("validate")
    validate.add_argument("plan_file")
    _add_plan_receipt_access(validate)
    validate.set_defaults(handler=_plan_validate)
    finalize = plan_sub.add_parser("finalize")
    finalize.add_argument("plan_file")
    finalize.add_argument("--repo", default=".")
    finalize.add_argument("--receipt-dir")
    finalize.add_argument("--data-dir")
    finalize.add_argument("--verify-key", action="append")
    finalize.add_argument("--signing-seed")
    finalize.add_argument("--herdr")
    finalize.add_argument("--omp")
    finalize.add_argument("--claude")
    finalize.add_argument("--reviewer-route")
    finalize.add_argument("--reviewer-model")
    finalize.add_argument("--reviewer-effort")
    finalize.add_argument("--reviewer-profile")
    finalize.add_argument("--reviewer-session-dir")
    finalize.add_argument("--reviewer-report-file")
    finalize.add_argument("--route-receipt", action="append")
    finalize.add_argument("--route-verify-key", action="append")
    finalize.add_argument("--finalization-timeout-s", type=float, default=600.0)
    finalize.add_argument("--reviewer-turn-timeout-s", type=float, default=120.0)
    finalize.add_argument("--reviewer-poll-interval-s", type=float, default=1.0)
    finalize.set_defaults(handler=_plan_finalize)
    show = plan_sub.add_parser("show")
    show.add_argument("digest")
    _add_plan_receipt_access(show)
    show.set_defaults(handler=_plan_show)
    listing = plan_sub.add_parser("list")
    _add_plan_receipt_access(listing)
    listing.set_defaults(handler=_plan_list)

    run = root.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    start = run_sub.add_parser("start")
    _add_run_execution_options(start)
    start.add_argument("digest")
    start.set_defaults(handler=_run_start)
    status = run_sub.add_parser("status")
    status.add_argument("run_id")
    _add_db(status)
    status.set_defaults(handler=_run_status)
    cancel = run_sub.add_parser("cancel")
    cancel.add_argument("run_id")
    _add_db(cancel)
    cancel.set_defaults(handler=_run_cancel)
    resume = run_sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--digest")
    _add_run_execution_options(resume)
    resume.set_defaults(handler=_run_resume)

    retry = root.add_parser("retry")
    retry.add_argument("run_id")
    retry.add_argument("node_id")
    retry.add_argument("--force", action="store_true")
    _add_db(retry)
    retry.set_defaults(handler=_escape)
    skip = root.add_parser("skip")
    skip.add_argument("run_id")
    skip.add_argument("node_id")
    skip.add_argument("--accept-sha", required=True)
    skip.add_argument("--repo", default=".")
    _add_db(skip)
    skip.set_defaults(handler=_escape)
    abandon = root.add_parser("abandon")
    abandon.add_argument("run_id")
    abandon.add_argument("node_id")
    _add_db(abandon)
    abandon.set_defaults(handler=_escape)

    workspace = root.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(
        dest="workspace_command", required=True)

    workspace_validate = workspace_sub.add_parser("validate")
    _add_workspace_manifest_file(workspace_validate)
    workspace_validate.set_defaults(handler=_workspace_validate)

    workspace_finalize = workspace_sub.add_parser("finalize")
    _add_workspace_manifest_file(workspace_finalize)
    workspace_finalize.add_argument("--plan-receipt-dir", required=True)
    _add_workspace_receipt_access(workspace_finalize)
    workspace_finalize.add_argument("--signing-seed", required=True)
    workspace_finalize.set_defaults(handler=_workspace_finalize)

    workspace_start = workspace_sub.add_parser("start")
    _add_workspace_manifest_file(workspace_start)
    _add_workspace_receipt_access(workspace_start)
    _add_workspace_execution_options(workspace_start)
    workspace_start.set_defaults(handler=_workspace_start)

    workspace_status = workspace_sub.add_parser("status")
    workspace_status.add_argument("--db", required=True)
    workspace_status.add_argument("--run-id", required=True)
    workspace_status.set_defaults(handler=_workspace_status)

    workspace_cancel = workspace_sub.add_parser("cancel")
    workspace_cancel.add_argument("--db", required=True)
    workspace_cancel.add_argument("--run-id", required=True)
    workspace_cancel.add_argument("--actor", required=True)
    workspace_cancel.set_defaults(handler=_workspace_cancel)

    workspace_resume = workspace_sub.add_parser("resume")
    _add_workspace_manifest_file(workspace_resume)
    _add_workspace_receipt_access(workspace_resume)
    _add_workspace_execution_options(workspace_resume)
    workspace_resume.set_defaults(handler=_workspace_resume)

    workspace_publish = workspace_sub.add_parser("publish")
    workspace_publish.add_argument("--manifest-dir", required=True)
    workspace_publish.add_argument("--db", required=True)
    workspace_publish.add_argument("--run-id", required=True)
    workspace_publish.add_argument("--actor", required=True)
    workspace_publish.set_defaults(handler=_workspace_publish)

    workspace_rollback = workspace_sub.add_parser("rollback")
    workspace_rollback.add_argument("--manifest-dir", required=True)
    workspace_rollback.add_argument("--db", required=True)
    workspace_rollback.add_argument("--run-id", required=True)
    workspace_rollback.add_argument("--actor", required=True)
    workspace_rollback.set_defaults(handler=_workspace_rollback)
    return parser


def parser_verbs(parser: argparse.ArgumentParser) -> Tuple[str, ...]:
    found = []

    def walk(current: argparse.ArgumentParser, prefix: Tuple[str, ...]) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                path = prefix + (name,)
                if any(isinstance(item, argparse._SubParsersAction)
                       for item in child._actions):
                    walk(child, path)
                else:
                    found.append(" ".join(path))

    walk(parser, ())
    return tuple(found)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except _WORKSPACE_TYPED_ERRORS as exc:
        if args.command == "workspace":
            return _workspace_refusal(_workspace_error_code(exc), str(exc))
        if isinstance(exc, (lc.LifecycleError, OSError, ValueError)):
            print(json.dumps({"outcome": type(exc).__name__.upper(),
                              "detail": str(exc)}, sort_keys=True))
            return 2
        raise
    except (lc.LifecycleError, OSError, ValueError, sqlite3.Error) as exc:
        if args.command == "workspace":
            return _workspace_internal_error(exc)
        print(json.dumps({"outcome": type(exc).__name__.upper(),
                          "detail": str(exc)}, sort_keys=True))
        return 2
    except Exception as exc:
        if args.command == "workspace":
            return _workspace_internal_error(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
