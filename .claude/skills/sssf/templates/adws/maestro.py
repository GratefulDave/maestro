#!/usr/bin/env python3
"""Maestro factory CLI: run start/resume/amend/status."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sqlite3
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

# This must run before third-party and local imports can dirty a deployed checkout.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import yaml

from adw_modules import attend as att
from adw_modules import git_publication as gitpub
from adw_modules import hidden_vault as hv
from adw_modules.handoff_budget import (
    OMP_CONTEXT_WINDOW_TOKENS,
    route_publishes_a_window,
)
from adw_modules import code_review as cr
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import plan_contract_ingress as ingress
from adw_modules import route_admission as admission
from adw_modules import private_review as prv
from adw_modules import provisioning
from adw_modules import review_standards as rvs
from adw_modules import scheduler_types as st
from adw_modules import step_log
from adw_modules import tests_chain as tchain
from adw_modules.lifecycle import (
    ArtifactStore,
    LedgerSchemaUnsupported,
    RunAlreadyExists,
)
from adw_modules.reporting_registry import read_run, registered_run, register_installation
from adw_modules.dashboard_autoload import maybe_autoload_dashboard
from adw_modules.route_receipts import load_admitted_routes, load_public_key
from adw_modules.runtime_state import LEDGER_FILENAME, RuntimeStateRefused, RuntimeStateRoot
from adw_modules.utils import now_iso
from adw_modules.scheduler import (
    FactoryRefused,
    FactoryScheduler,
    LaneContext,
    LaunchFailed,
    OrderedLocks,
    RunRepositoryMismatch,
    StageActor,
    apply_factory_amendment,
    binding_from_run,
    create_factory_run,
    durable_integration_tip,
    plan_artifact_ref_for,
    require_deployment,
    run_row,
    runs_for_target,
    target_from_binding,
)
# Private on purpose: the actor provisions a role tree with the binding this
# run was admitted with, read off the launcher by the same two helpers the
# scheduler uses for its own trees. Re-reading `maestro.config.yaml` here
# would let a role tree be provisioned with a command the run was not
# admitted with.
from adw_modules.scheduler import (  # noqa: F401
    _record_as_lane_artifact,
    _resolved_provision_argv,
    _resolved_provision_timeout,
)
from adw_modules.plan_model import PlanCompileError

_MAESTRO_CONFIG_FILE = Path("adws") / "maestro.config.yaml"
_MAESTRO_SCHEMA = "maestro-config.v1"
_INVOCATION_WORKSPACE: ContextVar[str] = ContextVar("invocation_workspace", default="")


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
_ATTEND_FIELDS = frozenset(
    (
        "max_amendments_per_lane",
        "max_amendments_per_run",
        "route",
        "planctl",
        "validate_argv",
        "reviewer_id",
        "reviewer_vendor",
    )
)



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


def _config_concurrency(value: object, label: str) -> int:
    """Worker threads for independent ready lanes. Absent means 1.

    1 keeps every stage inline on the scheduler's own thread, which is what
    every existing deployment runs today; a deployment opts into concurrent
    author/review/build stages by raising it. Merges are serialized at any
    value.
    """
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _MaestroConfigurationError(label + " must be an integer >= 1")
    return int(value)


def _config_timeout(value: object, label: str) -> float:
    """Seconds allowed for one provisioning run. Absent keeps the default.

    A deployment whose `provision_argv` installs several manifests on a cold
    cache legitimately needs longer than the default, and a timeout there is
    reported as a provisioning failure -- which would read as a broken command
    rather than a slow one.
    """
    if value is None:
        return lch.PROVISION_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _MaestroConfigurationError(label + " must be a positive number")
    seconds = float(value)
    if seconds <= 0:
        raise _MaestroConfigurationError(label + " must be a positive number")
    return seconds


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
        canonical[role] = _canonical_role_route(
            value[role], "role_routes.{}".format(role)
        )
    return MappingProxyType(canonical)


def _canonical_role_route(value: object, label: str) -> Mapping[str, str]:
    """One route binding. Absent is an empty mapping, which binds nothing.

    Factored out of `_canonical_role_routes` so `attend.route` is validated by
    the same rules as the five lane roles rather than by a second copy that
    could drift -- an attend route that names a model on an omp profile has to
    refuse here, not at dispatch in front of an operator waiting on a run.
    """
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise _MaestroConfigurationError("{} must be a mapping".format(label))
    extras = frozenset(value) - _ROLE_ROUTE_FIELDS
    if extras:
        raise _MaestroConfigurationError(
            "{} has unsupported fields: {}".format(label, ", ".join(sorted(extras)))
        )
    route = _config_string(value.get("route"), "{}.route".format(label))
    model = _optional_config_string(value.get("model"), "{}.model".format(label))
    effort = _optional_config_string(value.get("effort"), "{}.effort".format(label))
    profile = _optional_config_string(value.get("profile"), "{}.profile".format(label))
    if route == "omp":
        if not profile or model or effort:
            raise _MaestroConfigurationError(
                "{} must use only an omp profile".format(label)
            )
    elif route == "claude":
        if not model or not effort or profile:
            raise _MaestroConfigurationError(
                "{} must use Claude model and effort only".format(label)
            )
    else:
        raise _MaestroConfigurationError(
            "{}.route must be omp or claude".format(label)
        )
    return MappingProxyType(
        {"route": route, "model": model, "effort": effort, "profile": profile}
    )

def _config_bound(value: object, label: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MaestroConfigurationError(label + " must be an integer >= 0")
    return int(value)


def _canonical_attend(value: object) -> Mapping[str, Any]:
    """`attend.*` off a loaded config. Absent means the verb is disabled.

    `max_amendments_per_lane` defaults to 0, and 0 is what `run attend` refuses
    on. That default is the whole opt-in: an upgraded deployment keeps parking
    for its operator until someone sets the key in *that* deployment's config,
    the same way `concurrency` stays 1 until someone raises it. The route is
    validated here rather than in `attend.py`, which must not name a route.
    """
    if value is None:
        return MappingProxyType(
            {
                "max_amendments_per_lane": 0,
                "max_amendments_per_run": 10,
                "route": MappingProxyType({}),
                "planctl": None,
                "validate_argv": (),
                "reviewer_id": "maestro-attend",
                "reviewer_vendor": "maestro",
            }
        )
    if not isinstance(value, Mapping):
        raise _MaestroConfigurationError("attend must be a mapping")
    extras = frozenset(value) - _ATTEND_FIELDS
    if extras:
        raise _MaestroConfigurationError(
            "attend has unsupported fields: " + ", ".join(sorted(extras))
        )
    per_lane = _config_bound(
        value.get("max_amendments_per_lane"), "attend.max_amendments_per_lane", 0
    )
    per_run = _config_bound(
        value.get("max_amendments_per_run"), "attend.max_amendments_per_run", 10
    )
    route = _canonical_role_route(value.get("route"), "attend.route")
    if per_lane > 0 and not route:
        raise _MaestroConfigurationError(
            "attend.route is required once attend.max_amendments_per_lane is set"
        )
    planctl = _optional_config_string(value.get("planctl"), "attend.planctl")
    if planctl and not Path(planctl).is_absolute():
        raise _MaestroConfigurationError("attend.planctl must be absolute")
    return MappingProxyType(
        {
            "max_amendments_per_lane": per_lane,
            "max_amendments_per_run": per_run,
            "route": route,
            "planctl": Path(planctl) if planctl else None,
            "validate_argv": _config_argv(
                value.get("validate_argv"), "attend.validate_argv"
            )
            if value.get("validate_argv") is not None
            else (),
            "reviewer_id": _optional_config_string(
                value.get("reviewer_id"), "attend.reviewer_id"
            )
            or "maestro-attend",
            "reviewer_vendor": _optional_config_string(
                value.get("reviewer_vendor"), "attend.reviewer_vendor"
            )
            or "maestro",
        }
    )


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
    loaded["provision_timeout_s"] = _config_timeout(
        loaded.get("provision_timeout_s"), "provision_timeout_s"
    )
    loaded["dashboard"] = _canonical_dashboard(loaded.get("dashboard"))
    loaded["attend"] = _canonical_attend(loaded.get("attend"))
    loaded["concurrency"] = _config_concurrency(
        loaded.get("concurrency"), "concurrency"
    )
    # One key, read where the tool reads it, so a deployment cannot be told two
    # different things about when a tests lane stops.
    try:
        loaded["stall_regression_on_findings"] = st.stall_regression_on_findings(
            loaded
        )
    except ValueError as exc:
        raise _MaestroConfigurationError(str(exc)) from exc
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


#: Seconds a superseded role pane gets to be proven quiescent and closed.
#: Matches the launcher's own post-launch cancel budget; the lane never waits
#: on this, because a refusal here is reported rather than raised.
_SUPERSEDED_CLOSE_SECONDS = 5.0


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


def _resolved_under(root: Path, relative: str) -> Path:
    """A repository-relative path resolved inside `root`, or a refusal.

    The sealed paths written into an operator tree come out of the vault, and
    a `..` component in one of them would write outside the tree the role
    contract confines this agent to.
    """
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise FactoryRefused("OPERATOR_TREE_PATH_ESCAPE:{}".format(relative)) from exc
    return candidate


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


_GENERATED_ROLE_DIRS = frozenset(
    ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
)
_GENERATED_ROLE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _generated_role_output(relative: str) -> bool:
    parts = Path(relative.replace("\\", "/")).parts
    if any(part in _GENERATED_ROLE_DIRS for part in parts):
        return True
    name = parts[-1]
    return (
        name.endswith(_GENERATED_ROLE_SUFFIXES)
        or name == ".coverage"
        or name.startswith(".coverage.")
    )


def _role_agent_scratch(relative: str) -> bool:
    """Pane scratch belongs to the harness, not to any role's output."""
    parts = Path(relative.replace("\\", "/")).parts
    return bool(parts) and parts[0] == lch.ROLE_AGENT_DIR


def _open_regular_under(root: Path, path: Path) -> tuple[str, int]:
    """Open a regular file under `root` without ever following a symlink.

    Returns `(relative, fd)`; the caller owns the descriptor. Refuses a path
    that escapes `root`, that traverses or ends in a symlink, or whose final
    component is not a regular file. `O_NONBLOCK` keeps a FIFO from blocking
    the open, so a non-regular path refuses instead of hanging collection.
    """
    relative = _relative_under(root, path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow | nonblock
    file_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | nonblock
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
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FactoryRefused("ROLE_OUTPUT_UNSAFE:{0}".format(relative))
    except BaseException:
        os.close(fd)
        raise
    return relative, fd


def _drain_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _role_output_disposition(root: Path, path: Path) -> tuple[str, bytes | None]:
    """The one generated-output policy every role-output path applies.

    Containment, no-follow and regular-file are proven *before* a path may be
    ignored, so a generated-looking symlink or non-regular path still refuses.
    Returns `(relative, None)` for known generated output every role drops, or
    `(relative, bytes)` for output the role keeps.
    """
    relative, fd = _open_regular_under(root, path)
    try:
        if _generated_role_output(relative):
            return relative, None
        return relative, _drain_fd(fd)
    finally:
        os.close(fd)


def _read_regular_bytes_under(root: Path, path: Path) -> tuple[str, bytes]:
    """Read a regular file under `root`. Never follow symlinks."""
    relative, fd = _open_regular_under(root, path)
    try:
        return relative, _drain_fd(fd)
    finally:
        os.close(fd)


def _read_regular_text_under(root: Path, path: Path) -> str:
    return _read_regular_bytes_under(root, path)[1].decode("utf-8")


TEST_CRAFT_ANTIPATTERNS = (
    "## Test-craft anti-patterns\n"
    "Three shapes make a test worthless. Do not write one, and say so if the "
    "acceptance you were given asks for one.\n"
    "- Implementation-coupled: mocks internal collaborators, tests private "
    "methods, or verifies through a side channel. Tell: the test breaks on a "
    "refactor with no behaviour change.\n"
    "- Tautological: the assertion recomputes the expected value the same way "
    "the code does, so it passes by construction. Expected values must come "
    "from an independent source: a known-good literal, a worked example, the "
    "spec. Tell: assert add(2, 3) == 2 + 3.\n"
    "- Horizontal slicing / shape-asserting: all tests first then all "
    "implementation, so tests verify an imagined shape rather than behaviour. "
    "Tell: assertions on structure (keys exist, type is list) with no "
    "behavioural expectation.\n"
    "Loop rules: red before green; one seam, one test per cycle; refactoring "
    "is not part of the loop.\n"
)

TEST_CRAFT_REVIEWER_QUESTION = (
    "## Test-craft questions\n"
    "Answer all three, every turn. Name any case that is "
    "implementation-coupled (mocks internal collaborators, tests private "
    "methods, or verifies through a side channel; the tell is a test that "
    "breaks on a refactor with no behaviour change), with the case id. Name "
    "any case that is tautological (the assertion recomputes the expected "
    "value the same way the code does, so it passes by construction; expected "
    "values must come from an independent source: a known-good literal, a "
    "worked example, the spec), with the case id. Name any case that asserts "
    "shape rather than behaviour (assertions on structure such as keys exist "
    "or type is list, with no behavioural expectation), with the case id. A "
    "named case is a located finding that discharges nothing, so the verdict "
    "is REVISE.\n"
)


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


#: Where a test double may stand, and where it may not. Appended to both tester
#: rules, because a `tests` lane and a hidden-validator lane draw the same seam.
#:
#: Authoring guidance, not a verdict axis: nothing reads this back and no
#: transition keys on it. What it buys is a way for the tester to *report* an
#: unreachable subject in its envelope instead of asserting through another
#: lane's fixture and failing for a reason that is not about the product.
#: `lane-wp7-gw-issue-build` spent three attempts and parked with no candidate
#: on exactly that shape: its acceptance reached `/v1/faers/dpa` through a
#: `SourceHandler` stand-in owned by a different lane, which routes a fixed
#: path list and 404s everything else. The lane could neither fix the stand-in
#: nor pass without it, and had no vocabulary for saying so.
TEST_DOUBLE_BOUNDARY = (
    "## Where a test double belongs\n"
    "Substitute only at a boundary this lane does not own and cannot run: a "
    "third-party service, another service across a network, the clock, "
    "randomness. Never substitute a collaborator that lives inside this lane's "
    "declared outputs -- exercise the real one through its interface. A double "
    "there asserts the shape you imagined rather than the behaviour that was "
    "built, and it keeps passing after the behaviour breaks.\n"
    "A double answers one operation with one shape. A single dispatcher that "
    "routes a whitelist of paths and fails everything else is not a double, it "
    "is a second implementation: the next case added falls off the whitelist "
    "and fails for a reason that has nothing to do with the product. Prefer one "
    "named stand-in per operation over one conditional stand-in for all of "
    "them.\n"
    "If a case can only reach its subject through a file this lane does not "
    "own, the seam is in the wrong place and no test written here will fix it. "
    "Say that in the envelope, naming the file and the case. Reporting an "
    "unreachable subject is a complete answer; asserting through it is not.\n"
)


class HerdrStageActor:
    """Persistent role panes. Envelope bytes are payload, not stage."""

    def __init__(
        self,
        launcher: lch.LauncherAdapter,
        state_root: Path,
        target: gitpub.TargetBinding,
        role_routes: Mapping[str, Mapping[str, str]],
        lane_specs: Mapping[str, Mapping[str, Any]] | None = None,
        operator_route: Mapping[str, str] | None = None,
    ) -> None:
        self.launcher = launcher
        self.state_root = Path(state_root)
        self.worktrees = self.state_root / "worktrees"
        self.target = target
        # The operator route is bound beside the five lane routes rather
        # than inside them: `role_routes` must name exactly the lane roles, and
        # a deployment that never opted into `run attend` binds no operator at
        # all. A dispatch without one is a KeyError at the launch, which is
        # where the missing configuration actually bites.
        routes = dict(_canonical_role_routes(role_routes))
        if operator_route:
            routes["operator"] = MappingProxyType(dict(operator_route))
        self.role_routes = MappingProxyType(routes)
        self.lane_specs = MappingProxyType(
            {
                str(lane_id): MappingProxyType(dict(spec))
                for lane_id, spec in (lane_specs or {}).items()
            }
        )
        self.project_identity = _project_identity(target)
        self._roles: dict[tuple[str, str, str], _RoleSession] = {}

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

    def _add_worktree(
        self, dest: Path, sha: str, *, repo: Path | None = None,
        no_checkout: bool = False,
    ) -> None:
        dest = Path(dest)
        precreated = _clear_precreated_role_cwd(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        repo = repo or Path(self.target.target_repository_root)
        flags = ["--no-checkout"] if no_checkout else []
        subprocess.check_call(
            ["git", "-C", str(repo), "worktree", "add", "--detach", *flags, str(dest), sha],
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
        # One object inside a list, with each key carrying a placeholder --
        # never the bare key names. `list(REVISE_FINDING_KEYS)` renders as
        # `["implementation_area", "observed_behavior", ...]`, which reads as
        # "findings is an array of four strings", and a reviewer that reads it
        # that way fills the array positionally. On run 7a80027d the
        # lane-wp1-sections-tests reviewer did exactly that: one correct,
        # complete finding flattened into four string elements in key order.
        # `require_revise_findings` then refused it deep inside
        # `tests_chain.review_test_draft`, and the CanonicalIdentityError left
        # the scheduler and ended the run, taking four healthy in-flight lanes
        # with it. The reviewer had done the work; only the shape was wrong.
        findings = [{key: "<{0}>".format(key) for key in st.REVISE_FINDING_KEYS}]
        if role == "tester":
            return {"private_files": {"<path>": "<utf-8 contents>"}}
        if role == "builder":
            return {"candidate_sha": "<optional git sha>", "changed": "<optional bool>"}
        if role == "code-reviewer":
            return {
                "verdict": "PASS|REVISE",
                "findings": findings + list(st.FINDING_OPTIONAL_KEYS),
            }
        if role == "test-reviewer":
            return {"verdict": "PASS|REVISE", "findings": findings}
        if role == "operator":
            return {
                "revision_path": "<revision_out_path, exactly as given>",
                "rationale": {key: "<{0}>".format(key) for key in att.RATIONALE_KEYS},
            }
        return {
            "verdict": "PASS|REVISE",
            "findings": findings,
            "affected_lanes": ["<lane-id>"],
        }

    #: The one measurement a tester owes before it returns an envelope, and
    #: the anti-measurement it keeps reaching for instead.
    #:
    #: Measured, FDAdb run `a2ea7355699c4dff93bc82ac89415475`,
    #: `lane-wp8r-route-tests`, 2026-09-05. The tester submitted a draft whose
    #: module deadlocks vitest at load; collection enumerated nothing for the
    #: full 120s budget twice and ended the run. What the tester had actually
    #: done before submitting, from its own transcript: a grep-based self-check
    #: reporting `it count 8 / describe 1`, and one bash call that returned no
    #: output in 0.12 seconds. It had run a real listing earlier in the lane and
    #: stopped. Running the real listing by hand answers in 7.6s.
    #:
    #: A static scan is not a weaker version of that measurement, it is a
    #: different question with a different answer: `it.each` expands to many
    #: cases, a `describe` body never invoked registers none, and a module that
    #: deadlocks at import greps identically to one that loads. Both tester
    #: rules carry this because both drafts go through the same preflight
    #: (`scheduler._collect_private_draft`), whatever the lane kind.
    _PROHIBITION_RULE = (
        "When an obligation forbids a behaviour -- 'never orders among', "
        "'must not coalesce', 'is absent from' -- it names a FAMILY of wrong "
        "implementations, not one. Cover the family: if the plan names one "
        "member, enumerate the rest yourself and assert each is rejected. And "
        "assert the boundary from the other side too, with a positive control "
        "-- a legal near-miss the implementation is allowed to have, which "
        "your case must keep passing. A case that rejects one member is "
        "vacuous against its siblings; a case with no positive control "
        "rejects correct implementations as readily as wrong ones, and you "
        "will be sent back for the opposite defect. Measured on FDAdb "
        "lane-wp4-recalls-tests: 'never orders among Class I, II and III' was "
        "discharged by forbidding an ascending severity sort, and a "
        "descending severity sort passed the whole case; the round before, "
        "the same case had rejected a legitimate identifier ordering."
    )

    _PROHIBITION_REVIEW_RULE = (
        "For every obligation that forbids a behaviour, probe the whole "
        "family, not the member the plan happened to name: if the case "
        "rejects one direction, orientation, or ordering of the prohibited "
        "behaviour, try the others before passing it. Check the other side "
        "too -- a case with no positive control rejects legal "
        "implementations, and that is a REVISE of its own, not a strength."
    )

    _TESTER_COLLECT_RULE = (
        "Before you return the envelope, enumerate your own draft with the "
        "gate's own runner and see the listing. vitest: "
        "`<vitest binary> list --run [--config <gate config>] <your files>`; "
        "pytest: `<pytest binary> --collect-only -q -o addopts= <your files>`. "
        "The harness runs exactly that and will not accept a draft it cannot "
        "enumerate. A listing that appears and then does not exit is fine -- "
        "some configs hold the runner open, and the harness reads what was "
        "printed; kill it once you have seen your case ids. Nothing printed at "
        "all means no module finished loading, which is your defect to fix, "
        "not the harness's. Counting `it(`, `test(`, or `def test_` with grep, "
        "or any other static scan of the file, is NOT this measurement and "
        "does not discharge it: `it.each` expands, a `describe` body that is "
        "never invoked registers nothing, and a module that deadlocks at "
        "import greps exactly like one that loads."
    )

    _PUBLIC_INTERFACE_RULE = (
        "Use the public lane spec, public_acceptance and public_contract as "
        "the authority for module/import paths, export or callable names, "
        "argument and return shapes, and observable errors. A module path "
        "alone does not declare a callable. Tests must consume that public "
        "interface, not invent an unstated binding or require an alias chosen "
        "only inside a private test. Test reviewers must report such a binding "
        "or an ambiguous public interface through existing actionable REVISE "
        "findings about contract adequacy, not prescribe product implementation "
        "or pass a guessed binding. Builders implement the public interface; "
        "do not guess unspecified exports from sealed-test observations or "
        "spray aliases to satisfy an unknown name."
    )

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
                "is fixed by resubmitting byte-identical files.\n"
                + self._TESTER_COLLECT_RULE
                + "\n"
                + self._PROHIBITION_RULE
            )
        else:
            tester_rule = (
                "Inspect the product checkout without modifying product files. "
                "Return private test files only through the requested envelope. "
                "Private paths must not collide with declared product outputs. "
                "Write hidden validator/meta-test files that exercise builder "
                "outputs; never replace those outputs. On correction turns, "
                "apply revise_findings to hidden validators; do not claim a "
                "finding is fixed by resubmitting byte-identical hidden files.\n"
                + self._TESTER_COLLECT_RULE
                + "\n"
                + self._PROHIBITION_RULE
            )
        tester_rule = (
            tester_rule
            + "\n\n"
            + TEST_DOUBLE_BOUNDARY
            + "\n\n"
            + TEST_CRAFT_ANTIPATTERNS
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
                "requires red-at-base.\n"
                + self._PROHIBITION_REVIEW_RULE
                + "\n"
                + TEST_CRAFT_REVIEWER_QUESTION
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
                "must not be inferred, requested, or cited.\n"
                "For every declared output the candidate changed, enumerate its "
                "callers inside this checkout before deciding: "
                "`codemap impact <file> --direction reverse`, then "
                "`grep -rl \"<module specifier>\" <source root>` with no "
                "`--include` filter, because codemap parses only some "
                "extensions and an unparsed importer is indistinguishable from "
                "no importer. Read how each caller actually invokes the changed "
                "code, and check the candidate against that call, not only "
                "against the call the lane's own tests make. A caller the "
                "candidate breaks is a finding even though the caller is not a "
                "declared output: the defect is in the declared output and so "
                "is its repair, which is what keeps the finding actionable.\n\n"
                + rvs.standards_section(
                    rvs.discover_standards_files(self.target.target_repository_root)
                )
            ),
            "operator": (
                "## Operator obligations\n"
                "You are authoring a plan revision, not implementing a lane. "
                "A lane has parked because its reviewed rounds stopped moving "
                "against the contract it was given; your job is to name the "
                "gap between what the sealed suite asserts and what the "
                "contract says, and to close it with the smallest edit that "
                "reaches this lane's projection -- normally one seam contract "
                "on the lane's own carrier.\n"
                "You may read the sealed acceptance suite. It is in this "
                "checkout under `sealed/`, and reading it is the privilege "
                "this role exists to exercise: the builder and every reviewer "
                "still cannot, and nothing you write may hand it to them as "
                "test source. Stating an expectation the suite asserts, as a "
                "contract clause in your own words, is exactly what you are "
                "here to do; pasting the test file into the contract is not.\n"
                "Write the revision IR at the path the per-turn JSON names, "
                "and nowhere else. Do not edit the current IR in place. Do not "
                "run any `maestro` verb, any git command, or `planctl` -- the "
                "harness validates, mints the receipt, projects, and applies. "
                "Do not add, remove, or rewire a lane: an edit that reaches "
                "any lane outside allowed_lane_ids is refused whole, and the "
                "lane you were called for gets nothing.\n"
                "Return the rationale in the envelope. `contract_gap` must "
                "name the clause that was missing or wrong, not restate that "
                "the lane failed."
            ),
            "integration-reviewer": (
                "Review the exact integration checkout read-only. Return a "
                "verdict, findings, and only genuinely affected lane IDs. "
                "Each merged build lane's accepted test suite is in this "
                "checkout beside the code it judged; read the two together. "
                "The suite of a tests lane no build lane consumes is absent "
                "by design, and its absence is never a finding: the harness "
                "measures every run-level sealed gate itself, against this "
                "same integration SHA with that suite overlaid, before you "
                "are asked, and a gate that failed reaches you as a REVISE "
                "you are not consulted about. Do not run or re-run a "
                "declared gate command. Judge the code in this checkout "
                "against the lane contracts.\n"
                "You are the only reader who sees every lane's callers at "
                "once, so enumerate them: for each file the merged surface "
                "changed, run `codemap impact <file> --direction reverse` and "
                "then `grep -rl \"<module specifier>\" <source root>` with no "
                "`--include` filter -- codemap parses only some extensions, "
                "and an unparsed importer looks exactly like no importer. A "
                "caller no lane declared is the one thing no lane-level "
                "reviewer could have checked. Read how it invokes the changed "
                "code and say whether the merged surface still satisfies it."
            ),
        }
        try:
            role_rule = role_rules[role]
        except KeyError as exc:
            raise FactoryRefused("UNKNOWN_ROLE:{}".format(role)) from exc
        if role in ("tester", "test-reviewer", "builder"):
            role_rule += "\n" + self._PUBLIC_INTERFACE_RULE
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
            "- Write only what this role permits, then write the requested UTF-8 JSON envelope to its `.part` sibling and rename it into place, and stop.\n\n"
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

    @staticmethod
    def _sealed_counts_red(counts: Mapping[str, Any]) -> bool:
        """Whether the measured sealed suite disagrees with the candidate.

        Mirrors `code_review`'s own `runner_failed` on the public counts alone:
        anything failed, anything errored, or nothing executed. `min_cases` is
        not visible here, so zero-executed stands in for the under-count case.
        """
        try:
            failed = int(counts.get("failed", 0) or 0)
            errored = int(counts.get("errored", 0) or 0)
            executed = int(counts.get("executed", 0) or 0)
        except (TypeError, ValueError):
            return False
        return bool(failed or errored or executed == 0)

    @staticmethod
    def _failure_instruction(lines: Sequence[str]) -> str:
        """Name the failures the builder is otherwise left to guess at.

        These are the runner's own lines with every private token already
        redacted upstream. They say which symbol or shape is wrong; they do not
        say what any test expects.
        """
        shown = "\n".join("  {0}".format(line) for line in lines)
        return (
            " The sealed suite reported these failures against your last "
            "candidate, verbatim from the runner with private values "
            "redacted:\n{0}\nFix the causes named here. They are the actual "
            "errors, not a summary of them. A [redacted] marker had a "
            "private value removed -- treat the surrounding text as the "
            "signal. Do not guess at failures that are not listed.".format(shown)
        )

    @staticmethod
    def _bound_surface_instruction(surface: Mapping[str, Any]) -> str:
        """Render observed names without making them a second public contract."""
        modules = []
        for entry in surface.get("modules") or ():
            if not isinstance(entry, Mapping):
                continue
            specifier = str(entry.get("specifier") or "")
            if not specifier:
                continue
            symbols = [str(name) for name in entry.get("symbols") or () if str(name)]
            modules.append(
                "{0} exports {1}".format(specifier, ", ".join(symbols))
                if symbols
                else specifier
            )
        keys = [str(key) for key in surface.get("object_keys") or () if str(key)]
        if not modules and not keys:
            return ""
        text = (
            " bound_surface records names observed in the sealed suite; it "
            "does not define or override the public interface. Implement the "
            "names declared by the public lane spec and public_contract. "
            "An empty symbols list declares no callable, and an observed name "
            "cannot fill a missing public signature. Do not invent exports "
            "or aliases from this observation."
        )
        if modules:
            text += " Modules and the symbols imported from them: {0}.".format(
                "; ".join(modules)
            )
        if keys:
            text += " Keys the assertions read off returned objects: {0}.".format(
                ", ".join(keys)
            )
        text += (
            " The VALUES behind those names -- expected strings, numbers, "
            "fixture data, and the specific results the assertions compare "
            "against -- are deliberately withheld and cannot be recovered from "
            "this list. Do not guess at one and do not hardcode one. Derive the "
            "behavior from public_contract and implement it; a value that "
            "happens to satisfy a case you imagined is not the contract."
        )
        return text

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
            "Do not delegate. Stay inside CWD. Create UTF-8 JSON at {0}.part, "
            "then rename it to {0}, then stop. The rename declares the turn "
            "finished; never write into {0} directly, because a reader polling "
            "it reads a partial file as a failed turn. Schema: {1}. CWD: {2}."
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
            surface = extra.get("bound_surface")
            if isinstance(surface, Mapping):
                instructions += self._bound_surface_instruction(surface)
            failures = extra.get("redacted_failures")
            if isinstance(failures, Sequence) and not isinstance(failures, str):
                lines = [str(item) for item in failures if str(item).strip()]
                if lines:
                    instructions += self._failure_instruction(lines)
        elif role == "code-reviewer":
            instructions += (
                " Inspect this candidate product checkout. Findings must be "
                "resolvable within declared_outputs. Private tests are absent."
            )
            counts = extra.get("sealed_result_summary")
            if isinstance(counts, Mapping) and self._sealed_counts_red(counts):
                instructions += (
                    " sealed_result_summary is the already-measured result of "
                    "the sealed acceptance suite against THIS candidate: "
                    "{0} executed, {1} passed, {2} failed, {3} errored. The "
                    "suite is red, so the correct verdict is REVISE and PASS "
                    "will be overridden. You cannot see the tests. Your job is "
                    "to read the candidate against public_contract and "
                    "declared_outputs and say WHICH code is wrong and HOW to "
                    "fix it. Every finding must name a file in "
                    "declared_outputs and the specific behavior that is wrong "
                    "-- a missing branch, an unhandled input, a contract clause "
                    "the code does not satisfy. Do not restate that tests "
                    "failed; the builder already knows that and cannot act on "
                    "it. Do not guess at test names or assertion text."
                ).format(
                    counts.get("executed", 0),
                    counts.get("passed", 0),
                    counts.get("failed", 0),
                    counts.get("errored", 0),
                )
            if extra.get("sealed_findings_required"):
                instructions += (
                    " Your previous answer for this candidate carried no "
                    "actionable finding while the sealed suite was red. That "
                    "left the builder with nothing to change. Return REVISE "
                    "with at least one finding naming a file in "
                    "declared_outputs and the concrete change it needs."
                )
        elif role == "integration-reviewer":
            instructions += " Inspect this exact integration SHA. Return verdict, findings, affected_lanes."
        elif role == "operator":
            instructions += (
                " Read current_ir_path, the sealed suite under sealed/, the "
                "lane gate table, and the reviews. Write the revised IR at "
                "revision_out_path and return that exact path plus the "
                "rationale. Change only lanes in allowed_lane_ids. Run no "
                "maestro verb, no git command, and no planctl."
            )
        if role in ("tester", "test-reviewer", "builder"):
            instructions += " " + self._PUBLIC_INTERFACE_RULE
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

    def _deleted_tracked(self, checkout: Path, *pathspec: str) -> frozenset[str]:
        listed = self._git(
            checkout, "ls-files", "-z", "--deleted", "--", *pathspec, check=False
        )
        return frozenset(item for item in listed.split("\0") if item)

    def _collect_uncommitted(
        self, checkout: Path, outputs: Sequence[str] = ()
    ) -> dict[str, str]:
        """Collect role output from the working tree.

        Scoped to `outputs` when the caller passes them, the same way
        `_commit_declared` scopes the builder's pathspec. Only a tests lane
        passes a scope: its private files must equal its declared outputs, so
        an unscoped sweep read a toolchain byproduct the role never declared
        -- a package manager's lockfile, a stray `__pycache__` entry -- as
        role output and refused TYPED_TEST_OUTPUTS for a file no role wrote.
        A build lane declares product paths its tester does not write to, and
        the unscoped sweep is how that lane's private tests are delivered, so
        it passes no scope and keeps the whole-tree behaviour.
        """
        requested = tuple(str(item) for item in outputs)
        pathspec = ("--",) + requested if requested else ()
        listed = self._git(
            checkout,
            "ls-files",
            "-o",
            "-m",
            "--exclude-standard",
            *pathspec,
            check=False,
        )
        deleted = self._deleted_tracked(checkout, *requested)
        files: dict[str, str] = {}
        for rel in listed.splitlines():
            rel = rel.strip()
            if not rel or _role_agent_scratch(rel):
                continue
            if rel in deleted:
                # A deletion is real role output that a path->content draft
                # cannot carry. Refuse rather than lose it silently.
                raise FactoryRefused(f"ROLE_OUTPUT_DELETED:{rel}")
            try:
                relative, payload = _role_output_disposition(checkout, checkout / rel)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise FactoryRefused(f"ROLE_OUTPUT_UNSAFE:{rel}") from exc
            if payload is None:
                continue
            try:
                content = payload.decode("utf-8")
            except UnicodeError as exc:
                raise FactoryRefused(f"ROLE_OUTPUT_UNSAFE:{rel}") from exc
            files[relative] = content
        return files


    def _safe_role_pathspec(
        self,
        checkout: Path,
        listed: Sequence[str],
        deleted: frozenset[str],
    ) -> tuple[str, ...]:
        """Apply the shared role-output policy to a builder pathspec.

        A deleted tracked path stays in the pathspec so `git add -A` records
        the deletion. Known generated output is dropped only after the same
        containment, no-follow and regular-file proof the tester collection
        path makes, so a generated-looking symlink, a FIFO, a directory or an
        escaping path refuses before any candidate commit exists.
        """
        kept: list[str] = []
        for rel in listed:
            if not rel or _role_agent_scratch(rel):
                continue
            if rel in deleted:
                kept.append(rel)
                continue
            try:
                _relative, payload = _role_output_disposition(checkout, checkout / rel)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise FactoryRefused(f"ROLE_OUTPUT_UNSAFE:{rel}") from exc
            if payload is None:
                continue
            kept.append(rel)
        return tuple(kept)

    @staticmethod
    def _exclude_pathspec(paths: Sequence[str]) -> tuple[str, ...]:
        """`paths` as pathspec terms that subtract, matched literally.

        `literal` because a sealed path is a git tree path and may hold a
        glob character; without it `tests/[id].py` would subtract nothing.
        """
        return tuple(":(exclude,literal){0}".format(str(path)) for path in paths)

    def _strip_paths(self, checkout: Path, paths: Sequence[str]) -> None:
        """Remove `paths` from a working tree without recording a deletion.

        The index is left alone, so nothing here can reach a candidate: the
        removal is invisible to `git diff --cached base`, and callers subtract
        the same paths from the commit pathspec so `git add -A` cannot stage
        it either. `git reset --hard` puts every one of them back, which is
        why this runs after each materialization rather than once.

        `os.unlink` never follows a symlink, so a link planted at a sealed
        path is removed rather than dereferenced. Emptied parents go too:
        leaving `tests/acceptance/` behind names the suite's location, which
        the builder is not told either.
        """
        root = checkout.resolve()
        for raw in paths:
            relative = PurePosixPath(str(raw))
            if relative.is_absolute() or ".." in relative.parts:
                raise FactoryRefused("SEALED_STRIP_PATH_UNSAFE:{0}".format(raw))
            target = checkout / Path(*relative.parts)
            try:
                os.unlink(target)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                # Anything else -- a directory planted at a sealed path, an
                # unwritable parent -- fails closed. A builder launched over
                # its own suite is worse than a refused lane.
                raise FactoryRefused("SEALED_STRIP_REFUSED:{0}".format(raw)) from exc
            parent = target.parent
            while parent != checkout and root in parent.resolve().parents:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _commit_declared(
        self,
        checkout: Path,
        outputs: Sequence[str],
        base: str,
        *,
        strip: Sequence[str] = (),
    ) -> tuple[str, bool]:
        requested = tuple(str(item) for item in outputs)
        excluded = self._exclude_pathspec(strip)
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
                *excluded,
            )
            pathspec = self._safe_role_pathspec(
                checkout,
                tuple(item for item in listed.split("\0") if item),
                self._deleted_tracked(checkout, *requested, *excluded),
            )
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
            self._strip_paths(checkout, strip)
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
        candidate = self._git(checkout, "rev-parse", "HEAD")
        # `reset --hard` above put the suite back and the commit carries it.
        # Take it out again before returning: the builder's session lives in
        # this directory between turns.
        self._strip_paths(checkout, strip)
        return candidate, True

    def _clean_checkout(self, checkout: Path) -> None:
        self._git(checkout, "clean", "-fdx", "-e", lch.ROLE_AGENT_DIR)

    def _refresh_git_checkout(self, checkout: Path, sha: str) -> None:
        if not (checkout / ".git").exists():
            raise FactoryRefused("ROLE_CHECKOUT_MISSING")
        self._git(checkout, "reset", "--hard", sha)
        self._clean_checkout(checkout)

    def _refresh_builder_checkout(
        self,
        checkout: Path,
        outputs: Sequence[str],
        base: str,
        *,
        strip: Sequence[str] = (),
    ) -> None:
        """Put the builder's checkout at `base`, minus its own sealed suite.

        `strip` is the lane's own acceptance suite. Its base may legitimately
        carry it: a build lane's merge releases the predecessor suite into the
        integration ref, so after an amendment the lane restarts at an
        integration head holding the very tests it is graded against. The
        merge cannot un-release it, and a builder that can read its
        acceptance tests writes to the assertions instead of the requirement
        -- this repository's `sealed_probe` recovered thirteen hidden literals
        from one bit of feedback per query and shipped a candidate that looked
        clean. The removal stays out of the index, so the candidate delta
        never names the suite and `validate_declared_ownership` sees the same
        delta it always did.

        A stripped path is subtracted from the dirtiness measurement too:
        absence here is the guard doing its job, not the builder's work.
        """
        if not (checkout / ".git").exists():
            raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
        head = self._git(checkout, "rev-parse", "HEAD")
        parent = self._git(checkout, "rev-parse", "HEAD^", check=False)
        scope = ("--", ".") + self._exclude_pathspec(strip) if strip else ()
        dirty = bool(self._git(checkout, "status", "--porcelain", *scope))
        if not dirty and (head == base or parent == base):
            self._clean_checkout(checkout)
            self._strip_paths(checkout, strip)
            return
        self._commit_declared(checkout, outputs, base, strip=strip)


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

    def _role_key(self, ctx: LaneContext, role: str) -> tuple[str, str, str]:
        """Identity of one role's long-lived agent session.

        The lane's spec digest is part of the identity because a plan revision
        rewrites the lane's contract while the superseded text stays in the
        bound agent's context window. On run f50638ab a code reviewer went from
        turn 46 to turn 51 across an amendment and kept quoting the acceptance
        sentence the amendment had already corrected: the document was fixed
        and the reader was not.

        The digest is the key rather than the `amend` verb, so a fresh session
        falls out of every path that changes a spec instead of out of the one
        path somebody remembered to special-case. `_role_dir` is deliberately
        not keyed the same way -- the checkout is bound by path, and its
        candidate commits have to survive the session that made them.
        """
        return (ctx.lane.lane_id, role, ctx.lane.spec_digest)

    def _role_dir(self, ctx: LaneContext, role: str, *, create: bool = True) -> Path:
        path = self.worktrees / ctx.run_id / ctx.lane.lane_id / role
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _lane_child_anchor(self, ctx: LaneContext, cwd: Path) -> Path | None:
        return self._child_anchor(ctx.run_id, ctx.lane.lane_id, cwd)

    def _child_anchor(self, run_id: str, lane_id: str, cwd: Path) -> Path | None:
        invoking = self.launcher.invoking_repository(os.environ)
        if invoking is None:
            # Without an invoking Space, retain the target repository's
            # source-workspace lookup, but never anchor a private role tree.
            invoking = Path(self.target.target_repository_root)
        git = gitpub.BoundGit(invoking)
        owner = hashlib.sha256(str(git.git_common_dir()).encode()).hexdigest()[:16]
        anchor = self.state_root / "ui-worktrees" / run_id / owner / lane_id
        if (anchor / ".git").exists():
            if gitpub.BoundGit(anchor).git_common_dir() != git.git_common_dir():
                raise FactoryRefused("UI_ANCHOR_GIT_BINDING_MISMATCH")
        else:
            self._add_worktree(anchor, "HEAD", repo=invoking, no_checkout=True)
        return anchor

    def restore_layout(self, run_id: str, lanes: Sequence[st.LaneProjection]) -> None:
        """Reopen role shells before native gates, never replay role work."""
        try:
            for lane in lanes:
                lane_root = self.worktrees / run_id / lane.lane_id
                role_cwds = {
                    role: lane_root / role / "checkout" for role in lch.LANE_PANE_ROLES
                }
                # Future roles remain lazy; only existing execution locations
                # participated in this run and need their shell restored.
                for role, cwd in role_cwds.items():
                    if not cwd.is_dir():
                        continue
                    route = self.role_routes[role]
                    self.launcher.restore_layout(lch.LaunchSpec(
                        correlation_token=lch.role_session_token(run_id, lane.lane_id, role),
                        worktree=cwd, prompt_path=cwd, envelope_path=cwd,
                        route=route["route"], model=route["model"], effort=route["effort"],
                        profile=route["profile"], session_dir=lane_root / role,
                        environment=self._launch_environment(cwd),
                        lane_key=lane.lane_id, lane_label=lane.lane_id, pane_role=role,
                        run_id=run_id,
                        repository_fingerprint=self.target.target_repository_fingerprint,
                        repository_root=Path(self.target.target_repository_root),
                        workspace_label=str(getattr(self.launcher, "workspace_label", "") or lch.workspace_label_for(self.project_identity, run_id)),
                        pane_group_size=len(lch.LANE_PANE_ROLES), role_cwds=role_cwds,
                        child_anchor=self._child_anchor(run_id, lane.lane_id, cwd),
                    ))
        except lch.LaunchRefused as exc:
            raise self._launch_failed(exc) from exc

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

    def _redacted_failures(self, ctx: LaneContext) -> list[str]:
        """Failure lines carried on the prior code review, if it left any."""
        record = ctx.artifacts.get("CODE_REVIEW")
        payload = getattr(record, "payload", None) or {}
        if not isinstance(payload, Mapping):
            return []
        lines = payload.get("redacted_failures")
        if not isinstance(lines, Sequence) or isinstance(lines, str):
            return []
        return [str(item) for item in lines if str(item).strip()]

    def _payload_ok(self, role: str, payload: Mapping[str, Any]) -> bool:
        if role in ("test-reviewer", "code-reviewer", "integration-reviewer"):
            return payload.get("verdict") in (
                st.ReviewerVerdict.PASS.value,
                st.ReviewerVerdict.REVISE.value,
            )
        return True

    #: Set by FactoryScheduler when an operator console is attached. Reporting
    #: only: nothing reads these and no transition keys on one.
    step: Any = None

    def _say(self, lane_id: str, message: str, detail: str = "") -> None:
        if self.step is None:
            return
        try:
            self.step(lane_id, message, detail)
        except Exception:
            pass

    def _await_envelope(
        self, handle: object, envelope: Path, role: str, lane_id: str = ""
    ) -> Mapping[str, Any]:
        bound = Path(getattr(handle, "launched_cwd", "") or Path(envelope).parent)
        waited = 0.0
        while True:
            try:
                raw = _read_regular_text_under(bound, envelope)
                payload = json.loads(raw)
            except FileNotFoundError:
                payload = None
            except FactoryRefused:
                raise
            except (OSError, UnicodeError, ValueError):
                payload = None

            if isinstance(payload, dict) and self._payload_ok(role, payload):
                wait = getattr(self.launcher, "wait_for_idle", None)
                if wait is not None:
                    try:
                        wait(handle)
                    except lch.AgentNotInteractive as exc:
                        # The declaration is already on disk and already valid;
                        # this wait only lets the composer finish rendering so
                        # the next prompt is not typed into a busy one. The
                        # correction path re-checks that itself before it
                        # submits, so a slow render is not this run's answer.
                        self._say(
                            lane_id or "-",
                            "{0} composer still rendering".format(role),
                            str(exc),
                        )
                return payload
            # ── nothing but the envelope ends this wait ─────────────────────
            #
            # There used to be a `poll` here, and a GONE-or-EXITED branch that
            # raised `STAGE_PAYLOAD_MISSING`. Every observation it read was a
            # transport observation: whether herdr still lists a row under this
            # agent's name, whether a pane has been quiet, whether a partial
            # file parsed on one unlucky read. None of those answers the
            # question this method asks, which is whether the turn declared.
            #
            # The branch was rewritten repeatedly and each rewrite fixed one
            # arm. Reading the transcript for a terminal record ended attempts
            # on the agent's first message. Reading the pane's absence before
            # the envelope threw away completed work on run
            # run-14b7b75944094c52ac9c0add41ae46a2. A single `idle` sample
            # convicted a live builder on run-8d1a71f463e4430f92a125a8f8b3731d
            # 75 seconds before it declared. A missing herdr row ended
            # `lane-wp4-recalls-build` 245 seconds after dispatch on FDAdb run
            # 2489c772d7c04ad5a2f2bcaa2f4de11c, with an empty `results/` and a
            # prompt the builder never answered.
            #
            # Four incidents, one cause: a transport signal was allowed to
            # terminate a lane. Deleting the branch is the fix. There is no
            # confirmation window to tune and no state to add, because there is
            # no longer a second thing that can end the wait -- the envelope
            # ends it, and an agent that never declares leaves a lane visibly
            # waiting for the operator who is watching the run.
            time.sleep(0.1)
            waited += 0.1
            # A silent minute is indistinguishable from a hung agent. Say the
            # wait is still a wait, at a cadence that does not flood a terminal.
            if waited % 30 < 0.1:
                self._say(
                    lane_id or "-",
                    "waiting on {0}".format(role),
                    "{0}s elapsed".format(int(waited)),
                )

    def _launch_failed(self, exc: lch.LaunchRefused) -> LaunchFailed:
        return LaunchFailed(
            "{}:{}".format(exc.refusal.code, exc.detail),
            pane_created=bool(exc.pane_created),
        )

    def _retain_completed(self, handle: object, key: tuple[str, str, str]) -> None:
        retain = getattr(self.launcher, "retain", None)
        if retain is None:
            return
        try:
            retain(handle)
        except lch.LaunchRefused:
            self._roles.pop(key, None)

    def _release_superseded(self, key: tuple[str, str, str]) -> bool:
        """Close the session a previous revision of this (lane, role) bound.

        Dropping the record is not enough. `HerdrLauncher` keeps its own role
        registry keyed by `(lane_key, pane_role)` with no digest in it, and
        `launch` adopts a still-running role agent and resubmits into it, so a
        new key with the old pane still open resumes the same context window
        the new key exists to leave behind. `cancel` is the launcher-side close
        -- it proves quiescence, closes the pane, and drops the launcher's own
        role handle -- and it is the only pane close this actor performs.

        Best effort by construction: a close that refuses is reported in the
        step log and the record dropped anyway. A pane Herdr would not close is
        an operator's problem, never a reason to fail the lane.

        Answers whether anything was released, because the caller then owns a
        checkout an earlier revision's session left behind.
        """
        lane_id, role, _digest = key
        stale = [
            other
            for other in self._roles
            if other[0] == lane_id and other[1] == role and other != key
        ]
        cancel = getattr(self.launcher, "cancel", None)
        for other in stale:
            stored = self._roles.pop(other)
            if stored.handle is None or cancel is None:
                continue
            try:
                cancel(stored.handle, time.monotonic() + _SUPERSEDED_CLOSE_SECONDS)
            except Exception as exc:
                self._say(
                    lane_id,
                    "superseded {0} pane not closed".format(role),
                    "{0}: {1}".format(type(exc).__name__, exc),
                )
                continue
            self._say(
                lane_id,
                "closed superseded {0} pane".format(role),
                "spec {0}".format(other[2][:12]),
            )
        return bool(stale)

    def _prepared_cwd(self, cwd: Path, prepare_cwd: Callable[[Path], None]) -> None:
        """Materialize the tree, then install its dependencies. In that order.

        A tree is provisioned after its *final* materialization, never before
        one. Materializing is destructive by construction --
        `hv.refresh_materialized_commit` unlinks every child of the tree, and a
        git checkout resets it -- so anything installed before a later
        materialization is gone by the time the agent reads the tree.

        That is not hypothetical. Provisioning first lived in
        `HerdrLauncher.launch`, which runs after this dispatch's `prepare_cwd`
        and *before* `prepare_adopted_cwd`, and `prepare_adopted_cwd` is a
        materialization: it is called on the two paths that reach a pane which
        already exists -- a reused role pane and an adopted agent. On FDAdb run
        d246ae9592be478396ad5146a89f00ae the test reviewer was therefore
        dispatched into a tree whose `node_modules` had just been unlinked, and
        reported `ERR_MODULE_NOT_FOUND` for `vitest/config` over ten rounds. It
        was right every time.

        Both paths call this, so provisioning is always the last thing done to
        the tree before the agent is asked to read it. A failure is the same
        typed refusal the launcher raised, because it is the same failure: the
        deployment's command, in the tree an actor was about to be dispatched
        into, before any agent is asked to fix it.
        """
        prepare_cwd(cwd)
        argv = _resolved_provision_argv(self, None)
        if not argv:
            return
        try:
            provisioning.provision_tree(
                cwd, argv, _resolved_provision_timeout(self)
            )
        except provisioning.ReviewProvisioningError as exc:
            raise lch.LaunchRefused(
                lch.LaunchRefusal.PROVISION_FAILED,
                exc.detail or str(exc),
                pane_created=False,
            ) from exc

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
        # Every dispatch crosses here, which is why the release is here and
        # not in the five stage methods that each compute the same key.
        self._release_superseded(key)
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
        # The tree the role reads is materialized from the artifact this
        # dispatch names, on every dispatch, here and nowhere else. This used
        # to be three conditional calls -- one on the resubmit path, one
        # behind `superseded`, and the tester's own refresh inside `_prepare`
        # -- which left the ordinary first-launch path with no refresh at all.
        # That path is not rare: it is taken whenever the in-memory role
        # record is gone but the checkout and the agent's pane are not, which
        # is every `run resume` and every `_retain_completed` that dropped the
        # key. On run a2ea7355 the test reviewer read the round-1 draft for
        # three consecutive rounds and reported round-1's defects each time,
        # while the tester had fixed both in round 2. Nothing could notice: a
        # private tree carries no sha, and the ledger records the input
        # artifact id rather than the bytes the reader was given, so
        # `input_digest` moved every round while the file did not.
        try:
            self._prepared_cwd(cwd, prepare_cwd)
        except lch.LaunchRefused as refused:
            raise self._launch_failed(refused) from refused
        # The path this dispatch already materialized and provisioned above.
        # `prepare_adopted_cwd` below is called on the two paths that reach a
        # pane which already exists (a reused role pane, an adopted agent);
        # when the cwd the launcher hands back is this same path, the launcher
        # has already verified it is bound to the tree this dispatch prepared
        # (`actual != worktree` is refused before the callback runs), so
        # materializing and provisioning it again would be a byte-identical
        # repeat of what just ran a few lines up -- on FDAdb this is ~2
        # minutes of `npm ci` + `uv venv` wasted on every reused-pane
        # dispatch, which is the common case for a long-running lane.
        dispatch_prepared = cwd.resolve()
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
            if adopted != dispatch_prepared:
                self._prepared_cwd(adopted, prepare_cwd)
            self._materialize_role_instructions(
                adopted, role, route["route"], ctx.lane.lane_kind
            )
            envelope.parent.mkdir(parents=True, exist_ok=True)
            write_prompt(adopted)

        lane_id = ctx.lane.lane_id
        token = lch.role_session_token(ctx.run_id, lane_id, role)

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
            repository_root=Path(self.target.target_repository_root),
            stage=ctx.stage.value,
            input_digest=ctx.input_digest,
            workspace_label=self._workspace_label(ctx),
            pane_group_size=len(lch.LANE_PANE_ROLES),
            role_cwds=self._role_cwds(ctx),
            child_anchor=self._lane_child_anchor(ctx, cwd),
            prepare_adopted_cwd=prepare_adopted_cwd,
        )
        try:
            handle = self.launcher.launch(spec)
        except lch.LaunchRefused as extra_exc:
            raise self._launch_failed(extra_exc) from extra_exc
        cwd_used = Path(getattr(handle, "launched_cwd", cwd))
        self._say(
            lane_id,
            "dispatched {0}".format(role),
            "{0} {1}".format(route["route"], route.get("model") or route.get("profile") or ""),
        )
        payload = self._await_envelope(handle, envelope, role, lane_id)
        self._say(lane_id, "{0} replied".format(role), "turn {0}".format(turn))
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
            # Reuse of an existing checkout, whatever it holds. `_launch`
            # refreshes every tree it dispatches into, so this returns the
            # path and states nothing about its bytes. It used to refresh the
            # tester's, and only the tester's, which read as a reuse guard and
            # was really the one role that happened to be covered.
            if private_tree:
                return attempt, None
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
        key: tuple[str, str, str],
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
            scope: Sequence[str] = ()
            if ctx.lane.lane_kind == st.LANE_KIND_TESTS:
                scope = ctx.lane.declared_outputs
            files.update(self._collect_uncommitted(cwd_used, scope))
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
        # The lane's own acceptance suite, kept out of the checkout for the
        # whole turn. Never in `extra` -- these are paths, and a path is a
        # name the builder has no reason to hold.
        strip = tuple(ctx.sealed_private_paths)
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
        if ctx.bound_surface is not None:
            # Names only. Module specifiers, exported symbols, and result-object
            # keys -- the identifiers the sealed assertions resolve against. No
            # literal, number, selector, or fixture value moves here.
            extra["bound_surface"] = dict(ctx.bound_surface)
        findings = self._revise_findings(ctx, "CODE_REVIEW")
        if findings is not None:
            # The same constant, the same predicate, and the same position as
            # `code_review.builder_view` uses. Two readers assemble a prior
            # review for the builder -- that view and this payload -- and they
            # have to agree about which of the two things in front of it is
            # ground truth. Nothing is filtered: a located finding is true
            # whatever the suite says, so only the order of authority is
            # stated.
            record = getattr(ctx.artifacts.get("CODE_REVIEW"), "payload", None) or {}
            summary = (
                record.get("public_result_summary")
                if isinstance(record, Mapping)
                else None
            )
            if isinstance(summary, Mapping) and cr._summary_is_red(summary):
                findings.insert(0, cr._FINDINGS_FRAMING)
            extra["revise_findings"] = findings
        failures = self._redacted_failures(ctx)
        if failures:
            # The runner's own failure lines, already redacted against the
            # sealed token set where they were produced. Without these the
            # builder is told a count and has to guess which cases it names.
            extra["redacted_failures"] = failures
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
        # The strip used to be repeated here because `_launch` skipped
        # `prepare_cwd` on a first launch, and that is the turn an amended
        # lane opens with, against an integration head carrying its own
        # suite. `_launch` now prepares every tree it dispatches into, and
        # `_refresh_builder_checkout` strips on both of its branches, so the
        # guard holds for the same reason every other role's does.
        _payload, _handle, cwd_used = self._launch(
            ctx,
            "builder",
            cwd,
            extra,
            prepare_cwd=lambda path: self._refresh_builder_checkout(
                path, ctx.lane.declared_outputs, sha, strip=strip
            ),
        )
        cwd_used = self._bind_checkout(key, attempt, checkout, cwd_used)
        candidate_sha, changed = self._commit_declared(
            cwd_used, ctx.lane.declared_outputs, sha, strip=strip
        )
        return {"candidate_sha": candidate_sha, "changed": changed}

    def review_code(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        extra: dict[str, Any] = {
            "candidate_sha": ctx.candidate_sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            "sealed_digest": ctx.sealed_digest,
        }
        if ctx.sealed_result_summary is not None:
            # Counts only. These are the same five integers the builder already
            # receives as public_result_summary, so no sealed source, case name,
            # or assertion text moves here.
            extra["sealed_result_summary"] = dict(ctx.sealed_result_summary)
        if ctx.sealed_findings_required:
            extra["sealed_findings_required"] = True
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

    #: Files the operator agent's tree carries, relative to its CWD.
    _OPERATOR_INPUTS = "inputs"
    _OPERATOR_SEALED = "sealed"
    _OPERATOR_OUT = "revisions"

    def _write_operator_tree(
        self, cwd: Path, request: att.OperatorRequest
    ) -> None:
        """Materialize everything the operator agent reads, on every dispatch.

        On disk rather than in the prompt: the sealed suite and four rounds of
        findings are the bulk of this handoff, and B13's size check is made
        against the route's window at launch. A prompt that carries them is a
        prompt that can overflow, and an overflowing agent answers about a
        different lane.
        """
        inputs = cwd / self._OPERATOR_INPUTS
        sealed = cwd / self._OPERATOR_SEALED
        for directory in (inputs, sealed, cwd / self._OPERATOR_OUT):
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True, exist_ok=True)
        (inputs / "current.ir.json").write_bytes(Path(request.ir_path).read_bytes())
        (inputs / "lane_gates.txt").write_text(request.lane_gates, encoding="utf-8")
        (inputs / "amendment_rules.md").write_text(
            request.amendment_rules, encoding="utf-8"
        )
        (inputs / "reviews.json").write_text(
            json.dumps(
                {
                    "public_contract": st.json_ready(request.public_contract),
                    "redacted_failures": list(request.redacted_failures),
                    "reviews": [st.json_ready(row) for row in request.reviews],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        for relative, text in sorted(request.sealed_files.items()):
            destination = _resolved_under(sealed, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        lch.scratch_environment(cwd)

    def attend_operator(
        self, ctx: LaneContext, request: att.OperatorRequest
    ) -> Mapping[str, Any]:
        """Dispatch the operator agent that authors an attended amendment.

        Its tree is not a git checkout. That is deliberate: this role authors a
        plan, never a candidate, and giving it a worktree would give it a HEAD
        to commit against and the product repo to wander into. It gets its
        inputs as files and one path to write.
        """
        attempt = self._role_dir(ctx, "operator")
        cwd = attempt / "checkout"
        cwd.mkdir(parents=True, exist_ok=True)
        extra = {
            "allowed_lane_ids": list(request.allowed_lane_ids),
            "amendment_rules_path": str(
                (cwd / self._OPERATOR_INPUTS / "amendment_rules.md").resolve()
            ),
            "current_ir_path": str(
                (cwd / self._OPERATOR_INPUTS / "current.ir.json").resolve()
            ),
            "lane_gates_path": str(
                (cwd / self._OPERATOR_INPUTS / "lane_gates.txt").resolve()
            ),
            "next_plan_revision": request.next_plan_revision,
            "parked_stage": request.stage,
            "reviews_path": str(
                (cwd / self._OPERATOR_INPUTS / "reviews.json").resolve()
            ),
            "revision_out_path": request.revision_out_path,
            "round": request.round_number,
            "sealed_suite_dir": str((cwd / self._OPERATOR_SEALED).resolve()),
            "sealed_suite_files": sorted(request.sealed_files),
        }
        payload, _handle, cwd_used = self._launch(
            ctx,
            "operator",
            cwd,
            extra,
            prepare_cwd=lambda path: self._write_operator_tree(path, request),
        )
        del cwd_used
        return payload

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

    def close_run_panes(self, run_id: str) -> Tuple[str, ...]:
        """Close every pane Herdr still holds for `run_id`, handle or not.

        `complete_run_spaces` can only reach what this process launched, and a
        fresh invocation launched nothing. An amendment runs in exactly that
        process, so without this the panes of the parked run stay open and the
        amended scheduler opens a second set beside them.

        Sessions this process *does* hold are dropped alongside their panes,
        so a later `complete_run_spaces` does not try to rename a pane that is
        already gone.
        """
        close = getattr(self.launcher, "close_run_panes", None)
        if close is None:
            return ()
        closed = tuple(close(run_id))
        for key, stored in list(self._roles.items()):
            if self._session_run_id(stored) == run_id:
                self._roles.pop(key, None)
                self._safe_remove_attempt(stored.attempt, stored.checkout)
        return closed

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
            anchors = self.state_root / "ui-worktrees" / run_id
            if anchors.is_dir():
                for anchor in anchors.glob("*/*"):
                    if (anchor / ".git").is_file():
                        subprocess.check_call(
                            ["git", "-C", str(anchor), "worktree", "remove", "--force", str(anchor)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
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
        findings = list(payload.get("findings") or ())
        # Refuse a misshapen finding here, where the envelope is still in
        # hand and the role that produced it is known. Without this the first
        # thing to notice is `require_revise_findings`, several frames inside
        # the artifact writer, and what reaches the operator is a bare
        # CanonicalIdentityError naming neither the lane nor the role nor what
        # was actually wrong. Same termination, stated.
        for index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                raise FactoryRefused(
                    "REVIEW_FINDINGS_MALFORMED:findings[{0}] is {1}, not an "
                    "object carrying {2}".format(
                        index,
                        type(finding).__name__,
                        ", ".join(st.REVISE_FINDING_KEYS),
                    )
                )
        return st.ReviewerVerdict(payload["verdict"]), findings


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
    attend_route = (layout.get("attend") or {}).get("route") or {}
    # The operator agent is dispatched through the same launcher as every
    # other role, so its route is admitted by the same executed receipt. A
    # deployment cannot opt into `run attend` on a route it never captured.
    configured_routes = {binding["route"] for binding in role_routes.values()} | {
        binding for binding in (attend_route.get("route"),) if binding
    }
    if not all(admitted.admits(route) for route in configured_routes):
        raise FactoryRefused("ROUTE_RECEIPTS_REQUIRED")
    launcher = lch.HerdrLauncher(
        herdr_path=Path(str(executables.get("herdr") or "herdr")),
        omp_path=Path(str(executables.get("omp") or "omp")),
        claude_path=Path(str(executables.get("claude") or "claude")),
        admitted_routes=admitted,
        provision_argv=layout.get("provision_argv") or (),
        provision_timeout_s=layout.get("provision_timeout_s")
        or lch.PROVISION_TIMEOUT_S,
        workspace_label=lch.workspace_label_for(_project_identity(target), run_id),
        parent_workspace_id=_INVOCATION_WORKSPACE.get(),
    )
    plan = json.loads(compiled.plan_bytes)
    lane_specs = {
        str(lane["id"]): st.json_ready(lane["spec"]) for lane in plan["lanes"]
    }
    return HerdrStageActor(
        launcher,
        runtime.path,
        target,
        role_routes,
        lane_specs=lane_specs,
        operator_route=attend_route or None,
    )


def _compile_plan(path: Path, *, revision: int, ref: str) -> st.CompiledPlan:
    """Read and compile one plan artifact.

    Reading it is the only place a missing or unreadable *plan file* is a
    configuration fact, so the mapping belongs here rather than in `main`:
    every other `FileNotFoundError` a run can raise -- a missing `git`, `omp`
    or `claude` executable, a role checkout removed mid-turn, a vault path
    gone -- is a failure of the run, not of its configuration, and must not
    be relabelled.
    """
    try:
        stored = path.read_bytes()
    except OSError as exc:
        raise _MaestroConfigurationError(
            "cannot read plan artifact {0}: {1}".format(path, exc)
        ) from exc
    return plan_compiler.compile_plan(
        stored, plan_revision=revision, plan_artifact_ref=ref
    )


_PLANS_RELATIVE = Path(".maestro") / "plans"
_PLAN_ARTIFACT_NAME = "maestro-plan.v1"


def _repository_from_cwd(cwd: Path | None = None) -> Path:
    """The primary worktree the operator is standing in, or a typed refusal.

    A linked worktree shares its Git common directory with the repository's
    primary working tree, so `git_primary_workdir` answers the *primary* path
    for either one. Equality with the discovered top level is therefore the
    proof, and a linked lane checkout refuses instead of silently binding the
    repository some other checkout happens to own.
    """
    here = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    try:
        # `BoundGit` so discovery runs under the same cleaned environment as
        # every other Git call: an ambient GIT_DIR must not decide which
        # repository the operator is standing in.
        top = gitpub.BoundGit(here).text("rev-parse", "--show-toplevel")
    except gitpub.GitError as exc:
        raise _MaestroConfigurationError(
            "not inside a Git working tree: {0}".format(here)
        ) from exc
    if not top:
        raise _MaestroConfigurationError(
            "not inside a Git working tree: {0}".format(here)
        )
    root = Path(top).resolve()
    git = gitpub.BoundGit(root)
    try:
        if git.is_bare():
            raise _MaestroConfigurationError(
                "bare repository has no working tree: {0}".format(root)
            )
        git_dir = git.git_dir().resolve()
        common = git.git_common_dir().resolve()
    except gitpub.GitError as exc:
        raise gitpub.GitPublicationRefused(exc.code, exc.detail) from exc
    if git_dir != common or lch.git_primary_workdir(root) != root:
        raise _MaestroConfigurationError(
            "not the repository's primary worktree: {0}".format(root)
        )
    return root


def _main_ref_from_head(repo: Path) -> str:
    """The checked-out branch as its full ref. Detached HEAD refuses."""
    try:
        return gitpub.BoundGit(Path(repo)).symbolic_head()
    except gitpub.GitError as exc:
        raise gitpub.GitPublicationRefused(exc.code, exc.detail) from exc


def _worktree_git_dir(repo: Path) -> str:
    try:
        return str(gitpub.BoundGit(Path(repo)).git_dir())
    except gitpub.GitError as exc:
        raise gitpub.GitPublicationRefused(exc.code, exc.detail) from exc


def _plan_artifact_for(repo: Path, name: str) -> Path:
    """The one installed artifact for an exact plan name.

    `<repo>/.maestro/plans/<name>/maestro-plan.v1` and nothing else: no
    recursive search, no fuzzy match, and no path that leaves the plans
    directory once symlinks are resolved.
    """
    plans = (Path(repo) / _PLANS_RELATIVE).resolve()
    if (
        not name
        or name != name.strip()
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise _MaestroConfigurationError(
            "plan name must be one installed plan directory name: {0!r}".format(name)
        )
    candidate = plans / name / _PLAN_ARTIFACT_NAME
    resolved = candidate.resolve()
    if plans not in resolved.parents:
        raise _MaestroConfigurationError(
            "plan artifact escapes {0}: {1}".format(plans, resolved)
        )
    if not resolved.is_file():
        raise _MaestroConfigurationError("no plan artifact at {0}".format(candidate))
    return resolved


def _matching_runs(
    store: ArtifactStore,
    target: gitpub.TargetBinding,
    compiled: st.CompiledPlan,
    plan_artifact_ref: str,
) -> Tuple[str, ...]:
    """Nonterminal runs of this plan against this repository identity.

    Deterministic from persisted facts only: the target fingerprint and main
    ref narrow the rows, the active revision's plan artifact ref or the
    compiled digest identifies the plan, and the derived run status drops a
    run that has already published.
    """
    found: list[str] = []
    for row in runs_for_target(
        store,
        repository_fingerprint=target.target_repository_fingerprint,
        main_ref=target.target_main_ref,
    ):
        run_id = str(row["run_id"])
        revision = int(row["plan_revision"])
        ref = plan_artifact_ref_for(store, run_id, revision)
        if ref != plan_artifact_ref and row["plan_digest"] != compiled.plan_digest:
            continue
        status = store.derive_run_status(run_id, durable_integration_tip(store, run_id))
        if status is st.RunStatus.COMPLETE:
            continue
        found.append(run_id)
    return tuple(found)


def _run_plan(args: argparse.Namespace) -> int:
    """The single-entry operator invocation: a plan name and nothing else.

    Repository, main ref, plan artifact, runtime and run identity are all
    inferred here; the decision is then normalised into the existing
    `_run_start` / `_run_resume`, which stay the only paths that build a
    scheduler. Lookup and creation happen under the existing level-1 run lock
    so two simultaneous first invocations cannot both create a run; the lock
    is released before the scheduler runs, which re-acquires it itself.
    """
    maestro_file = _executing_maestro_file()
    repo = _repository_from_cwd()
    main_ref = _main_ref_from_head(repo)
    plan_path = _plan_artifact_for(repo, str(args.plan_name))
    layout = _load_deployment_config(maestro_file)
    require_deployment(maestro_file, repo)
    runtime = _open_runtime(layout, repo)
    resume_id = ""
    start_id = ""
    try:
        runtime.ensure_layout()
        # Binding the target takes a non-blocking exclusive lock on the
        # worktree Git directory, so it belongs *inside* the level-1 run lock
        # too: outside it, two simultaneous first invocations collide there
        # and one refuses before it can discover the other's run.
        locks = OrderedLocks(runtime, _worktree_git_dir(repo))
        locks.acquire(1)
        try:
            compiled = _compile_plan(plan_path, revision=1, ref=str(plan_path))
            target = gitpub.bind_target_worktree(repo, main_ref)
            store = _open_store(runtime)
            try:
                matches = _matching_runs(store, target, compiled, str(plan_path))
                if len(matches) > 1:
                    raise FactoryRefused(
                        "AMBIGUOUS_NONTERMINAL_RUNS:{0}".format(",".join(matches))
                    )
                if matches:
                    resume_id = matches[0]
                else:
                    start_id = uuid.uuid4().hex
                    create_factory_run(
                        store=store,
                        run_id=start_id,
                        compiled=compiled,
                        runtime=runtime,
                        target=target,
                    )
            finally:
                store.close()
        finally:
            locks.release()
    finally:
        runtime.close()
    if resume_id:
        return _run_resume(argparse.Namespace(run_id=resume_id))
    return _run_start(
        argparse.Namespace(
            plan=str(plan_path),
            repo=str(repo),
            main_ref=main_ref,
            run_id=start_id,
        )
    )


def _run_start(args: argparse.Namespace) -> int:
    maestro_file = _executing_maestro_file()
    repo = Path(args.repo).resolve() if args.repo else _repository_from_cwd()
    main_ref = args.main_ref or _main_ref_from_head(repo)
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
            # Pin at creation, so the window between `run start` and the first
            # bind is not one in which editing the plan invalidates the run.
            _pin_plan_artifact(plan_path, _pinned_plan_artifact(runtime, run_id, 1))
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
            console = step_log.RunReporter(run_id, runtime.path)
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
                step=console.step,
                compiled=compiled,
                concurrency=layout.get("concurrency") or 1,
                regression_on_findings=bool(
                    layout.get("stall_regression_on_findings")
                ),
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


def _pinned_plan_artifact(runtime: Any, run_id: str, plan_revision: int) -> Path:
    # `runtime` is read for its `path` alone, so the annotation is what a
    # caller can actually satisfy rather than the whole state root.
    """The run's own copy of one plan revision, under `runtime_state_root`.

    A recorded revision is an immutable input, and until this existed it was
    addressed as a path into the operator's working tree. `_bind_existing_run`
    recompiles that path and refuses `PLAN_ARTIFACT_MISMATCH` when the digest
    moved -- and it gates `resume`, `amend` and `status` alike. So the edit an
    amendment requires was the edit that stopped all three verbs, and the only
    way out was to restore the exact bytes, which meant the plan could not be
    amended in place at all. Measured 2026-09-06 on FDAdb run 2489c772, whose
    `plan_revisions.plan_artifact_ref` was
    `<repo>/.maestro/plans/fdadb-wp4/maestro-plan.v1` while
    `runtime_state_root/plans/` was empty.

    `MAESTRO_architecture.md` already says copied plans live under the
    deployment's `runtime_state_root`; the empty directory was the receipt that
    the copy was never written. Nothing else changes: `plan_artifact_ref` keeps
    the value it was recorded with, so no input digest moves, and the digest
    check keeps its meaning -- it now checks the run's own copy rather than a
    file anyone can edit.
    """
    return (
        runtime.path
        / "plans"
        / run_id
        / "r{0}".format(int(plan_revision))
        / _PLAN_ARTIFACT_NAME
    )


def _pin_plan_artifact(source: Path, pinned: Path) -> None:
    """Copy one revision's bytes under `runtime_state_root`, once."""
    if pinned.exists():
        return
    pinned.parent.mkdir(parents=True, exist_ok=True)
    scratch = pinned.with_name(pinned.name + ".tmp")
    scratch.write_bytes(Path(source).read_bytes())
    scratch.replace(pinned)


def _existing_run_deployment(run_id: str) -> tuple[Path, dict[str, Any]]:
    """Prefer a known local run, otherwise recover its deployment from Git."""
    executing = _executing_maestro_file()
    local_config = _deployment_product_root(executing) / _MAESTRO_CONFIG_FILE
    try:
        if local_config.is_file():
            layout = _load_deployment_config(executing)
            database = layout["runtime_state_root"] / LEDGER_FILENAME
            if read_run(database, run_id) is not None:
                return executing, layout
        found = registered_run(run_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise _RunRefused("RUN_DISCOVERY_REFUSED", str(exc)) from exc
    if found is None:
        raise _RunRefused("RUN_NOT_FOUND", run_id)
    database, row = found
    state_root = Path(row["runtime_state_root"]).resolve()
    if database != state_root / LEDGER_FILENAME:
        raise _RunRefused("RUN_DISCOVERY_REFUSED", "ledger differs from stored runtime root")
    target_root = Path(row["target_repository_root"])
    common_dir = Path(row["target_git_common_dir"]).resolve()
    git = gitpub.BoundGit(target_root)
    if Path(git.text("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common_dir:
        raise RunRepositoryMismatch("target Git common directory changed")
    candidates = []
    for field in git.text("worktree", "list", "--porcelain", "-z").split("\0"):
        if not field.startswith("worktree "):
            continue
        candidate = Path(field[len("worktree "):]) / "adws" / "maestro.py"
        config = candidate.parent / "maestro.config.yaml"
        if not candidate.is_file() or not config.is_file():
            continue
        try:
            layout = _load_deployment_config(candidate)
        except _MaestroConfigurationError:
            continue
        if layout["runtime_state_root"].resolve() != state_root:
            continue
        require_deployment(candidate, target_root)
        candidates.append((candidate, layout))
    if len(candidates) != 1:
        raise _RunRefused(
            "RUN_DEPLOYMENT_UNRESOLVED",
            "run {0}: expected one matching deployment, found {1}".format(run_id, len(candidates)),
        )
    return candidates[0]


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
    maestro_file, layout = _existing_run_deployment(run_id)
    runtime = _open_runtime(layout, layout["repo"])
    runtime.ensure_layout()
    store = _open_store(runtime)
    try:
        row = run_row(store, run_id)
        if runtime.path.resolve() != Path(row["runtime_state_root"]).resolve():
            raise FactoryRefused("RUNTIME_STATE_MISMATCH")
        require_deployment(maestro_file, Path(row["target_repository_root"]))
        runtime.revalidate(row["runtime_state_fingerprint"])
        target = target_from_binding(binding_from_run(row))
        revision = int(row["plan_revision"])
        plan_ref = Path(plan_artifact_ref_for(store, run_id, revision))
        pinned = _pinned_plan_artifact(runtime, run_id, revision)
        # The pin is authoritative once it exists. A run created before it did
        # has none, so the first bind reads the recorded path exactly as
        # before, and pins it only after the digest check has proven those are
        # still the revision's bytes.
        compiled = _compile_plan(
            pinned if pinned.is_file() else plan_ref,
            revision=revision,
            ref=str(plan_ref),
        )
        if compiled.plan_digest != row["plan_digest"]:
            raise FactoryRefused("PLAN_ARTIFACT_MISMATCH")
        _pin_plan_artifact(plan_ref, pinned)
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
            console = step_log.RunReporter(run_id, runtime.path)
            console.opened(
                "resume",
                run_id,
                row["target_repository_root"],
                row["target_main_ref"],
                (lane.lane_id for lane in store.active_projection(run_id)),
            )
            actor = _actor_for(runtime, layout, target, run_id, compiled)
            actor.restore_layout(run_id, store.active_projection(run_id))
            scheduler = FactoryScheduler(
                store,
                run_id,
                actor,
                runtime,
                target,
                stage_started=console.stage_started,
                stage_completed=console.stage_completed,
                step=console.step,
                compiled=compiled,
                concurrency=layout.get("concurrency") or 1,
                regression_on_findings=bool(
                    layout.get("stall_regression_on_findings")
                ),
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
            _pin_plan_artifact(
                Path(args.plan),
                _pinned_plan_artifact(runtime, run_id, row["plan_revision"] + 1),
            )
            console = step_log.RunReporter(run_id, runtime.path)
            console.opened(
                "amend",
                run_id,
                row["target_repository_root"],
                row["target_main_ref"],
                (lane.lane_id for lane in store.active_projection(run_id)),
            )
            actor = _actor_for(runtime, layout, target, run_id, compiled)
            # Every pane of this run belongs to the revision the amendment
            # just superseded, and this process holds no handle to any of
            # them. Close them before the scheduler below launches its own,
            # or the operator is left reading two sets of panes for one run
            # and cannot tell which is live.
            close_panes = getattr(actor, "close_run_panes", None)
            if close_panes is not None:
                close_panes(run_id)
            scheduler = FactoryScheduler(
                store,
                run_id,
                actor,
                runtime,
                target,
                stage_started=console.stage_started,
                stage_completed=console.stage_completed,
                step=console.step,
                compiled=compiled,
                concurrency=layout.get("concurrency") or 1,
                regression_on_findings=bool(
                    layout.get("stall_regression_on_findings")
                ),
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


ATTEND_AMENDMENT_RULES = """# Amendment rules (from docs/plan-authoring.md)

- Edit the IR, never the projected plan. The plan is a projection; a hand-edit
  of it is discarded by the next projection and is refused at run start.
- Do not add, remove, or rewire a lane. Topology is fixed for the life of a
  run: removing a lane is refused at any stage, and a dependency change to a
  merged lane is refused.
- Change the smallest carrier that reaches the parked lane's projection. A
  seam contract on the lane's own carrier is normally that carrier.
- A lane whose canonical spec, ordered `needs`, ordered declared outputs, or
  authored `lane_kind` moves is a changed lane: it restarts at PLANNED and
  every former input of that lane is invalidated. A lane you did not intend to
  change is finished work you are about to discard.
- A claim binding copied into every tests lane re-digests every tests lane.
  Bind the edit to the claims the parked lane discharges.
- Weakening a check, a verdict, or an error path is a contract change, not a
  fix for a blocked lane. If the contract is wrong, say so in the rationale;
  do not delete the obligation the suite is asserting.
"""


def _attend_policy(layout: Mapping[str, Any]) -> att.AttendPolicy:
    raw = layout.get("attend") or {}
    return att.AttendPolicy(
        max_amendments_per_lane=int(raw.get("max_amendments_per_lane") or 0),
        max_amendments_per_run=int(raw.get("max_amendments_per_run") or 0),
        route=raw.get("route") or {},
        planctl=raw.get("planctl"),
        validate_argv=tuple(raw.get("validate_argv") or ()),
        reviewer_id=str(raw.get("reviewer_id") or "maestro-attend"),
        reviewer_vendor=str(raw.get("reviewer_vendor") or "maestro"),
    )


def _reviewer_hmac_key(runtime: RuntimeStateRoot) -> str:
    """The plan-contract reviewer key, from where the runtime already keeps it.

    `route_admission.provision_keys` mints it once under the state root's keys
    directory and never regenerates it, because a new key silently invalidates
    every approval receipt already signed with the old one. Resolved here
    rather than hardcoded so a deployment that moved its state root moves its
    key with it.
    """
    path = runtime.path / "keys" / admission.REVIEWER_HMAC_KEY_FILE
    try:
        material = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise att.AttendRefused(
            att.KEY_UNRESOLVED, "{0}: {1}".format(path, exc)
        ) from exc
    if not material:
        raise att.AttendRefused(att.KEY_UNRESOLVED, "{0} is empty".format(path))
    return material


def _planctl_binary(policy: att.AttendPolicy) -> Path:
    if policy.planctl is None:
        raise att.AttendRefused(
            att.PLANCTL_UNRESOLVED, "attend.planctl is not configured"
        )
    if not policy.planctl.is_file():
        raise att.AttendRefused(
            att.PLANCTL_UNRESOLVED, "{0} is not a file".format(policy.planctl)
        )
    return policy.planctl


def _run_planctl(
    binary: Path, argv: Sequence[str], *, key: Optional[str] = None
) -> None:
    """One planctl subcommand, refused by exit status and nothing else.

    The refusal carries planctl's own output because a receipt or validation
    refusal names the IR field that is wrong, and that sentence is the whole
    value of running it. It is never parsed: the decision is the exit code.
    """
    environment = dict(os.environ)
    environment.pop(admission.REVIEWER_HMAC_KEY_ENV, None)
    if key is not None:
        environment[admission.REVIEWER_HMAC_KEY_ENV] = key
    result = subprocess.run(
        [sys.executable, str(binary), *argv],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if result.returncode == 0:
        return
    detail = (result.stdout or "") + (result.stderr or "")
    raise att.AttendRefused(
        att.REVISION_REFUSED,
        "planctl {0} exited {1}: {2}".format(
            argv[0], result.returncode, detail.strip()[:2000]
        ),
    )


def _attend_lane_gates(
    runtime: RuntimeStateRoot, run_id: str, lane_id: str, regression: bool
) -> str:
    """The gate table an operator reads, rendered by the tool that owns it.

    Imported here rather than at module scope: `tools/` is a sibling of the
    package and a deployment that trimmed it must still be able to run every
    other verb. A missing tool costs the operator agent one input, not the run.
    """
    try:
        from tools import lane_gates
    except ImportError as exc:  # pragma: no cover - trimmed deployment
        return "lane gate table unavailable: {0}".format(exc)
    conn = lane_gates.open_readonly(runtime.ledger_path())
    try:
        rows = [
            row
            for row in lane_gates.lane_rows(conn, run_id)
            if str(row["lane_id"]) == lane_id
        ]
        if not rows:
            return "no lane row for {0}".format(lane_id)
        table = lane_gates.lane_table(
            conn,
            run_id,
            rows[0],
            str(runtime.path),
            "runtime_sync not read by attend",
            regression_on_findings=regression,
        )
        return lane_gates.render(run_id, lane_id, table)
    finally:
        conn.close()


def _attend_sealed_files(
    store: ArtifactStore,
    runtime: RuntimeStateRoot,
    run_id: str,
    lane: st.LaneProjection,
) -> dict[str, str]:
    """The sealed suite, read out of the vault for the operator agent alone.

    This is the one place outside code review that decrypts the private-test
    boundary, and it is a deliberate trade the operator opted into by setting
    `attend.max_amendments_per_lane`. Nothing downstream of here hands these
    bytes to a builder or a reviewer: they are written into the operator's own
    tree, which is not a git checkout and is never merged.
    """
    artifact_id = store.sealed_bundle_artifact_id(run_id, lane.lane_id)
    if artifact_id is None:
        return {}
    record = store.get_lane_artifact(artifact_id)
    vault = hv.ensure_vault(runtime.path, run_id)
    blobs = tchain.sealed_private_files(
        vault, _record_as_lane_artifact(record, lane)
    )
    return {
        path: hv.cat_blob(vault, blob).decode("utf-8", errors="replace")
        for path, blob in blobs.items()
    }


def _attend_request(
    store: ArtifactStore,
    runtime: RuntimeStateRoot,
    layout: Mapping[str, Any],
    run_id: str,
    lane_id: str,
    compiled: st.CompiledPlan,
    ir_path: Path,
    operator_cwd: Path,
) -> att.OperatorRequest:
    """Everything the operator agent reads, assembled from the ledger."""
    lane = next(item for item in compiled.lanes if item.lane_id == lane_id)
    stage = store.lane_stage(run_id, lane_id)
    reviews: list[Mapping[str, Any]] = []
    for kind in (st.ArtifactKind.TEST_REVIEW, st.ArtifactKind.CODE_REVIEW):
        for payload in store.lane_artifact_payloads(run_id, lane_id, kind, 4):
            reviews.append({"kind": kind.value, **dict(payload)})
    plan = store.latest_lane_artifact_payload(
        run_id, lane_id, st.ArtifactKind.LANE_PLAN
    )
    contract = {}
    if isinstance(plan, Mapping):
        contract = dict(plan.get("public_contract") or {})
    failures: tuple[str, ...] = ()
    builder = store.latest_lane_artifact_payload(
        run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
    )
    if isinstance(builder, Mapping):
        raw = builder.get("redacted_failures")
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            failures = tuple(str(item) for item in raw)
    next_revision = compiled.plan_revision + 1
    return att.OperatorRequest(
        run_id=run_id,
        lane_id=lane_id,
        stage=stage.value,
        round_number=len(reviews),
        plan_revision=compiled.plan_revision,
        next_plan_revision=next_revision,
        public_contract=contract,
        reviews=tuple(reviews),
        redacted_failures=failures,
        lane_gates=_attend_lane_gates(
            runtime,
            run_id,
            lane_id,
            bool(layout.get("stall_regression_on_findings")),
        ),
        ir_path=str(ir_path),
        revision_out_path=str(
            operator_cwd
            / HerdrStageActor._OPERATOR_OUT
            / "r{0}.ir.json".format(next_revision)
        ),
        sealed_files=_attend_sealed_files(store, runtime, run_id, lane),
        amendment_rules=ATTEND_AMENDMENT_RULES,
        allowed_lane_ids=att.paired_lane_ids(compiled.lanes, lane_id),
    )


def _attend_project(
    policy: att.AttendPolicy,
    runtime: RuntimeStateRoot,
    repo: Path,
    plans_dir: Path,
    run_id: str,
    revision_ir: Path,
    revision: int,
) -> tuple[st.CompiledPlan, Path]:
    """Validate, approve, project and compile one authored revision.

    The same four steps the human ran by hand on FDAdb `d246ae95`, in the same
    order and against the same binary. The IR is copied under the repository
    first because `planctl --repo-root` refuses an IR outside the root it is
    told to resolve sources against, and because a revision that is applied
    has to survive the operator agent's scratch tree.
    """
    binary = _planctl_binary(policy)
    key = _reviewer_hmac_key(runtime)
    plans_dir.mkdir(parents=True, exist_ok=True)
    stem = "{0}.r{1}".format(run_id, revision)
    ir = plans_dir / (stem + ".ir.json")
    rendered = plans_dir / (stem + ".html")
    receipt = plans_dir / (stem + ".receipt.json")
    plan_out = plans_dir / (stem + ".plan.json")
    for stale in (rendered, receipt, plan_out):
        if stale.exists():
            stale.unlink()
    shutil.copyfile(revision_ir, ir)
    root = ["--repo-root", str(repo)]
    _run_planctl(binary, ["render", str(ir), "--out", str(rendered), *root])
    _run_planctl(
        binary,
        [
            "review",
            str(ir),
            "--rendered",
            str(rendered),
            "--receipt-out",
            str(receipt),
            "--reviewer",
            policy.reviewer_id,
            "--reviewer-vendor",
            policy.reviewer_vendor,
            *root,
        ],
        key=key,
    )
    _run_planctl(
        binary,
        [
            "validate",
            str(ir),
            "--rendered",
            str(rendered),
            "--receipt",
            str(receipt),
            "--require-approved",
            *root,
            *policy.validate_argv,
        ],
    )
    try:
        ingress.author_from_plan_contract(ir, receipt, plan_out, repo, rendered)
    except Exception as exc:
        raise att.AttendRefused(
            att.REVISION_REFUSED, "{0}: {1}".format(type(exc).__name__, exc)
        ) from exc
    try:
        compiled = _compile_plan(plan_out, revision=revision, ref=str(plan_out))
    except PlanCompileError as exc:
        raise att.AttendRefused(att.REVISION_REFUSED, str(exc)) from exc
    return compiled, plan_out


def _run_attend(args: argparse.Namespace) -> int:
    """Run the factory and author the amendment a NO_PROGRESS park needs.

    Everything about a lane's lifecycle is unchanged. This verb runs the same
    scheduler `run resume` runs, and when that scheduler parks a lane for
    making no progress it does what the operator would have done by hand
    instead of returning to a prompt: dispatches one agent to author a plan
    revision, validates and projects it exactly as `run amend` requires, and
    applies it through `apply_factory_amendment`. Any other wait reason, and
    any lane past its bound, parks as it does today.
    """
    run_id = args.run_id
    layout, runtime, store, row, target, compiled = _bind_existing_run(run_id)
    policy = _attend_policy(layout)
    if not policy.enabled:
        store.close()
        runtime.close()
        return _RunRefused(
            att.DISABLED,
            "set attend.max_amendments_per_lane in this deployment's "
            "maestro.config.yaml to enable run attend",
        ).emit()
    maybe_autoload_dashboard(
        layout,
        repository=Path(row["target_repository_root"]),
        ledger=runtime.ledger_path(),
    )
    session_id = uuid.uuid4().hex
    repo = Path(row["target_repository_root"])
    plans_dir = Path(plan_artifact_ref_for(store, run_id, row["plan_revision"]))
    plans_dir = plans_dir.resolve().parent
    console = step_log.RunReporter(run_id, runtime.path)
    state: dict[str, Any] = {"passes": 0}

    def build_scheduler(plan: st.CompiledPlan) -> tuple[Any, Any]:
        actor = _actor_for(runtime, layout, target, run_id, plan)
        if state["passes"]:
            close_panes = getattr(actor, "close_run_panes", None)
            if close_panes is not None:
                close_panes(run_id)
        scheduler = FactoryScheduler(
            store,
            run_id,
            actor,
            runtime,
            target,
            stage_started=console.stage_started,
            stage_completed=console.stage_completed,
            step=console.step,
            compiled=plan,
            concurrency=layout.get("concurrency") or 1,
            regression_on_findings=bool(layout.get("stall_regression_on_findings")),
        )
        return actor, scheduler

    def run_scheduler(plan: st.CompiledPlan) -> st.RunStatus:
        actor, scheduler = build_scheduler(plan)
        state["actor"] = actor
        actor.restore_layout(run_id, store.active_projection(run_id))
        scheduler.resume_waiting()
        status = scheduler.run()
        state["passes"] = state["passes"] + 1
        return status

    def operator_cwd_for(lane_id: str) -> Path:
        return runtime.path / "worktrees" / run_id / lane_id / "operator" / "checkout"

    def request_for(lane_id: str, plan: st.CompiledPlan) -> att.OperatorRequest:
        pinned = _pinned_plan_artifact(runtime, run_id, plan.plan_revision)
        source = plans_dir / "{0}.r{1}.ir.json".format(run_id, plan.plan_revision)
        return _attend_request(
            store,
            runtime,
            layout,
            run_id,
            lane_id,
            plan,
            source if source.is_file() else pinned,
            operator_cwd_for(lane_id),
        )

    def dispatch(request: att.OperatorRequest) -> Mapping[str, Any]:
        actor = state.get("actor")
        propose = getattr(actor, "attend_operator", None)
        if propose is None:
            raise att.AttendRefused(
                att.OPERATOR_FAILED, "actor cannot dispatch an operator agent"
            )
        lane = next(
            item
            for item in store.active_projection(run_id)
            if item.lane_id == request.lane_id
        )
        ctx = LaneContext(
            run_id=run_id,
            lane=lane,
            plan_revision=request.plan_revision,
            plan_digest=row["plan_digest"],
            plan_artifact_ref=plan_artifact_ref_for(
                store, run_id, request.plan_revision
            ),
            input_digest=st.digest_canonical(
                {
                    "attend_session_id": session_id,
                    "lane_id": request.lane_id,
                    "plan_revision": request.plan_revision,
                    "schema_version": st.CANONICAL_SCHEMA_VERSION,
                }
            ),
            stage=st.LaneStage(request.stage),
            artifacts={},
        )
        return propose(ctx, request)

    def project(revision_ir: Path, revision: int) -> st.CompiledPlan:
        plan, plan_out = _attend_project(
            policy, runtime, repo, plans_dir, run_id, revision_ir, revision
        )
        state["plan_path"] = plan_out
        return plan

    def apply(plan: st.CompiledPlan) -> Any:
        record = apply_factory_amendment(
            store, run_id, plan, runtime=runtime, target=target
        )
        _pin_plan_artifact(
            Path(state["plan_path"]),
            _pinned_plan_artifact(runtime, run_id, plan.plan_revision),
        )
        return record

    started = now_iso()
    store.record_attend_session(
        run_id,
        session_id=session_id,
        phase=st.ATTEND_PHASE_START,
        payload={
            "started_at": started,
            "max_amendments_per_lane": policy.max_amendments_per_lane,
            "max_amendments_per_run": policy.max_amendments_per_run,
            "plan_revision": compiled.plan_revision,
        },
    )
    outcome: att.AttendOutcome | None = None
    stop_reason = ""
    status = st.RunStatus.WAITING
    try:
        try:
            console.opened(
                "attend",
                run_id,
                row["target_repository_root"],
                row["target_main_ref"],
                (lane.lane_id for lane in store.active_projection(run_id)),
            )
            try:
                outcome = att.attend_run(
                    store=store,
                    run_id=run_id,
                    policy=policy,
                    compiled=compiled,
                    session_id=session_id,
                    run_scheduler=run_scheduler,
                    request_for=request_for,
                    dispatch=dispatch,
                    project=project,
                    apply_amendment=apply,
                    say=console.step,
                )
                status = outcome.status
                stop_reason = outcome.stop_reason
                applied = outcome.applied
            except att.AttendRefused as refused:
                stop_reason = refused.code
                applied = ()
                raise
            finally:
                store.record_attend_session(
                    run_id,
                    session_id=session_id,
                    phase=st.ATTEND_PHASE_STOP,
                    payload={
                        "started_at": started,
                        "stopped_at": now_iso(),
                        "stop_reason": stop_reason or "UNRECORDED",
                        "revisions_applied": len(applied),
                        "lanes_amended": sorted(
                            {item.lane_id for item in applied}
                        ),
                        "amendments": [
                            {
                                "lane_id": item.lane_id,
                                "plan_revision": item.plan_revision,
                                "amendment_artifact_id": item.amendment_artifact_id,
                                "rationale_artifact_id": item.rationale_artifact_id,
                            }
                            for item in applied
                        ],
                    },
                )
            console.finished(run_id, status)
        finally:
            store.close()
    except att.AttendRefused as refused:
        return _RunRefused(refused.code, refused.detail).emit()
    finally:
        runtime.close()
    print(
        json.dumps(
            {
                "outcome": "ATTENDED",
                "run_id": run_id,
                "status": status.value,
                "stop_reason": stop_reason,
                "session_id": session_id,
                "revisions_applied": len(outcome.applied) if outcome else 0,
            },
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
            attend_rows = [
                dict(row["payload"])
                for row in store.run_artifacts_of_kind(
                    run_id, st.ArtifactKind.ATTEND_SESSION
                )
            ]
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
                **({"attend": attend_rows} if attend_rows else {}),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro")
    # The single-entry operator invocation. It is an option on the root
    # parser, not a verb, so the frozen verb surface is unchanged.
    parser.add_argument("--plan", dest="plan_name", metavar="PLAN")
    parser.set_defaults(handler=None)
    root = parser.add_subparsers(dest="command", required=False)

    run = root.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    start = run_sub.add_parser("start")
    start.add_argument("plan")
    start.add_argument("--repo")
    start.add_argument("--main-ref")
    start.add_argument("--run-id")
    start.set_defaults(handler=_run_start)

    resume = run_sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.set_defaults(handler=_run_resume)

    amend = run_sub.add_parser("amend")
    amend.add_argument("plan")
    amend.add_argument("--run", dest="run_id", required=True)
    amend.set_defaults(handler=_run_amend)

    attend = run_sub.add_parser("attend")
    attend.add_argument("--run", dest="run_id", required=True)
    attend.set_defaults(handler=_run_attend)

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
    parser = build_parser()
    args = parser.parse_args(list(raw))
    plan_name = getattr(args, "plan_name", None)
    if args.command is None:
        if plan_name is None:
            parser.error("--plan <plan-name> or a run verb is required")
        handler: Callable[[argparse.Namespace], int] = _run_plan
    else:
        if plan_name is not None:
            parser.error("--plan is the whole invocation; it takes no verb")
        handler = args.handler
    invocation = _INVOCATION_WORKSPACE.set(os.environ.get("HERDR_WORKSPACE_ID", ""))
    try:
        return int(handler(args))
    except _RunRefused as exc:
        return exc.emit()
    except PlanCompileError as exc:
        return _RunRefused("PLAN_COMPILE_REFUSED", str(exc)).emit()
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
        # The launcher's `pane_created` is the operator's only signal that a
        # role pane survived the refusal and is theirs to close; a launch that
        # refused before any pane existed leaves nothing behind.
        detail = exc.detail + (":pane_retained" if exc.pane_created else "")
        return _RunRefused("LAUNCH_FAILED", detail).emit()
    except gitpub.GitPublicationRefused as exc:
        return _RunRefused(exc.code, str(exc)).emit()
    except RunAlreadyExists as exc:
        # `_run_plan` created the run under the run lock and `_run_start`
        # re-binds the target before creating it again; if the main ref moved
        # in that window the second binding differs and `create_factory_run`
        # re-raises. The ledger already holds the run, so the next invocation
        # resumes it -- the operator gets the typed code, not a traceback.
        return _RunRefused(exc.code, str(exc)).emit()
    except _MaestroConfigurationError as exc:
        return _RunRefused("RUN_CONFIGURATION_REQUIRED", str(exc)).emit()
    except prv.PrivateReviewError as exc:
        # A review tree that cannot be provisioned, or a project no available
        # interpreter satisfies, is a fault of this machine. It is already kept
        # out of the builder's findings by being raised rather than recorded;
        # this keeps it out of a traceback too, so the frozen operator surface
        # answers with the same typed JSON it answers every other refusal with.
        # Every other `PrivateReviewError` names a factory invariant, not an
        # environment, and is deliberately left to surface as it does today.
        detail = cr.sealed_environment_detail(exc)
        if detail is None:
            raise
        return _RunRefused(cr.SEALED_ENVIRONMENT_OUTCOME, detail).emit()
    finally:
        _INVOCATION_WORKSPACE.reset(invocation)


if __name__ == "__main__":
    raise SystemExit(main())
