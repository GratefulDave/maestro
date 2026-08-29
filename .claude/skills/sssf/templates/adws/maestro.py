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

# This must run before third-party and local imports can dirty a deployed checkout.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import yaml

from adw_modules import git_publication as gitpub
from adw_modules import hidden_vault as hv
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore, LedgerSchemaUnsupported
from adw_modules.route_receipts import load_admitted_routes, load_public_key
from adw_modules.runtime_state import RuntimeStateRefused, RuntimeStateRoot
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
    loaded["runner_profile"] = _config_string(
        loaded.get("runner_profile"), "runner_profile"
    )
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
        state_root: Path,
        target: gitpub.TargetBinding,
        runner_profile: str,
    ) -> None:
        if not runner_profile.strip():
            raise FactoryRefused("RUNNER_PROFILE_REQUIRED")
        self.launcher = launcher
        self.state_root = Path(state_root)
        self.worktrees = self.state_root / "worktrees"
        self.target = target
        self.runner_profile = runner_profile.strip()

    def _git(self, repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise FactoryRefused(
                (result.stderr or result.stdout or "git failed").strip()
            )
        return (result.stdout or "").strip()

    def _base_sha(self, ctx: LaneContext, role: str, integration_sha: str = "") -> str:
        if role == "code-reviewer" and ctx.candidate_sha:
            return ctx.candidate_sha
        if role == "integration-reviewer" and integration_sha:
            return integration_sha
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
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), sha],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _safe_remove_attempt(self, attempt: Path, checkout: Path | None) -> None:
        try:
            resolved = attempt.resolve()
        except OSError:
            return
        root = self.worktrees.resolve()
        if root not in resolved.parents or resolved == root:
            return
        repo = Path(self.target.target_repository_root)
        if checkout is not None and checkout.exists():
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

    def _schema(self, role: str) -> dict[str, Any]:
        findings = list(st.REVISE_FINDING_KEYS)
        if role == "tester":
            return {"private_files": {"<path>": "<utf-8 contents>"}}
        if role == "builder":
            return {"candidate_sha": "<optional git sha>", "changed": "<optional bool>"}
        if role in ("test-reviewer", "code-reviewer"):
            return {"verdict": "PASS|REVISE", "findings": findings}
        return {
            "verdict": "PASS|REVISE",
            "findings": findings,
            "affected_lanes": ["<lane-id>"],
        }

    def _prompt(
        self,
        ctx: LaneContext,
        role: str,
        envelope: Path,
        cwd: Path,
        extra: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        envelope_path = str(envelope.resolve())
        schema = self._schema(role)
        instructions = (
            "Write UTF-8 JSON to {0} and stop. Schema: {1}. CWD: {2}."
        ).format(envelope_path, json.dumps(schema, sort_keys=True), cwd.resolve())
        if role == "tester":
            instructions += " Create private tests here. Do not git commit. Do not write the product repo."
        elif role == "test-reviewer":
            instructions += " Inspect this private TEST_DRAFT tree. Return PASS or REVISE findings. No leaked literals."
        elif role == "builder":
            instructions += " Edit only declared_outputs. Commit those files. No private tests, fixtures, or vault paths."
        elif role == "code-reviewer":
            instructions += (
                " Inspect this candidate product checkout. Private tests are absent."
            )
        elif role == "integration-reviewer":
            instructions += " Inspect this exact integration SHA. Return verdict, findings, affected_lanes."
        if role in ("test-reviewer", "code-reviewer", "integration-reviewer"):
            instructions += " PASS requires findings=[]. REVISE requires at least one actionable finding."
        if role == "integration-reviewer":
            instructions += " PASS requires affected_lanes=[]. REVISE requires a nonempty affected_lanes subset."
        body: dict[str, Any] = {
            "envelope_path": envelope_path,
            "envelope_schema": schema,
            "instructions": instructions,
            "lane_id": ctx.lane.lane_id,
            "plan_revision": ctx.plan_revision,
            "role": role,
            "run_id": ctx.run_id,
            "stage": ctx.stage.value,
            "working_directory": str(cwd.resolve()),
        }
        body.update(extra)
        if role == "builder":
            for key in list(body):
                if key in st.FORBIDDEN_PRIVATE_KEYS or key in (
                    "private_files",
                    "vault_path",
                    "vault_ref",
                ):
                    del body[key]
            text = json.dumps(body, sort_keys=True)
            if "vaults/" in text or "private_files" in text:
                raise FactoryRefused("PRIVATE_TEST_LEAK")
        return st.json_ready(body)

    def _collect_uncommitted(self, checkout: Path) -> dict[str, str]:
        listed = self._git(
            checkout, "ls-files", "-o", "-m", "--exclude-standard", check=False
        )
        files: dict[str, str] = {}
        for rel in listed.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            path = checkout / rel
            if path.is_file():
                files[rel.replace("\\", "/")] = path.read_text(encoding="utf-8")
        return files

    def _commit_declared(
        self, checkout: Path, outputs: Sequence[str], base: str
    ) -> tuple[str, bool]:
        for rel in outputs:
            if (checkout / rel).is_file():
                self._git(checkout, "add", "--", rel)
        if self._git(checkout, "status", "--porcelain"):
            self._git(checkout, "config", "user.email", "maestro-builder@invalid")
            self._git(checkout, "config", "user.name", "maestro-builder")
            self._git(checkout, "commit", "-m", "declared outputs")
        sha = self._git(checkout, "rev-parse", "HEAD")
        return sha, sha != base

    @staticmethod
    def _launch_environment(attempt: Path) -> dict[str, str]:
        scratch = attempt / "scratch"
        redirects = {
            "TMPDIR": str(scratch / "tmp"),
            "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
            "PYTEST_ADDOPTS": "-o cache_dir={}".format(scratch / "pytest_cache"),
            "COVERAGE_FILE": str(scratch / "coverage"),
            "RUFF_CACHE_DIR": str(scratch / "ruff"),
            "npm_config_cache": str(scratch / "npm"),
        }
        if set(redirects) != set(lch.SCRATCH_ENV_KEYS):
            raise FactoryRefused("SCRATCH_ENV_CONTRACT_MISMATCH")
        for key in (
            "TMPDIR",
            "PYTHONPYCACHEPREFIX",
            "RUFF_CACHE_DIR",
            "npm_config_cache",
        ):
            Path(redirects[key]).mkdir(parents=True, exist_ok=True)
        (scratch / "pytest_cache").mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment.update(redirects)
        return environment

    def _launch(
        self,
        ctx: LaneContext,
        role: str,
        cwd: Path,
        extra: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        attempt = cwd if cwd.name != "checkout" else cwd.parent
        if (attempt / "session").parent != attempt:
            attempt = cwd.parent
        session = attempt / "session"
        session.mkdir(exist_ok=True)
        envelope = attempt / "envelope.json"
        prompt = attempt / "prompt.json"
        prompt.write_bytes(
            st.canonical_bytes(self._prompt(ctx, role, envelope, cwd, extra))
        )
        spec = lch.LaunchSpec(
            correlation_token="{}:{}:{}:{}".format(
                ctx.run_id, ctx.lane.lane_id, ctx.stage.value, ctx.input_digest[:12]
            ),
            worktree=cwd,
            prompt_path=prompt,
            envelope_path=envelope,
            route="omp",
            model="",
            effort="",
            profile=self.runner_profile,
            session_dir=session,
            environment=self._launch_environment(attempt),
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

    def _prepare(
        self, ctx: LaneContext, role: str, *, sha: str | None, private_tree: bool
    ) -> tuple[Path, Path | None]:
        attempt = self._new_attempt_dir(ctx)
        cwd = attempt / "checkout"
        checkout: Path | None = None
        if private_tree:
            draft = ctx.artifacts.get("TEST_DRAFT")
            if draft is None:
                raise FactoryRefused("missing TEST_DRAFT")
            vault = hv.ensure_vault(self.state_root, ctx.run_id)
            hv.materialize_commit(vault, hv.rev_parse(vault, draft.artifact_ref), cwd)
            return attempt, None
        if not sha:
            raise FactoryRefused("missing checkout sha")
        self._add_worktree(cwd, sha)
        return attempt, cwd

    def write_tests(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "tester")
        attempt, checkout = self._prepare(ctx, "tester", sha=sha, private_tree=False)
        try:
            extra = {
                "declared_outputs": list(ctx.lane.declared_outputs),
                "public_acceptance": list(ctx.lane.public_acceptance),
            }
            payload = self._launch(
                ctx, "tester", checkout or attempt / "checkout", extra
            )
            files = dict(payload.get("private_files") or {})
            if checkout is not None:
                files.update(self._collect_uncommitted(checkout))
                self._git(checkout, "reset", "--hard", sha, check=False)
                self._git(checkout, "clean", "-fd", check=False)
            return {"private_files": files}
        finally:
            self._safe_remove_attempt(attempt, checkout)

    def review_tests(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        attempt, checkout = self._prepare(
            ctx, "test-reviewer", sha=None, private_tree=True
        )
        try:
            cwd = attempt / "checkout"
            payload = self._launch(
                ctx, "test-reviewer", cwd, {"public_contract": ctx.public_contract}
            )
            return self._review_payload(payload)
        finally:
            self._safe_remove_attempt(attempt, checkout)

    def build(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "builder")
        extra = {
            "builder_base_sha": sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            "sealed_digest": ctx.sealed_digest,
        }
        attempt, checkout = self._prepare(ctx, "builder", sha=sha, private_tree=False)
        try:
            if checkout is None:
                raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
            payload = self._launch(ctx, "builder", checkout, extra)
            candidate_sha, changed = self._commit_declared(
                checkout, ctx.lane.declared_outputs, sha
            )
            if payload.get("candidate_sha"):
                candidate_sha = str(payload["candidate_sha"])
            if "changed" in payload:
                changed = bool(payload["changed"])
            return {"candidate_sha": candidate_sha, "changed": changed}
        finally:
            self._safe_remove_attempt(attempt, checkout)

    def review_code(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        extra = {
            "candidate_sha": ctx.candidate_sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            "sealed_digest": ctx.sealed_digest,
        }
        sha = self._base_sha(ctx, "code-reviewer")
        attempt, checkout = self._prepare(
            ctx, "code-reviewer", sha=sha, private_tree=False
        )
        try:
            payload = self._launch(
                ctx, "code-reviewer", checkout or attempt / "checkout", extra
            )
            return self._review_payload(payload)
        finally:
            self._safe_remove_attempt(attempt, checkout)

    def review_integration(
        self,
        ctx: LaneContext,
        lanes: Sequence[st.LaneProjection],
        integration_sha: str,
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]], Sequence[str]]:
        extra = {
            "integration_sha": integration_sha,
            "lane_ids": [lane.lane_id for lane in lanes],
        }
        sha = self._base_sha(ctx, "integration-reviewer", integration_sha)
        attempt, checkout = self._prepare(
            ctx, "integration-reviewer", sha=sha, private_tree=False
        )
        try:
            payload = self._launch(
                ctx, "integration-reviewer", checkout or attempt / "checkout", extra
            )
            verdict, findings = self._review_payload(payload)
            return verdict, findings, tuple(payload.get("affected_lanes") or ())
        finally:
            self._safe_remove_attempt(attempt, checkout)

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
    run_id: str,
) -> StageActor:
    executables = layout.get("executables") or {}
    if not isinstance(executables, dict):
        executables = {}
    profile = layout.get("runner_profile")
    if not isinstance(profile, str) or not profile.strip():
        raise FactoryRefused("RUNNER_PROFILE_REQUIRED")
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
        workspace_label=run_id,
    )
    return HerdrStageActor(launcher, runtime.path, target, profile.strip())


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
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id),
                runtime,
                target,
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
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id),
                runtime,
                target,
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
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id),
                runtime,
                target,
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
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id),
                runtime,
                target,
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
