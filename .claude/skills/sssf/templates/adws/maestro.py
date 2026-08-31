#!/usr/bin/env python3
"""Maestro factory CLI: run start/resume/amend/status."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

# This must run before third-party and local imports can dirty a deployed checkout.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import yaml

from adw_modules import factory_console as fconsole
from adw_modules import git_publication as gitpub
from adw_modules import hidden_vault as hv
from adw_modules.handoff_budget import (
    OMP_CONTEXT_WINDOW_TOKENS,
    route_publishes_a_window,
)
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import scheduler_types as st
from adw_modules import tests_chain as tchain
from adw_modules.lifecycle import ArtifactStore, LedgerSchemaUnsupported
from adw_modules.reporting_registry import register_installation
from adw_modules.dashboard_autoload import maybe_autoload_dashboard
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
    binding_from_run,
    create_factory_run,
    plan_artifact_ref_for,
    require_deployment,
    run_row,
    target_from_binding,
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


class CleanupRefused(RuntimeError):
    """COMPLETE space cleanup failed after publication; panes/cwds remain."""

    code = "CLEANUP_REFUSED"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


_ROLE_ROUTE_FIELDS = frozenset(("route", "model", "effort", "profile"))
_DASHBOARD_FIELDS = frozenset(("enabled", "launcher", "api_port", "ui_port", "open"))



def _config_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _MaestroConfigurationError(label + " must be a nonempty string")
    return value.strip()


def _optional_config_string(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _MaestroConfigurationError(label + " must be a string")
    return value.strip()


def _config_argv(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise _MaestroConfigurationError(label + " must be a nonempty list")
    argv: list[str] = []
    for index, item in enumerate(value):
        argv.append(_config_string(item, f"{label}[{index}]"))
    return tuple(argv)


def _canonical_role_routes(
    value: object,
) -> Mapping[str, Mapping[str, str]]:
    if not isinstance(value, Mapping):
        raise _MaestroConfigurationError("role_routes must be a mapping")
    expected = frozenset(lch.LANE_PANE_ROLES)
    if frozenset(value) != expected:
        raise _MaestroConfigurationError(
            "role_routes must bind exactly " + ", ".join(sorted(expected))
        )
    canonical: dict[str, Mapping[str, str]] = {}
    for role in lch.LANE_PANE_ROLES:
        binding = value[role]
        if not isinstance(binding, Mapping):
            raise _MaestroConfigurationError(
                "role_routes.{} must be a mapping".format(role)
            )
        extras = frozenset(binding) - _ROLE_ROUTE_FIELDS
        if extras:
            raise _MaestroConfigurationError(
                "role_routes.{} has unsupported fields: {}".format(
                    role, ", ".join(sorted(extras))
                )
            )
        route = _config_string(
            binding.get("route"), "role_routes.{}.route".format(role)
        )
        model = _optional_config_string(
            binding.get("model"), "role_routes.{}.model".format(role)
        )
        effort = _optional_config_string(
            binding.get("effort"), "role_routes.{}.effort".format(role)
        )
        profile = _optional_config_string(
            binding.get("profile"), "role_routes.{}.profile".format(role)
        )
        if route == "omp":
            if not profile or model or effort:
                raise _MaestroConfigurationError(
                    "role_routes.{} must use only an omp profile".format(role)
                )
        elif route == "claude":
            if not model or not effort or profile:
                raise _MaestroConfigurationError(
                    "role_routes.{} must use Claude model and effort only".format(role)
                )
        else:
            raise _MaestroConfigurationError(
                "role_routes.{}.route must be omp or claude".format(role)
            )
        canonical[role] = MappingProxyType(
            {
                "route": route,
                "model": model,
                "effort": effort,
                "profile": profile,
            }
        )
    return MappingProxyType(canonical)

def _config_port(value: object, label: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _MaestroConfigurationError(label + " must be an integer port")
    if not 1 <= value <= 65535:
        raise _MaestroConfigurationError(label + " must be an integer port")
    return value


def _canonical_dashboard(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _MaestroConfigurationError("dashboard must be a mapping")
    extras = frozenset(value) - _DASHBOARD_FIELDS
    if extras:
        raise _MaestroConfigurationError(
            "dashboard has unsupported fields: " + ", ".join(sorted(extras))
        )
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _MaestroConfigurationError("dashboard.enabled must be a boolean")
    launcher = value.get("launcher")
    if launcher is not None:
        launcher = _config_string(launcher, "dashboard.launcher")
    open_browser = value.get("open", True)
    if not isinstance(open_browser, bool):
        raise _MaestroConfigurationError("dashboard.open must be a boolean")
    return MappingProxyType(
        {
            "enabled": enabled,
            "launcher": launcher,
            "api_port": _config_port(
                value.get("api_port"), "dashboard.api_port", 4600
            ),
            "ui_port": _config_port(
                value.get("ui_port"), "dashboard.ui_port", 4317
            ),
            "open": open_browser,
        }
    )


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
    if "runner_profile" in loaded:
        raise _MaestroConfigurationError("runner_profile is unsupported")
    root = Path(_config_string(loaded.get("runtime_state_root"), "runtime_state_root"))
    if not root.is_absolute():
        raise _MaestroConfigurationError("runtime_state_root must be absolute")
    loaded["runtime_state_root"] = root
    loaded["role_routes"] = _canonical_role_routes(loaded.get("role_routes"))
    loaded["provision_argv"] = _config_argv(
        loaded.get("provision_argv"), "provision_argv"
    )
    loaded["dashboard"] = _canonical_dashboard(loaded.get("dashboard"))
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


def _project_identity(target: gitpub.TargetBinding) -> str:
    name = Path(target.target_repository_root).name or "maestro"
    return "{}-{}".format(name, target.target_repository_fingerprint)


class _RoleSession:
    def __init__(
        self,
        handle: object,
        cwd: Path,
        attempt: Path,
        checkout: Path | None,
        run_id: str = "",
    ) -> None:
        self.handle = handle
        self.cwd = cwd
        self.attempt = attempt
        self.checkout = checkout
        self.turns = 0
        self.run_id = run_id


def _precreated_role_cwd(dest: Path) -> bool:
    """Whether pane provisioning left only checkout-local scratch in `dest`."""
    try:
        if dest.is_symlink() or not dest.is_dir():
            return False
        return {child.name for child in dest.iterdir()} <= {lch.ROLE_AGENT_DIR}
    except OSError:
        return False


def _relative_under(root: Path, path: Path) -> str:
    root = Path(root).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        located = candidate.parent.resolve() / candidate.name
        try:
            relative = located.relative_to(root)
        except ValueError as exc:
            raise FactoryRefused("ROLE_OUTPUT_UNSAFE:outside checkout") from exc
    else:
        relative = candidate
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise FactoryRefused("ROLE_OUTPUT_UNSAFE:path")
    return str(relative).replace("\\", "/")



def _read_regular_text_under(root: Path, path: Path) -> str:
    """Read a regular file under `root`. Never follow symlinks."""
    relative = _relative_under(root, path)
    dir_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(Path(root).resolve()), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = Path(relative).parts
        for index, name in enumerate(parts):
            last = index == len(parts) - 1
            try:
                nxt = os.open(name, file_flags if last else dir_flags, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    raise FileNotFoundError(path) from exc
                raise FactoryRefused("ROLE_OUTPUT_UNSAFE:{0}".format(relative)) from exc
            os.close(fd)
            fd = nxt
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FactoryRefused("ROLE_OUTPUT_UNSAFE:{0}".format(relative))
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _clear_precreated_role_cwd(dest: Path) -> bool:
    """Empty a precreated role cwd without replacing its process-bound inode."""
    if not _precreated_role_cwd(dest):
        return False
    for child in dest.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    return True


class HerdrStageActor:
    """Persistent role panes. Envelope bytes are payload, not stage."""

    def __init__(
        self,
        launcher: lch.LauncherAdapter,
        state_root: Path,
        target: gitpub.TargetBinding,
        role_routes: Mapping[str, Mapping[str, str]],
        lane_specs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.launcher = launcher
        self.state_root = Path(state_root)
        self.worktrees = self.state_root / "worktrees"
        self.target = target
        self.role_routes = _canonical_role_routes(role_routes)
        self.lane_specs = MappingProxyType(
            {
                str(lane_id): MappingProxyType(dict(spec))
                for lane_id, spec in (lane_specs or {}).items()
            }
        )
        self.project_identity = _project_identity(target)
        self._roles: dict[tuple[str, str], _RoleSession] = {}

    def _validate_role_git(self, repo: Path) -> None:
        resolved = Path(repo).resolve()
        try:
            resolved.relative_to(self.worktrees.resolve())
        except ValueError:
            return
        try:
            marker = (resolved / ".git").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FactoryRefused("ROLE_GIT_BINDING_REFUSED:{}".format(exc)) from exc
        prefix = "gitdir:"
        if not marker.startswith(prefix):
            raise FactoryRefused("ROLE_GIT_BINDING_REFUSED:ROLE_GIT_BINDING_INVALID")
        raw = marker[len(prefix) :].strip()
        gitdir = Path(raw)
        if not gitdir.is_absolute():
            gitdir = resolved / gitdir
        try:
            relative = gitdir.resolve().relative_to(
                Path(self.target.target_git_common_dir).resolve()
            )
        except ValueError as extra:
            raise FactoryRefused(
                "ROLE_GIT_BINDING_REFUSED:ROLE_GIT_BINDING_MISMATCH"
            ) from extra
        if len(relative.parts) < 2 or relative.parts[0] != "worktrees":
            raise FactoryRefused("ROLE_GIT_BINDING_REFUSED:ROLE_GIT_BINDING_MISMATCH")

    def _git(self, repo: Path, *args: str, check: bool = True) -> str:
        self._validate_role_git(repo)
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

    def _git_bytes(
        self,
        repo: Path,
        *args: str,
        input_bytes: bytes | None = None,
    ) -> bytes:
        self._validate_role_git(repo)
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or b"git failed"
            raise FactoryRefused(detail.decode("utf-8", errors="replace").strip())
        return result.stdout

    def _base_sha(self, ctx: LaneContext, role: str, integration_sha: str = "") -> str:
        if role == "tester":
            return ctx.integration_head or self.target.integration_initial_sha
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
        return self._role_dir(ctx, "tester")

    def _add_worktree(self, dest: Path, sha: str) -> None:
        dest = Path(dest)
        precreated = _clear_precreated_role_cwd(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        repo = Path(self.target.target_repository_root)
        subprocess.check_call(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), sha],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if precreated:
            lch.scratch_environment(dest)

    def _path_is_live_retained(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for stored in self._roles.values():
            for candidate in (stored.cwd, stored.checkout):
                if candidate is None:
                    continue
                try:
                    live = candidate.resolve()
                except OSError:
                    continue
                if (
                    resolved == live
                    or live in resolved.parents
                    or resolved in live.parents
                ):
                    return True
        return False

    def _safe_remove_attempt(self, attempt: Path, checkout: Path | None) -> None:
        try:
            resolved = attempt.resolve()
        except OSError:
            return
        root = self.worktrees.resolve()
        if root not in resolved.parents or resolved == root:
            return
        if self._path_is_live_retained(attempt) or self._path_is_live_retained(
            checkout
        ):
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

    def _materialize_role_instructions(
        self, cwd: Path, role: str, route: str, lane_kind: str | None = None
    ) -> Path:
        if role == "tester" and lane_kind == st.LANE_KIND_TESTS:
            tester_rule = (
                "Inspect the product checkout without modifying product files. "
                "Return private acceptance files only through the requested envelope. "
                "Author files exactly at declared_outputs. Returned private_files "
                "paths must equal declared_outputs. On correction turns, apply "
                "revise_findings to those declared files; do not claim a finding "
                "is fixed by resubmitting byte-identical files."
            )
        else:
            tester_rule = (
                "Inspect the product checkout without modifying product files. "
                "Return private test files only through the requested envelope. "
                "Private paths must not collide with declared product outputs. "
                "Write hidden validator/meta-test files that exercise builder "
                "outputs; never replace those outputs. On correction turns, "
                "apply revise_findings to hidden validators; do not claim a "
                "finding is fixed by resubmitting byte-identical hidden files."
            )
        role_rules = {
            "tester": tester_rule,

            "test-reviewer": (
                "## Test-reviewer obligations\n"
                "Review private TEST_DRAFT tests against lane-plan obligations. "
                "Check behavior coverage, satisfiability/non-vacuity, and "
                "deterministic isolation. Return PASS or REVISE with actionable "
                "findings for the tester. Never review or prescribe product "
                "implementation. Never expose private tests to builder or "
                "product outputs. Review only the private_draft_overlay files "
                "listed in the per-turn JSON. Integration-seed and product "
                "files are context and out of scope. A private validator "
                "failing against the base is expected when falsifiability "
                "requires red-at-base."
            ),
            "builder": (
                "Modify only the declared product outputs. Never read private "
                "tests, fixtures, vault paths, or hidden test material."
            ),
            "code-reviewer": (
                "## Code-reviewer obligations\n"
                "Review the exact product candidate and declared outputs against "
                "the lane plan. Check implementation correctness, regressions, "
                "security, and maintainability. Return PASS or REVISE with "
                "actionable findings for the builder, each resolvable by editing "
                "declared outputs only. Never prescribe changes to files outside "
                "declared outputs. If an external test contradicts the lane's "
                "public contract, assess the candidate against the contract "
                "instead of demanding a test edit. Private tests are absent and "
                "must not be inferred, requested, or cited."
            ),
            "integration-reviewer": (
                "Review the exact integration checkout read-only. Return a "
                "verdict, findings, and only genuinely affected lane IDs."
            ),
        }
        try:
            role_rule = role_rules[role]
        except KeyError as exc:
            raise FactoryRefused("UNKNOWN_ROLE:{}".format(role)) from exc
        content = (
            "# Maestro {0} role contract\n\n"
            "- Work only in the assigned checkout: the process CWD.\n"
            "- Never inspect or access a parent repository, sibling worktree, "
            "integration checkout, Maestro orchestration source, or runtime-state tree.\n"
            "- Review or inspect only files physically contained in the assigned checkout; "
            "refuse requests to review, compare, or cite content outside it.\n"
            "- Use enabled profile and repository capabilities only for this role's task.\n"
            "- Native Read, Write, Edit, Bash, skills, and MCP remain available.\n"
            "- Do not delegate or spawn subagents.\n"
            "- Treat the per-turn JSON prompt, envelope path, and envelope schema as authoritative.\n"
            "- Never commit, branch, merge, or rebase; the broker owns Git publication.\n"
            "- Write only what this role permits, then write the requested UTF-8 JSON envelope and stop.\n\n"
            "{1}\n"
        ).format(role, role_rule)
        if route == "claude":
            content += (
                "\nClaude-only bound: review only this assigned worktree. Do not "
                "open, compare, or cite a sibling role checkout, parent repository, "
                "or any path outside this CWD.\n"
            )
        agent_root = lch.role_agent_dir(cwd)
        agent_root.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        for name in ("AGENTS.md", "CLAUDE.md"):
            (agent_root / name).write_bytes(encoded)
        return agent_root / ("CLAUDE.md" if route == "claude" else "AGENTS.md")

    def _prompt_lane_spec(self, ctx: LaneContext, role: str) -> dict[str, Any]:
        spec = dict(self.lane_specs[ctx.lane.lane_id])
        if role in ("builder", "code-reviewer") and ctx.lane.lane_kind == st.LANE_KIND_BUILD:
            spec.pop("gate", None)
        return spec

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
            "Work only inside CWD. Local file operations stay in CWD. Native "
            "Read, Write, Edit, Bash, skills, and MCP remain available. "
            "Do not delegate. Stay inside CWD. Create UTF-8 JSON at {0} and "
            "stop. Schema: {1}. CWD: {2}."
        ).format(envelope_path, json.dumps(schema, sort_keys=True), cwd.resolve())

        if role == "tester":
            if ctx.lane.lane_kind == st.LANE_KIND_TESTS:
                instructions += (
                    " Author private acceptance files exactly at "
                    "declared_outputs. Returned private_files paths must equal "
                    "declared_outputs. Do not git commit. Do not write the "
                    "product repo."
                )
                if extra.get("revise_findings"):
                    instructions += (
                        " Apply revise_findings to those declared files. Do not "
                        "claim a finding is fixed by resubmitting byte-identical "
                        "files."
                    )
            else:
                instructions += (
                    " Create private tests here. Do not git commit. Do not write "
                    "the product repo. Private paths must be hidden meta-tests, "
                    "never declared_outputs."
                )
                if extra.get("revise_findings"):
                    instructions += (
                        " Apply revise_findings to hidden validators. Do not claim "
                        "a finding is fixed by resubmitting byte-identical hidden "
                        "files."
                    )
        elif role == "test-reviewer":
            instructions += (
                " Review only the private TEST_DRAFT overlay files listed in "
                "private_draft_overlay. Integration-seed and product files in "
                "this checkout are context and out of scope. A private "
                "validator failing against the base is expected when "
                "falsifiability requires red-at-base; do not demand edits to "
                "declared product outputs. Return PASS or REVISE findings. "
                "No leaked literals."
            )
        elif role == "builder":
            instructions += " Edit only declared_outputs. Do not git commit; the broker commits those files. No private tests, fixtures, or vault paths."
        elif role == "code-reviewer":
            instructions += (
                " Inspect this candidate product checkout. Findings must be "
                "resolvable within declared_outputs. Private tests are absent."
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
        if self.lane_specs:
            try:
                body["lane_spec"] = self._prompt_lane_spec(ctx, role)
            except KeyError as exc:
                raise FactoryRefused("LANE_SPEC_MISSING") from exc
        body.update(extra)
        if role in ("builder", "code-reviewer"):
            for key in list(body):
                if key in st.FORBIDDEN_PRIVATE_KEYS or key in (
                    "private_files",
                    "private_draft_overlay",
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
            if not rel or rel == ".maestro-agent" or rel.startswith(".maestro-agent/"):
                continue
            try:
                content = _read_regular_text_under(checkout, checkout / rel)
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError) as exc:
                raise FactoryRefused(f"ROLE_OUTPUT_UNSAFE:{rel}") from exc
            files[rel.replace("\\", "/")] = content
        return files


    def _commit_declared(
        self, checkout: Path, outputs: Sequence[str], base: str
    ) -> tuple[str, bool]:
        requested = tuple(str(item) for item in outputs)
        pathspec: tuple[str, ...] = ()
        if requested:
            listed = self._git(
                checkout,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                *requested,
            )
            pathspec = tuple(item for item in listed.split("\0") if item)
        if pathspec:
            self._git(checkout, "add", "-A", "--", *pathspec)
            patch = self._git_bytes(
                checkout,
                "diff",
                "--cached",
                "--binary",
                base,
                "--",
                *pathspec,
            )
        else:
            patch = b""
        self._git(checkout, "reset", "--hard", base)
        self._clean_checkout(checkout)
        if not patch:
            return base, False
        self._git_bytes(
            checkout,
            "apply",
            "--index",
            "--binary",
            "-",
            input_bytes=patch,
        )
        self._git(checkout, "config", "user.email", "maestro-builder@invalid")
        self._git(checkout, "config", "user.name", "maestro-builder")
        self._git(checkout, "commit", "-m", "declared outputs")
        return self._git(checkout, "rev-parse", "HEAD"), True

    def _clean_checkout(self, checkout: Path) -> None:
        self._git(checkout, "clean", "-fdx", "-e", lch.ROLE_AGENT_DIR)

    def _refresh_git_checkout(self, checkout: Path, sha: str) -> None:
        if not (checkout / ".git").exists():
            raise FactoryRefused("ROLE_CHECKOUT_MISSING")
        self._git(checkout, "reset", "--hard", sha)
        self._clean_checkout(checkout)

    def _refresh_builder_checkout(
        self, checkout: Path, outputs: Sequence[str], base: str
    ) -> None:
        if not (checkout / ".git").exists():
            raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
        head = self._git(checkout, "rev-parse", "HEAD")
        parent = self._git(checkout, "rev-parse", "HEAD^", check=False)
        dirty = bool(self._git(checkout, "status", "--porcelain"))
        if not dirty and (head == base or parent == base):
            self._clean_checkout(checkout)
            return
        self._commit_declared(checkout, outputs, base)


    def _refresh_private_tree(self, ctx: LaneContext, cwd: Path) -> None:
        draft = ctx.artifacts.get("TEST_DRAFT")
        if draft is None:
            raise FactoryRefused("missing TEST_DRAFT")
        vault = hv.ensure_vault(self.state_root, ctx.run_id)
        hv.refresh_materialized_commit(
            vault, hv.rev_parse(vault, draft.artifact_ref), cwd
        )

    @staticmethod
    def _launch_environment(cwd: Path) -> dict[str, str]:
        redirects = lch.scratch_environment(cwd)
        if set(redirects) != set(lch.SCRATCH_ENV_KEYS):
            raise FactoryRefused("SCRATCH_ENV_CONTRACT_MISMATCH")
        environment = dict(os.environ)
        environment.update(redirects)
        return environment

    def _workspace_label(self, ctx: LaneContext) -> str:
        existing = str(getattr(self.launcher, "workspace_label", "") or "")
        if existing:
            return existing
        return lch.workspace_label_for(self.project_identity, ctx.run_id)

    def _role_key(self, ctx: LaneContext, role: str) -> tuple[str, str]:
        return (ctx.lane.lane_id, role)

    def _role_dir(self, ctx: LaneContext, role: str, *, create: bool = True) -> Path:
        path = self.worktrees / ctx.run_id / ctx.lane.lane_id / role
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _role_cwds(self, ctx: LaneContext) -> dict[str, Path]:
        return {
            role: self._role_dir(ctx, role, create=False) / "checkout"
            for role in lch.LANE_PANE_ROLES
        }

    def _revise_findings(self, ctx: LaneContext, artifact_key: str) -> list[Any] | None:
        record = ctx.artifacts.get(artifact_key)
        if record is None:
            return None
        payload = getattr(record, "payload", None) or {}
        if not isinstance(payload, Mapping):
            return None
        findings = payload.get("findings")
        if findings is None:
            return None
        return list(findings)

    def _payload_ok(self, role: str, payload: Mapping[str, Any]) -> bool:
        if role in ("test-reviewer", "code-reviewer", "integration-reviewer"):
            return payload.get("verdict") in (
                st.ReviewerVerdict.PASS.value,
                st.ReviewerVerdict.REVISE.value,
            )
        return True

    def _await_envelope(
        self, handle: object, envelope: Path, role: str
    ) -> Mapping[str, Any]:
        bound = Path(getattr(handle, "launched_cwd", "") or Path(envelope).parent)
        while True:
            envelope_exists = False
            try:
                raw = _read_regular_text_under(bound, envelope)
                envelope_exists = True
                payload = json.loads(raw)
            except FileNotFoundError:
                payload = None
            except FactoryRefused:
                raise
            except (OSError, UnicodeError, ValueError):
                envelope_exists = True
                payload = None

            if isinstance(payload, dict) and self._payload_ok(role, payload):
                wait = getattr(self.launcher, "wait_for_idle", None)
                if wait is not None:
                    wait(handle)
                return payload
            result = self.launcher.poll(handle)
            if result.state in (lch.PollState.GONE, lch.PollState.EXITED):
                outcome = (
                    "STAGE_PAYLOAD_INVALID"
                    if envelope_exists
                    else "STAGE_PAYLOAD_MISSING"
                )
                raise FactoryRefused(outcome)
            time.sleep(0.1)

    def _launch_failed(self, exc: lch.LaunchRefused) -> LaunchFailed:
        return LaunchFailed(
            "{}:{}".format(exc.refusal.value, exc.detail),
            pane_created=bool(exc.pane_created),
        )

    def _retain_completed(self, handle: object, key: tuple[str, str]) -> None:
        retain = getattr(self.launcher, "retain", None)
        if retain is None:
            return
        try:
            retain(handle)
        except lch.LaunchRefused:
            self._roles.pop(key, None)

    def _launch(
        self,
        ctx: LaneContext,
        role: str,
        cwd: Path,
        extra: Mapping[str, Any],
        *,
        prepare_cwd: Callable[[Path], None],
    ) -> tuple[Mapping[str, Any], object, Path]:
        key = self._role_key(ctx, role)
        route = self.role_routes[role]
        stored = self._roles.get(key)
        if stored is not None:
            cwd = stored.cwd
            attempt = stored.attempt
            turn = stored.turns + 1
        else:
            attempt = cwd if cwd.name != "checkout" else cwd.parent
            turn = 1
        while (
            lch.role_result_path(cwd, turn).exists()
            or lch.role_prompt_path(cwd, turn).exists()
        ):
            turn += 1
        if stored is not None:
            stored.turns = turn
            stored.run_id = ctx.run_id
        session = attempt / "session"
        session.mkdir(exist_ok=True)
        envelope = lch.role_result_path(cwd, turn)
        envelope.parent.mkdir(parents=True, exist_ok=True)
        prompt = lch.role_prompt_path(cwd, turn)

        def write_prompt(actual_cwd: Path) -> None:
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_bytes(
                st.canonical_bytes(self._prompt(ctx, role, envelope, actual_cwd, extra))
            )

        def prepare_adopted_cwd(actual_cwd: Path) -> None:
            adopted = Path(actual_cwd).resolve()
            prepare_cwd(adopted)
            self._materialize_role_instructions(
                adopted, role, route["route"], ctx.lane.lane_kind
            )
            envelope.parent.mkdir(parents=True, exist_ok=True)
            write_prompt(adopted)

        lane_id = ctx.lane.lane_id
        token = lch.role_session_token(ctx.run_id, lane_id, role)
        if stored is not None and stored.handle is not None:
            prepare_cwd(cwd)
            self._materialize_role_instructions(
                cwd, role, route["route"], ctx.lane.lane_kind
            )
            write_prompt(cwd)
            try:
                handle = self.launcher.resubmit(
                    stored.handle,
                    prompt,
                    route=route["route"],
                    expected_token=token,
                    envelope_path=envelope,
                )
            except lch.LaunchRefused as extra_exc:
                raise self._launch_failed(extra_exc) from extra_exc
            cwd_used = Path(getattr(handle, "launched_cwd", cwd))
            payload = self._await_envelope(handle, envelope, role)
            stored.handle = handle
            stored.cwd = cwd_used
            stored.turns = turn
            stored.run_id = ctx.run_id
            self._retain_completed(handle, key)
            return payload, handle, cwd_used

        system_prompt = self._materialize_role_instructions(
            cwd, role, route["route"], ctx.lane.lane_kind
        )
        write_prompt(cwd)
        spec = lch.LaunchSpec(
            correlation_token=token,
            worktree=cwd,
            prompt_path=prompt,
            envelope_path=envelope,
            route=route["route"],
            model=route["model"],
            effort=route["effort"],
            profile=route["profile"],
            system_prompt_path=system_prompt,
            session_dir=session,
            environment=self._launch_environment(cwd),
            context_window_tokens=(
                OMP_CONTEXT_WINDOW_TOKENS
                if route_publishes_a_window(route["route"])
                else None
            ),
            lane_key=lane_id,
            lane_label=lane_id,
            pane_role=role,
            run_id=ctx.run_id,
            repository_fingerprint=self.target.target_repository_fingerprint,
            stage=ctx.stage.value,
            input_digest=ctx.input_digest,
            workspace_label=self._workspace_label(ctx),
            pane_group_size=len(lch.LANE_PANE_ROLES),
            role_cwds=self._role_cwds(ctx),
            prepare_adopted_cwd=prepare_adopted_cwd,
        )
        try:
            handle = self.launcher.launch(spec)
        except lch.LaunchRefused as extra_exc:
            raise self._launch_failed(extra_exc) from extra_exc
        cwd_used = Path(getattr(handle, "launched_cwd", cwd))
        payload = self._await_envelope(handle, envelope, role)
        bound = _RoleSession(handle, cwd_used, attempt, None, ctx.run_id)
        bound.turns = turn
        self._roles[key] = bound
        self._retain_completed(handle, key)
        return payload, handle, cwd_used

    def _prepare(
        self, ctx: LaneContext, role: str, *, sha: str | None, private_tree: bool
    ) -> tuple[Path, Path | None]:
        attempt = self._role_dir(ctx, role)
        cwd = attempt / "checkout"
        precreated = _precreated_role_cwd(cwd)
        if (
            cwd.exists()
            and not precreated
            and ((cwd / ".git").exists() or any(cwd.iterdir()))
        ):
            if private_tree:
                return attempt, None
            if sha and role == "tester":
                self._refresh_git_checkout(cwd, sha)
            return attempt, cwd
        if private_tree:
            draft = ctx.artifacts.get("TEST_DRAFT")
            if draft is None:
                raise FactoryRefused("missing TEST_DRAFT")
            vault = hv.ensure_vault(self.state_root, ctx.run_id)
            if precreated:
                _clear_precreated_role_cwd(cwd)
            hv.materialize_commit(vault, hv.rev_parse(vault, draft.artifact_ref), cwd)
            lch.scratch_environment(cwd)
            return attempt, None
        if not sha:
            raise FactoryRefused("missing checkout sha")
        self._add_worktree(cwd, sha)
        return attempt, cwd

    def _bind_checkout(
        self,
        key: tuple[str, str],
        attempt: Path,
        checkout: Path | None,
        cwd_used: Path,
    ) -> Path:
        stored = self._roles[key]
        used = cwd_used.resolve()
        prepared = checkout or attempt / "checkout"
        if prepared.resolve() != used:
            try:
                used.relative_to(attempt.resolve())
            except ValueError:
                self._safe_remove_attempt(attempt, checkout)
        stored.cwd = used
        stored.attempt = used.parent if used.name == "checkout" else used
        stored.checkout = used if (used / ".git").exists() else None
        return used

    def write_tests(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "tester")
        key = self._role_key(ctx, "tester")
        stored = self._roles.get(key)

        def prepare(path: Path) -> None:
            if (path / ".git").exists():
                self._refresh_git_checkout(path, sha)

        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "tester", sha=sha, private_tree=False
            )
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        if (cwd / ".git").exists():
            self._refresh_git_checkout(cwd, sha)
        extra: dict[str, Any] = {
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_acceptance": list(ctx.lane.public_acceptance),
        }
        findings = list(ctx.draft_correction or ())
        if not findings:
            review_findings = self._revise_findings(ctx, "TEST_REVIEW")
            if review_findings is not None:
                findings = review_findings
        if findings:
            extra["revise_findings"] = findings
        invalidation = ctx.artifacts.get("TEST_INVALIDATION")
        if invalidation is not None:
            payload = getattr(invalidation, "payload", None) or {}
            extra["test_invalidation"] = {
                "artifact_id": getattr(invalidation, "artifact_id", ""),
                "code": payload.get("code") if isinstance(payload, Mapping) else None,
                "reason": payload.get("reason") if isinstance(payload, Mapping) else None,
            }

        payload, _handle, cwd_used = self._launch(
            ctx,
            "tester",
            cwd,
            extra,
            prepare_cwd=prepare,
        )
        cwd_used = self._bind_checkout(key, attempt, checkout, cwd_used)
        files = dict(payload.get("private_files") or {})
        if (cwd_used / ".git").exists():
            files.update(self._collect_uncommitted(cwd_used))
        return {"private_files": files}

    def review_tests(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        key = self._role_key(ctx, "test-reviewer")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "test-reviewer", sha=None, private_tree=True
            )
            cwd = attempt / "checkout"
        else:
            attempt, checkout = stored.attempt, None
            cwd = stored.cwd
        draft = ctx.artifacts.get("TEST_DRAFT")
        if draft is None:
            raise FactoryRefused("missing TEST_DRAFT")
        vault = hv.ensure_vault(self.state_root, ctx.run_id)
        extra = {
            "public_contract": ctx.public_contract,
            "private_draft_overlay": list(
                tchain.private_draft_overlay_paths(vault, draft)
            ),
        }
        payload, _handle, cwd_used = self._launch(
            ctx,
            "test-reviewer",
            cwd,
            extra,
            prepare_cwd=lambda path: self._refresh_private_tree(ctx, path),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        return self._review_payload(payload)

    def build(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "builder")
        extra: dict[str, Any] = {
            "builder_base_sha": sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            "sealed_digest": ctx.sealed_digest,
        }
        sealed = ctx.artifacts.get("SEALED_TEST_BUNDLE")
        if sealed is not None:
            extra["predecessor_bundle_id"] = sealed.artifact_id
            extra["predecessor_bundle_digest"] = ctx.sealed_digest
        findings = self._revise_findings(ctx, "CODE_REVIEW")
        if findings is not None:
            extra["revise_findings"] = findings
        key = self._role_key(ctx, "builder")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "builder", sha=sha, private_tree=False
            )
        else:
            attempt, checkout = stored.attempt, stored.checkout
        if checkout is None and stored is None:
            raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
        cwd = checkout or stored.cwd
        _payload, _handle, cwd_used = self._launch(
            ctx,
            "builder",
            cwd,
            extra,
            prepare_cwd=lambda path: self._refresh_builder_checkout(
                path, ctx.lane.declared_outputs, sha
            ),
        )
        cwd_used = self._bind_checkout(key, attempt, checkout, cwd_used)
        candidate_sha, changed = self._commit_declared(
            cwd_used, ctx.lane.declared_outputs, sha
        )
        return {"candidate_sha": candidate_sha, "changed": changed}

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
        key = self._role_key(ctx, "code-reviewer")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "code-reviewer", sha=sha, private_tree=False
            )
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        payload, _handle, cwd_used = self._launch(
            ctx,
            "code-reviewer",
            cwd,
            extra,
            prepare_cwd=lambda path: self._refresh_git_checkout(path, sha),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        return self._review_payload(payload)

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
        key = self._role_key(ctx, "integration-reviewer")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "integration-reviewer", sha=sha, private_tree=False
            )
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        payload, _handle, cwd_used = self._launch(
            ctx,
            "integration-reviewer",
            cwd,
            extra,
            prepare_cwd=lambda path: self._refresh_git_checkout(path, sha),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        verdict, findings = self._review_payload(payload)
        return verdict, findings, tuple(payload.get("affected_lanes") or ())

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

    def _session_run_id(self, stored: _RoleSession) -> str:
        if stored.run_id:
            return stored.run_id
        token = str(getattr(stored.handle, "correlation_token", "") or "")
        if ":" in token:
            return token.split(":", 1)[0]
        return ""

    def _handle_space_absent(self, handle: object | None) -> bool:
        if handle is None:
            return True
        cleaned = getattr(self.launcher, "_cleaned_absent", None)
        pane_id = str(getattr(handle, "pane_id", "") or "")
        workspace_id = str(getattr(handle, "workspace_id", "") or "")
        parent = str(getattr(handle, "parent_workspace_id", "") or "")
        child = str(getattr(handle, "child_workspace_id", "") or "")
        if isinstance(cleaned, set):
            if pane_id and pane_id in cleaned:
                return True
            if child and child in cleaned:
                return True
            if workspace_id and workspace_id in cleaned:
                return True
            if parent and parent in cleaned and pane_id in cleaned:
                return True
        poll = getattr(self.launcher, "poll", None)
        if poll is None:
            return False
        try:
            result = poll(handle)
        except BaseException:
            return False
        return getattr(result, "state", None) is lch.PollState.GONE

    def complete_run_spaces(self, run_id: str) -> None:
        sessions = [
            (key, stored)
            for key, stored in list(self._roles.items())
            if self._session_run_id(stored) == run_id
        ]
        handles = [
            stored.handle for _, stored in sessions if stored.handle is not None
        ]
        complete = getattr(self.launcher, "complete_run", None)
        if complete is None:
            if handles:
                raise CleanupRefused("COMPLETE_RUN_UNAVAILABLE")
            return
        refused: BaseException | None = None
        if handles:
            try:
                complete(
                    handles,
                    project_identity=self.project_identity,
                )
            except lch.LaunchRefused as exc:
                refused = exc
            except BaseException as exc:
                refused = exc
        success = refused is None
        for key, stored in sessions:
            if success or self._handle_space_absent(stored.handle):
                self._roles.pop(key, None)
                self._safe_remove_attempt(stored.attempt, stored.checkout)
        if refused is None:
            return
        if isinstance(refused, lch.LaunchRefused):
            raise CleanupRefused(
                "{}:{}".format(refused.refusal.code, refused.detail)
            ) from refused
        raise CleanupRefused(str(refused)) from refused

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
    compiled: st.CompiledPlan,
) -> StageActor:
    executables = layout.get("executables") or {}
    if not isinstance(executables, dict):
        executables = {}
    role_routes = layout.get("role_routes")
    if not isinstance(role_routes, Mapping):
        raise FactoryRefused("ROLE_ROUTES_REQUIRED")
    receipts = layout.get("route_receipts") or {}
    key_paths = layout.get("route_verify_keys") or ()
    if not isinstance(receipts, dict) or not receipts or not key_paths:
        raise FactoryRefused("ROUTE_RECEIPTS_REQUIRED")
    keys = tuple(load_public_key(Path(path)) for path in key_paths)
    admitted = load_admitted_routes(
        {str(name): Path(path) for name, path in receipts.items()},
        verify_keys=keys,
    )
    configured_routes = {binding["route"] for binding in role_routes.values()}
    if not all(admitted.admits(route) for route in configured_routes):
        raise FactoryRefused("ROUTE_RECEIPTS_REQUIRED")
    launcher = lch.HerdrLauncher(
        herdr_path=Path(str(executables.get("herdr") or "herdr")),
        omp_path=Path(str(executables.get("omp") or "omp")),
        claude_path=Path(str(executables.get("claude") or "claude")),
        admitted_routes=admitted,
        provision_argv=layout.get("provision_argv") or (),
        workspace_label=lch.workspace_label_for(_project_identity(target), run_id),
    )
    plan = json.loads(compiled.plan_bytes)
    lane_specs = {
        str(lane["id"]): st.json_ready(lane["spec"]) for lane in plan["lanes"]
    }
    return HerdrStageActor(
        launcher, runtime.path, target, role_routes, lane_specs=lane_specs
    )


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
            binding = create_factory_run(
                store=store,
                run_id=run_id,
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            target = target_from_binding(binding)
            register_installation(
                database=runtime.ledger_path(),
                plans_dir=plan_path.resolve().parent,
                repository=repo,
                state=runtime.path,
            )
            maybe_autoload_dashboard(
                layout,
                repository=repo,
                ledger=runtime.ledger_path(),
            )
            console = fconsole.FactoryConsole()
            console.opened(
                "start",
                run_id,
                target.target_repository_root,
                target.target_main_ref,
                (lane.lane_id for lane in compiled.lanes),
            )
            scheduler = FactoryScheduler(
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id, compiled),
                runtime,
                target,
                stage_started=console.stage_started,
                stage_completed=console.stage_completed,
                compiled=compiled,
            )
            status = scheduler.run()
            console.finished(run_id, status)
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
    st.CompiledPlan,
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
        target = target_from_binding(binding_from_run(row))
        plan_ref = Path(plan_artifact_ref_for(store, run_id, int(row["plan_revision"])))
        compiled = _compile_plan(
            plan_ref,
            revision=int(row["plan_revision"]),
            ref=str(plan_ref),
        )
        if compiled.plan_digest != row["plan_digest"]:
            raise FactoryRefused("PLAN_ARTIFACT_MISMATCH")
        register_installation(
            database=runtime.ledger_path(),
            plans_dir=plan_ref.resolve().parent,
            repository=row["target_repository_root"],
            state=runtime.path,
        )
    except Exception:
        store.close()
        runtime.close()
        raise
    return layout, runtime, store, row, target, compiled


def _run_resume(args: argparse.Namespace) -> int:
    run_id = args.run_id
    layout, runtime, store, row, target, compiled = _bind_existing_run(run_id)
    maybe_autoload_dashboard(
        layout,
        repository=Path(row["target_repository_root"]),
        ledger=runtime.ledger_path(),
    )
    try:
        try:
            console = fconsole.FactoryConsole()
            console.opened(
                "resume",
                run_id,
                row["target_repository_root"],
                row["target_main_ref"],
                (lane.lane_id for lane in store.active_projection(run_id)),
            )
            scheduler = FactoryScheduler(
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id, compiled),
                runtime,
                target,
                stage_started=console.stage_started,
                stage_completed=console.stage_completed,
                compiled=compiled,
            )
            scheduler.resume_waiting()
            status = scheduler.run()
            console.finished(run_id, status)
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
    layout, runtime, store, row, target, _previous = _bind_existing_run(run_id)
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
            console = fconsole.FactoryConsole()
            console.opened(
                "amend",
                run_id,
                row["target_repository_root"],
                row["target_main_ref"],
                (lane.lane_id for lane in store.active_projection(run_id)),
            )
            scheduler = FactoryScheduler(
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id, compiled),
                runtime,
                target,
                stage_started=console.stage_started,
                stage_completed=console.stage_completed,
                compiled=compiled,
            )
            status = scheduler.run()
            console.finished(run_id, status)
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
    layout, runtime, store, _row, target, compiled = _bind_existing_run(run_id)
    try:
        try:
            gitpub.revalidate_binding(target)
            scheduler = FactoryScheduler(
                store,
                run_id,
                _actor_for(runtime, layout, target, run_id, compiled),
                runtime,
                target,
                compiled=compiled,
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
    except CleanupRefused as exc:
        return _RunRefused(exc.code, exc.detail).emit()
    except LaunchFailed as exc:
        return _RunRefused("LAUNCH_FAILED", exc.detail).emit()
    except gitpub.GitPublicationRefused as exc:
        return _RunRefused(exc.code, str(exc)).emit()
    except _MaestroConfigurationError as exc:
        return _RunRefused("RUN_CONFIGURATION_REQUIRED", str(exc)).emit()


if __name__ == "__main__":
    raise SystemExit(main())
