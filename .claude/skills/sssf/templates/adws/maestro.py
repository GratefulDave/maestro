#!/usr/bin/env python3
"""Maestro factory CLI: run start/resume/amend/status."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple
import yaml


from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore, LedgerSchemaUnsupported
from adw_modules.route_receipts import load_admitted_routes, load_public_key
from adw_modules.runtime_state import RuntimeStateRoot, RuntimeStateRefused
from adw_modules.scheduler import (
    FactoryRefused,
    FactoryScheduler,
    LaneContext,
    LaunchFailed,
    RunRepositoryMismatch,
    StageActor,
    apply_factory_amendment,
    create_factory_run,
    require_deployment,
    run_row,
)

_MAESTRO_CONFIG_FILE = Path("adws") / "maestro.config.yaml"
_MAESTRO_SCHEMA = "maestro-config.v1"


class _MaestroConfigurationError(ValueError):
    """The repository-local Maestro configuration is absent or unsafe."""


class _RunRefused(RuntimeError):
    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail

    def emit(self) -> int:
        print(
            json.dumps({"detail": self.detail, "outcome": self.outcome}, sort_keys=True)
        )
        return 3


def _config_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _MaestroConfigurationError(label + " must be a nonempty string")
    return value


def _executing_maestro_file() -> Path:
    return Path(__file__)


def _deployment_product_root(maestro_file: Path) -> Path:
    return maestro_file.resolve().parent.parent


def _load_maestro_config(repo: Path, config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise _MaestroConfigurationError("missing " + str(config_path))
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise _MaestroConfigurationError("config root must be a mapping")
    if loaded.get("schema") != _MAESTRO_SCHEMA:
        raise _MaestroConfigurationError("unsupported config schema")
    root = Path(_config_string(loaded.get("runtime_state_root"), "runtime_state_root"))
    if not root.is_absolute():
        raise _MaestroConfigurationError("runtime_state_root must be absolute")
    loaded["runtime_state_root"] = root
    loaded["repo"] = repo.resolve()
    return loaded


def _load_deployment_config(maestro_file: Path) -> dict[str, Any]:
    root = _deployment_product_root(maestro_file)
    return _load_maestro_config(root, root / _MAESTRO_CONFIG_FILE)


def _open_runtime(layout: Mapping[str, Any], target_root: Path) -> RuntimeStateRoot:
    return RuntimeStateRoot(
        layout["runtime_state_root"],
        overlap_paths=(target_root, layout.get("repo", target_root)),
    )


def _open_store(runtime: RuntimeStateRoot) -> ArtifactStore:
    try:
        return ArtifactStore(runtime.ledger_path())
    except LedgerSchemaUnsupported as exc:
        raise _RunRefused("LEDGER_SCHEMA_UNSUPPORTED", str(exc)) from exc


class HerdrStageActor:
    """Fresh launch per incomplete stage. Envelope bytes are payload, not stage."""

    def __init__(
        self,
        launcher: lch.LauncherAdapter,
        worktrees: Path,
        target: gitpub.TargetBinding,
    ) -> None:
        self.launcher = launcher
        self.worktrees = worktrees
        self.target = target

    def _checkout_sha(self, ctx: LaneContext) -> str:
        if ctx.builder_base_sha:
            return ctx.builder_base_sha
        if ctx.integration_head:
            return ctx.integration_head
        return self.target.integration_initial_sha

    def _new_attempt_dir(self, ctx: LaneContext) -> Path:
        base = (
            self.worktrees
            / ctx.run_id
            / ctx.lane.lane_id
            / ctx.stage.value
            / ctx.input_digest[:16]
        )
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="attempt-", dir=os.fspath(base)))

    def _add_worktree(self, dest: Path, sha: str) -> None:
        repo = Path(self.target.target_repository_root)
        subprocess.check_call(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(dest),
                sha,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _safe_remove_attempt(self, attempt: Path, checkout: Path) -> None:
        try:
            resolved = attempt.resolve()
        except OSError:
            return
        root = self.worktrees.resolve()
        if root not in resolved.parents or resolved == root:
            return
        repo = Path(self.target.target_repository_root)
        if checkout.exists():
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        shutil.rmtree(resolved, ignore_errors=True)

    def _request(self, ctx: LaneContext, role: str) -> Mapping[str, Any]:
        return st.json_ready(
            {
                "builder_base_sha": ctx.builder_base_sha,
                "candidate_ref": ctx.candidate_ref,
                "candidate_sha": ctx.candidate_sha,
                "input_digest": ctx.input_digest,
                "lane_id": ctx.lane.lane_id,
                "plan_digest": ctx.plan_digest,
                "plan_revision": ctx.plan_revision,
                "public_contract": ctx.public_contract,
                "role": role,
                "run_id": ctx.run_id,
                "sealed_digest": ctx.sealed_digest,
                "spec_digest": ctx.lane.spec_digest,
                "stage": ctx.stage.value,
            }
        )

    def _dispatch(self, ctx: LaneContext, role: str) -> Mapping[str, Any]:
        sha = self._checkout_sha(ctx)
        attempt = self._new_attempt_dir(ctx)
        checkout = attempt / "checkout"
        session = attempt / "session"
        session.mkdir()
        prompt = attempt / "prompt.json"
        envelope = attempt / "envelope.json"
        try:
            self._add_worktree(checkout, sha)
            request = self._request(ctx, role)
            prompt.write_bytes(st.canonical_bytes(request))
            spec = lch.LaunchSpec(
                correlation_token="{}:{}:{}:{}".format(
                    ctx.run_id, ctx.lane.lane_id, ctx.stage.value, ctx.input_digest[:12]
                ),
                worktree=checkout,
                prompt_path=prompt,
                envelope_path=envelope,
                route="omp",
                model="",
                effort="",
                profile=None,
                session_dir=session,
                context_window_tokens=8192,
                lane_key=ctx.lane.lane_id,
                pane_role=role,
                workspace_label=ctx.run_id,
            )
            handle = self.launcher.launch(spec)
            try:
                while True:
                    result = self.launcher.poll(handle)
                    if result.state in (lch.PollState.EXITED, lch.PollState.GONE):
                        break
                declared = handle.envelope_path or envelope
                if declared is None or not Path(declared).is_file():
                    raise FactoryRefused("STAGE_PAYLOAD_MISSING")
                return json.loads(Path(declared).read_text(encoding="utf-8"))
            finally:
                self.launcher.cancel(handle, time.monotonic() + 5.0)
        finally:
            self._safe_remove_attempt(attempt, checkout)

    def write_tests(self, ctx: LaneContext) -> Mapping[str, Any]:
        return self._dispatch(ctx, "tester")

    def review_tests(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        return self._review_payload(self._dispatch(ctx, "test-reviewer"))

    def seal_tests(self, ctx: LaneContext) -> Mapping[str, Any]:
        return self._dispatch(ctx, "sealer")

    def build(self, ctx: LaneContext) -> Mapping[str, Any]:
        payload = dict(self._dispatch(ctx, "builder"))
        if "candidate_sha" not in payload:
            raise FactoryRefused("BUILDER_CANDIDATE_MISSING")
        return payload

    def review_code(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        return self._review_payload(self._dispatch(ctx, "code-reviewer"))

    def review_integration(
        self,
        ctx: LaneContext,
        lanes: Sequence[st.LaneProjection],
        integration_sha: str,
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]], Sequence[str]]:
        payload = dict(self._dispatch(ctx, "integration-reviewer"))
        payload.setdefault("integration_sha", integration_sha)
        payload.setdefault("lane_ids", [lane.lane_id for lane in lanes])
        verdict, findings = self._review_payload(payload)
        affected = tuple(payload.get("affected_lanes") or ())
        return verdict, findings, affected

    def publish(
        self,
        ctx: LaneContext,
        *,
        fingerprint: str,
        expected_before: str,
        published_sha: str,
    ) -> Mapping[str, Any]:
        del expected_before
        return {
            "receipt_object": published_sha,
            "receipt_ref": st.publication_ref(ctx.run_id, fingerprint),
        }

    def _review_payload(
        self, payload: Mapping[str, Any]
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        if "verdict" not in payload:
            raise FactoryRefused("REVIEW_VERDICT_MISSING")
        return st.ReviewerVerdict(payload["verdict"]), list(
            payload.get("findings") or ()
        )


def _actor_for(
    runtime: RuntimeStateRoot,
    layout: Mapping[str, Any],
    target: gitpub.TargetBinding,
) -> StageActor:
    executables = layout.get("executables") or {}
    if not isinstance(executables, dict):
        executables = {}
    receipts = layout.get("route_receipts") or {}
    key_paths = layout.get("route_verify_keys") or ()
    if not isinstance(receipts, dict) or not receipts or not key_paths:
        raise FactoryRefused("ROUTE_RECEIPTS_REQUIRED")
    keys = tuple(load_public_key(Path(path)) for path in key_paths)
    admitted = load_admitted_routes(
        {str(name): Path(path) for name, path in receipts.items()},
        verify_keys=keys,
    )
    launcher = lch.HerdrLauncher(
        herdr_path=Path(str(executables.get("herdr") or "herdr")),
        omp_path=Path(str(executables.get("omp") or "omp")),
        claude_path=Path(str(executables.get("claude") or "claude")),
        admitted_routes=admitted,
        workspace_label="maestro-factory",
    )
    return HerdrStageActor(launcher, runtime.path / "worktrees", target)


def _compile_plan(path: Path, *, revision: int, ref: str) -> st.CompiledPlan:
    return plan_compiler.compile_plan(
        path.read_bytes(), plan_revision=revision, plan_artifact_ref=ref
    )


def _run_start(args: argparse.Namespace) -> int:
    maestro_file = _executing_maestro_file()
    repo = Path(args.repo).resolve()
    main_ref = args.main_ref
    if not main_ref:
        return _RunRefused(
            "RUN_CONFIGURATION_REQUIRED", "--main-ref is required"
        ).emit()
    layout = _load_deployment_config(maestro_file)
    require_deployment(maestro_file, repo)
    runtime = _open_runtime(layout, repo)
    try:
        runtime.ensure_layout()
        plan_path = Path(args.plan)
        compiled = _compile_plan(
            plan_path,
            revision=1,
            ref=str(plan_path.resolve()),
        )
        target = gitpub.bind_target_worktree(repo, main_ref)
        store = _open_store(runtime)
        run_id = args.run_id or uuid.uuid4().hex
        try:
            create_factory_run(
                store=store,
                run_id=run_id,
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            scheduler = FactoryScheduler(
                store, run_id, _actor_for(runtime, layout, target), runtime, target
            )
            status = scheduler.run()
        finally:
            store.close()
    finally:
        runtime.close()
    print(
        json.dumps(
            {"outcome": "STARTED", "run_id": run_id, "status": status.value},
            sort_keys=True,
        )
    )
    return 0


def _bind_existing_run(
    run_id: str,
) -> tuple[
    dict[str, Any],
    RuntimeStateRoot,
    ArtifactStore,
    Mapping[str, Any],
    gitpub.TargetBinding,
]:
    maestro_file = _executing_maestro_file()
    layout = _load_deployment_config(maestro_file)
    require_deployment(maestro_file, layout["repo"])
    runtime = _open_runtime(layout, layout["repo"])
    runtime.ensure_layout()
    store = _open_store(runtime)
    try:
        row = run_row(store, run_id)
        require_deployment(maestro_file, Path(row["target_repository_root"]))
        runtime.revalidate(row["runtime_state_fingerprint"])
        target = gitpub.bind_target_worktree(
            row["target_repository_root"], row["target_main_ref"]
        )
    except Exception:
        store.close()
        runtime.close()
        raise
    return layout, runtime, store, row, target


def _run_resume(args: argparse.Namespace) -> int:
    run_id = args.run_id
    layout, runtime, store, _row, target = _bind_existing_run(run_id)
    try:
        try:
            scheduler = FactoryScheduler(
                store, run_id, _actor_for(runtime, layout, target), runtime, target
            )
            scheduler.resume_waiting()
            status = scheduler.run()
        finally:
            store.close()
    finally:
        runtime.close()
    print(
        json.dumps(
            {"outcome": "RESUMED", "run_id": run_id, "status": status.value},
            sort_keys=True,
        )
    )
    return 0


def _run_amend(args: argparse.Namespace) -> int:
    run_id = args.run_id
    layout, runtime, store, row, target = _bind_existing_run(run_id)
    try:
        try:
            compiled = _compile_plan(
                Path(args.plan),
                revision=row["plan_revision"] + 1,
                ref=str(Path(args.plan).resolve()),
            )
            apply_factory_amendment(
                store,
                run_id,
                compiled,
                runtime=runtime,
                target=target,
            )
            scheduler = FactoryScheduler(
                store, run_id, _actor_for(runtime, layout, target), runtime, target
            )
            status = scheduler.run()
        finally:
            store.close()
    finally:
        runtime.close()
    print(
        json.dumps(
            {"outcome": "AMENDED", "run_id": run_id, "status": status.value},
            sort_keys=True,
        )
    )
    return 0


def _run_status(args: argparse.Namespace) -> int:
    run_id = args.run_id
    layout, runtime, store, _row, target = _bind_existing_run(run_id)
    try:
        try:
            gitpub.revalidate_binding(target)
            scheduler = FactoryScheduler(
                store, run_id, _actor_for(runtime, layout, target), runtime, target
            )
            status = scheduler.status()
            stages = {
                lane.lane_id: store.lane_stage(run_id, lane.lane_id).value
                for lane in store.active_projection(run_id)
            }
        finally:
            store.close()
    finally:
        runtime.close()
    print(
        json.dumps(
            {
                "outcome": "STATUS",
                "run_id": run_id,
                "status": status.value,
                "lanes": stages,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro")
    root = parser.add_subparsers(dest="command", required=True)

    run = root.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    start = run_sub.add_parser("start")
    start.add_argument("plan")
    start.add_argument("--repo", required=True)
    start.add_argument("--main-ref", required=True)
    start.add_argument("--run-id")
    start.set_defaults(handler=_run_start)

    resume = run_sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.set_defaults(handler=_run_resume)

    amend = run_sub.add_parser("amend")
    amend.add_argument("plan")
    amend.add_argument("--run", dest="run_id", required=True)
    amend.set_defaults(handler=_run_amend)

    status = run_sub.add_parser("status")
    status.add_argument("run_id")
    status.set_defaults(handler=_run_status)
    return parser


def parser_verbs(parser: argparse.ArgumentParser) -> Tuple[str, ...]:
    found = []

    def walk(current: argparse.ArgumentParser, prefix: Tuple[str, ...]) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                path = prefix + (name,)
                if any(
                    isinstance(item, argparse._SubParsersAction)
                    for item in child._actions
                ):
                    walk(child, path)
                else:
                    found.append(" ".join(path))

    walk(parser, ())
    return tuple(found)


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = tuple(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(list(raw))
    try:
        return int(args.handler(args))
    except _RunRefused as exc:
        return exc.emit()
    except RunRepositoryMismatch as exc:
        return _RunRefused("RUN_REPOSITORY_MISMATCH", str(exc)).emit()
    except LedgerSchemaUnsupported as exc:
        return _RunRefused("LEDGER_SCHEMA_UNSUPPORTED", str(exc)).emit()
    except RuntimeStateRefused as exc:
        return _RunRefused("RUNTIME_STATE_REFUSED", str(exc)).emit()
    except FactoryRefused as exc:
        return _RunRefused(exc.code, str(exc)).emit()
    except LaunchFailed as exc:
        return _RunRefused("LAUNCH_FAILED", exc.detail).emit()
    except gitpub.GitPublicationRefused as exc:
        return _RunRefused(exc.code, str(exc)).emit()
    except _MaestroConfigurationError as exc:
        return _RunRefused("RUN_CONFIGURATION_REQUIRED", str(exc)).emit()


if __name__ == "__main__":
    raise SystemExit(main())
