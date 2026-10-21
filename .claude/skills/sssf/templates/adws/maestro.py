#!/usr/bin/env python3
"""Maestro factory CLI: run start/resume/amend/status."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import sqlite3
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

# This must run before third-party and local imports can dirty a deployed checkout.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import yaml

from adw_modules import attend as att
from adw_modules import git_publication as gitpub
from adw_modules.handoff_budget import (
    OMP_CONTEXT_WINDOW_TOKENS,
    estimate_tokens_for_bytes,
    route_publishes_a_window,
)
from adw_modules import code_review as cr
from adw_modules import launcher as lch
from adw_modules import plan_approval
from adw_modules import plan_compiler
from adw_modules import plan_contract_ingress as ingress
from adw_modules import probes
from adw_modules import route_admission as admission
from adw_modules import run_preflight as rp
from adw_modules import review_contract as rc
from adw_modules import provisioning
from adw_modules import review_standards as rvs
from adw_modules import scheduler_types as st
from adw_modules import step_log
from adw_modules import tests_chain as tchain
from adw_modules import tree_materialize as tm
from adw_modules.lifecycle import (
    AmendmentAlreadyApplied,
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
    refuse_historical_artifacts,
    FactoryScheduler,
    LaneContext,
    LaunchFailed,
    OrderedLocks,
    RunPreflightRefused,
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
#: Set while the run preflight drives a real dispatch path. `_launch` stops
#: right after the tree is prepared, before a prompt, a pane or an agent
#: exists, so the preflight asserts the launch path's own decisions about a
#: tree instead of a reimplementation of them.
_PREPARE_ONLY: ContextVar[bool] = ContextVar("prepare_only", default=False)


class _PreparedOnly(Exception):
    """`_launch` reached the end of tree preparation under `_PREPARE_ONLY`."""

    def __init__(self, cwd: Path) -> None:
        super().__init__(str(cwd))
        self.cwd = cwd


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
#: The independent reviewer `run attend` dispatches beside its operator.
_ATTEND_REVIEWER_ROLE = "attend-reviewer"
_DASHBOARD_FIELDS = frozenset(("enabled", "launcher", "api_port", "ui_port", "open"))
_ATTEND_FIELDS = frozenset(
    (
        "max_amendments_per_lane",
        "max_amendments_per_run",
        "max_review_rounds",
        "route",
        "reviewer_route",
        "reviewer_vendor",
        "planctl",
        "plan_ir",
        "planctl_root_dir",
        "validate_argv",
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


#: A reused role session whose transcript passes this many tokens is replaced
#: by a fresh one seeded with a compact handoff. `0` turns the cap off.
DEFAULT_ROLE_SESSION_MAX_TOKENS = 1_000_000


#: Seconds a dispatched agent may sit `idle` with no envelope and a transcript
#: that does not grow, and no background shell or monitor of its own, before
#: the dispatch is refused `DISPATCH_NOT_CONSUMED`.
#: `0` turns the bound off.
DEFAULT_DISPATCH_IDLE_TIMEOUT_S = 600


def _config_dispatch_idle_timeout_s(value: object, label: str) -> int:
    """`dispatch_idle_timeout_s`: the idle bound on a dispatch. Absent keeps the default."""
    if value is None:
        return DEFAULT_DISPATCH_IDLE_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MaestroConfigurationError(label + " must be an integer >= 0")
    return int(value)


def _config_role_session_max_tokens(value: object, label: str) -> int:
    """`role_session_max_tokens`: the largest reused session. Absent keeps the default."""
    if value is None:
        return DEFAULT_ROLE_SESSION_MAX_TOKENS
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MaestroConfigurationError(label + " must be an integer >= 0")
    return int(value)


def _config_timeout(
    value: object, label: str, default: float | None = lch.PROVISION_TIMEOUT_S
) -> float | None:
    """Seconds allowed for one provisioning (or suite) run. Absent keeps the default.

    `suite_timeout_s` passes `default=None`: the suite default lives in
    `tests_chain.SUITE_TIMEOUT_S` and an absent key defers to it.

    A deployment whose `provision_argv` installs several manifests on a cold
    cache legitimately needs longer than the default, and a timeout there is
    reported as a provisioning failure -- which would read as a broken command
    rather than a slow one.
    """
    if value is None:
        return default
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
            _without_vendor(value[role]), "role_routes.{}".format(role)
        )
    return MappingProxyType(canonical)


def _without_vendor(binding: object) -> object:
    """The route binding with its operator-declared `vendor` removed.

    `vendor` is read by `_canonical_role_vendors` into its own mapping. Route
    admission and receipts bind the route mapping, so the declared vendor must
    never ride inside it: a config that adds `vendor:` keeps byte-identical
    canonical routes.
    """
    if isinstance(binding, Mapping) and "vendor" in binding:
        return {key: item for key, item in binding.items() if key != "vendor"}
    return binding


def _canonical_role_vendors(value: object) -> Mapping[str, str | None]:
    """Operator-declared vendor per lane role. Absent is None.

    Normalized (strip, casefold) because `probes.select_acceptor` compares
    them; a probe acceptor is independent of the code reviewer only when both
    vendors are declared and differ, so a route, profile or model difference
    is not read as one. `attend.route.vendor` and `attend.reviewer_vendor`
    are read by `_declared_vendor` under the same rule.
    """
    if not isinstance(value, Mapping):
        raise _MaestroConfigurationError("role_routes must be a mapping")
    vendors: dict[str, str | None] = {}
    for role in lch.LANE_PANE_ROLES:
        binding = value.get(role)
        declared = binding.get("vendor") if isinstance(binding, Mapping) else None
        vendors[role] = _declared_vendor(
            declared, "role_routes.{}.vendor".format(role)
        )
    return MappingProxyType(vendors)


def _declared_vendor(declared: object, label: str) -> str | None:
    """One declared vendor, normalized (strip, casefold). Absent is None."""
    if declared is None:
        return None
    text = _optional_config_string(declared, label).casefold()
    if not text:
        raise _MaestroConfigurationError(label + " must not be blank")
    return text


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
    the same way `concurrency` stays 1 until someone raises it. The routes are
    validated here rather than in `attend.py`, which must not name a route.

    An enabled deployment declares two agents: the author on `attend.route`
    and an independent reviewer on `attend.reviewer_route`. Independence is
    the probe rule: both vendors declared (`attend.route.vendor`,
    `attend.reviewer_vendor`) and different. A route, profile or model
    difference is not read as one, and an undeclared vendor is not
    independent of anything.
    """
    if value is None:
        return MappingProxyType(
            {
                "max_amendments_per_lane": 0,
                "max_amendments_per_run": 10,
                "max_review_rounds": 3,
                "route": MappingProxyType({}),
                "vendor": None,
                "reviewer_route": MappingProxyType({}),
                "reviewer_vendor": None,
                "planctl": None,
                "planctl_root_dir": None,
                "validate_argv": (),
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
    raw_route = value.get("route")
    route = _canonical_role_route(_without_vendor(raw_route), "attend.route")
    vendor = _declared_vendor(
        raw_route.get("vendor") if isinstance(raw_route, Mapping) else None,
        "attend.route.vendor",
    )
    reviewer_route = _canonical_role_route(
        value.get("reviewer_route"), "attend.reviewer_route"
    )
    reviewer_vendor = _declared_vendor(
        value.get("reviewer_vendor"), "attend.reviewer_vendor"
    )
    review_rounds = _config_bound(
        value.get("max_review_rounds"), "attend.max_review_rounds", 3
    )
    if per_lane > 0 and not route:
        raise _MaestroConfigurationError(
            "attend.route is required once attend.max_amendments_per_lane is set"
        )
    if per_lane > 0 and not reviewer_route:
        raise _MaestroConfigurationError(
            "attend.reviewer_route is required once "
            "attend.max_amendments_per_lane is set"
        )
    if per_lane > 0 and review_rounds < 1:
        raise _MaestroConfigurationError(
            "attend.max_review_rounds must be >= 1 once "
            "attend.max_amendments_per_lane is set"
        )
    if reviewer_route:
        if reviewer_vendor is None:
            raise _MaestroConfigurationError(
                "attend.reviewer_vendor is required once attend.reviewer_route is set"
            )
        if vendor is None:
            raise _MaestroConfigurationError(
                "attend.route.vendor is required once attend.reviewer_route is "
                "set: an undeclared author vendor is not independent of the "
                "reviewer"
            )
        if vendor == reviewer_vendor:
            raise _MaestroConfigurationError(
                "attend.reviewer_vendor must differ from attend.route.vendor"
            )
    planctl = _optional_config_string(value.get("planctl"), "attend.planctl")
    if planctl and not Path(planctl).is_absolute():
        raise _MaestroConfigurationError("attend.planctl must be absolute")
    plan_ir = _optional_config_string(value.get("plan_ir"), "attend.plan_ir")
    if plan_ir and not Path(plan_ir).is_absolute():
        raise _MaestroConfigurationError("attend.plan_ir must be absolute")
    root_dir = _optional_config_string(
        value.get("planctl_root_dir"), "attend.planctl_root_dir"
    )
    if root_dir and not Path(root_dir).is_absolute():
        raise _MaestroConfigurationError("attend.planctl_root_dir must be absolute")
    return MappingProxyType(
        {
            "max_amendments_per_lane": per_lane,
            "max_amendments_per_run": per_run,
            "max_review_rounds": review_rounds,
            "route": route,
            "vendor": vendor,
            "reviewer_route": reviewer_route,
            "reviewer_vendor": reviewer_vendor,
            "planctl": Path(planctl) if planctl else None,
            "plan_ir": Path(plan_ir) if plan_ir else None,
            "planctl_root_dir": Path(root_dir) if root_dir else None,
            "validate_argv": _config_argv(
                value.get("validate_argv"), "attend.validate_argv"
            )
            if value.get("validate_argv") is not None
            else (),
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
    # The one place a deployment's keys directory is decided. `keys_dir` names
    # it; without it the keys are where `route_admission.provision_keys` mints
    # them, under the state root. FDAdb's live beside its route receipts in
    # `~/.maestro/FDAdb/keys` while its state root moved, so reading the state
    # root alone refused every reviewer-key read there.
    if "keys_dir" in loaded:
        keys_dir = Path(_config_string(loaded.get("keys_dir"), "keys_dir"))
        if not keys_dir.is_absolute():
            raise _MaestroConfigurationError("keys_dir must be absolute")
        loaded["keys_dir"] = keys_dir
    else:
        loaded["keys_dir"] = root / "keys"
    # Vendors first: they are read off the raw bindings, and the canonical
    # routes that replace them carry no `vendor`.
    loaded["role_vendors"] = _canonical_role_vendors(loaded.get("role_routes"))
    loaded["role_routes"] = _canonical_role_routes(loaded.get("role_routes"))
    loaded["provision_argv"] = _config_argv(
        loaded.get("provision_argv"), "provision_argv"
    )
    loaded["provision_timeout_s"] = _config_timeout(
        loaded.get("provision_timeout_s"), "provision_timeout_s"
    )
    loaded["suite_timeout_s"] = _config_timeout(
        loaded.get("suite_timeout_s"), "suite_timeout_s", None
    )
    loaded["dashboard"] = _canonical_dashboard(loaded.get("dashboard"))
    loaded["attend"] = _canonical_attend(loaded.get("attend"))
    loaded["concurrency"] = _config_concurrency(
        loaded.get("concurrency"), "concurrency"
    )
    loaded["role_session_max_tokens"] = _config_role_session_max_tokens(
        loaded.get("role_session_max_tokens"), "role_session_max_tokens"
    )
    loaded["dispatch_idle_timeout_s"] = _config_dispatch_idle_timeout_s(
        loaded.get("dispatch_idle_timeout_s"), "dispatch_idle_timeout_s"
    )
    # One key, read where the tool reads it, so a deployment cannot be told two
    # different things about when a tests lane stops.
    try:
        loaded["stall_regression_on_findings"] = st.stall_regression_on_findings(
            loaded
        )
    except ValueError as exc:
        raise _MaestroConfigurationError(str(exc)) from exc
    try:
        loaded["review_max_probe_rounds"] = st.review_max_probe_rounds(loaded)
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

#: The file in a role's `session` dir naming the turn its live session began at.
_SESSION_EPOCH_FILE = "session-epoch"


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

    The suite paths written into an operator tree come off an artifact, and a
    `..` component in one of them would write outside the tree the role
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


#: Prompt sections whose size scales with the plan rather than with the role.
#: A tests lane's `obligations` carries its paired build lanes' whole obligation
#: sets, so one section reached 547 KB and put a test-reviewer prompt at ~287k
#: tokens against a 262k window (PROMPT_TOO_LARGE). Each section is written to
#: its own file beside the prompt and the prompt carries a reference instead,
#: so the launch size check measures the bytes that enter the prompt.
PLAN_SCALED_PROMPT_SECTIONS: tuple[tuple[str, ...], ...] = (
    ("lane_spec", "obligations"),
    ("lane_spec", "interface"),
    ("public_contract", "acceptance_criteria"),
    ("public_contract", "interface"),
    ("public_acceptance",),
)

_CONTEXT_FILE_INSTRUCTION = (
    " A prompt value of the form {\"context_file\": <path>, \"sha256\": ..., "
    "\"bytes\": ...} is a section of this prompt stored as JSON in that file "
    "inside CWD. Read every such file before you act; it carries the same "
    "authority as the prompt text."
)


def write_role_prompt(prompt: Path, sections: Mapping[str, Any]) -> None:
    """Write a role prompt, moving plan-scaled sections into context files.

    The files live under `.maestro-agent/context/<prompt stem>/`, which is the
    harness's own directory inside the role's CWD: never a product path, never
    collected as role output, and one directory per turn so a prompt and the
    sections it references are never out of step.
    """
    body: dict[str, Any] = st.json_ready(sections)
    context = prompt.parent / "context" / prompt.stem
    moved = False
    for section in PLAN_SCALED_PROMPT_SECTIONS:
        parent: Any = body
        for key in section[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict) or section[-1] not in parent:
            continue
        data = st.canonical_bytes(parent[section[-1]])
        path = context / (".".join(section) + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        parent[section[-1]] = {
            "context_file": str(path.resolve()),
            "sha256": st.digest_bytes(data),
            "bytes": len(data),
        }
        moved = True
    if moved:
        body["instructions"] = str(body.get("instructions") or "") + (
            _CONTEXT_FILE_INSTRUCTION
        )
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_bytes(st.canonical_bytes(body))


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
    "is REVISE. The violated_requirement field is checked mechanically and "
    "must quote the public contract verbatim; a finding that does not quote "
    "it is sent back once, and a second miss parks the lane.\n"
)


def _clear_precreated_role_cwd(dest: Path) -> bool:
    """Empty a precreated role cwd without replacing its process-bound inode."""
    if not _precreated_role_cwd(dest):
        return False
    tm.clear_tree(dest)
    return True


#: Where a test double may stand, and where it may not. Appended to both tester
#: rules, because a `tests` lane and an untyped validator lane draw the same seam.
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


_PROBE_VIEW_KEYS = ("probe_id", "path", "requirement", "intent")


class HerdrStageActor:
    """Persistent role panes. Envelope bytes are payload, not stage."""

    role_session_max_tokens: int = DEFAULT_ROLE_SESSION_MAX_TOKENS
    dispatch_idle_timeout_s: int = DEFAULT_DISPATCH_IDLE_TIMEOUT_S

    def __init__(
        self,
        launcher: lch.LauncherAdapter,
        state_root: Path,
        target: gitpub.TargetBinding,
        role_routes: Mapping[str, Mapping[str, str]],
        lane_specs: Mapping[str, Mapping[str, Any]] | None = None,
        operator_route: Mapping[str, str] | None = None,
        role_vendors: Mapping[str, str | None] | None = None,
        attend_reviewer_route: Mapping[str, str] | None = None,
        role_session_max_tokens: int = DEFAULT_ROLE_SESSION_MAX_TOKENS,
        dispatch_idle_timeout_s: int = DEFAULT_DISPATCH_IDLE_TIMEOUT_S,
    ) -> None:
        self.role_session_max_tokens = int(role_session_max_tokens)
        self.dispatch_idle_timeout_s = int(dispatch_idle_timeout_s)
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
        if attend_reviewer_route:
            routes[_ATTEND_REVIEWER_ROLE] = MappingProxyType(
                dict(attend_reviewer_route)
            )
        self.role_routes = MappingProxyType(routes)
        # Beside the routes, never inside them: `routes` is what route
        # admission binds, and the declared vendor only decides whether a
        # probe acceptor is independent of the code reviewer.
        self.role_vendors = MappingProxyType(dict(role_vendors or {}))
        self._probe_stash: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._probe_stash_lock = threading.Lock()
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
        if role in ("code-reviewer", "test-reviewer") and ctx.candidate_sha:
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

    def _schema(
        self, role: str, *, with_probes: bool = False, mode: str | None = None
    ) -> dict[str, Any]:
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
        if mode in (probes.MODE_ACCEPT, probes.MODE_CHALLENGE):
            # One object inside a list, for the same reason as `findings`.
            return {
                "verdict": "PASS|REVISE",
                "probe_verdicts": [
                    {
                        "probe_id": "<probe_id, exactly as given>",
                        "verdict": "PASS|REVISE",
                        "reason": "<why>",
                    }
                ],
            }
        if role == "tester":
            return {"test_files": {"<path>": "<utf-8 contents>"}}
        if role == "builder":
            return {
                "candidate_sha": "<optional git sha>",
                "changed": "<optional bool>",
                "suite_defects": [
                    {
                        "test_id": "<optional: a failing case id, exactly as in failure_output>",
                        "reason": "<why no implementation satisfying public_contract can pass it>",
                    }
                ],
            }
        if role == "code-reviewer":
            # The optional keys ride inside the one example object; appended
            # as bare strings they re-create the flattening described above.
            example = dict(findings[0])
            example["axis"] = "<optional: {0}>".format("|".join(st.FINDING_AXES))
            example["severity"] = "<optional: {0}>".format(
                "|".join(st.FINDING_SEVERITIES)
            )
            example["attribution"] = "<optional: {0}>".format(
                "|".join(st.FINDING_ATTRIBUTIONS)
            )
            example["test_id"] = (
                "<optional: a failing_test_ids or suite_defect_claims id, exactly as given>"
            )
            envelope: dict[str, Any] = {
                "verdict": "PASS|REVISE",
                "findings": [example],
            }
            if with_probes:
                envelope["probes"] = [
                    {
                        "path": "<optional: path of a probe file you wrote>",
                        "requirement": "<verbatim public_contract quote>",
                        "intent": "<what the probe asserts>",
                    }
                ]
                envelope["challenges"] = [
                    {
                        "probe_id": "<optional: id of an existing probe>",
                        "reason": "<why that probe is invalid>",
                    }
                ]
            return envelope
        if role == "test-reviewer":
            return {"verdict": "PASS|REVISE", "findings": findings}
        if role == "operator":
            return {
                "revision_path": "<revision_out_path, exactly as given>",
                "rationales": [
                    {key: "<{0}>".format(key) for key in att.RATIONALE_KEYS}
                ],
                "runtime_defects": [
                    {
                        "lane": "<required: the id of a parked LANE_FAULT lane>",
                        "detail": "<why no plan edit can fix this fault>",
                    }
                ],
            }
        if role == _ATTEND_REVIEWER_ROLE:
            return {
                "verdict": "PASS|REVISE",
                "findings": [
                    {key: "<{0}>".format(key) for key in att.REVIEW_FINDING_KEYS}
                ],
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
    #: (`scheduler._collect_draft`), whatever the lane kind.
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
        "alone does not declare a callable. public_contract.interface is the "
        "declared binding authority: each entry names the module, the export "
        "and its signature the suite may call. Tests must consume that "
        "declared interface, not invent an unstated binding or require an "
        "alias chosen only inside a test. Test reviewers must report such a "
        "binding or an ambiguous public interface through existing actionable "
        "REVISE findings about contract adequacy, not prescribe product "
        "implementation or pass a guessed binding. Builders implement the "
        "public interface; do not spray aliases to satisfy a name the "
        "accepted suite happens to use when the public contract does not "
        "declare it -- the reviewer grades against the contract, and a suite "
        "that binds to an undeclared name is a finding against the suite, "
        "not a licence."
    )

    def _materialize_role_instructions(
        self, cwd: Path, role: str, route: str, lane_kind: str | None = None
    ) -> Path:
        if role == "tester" and lane_kind == st.LANE_KIND_TESTS:
            tester_rule = (
                "Inspect the product checkout without modifying product files. "
                "Return acceptance files only through the requested envelope. "
                "Author files exactly at declared_outputs. Returned test_files "
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
                "Return test files only through the requested envelope. "
                "Test paths must not collide with declared product outputs. "
                "Write validator/meta-test files that exercise builder "
                "outputs; never replace those outputs. On correction turns, "
                "apply revise_findings to those validators; do not claim a "
                "finding is fixed by resubmitting byte-identical files.\n"
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
                "Review the TEST_DRAFT tests against lane-plan obligations. "
                "This checkout is the draft candidate: the integration head "
                "plus the draft's files, which the per-turn JSON lists as "
                "test_files. Check behavior coverage, satisfiability/non-vacuity, "
                "and deterministic isolation. Return PASS or REVISE with "
                "actionable findings for the tester. Never review or prescribe "
                "product implementation. Review only the test_files; product "
                "files are context and out of scope. A validator failing "
                "against the base is expected when falsifiability requires "
                "red-at-base. The suite you accept is carried, byte for byte, "
                "into the builder's checkout and is what the builder's candidate "
                "is measured against; the builder will read it.\n"
                + self._PROHIBITION_REVIEW_RULE
                + "\n"
                + TEST_CRAFT_REVIEWER_QUESTION
            ),
            "builder": (
                "## Builder obligations\n"
                "Modify only the declared product outputs. The accepted test "
                "suite this lane is graded against is in this checkout at the "
                "paths the per-turn JSON lists as test_paths: read it. You may "
                "not create, edit, delete, rename or copy any of those paths; a "
                "candidate whose delta names one is refused before any reviewer "
                "reads it (CANDIDATE_TEST_PATH_REFUSED), and the review runs "
                "the suite only after proving the bytes at those paths are the "
                "accepted ones (TEST_SUITE_TAMPERED). The code reviewer grades "
                "the candidate against the plan's acceptance criteria and the "
                "public contract, not against the suite."
            ),
            "code-reviewer": (
                "## Code-reviewer obligations\n"
                "Review the exact product candidate and declared outputs against "
                "the lane plan. Check implementation correctness, regressions, "
                "security, and maintainability. Return PASS or REVISE with "
                "actionable findings for the builder, each resolvable by editing "
                "declared outputs only. Never prescribe changes to files outside "
                "declared outputs. The accepted test suite is in this checkout "
                "at the paths listed as test_paths, and the harness has already "
                "run it against this exact candidate: suite_result_summary is "
                "that measurement. Read the suite to locate a failure; grade "
                "the candidate against public_contract and the lane plan's "
                "acceptance criteria, not against the suite. An assertion that "
                "contradicts the public contract is a finding you report, not a "
                "test edit you demand. When a failure is a defect in the "
                "accepted suite itself -- a fixture, harness or example the "
                "suite builds wrongly, which no candidate could satisfy -- set "
                "that finding's attribution to accepted_suite and start "
                "implementation_area with the suite path and line, "
                "<path>:<line>. Such a finding sends the suite back to its "
                "tests lane, whose test reviewer still grades the repair "
                "against the unchanged public contract; it is never a way to "
                "relax an assertion the contract supports. The "
                "violated_requirement field is checked mechanically and must "
                "quote the public contract verbatim; a finding that does not "
                "quote it is sent back once, and a second miss parks the "
                "lane.\n"
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
                "Every lane in `parked_lanes` is waiting on you, and one "
                "revision answers all of them. A `NO_PROGRESS` lane parked "
                "because its reviewed rounds stopped moving against the "
                "contract it was given; an `AMENDMENT_REQUIRED` lane parked "
                "because the final integration review named it, and its "
                "findings are in that lane's `final_review.json`. For each, "
                "name the gap between what the accepted suite (or the final "
                "review) says and what the contract says, and close it with "
                "the smallest edit that reaches that lane's projection -- "
                "normally one seam contract on the lane's own carrier. An "
                "`AMENDMENT_REQUIRED` lane's spec must change, or the "
                "amendment is refused.\n"
                "A `LANE_FAULT` lane parked because harness code raised while "
                "running it; the exception is `fault` in that lane's "
                "`reviews.json`. If the plan caused it -- an oversized or "
                "mis-specified lane -- answer it with the same kind of edit "
                "and a rationale. If no plan edit can fix it, it is a runtime "
                "defect: list it under `runtime_defects` as "
                "`{\"lane\": \"<parked LANE_FAULT lane id>\", \"detail\": "
                "\"<the defect>\"}` -- `lane` is required, and only a "
                "parked `LANE_FAULT` lane can be one -- give it no rationale, "
                "and leave its "
                "contract alone. That lane stays parked and an issue is "
                "filed; when every parked lane is a runtime defect, write no "
                "revision.\n"
                "Each lane's accepted acceptance suite is in this checkout "
                "under `suite/<lane_id>/`; the builder and every reviewer read "
                "the same files in their own checkouts. Stating an expectation "
                "the suite asserts, as a contract clause in your own words, is "
                "exactly what you are here to do; pasting the test file into "
                "the contract is not.\n"
                "Write the revision IR at the path the per-turn JSON names, "
                "and nowhere else. Do not edit the current IR in place. Do not "
                "run any `maestro` verb, any git command, or `planctl` -- an "
                "independent reviewer reads your revision, and the harness "
                "validates, mints the receipt, projects, and applies. "
                "Do not add, remove, or rewire a lane: an edit that reaches "
                "any lane outside allowed_lane_ids is refused whole, and no "
                "lane gets anything.\n"
                "When `reviewer_findings_path` is given, the reviewer said "
                "REVISE to your previous revision (`previous_revision_path`): "
                "fix every located finding in it.\n"
                "When `envelope_error` is given, your previous envelope was "
                "refused with exactly that error: return a corrected envelope "
                "for the same turn, keeping a revision that was already "
                "valid.\n"
                "Return one rationale per parked lane in the envelope. "
                "`contract_gap` must name the clause that was missing or "
                "wrong, not restate that the lane failed."
            ),
            _ATTEND_REVIEWER_ROLE: (
                "## Attended-amendment reviewer obligations\n"
                "You are the independent reviewer of a plan revision another "
                "agent authored to clear parked lanes. You did not write it "
                "and you do not edit it. Read the revision against the "
                "current IR it revises, the planctl question surface of the "
                "revision, the author's rationales, and each parked lane's "
                "evidence under `lanes/<lane_id>/` and `suite/<lane_id>/`.\n"
                "Return REVISE when the revision does not close each parked "
                "lane's gap, weakens a check, verdict or error path, reaches "
                "a lane outside allowed_lane_ids, or leaves a two-"
                "implementations pair or interface defect unresolved. Every "
                "REVISE finding names its `location` (a JSON pointer into the "
                "revision IR), the `defect`, and the `required_change`. PASS "
                "requires findings=[].\n"
                "When `envelope_error` is given, your previous envelope was "
                "refused with exactly that error: return a corrected "
                "envelope for the same review.\n"
                "Do not run any `maestro` verb, any git command, or `planctl`."
            ),
            "integration-reviewer": (
                "Review the exact integration checkout read-only. Return a "
                "verdict, findings, and only genuinely affected lane IDs. "
                "Every tests lane's accepted suite is in this checkout: each "
                "tests lane merged its suite into the integration ref before "
                "the build lane that consumes it started, so read each suite "
                "beside the code it judged. The harness measures every "
                "run-level gate itself, against this same integration SHA, "
                "before you are asked, and a gate that failed reaches you as "
                "a REVISE you are not consulted about. Do not run or re-run a "
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
            "- Delegation is allowed: split disjoint work across subagents to save context (Claude: `/team`; omp: its own subagents). Subagents obey this contract; you own the envelope.\n"
            "- Run tests and waits in the foreground. Never end a turn while a background shell or monitor you started is still running: the harness reads an ended turn as finished work, and an idle agent with no envelope is refused.\n"
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
    def _suite_counts_red(counts: Mapping[str, Any]) -> bool:
        """Whether the measured suite disagrees with the candidate.

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

        These are the runner's own lines, verbatim. They name the case, the
        error class and the assertion; the suite itself is in the checkout,
        so the builder can open the case at the line the runner names.
        """
        shown = "\n".join("  {0}".format(line) for line in lines)
        return (
            " The accepted suite reported these failures against your last "
            "candidate, verbatim from the runner:\n{0}\nFix the causes named "
            "here. They are the actual errors, not a summary of them; open the "
            "named case in the suite under test_paths to read the assertion in "
            "full. Do not guess at failures that are not listed.".format(shown)
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
            " bound_surface records names observed in the accepted suite; it "
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
            " This is an index into the suite in your checkout, not a second "
            "contract. Derive the behavior from public_contract and implement "
            "it; a value hardcoded to satisfy one assertion is not the contract "
            "and the reviewer grades against the contract."
        )
        return text

    def _probes_offered(self, ctx: LaneContext) -> bool:
        """Whether this code review is asked to author probes.

        Needs budget left (`max_probe_rounds` 0, or exhausted, is zero) and an
        acceptor whose declared vendor differs from the code-reviewer's. With
        either missing the reviewer's ordinary verdict is all that is asked
        for, so nothing it writes could be admitted and the mandate is omitted.
        """
        if int(getattr(ctx, "probe_budget_left", 0) or 0) <= 0:
            return False
        return not isinstance(
            probes.select_acceptor(self.role_vendors), probes.Rejection
        )

    @staticmethod
    def _probe_instruction(extra: Mapping[str, Any]) -> str:
        text = (
            " Probes. In addition to your verdict you may author up to "
            "{0} probes: small tests, written as files in THIS checkout, that "
            "fail on this candidate because it breaks the public contract. A "
            "probe is how a defect you can name becomes something the builder "
            "can run. Rules: (1) requirement is a verbatim quote of "
            "public_contract, at least 12 characters, and the probe's "
            "expectation must follow from that quote and from nothing you "
            "learned by reading the candidate; (2) import only the modules and "
            "names the public contract's interface declares, so the probe "
            "binds what the plan promises and not the candidate's internals; "
            "(3) write each probe file at a new path that is not a declared "
            "output, not one of test_paths and not an existing probe, and "
            "list it in `probes` with its requirement and intent; (4) use the "
            "same runner as the accepted suite, with at most {1} cases per "
            "probe; (5) do not edit any tracked file. The harness runs each "
            "probe against this candidate. A probe that passes, errors, or "
            "does not follow from its quote is dropped and never reaches the "
            "builder; a failing one is judged by a reviewer from another "
            "vendor before it binds the builder. To retire an existing probe "
            "you believe is wrong, list it in `challenges` with the reason; "
            "you cannot retire one yourself. Your PASS/REVISE verdict is not "
            "changed by any of this."
        ).format(probes.MAX_PROBES_PER_ROUND, probes.MAX_PROBE_CASES)
        existing = extra.get("existing_probes")
        if existing:
            text += (
                " existing_probes lists probes already binding the builder; "
                "do not resubmit them."
            )
        rejected = extra.get("rejected_probes")
        if rejected:
            text += (
                " rejected_probes lists submissions from your previous answer "
                "that were refused, each with its code. Fix or drop them and "
                "answer again; accepted ones are kept."
            )
        return text

    @staticmethod
    def _acceptance_instruction(mode: str) -> str:
        decision = (
            "A PASS means the probe is valid and stands; a REVISE means it is "
            "invalid and is not admitted."
            if mode == probes.MODE_ACCEPT
            else "A PASS means the probe is valid and stays binding; a REVISE "
            "means it is invalid and is retired."
        )
        return (
            " This turn is a probe acceptance, not an authoring turn: write "
            "nothing in this checkout, create no files, and run no git "
            "command that changes it. This checkout is the candidate. For "
            "each entry in probes, read its source, its requirement and the "
            "failure the harness measured, and answer one question: does the "
            "assertion follow from the cited requirement in public_contract, "
            "with its expected value derived from the contract and not from "
            "reading the candidate, and is the measured failure an assertion "
            "failure of the candidate rather than a fault in the probe? "
            + decision
            + " Return exactly one probe_verdicts entry per probe_id given, "
            "no other ids and no duplicates, and a top-level verdict that is "
            "PASS only if every entry is PASS."
        )

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
        mode = extra.get("mode")
        acceptance = (
            role in probes.ACCEPTOR_ROLES
            and mode in (probes.MODE_ACCEPT, probes.MODE_CHALLENGE)
        )
        mandate = role == "code-reviewer" and self._probes_offered(ctx)
        schema = self._schema(
            role, with_probes=mandate, mode=mode if acceptance else None
        )
        instructions = (
            "Work only inside CWD. Local file operations stay in CWD. Native "
            "Read, Write, Edit, Bash, skills, and MCP remain available. "
            "Subagents may be used; they stay inside CWD too. Create UTF-8 JSON at {0}.part, "
            "then rename it to {0}, then stop. The rename declares the turn "
            "finished; never write into {0} directly, because a reader polling "
            "it reads a partial file as a failed turn. Schema: {1}. CWD: {2}."
        ).format(envelope_path, json.dumps(schema, sort_keys=True), cwd.resolve())

        if acceptance:
            instructions += self._acceptance_instruction(str(mode))
        elif role == "tester":
            if ctx.lane.lane_kind == st.LANE_KIND_TESTS:
                instructions += (
                    " Author acceptance files exactly at "
                    "declared_outputs. Returned test_files paths must equal "
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
                    " Create tests here. Do not git commit. Do not write "
                    "the product repo. Test paths must be validator/meta-test "
                    "files, never declared_outputs."
                )
                if extra.get("revise_findings"):
                    instructions += (
                        " Apply revise_findings to those validators. Do not "
                        "claim a finding is fixed by resubmitting byte-identical "
                        "files."
                    )
            paths = extra.get("accepted_suite_paths")
            if paths:
                instructions += (
                    " The accepted suite is in this checkout at: {0}. Start "
                    "from it and change only what revise_findings cite by "
                    "path:line or node id"
                ).format(", ".join(str(item) for item in paths))
                if extra.get("spec_diff") is not None:
                    instructions += (
                        ", plus tests bound to the items spec_diff lists as "
                        "added, removed or changed"
                    )
                instructions += (
                    ". Do not remove, rewrite or reorder any other test or "
                    "helper, and do not rewrite the suite: the harness "
                    "compares your draft with the accepted suite per test and "
                    "refuses SUITE_REDRAFT_OUT_OF_SCOPE for a changed or "
                    "removed unit nothing names. Add a test only when a "
                    "finding or a new or changed spec item requires it."
                )
        elif role == "test-reviewer":
            instructions += (
                " This checkout is the TEST_DRAFT candidate. Review only the "
                "draft files listed in test_files. Product files in this "
                "checkout are context and out of scope. A validator failing "
                "against the base is expected when falsifiability requires "
                "red-at-base; do not demand edits to declared product outputs. "
                "Return PASS or REVISE findings."
            )
        elif role == "builder":
            instructions += (
                " Edit only declared_outputs. Do not git commit; the broker "
                "commits those files."
            )
            paths = extra.get("test_paths")
            if isinstance(paths, Sequence) and not isinstance(paths, str) and paths:
                instructions += (
                    " The accepted test suite you are graded against is in this "
                    "checkout at: {0}. Read it. Do not create, edit, delete, "
                    "rename or copy any of those paths: a candidate whose delta "
                    "names one is refused before review (CANDIDATE_TEST_PATH_"
                    "REFUSED). The code reviewer grades against public_contract "
                    "and the plan's acceptance criteria, not against the suite. "
                    "If a failing case cannot be passed by any implementation "
                    "that satisfies public_contract, do not shape the product "
                    "to it: list it in suite_defects with its exact test_id and "
                    "the reason. The code reviewer must rule on every claim, and "
                    "a case it attributes to the accepted suite goes back to the "
                    "tests lane. Omit suite_defects otherwise."
                ).format(", ".join(str(item) for item in paths))
            surface = extra.get("bound_surface")
            if isinstance(surface, Mapping):
                instructions += self._bound_surface_instruction(surface)
            failures = extra.get("failure_output")
            if isinstance(failures, Sequence) and not isinstance(failures, str):
                lines = [str(item) for item in failures if str(item).strip()]
                if lines:
                    instructions += self._failure_instruction(lines)
        elif role == "code-reviewer":
            instructions += (
                " Inspect this candidate product checkout. Findings must be "
                "resolvable within declared_outputs. The accepted test suite "
                "is in this checkout at test_paths; the harness has already "
                "measured it against this candidate. Grade against "
                "public_contract, not against the suite."
            )
            counts = extra.get("suite_result_summary")
            red = isinstance(counts, Mapping) and self._suite_counts_red(counts)
            if isinstance(counts, Mapping) and counts.get("crashed"):
                instructions += (
                    " The accepted suite's runner process did not finish: {0}. "
                    "Its counts are not a measurement of this candidate. The "
                    "runner's last lines: {1}"
                ).format(
                    counts["crashed"],
                    " | ".join(str(line) for line in counts.get("crash_tail") or ())
                    or "(none)",
                )
            elif isinstance(counts, Mapping) and counts.get("crash_note"):
                instructions += (
                    " The accepted suite's runner finished every case and then "
                    "died: {0}. The counts are the per-case results and stand; "
                    "the crash came after them and is not a finding against "
                    "this candidate. The runner's last lines: {1}"
                ).format(
                    counts["crash_note"],
                    " | ".join(str(line) for line in counts.get("crash_tail") or ())
                    or "(none)",
                )
            if red:
                instructions += (
                    " suite_result_summary is the already-measured result of "
                    "the accepted suite against THIS candidate: "
                    "{0} executed, {1} passed, {2} failed, {3} errored. The "
                    "suite is red, so the correct verdict is REVISE and PASS "
                    "will be overridden. Read the failing cases in the suite "
                    "under test_paths, then read the candidate against "
                    "public_contract and declared_outputs and say WHICH code "
                    "is wrong and HOW to fix it. Every candidate finding must name a "
                    "file in declared_outputs and the specific behavior that "
                    "is wrong -- a missing branch, an unhandled input, a "
                    "contract clause the code does not satisfy. Do not restate "
                    "that tests failed; the builder already knows that and "
                    "cannot act on it."
                ).format(
                    counts.get("executed", 0),
                    counts.get("passed", 0),
                    counts.get("failed", 0),
                    counts.get("errored", 0),
                )
            failing = [str(item) for item in extra.get("failing_test_ids") or ()]
            claims = [
                item for item in extra.get("suite_defect_claims") or ()
                if isinstance(item, Mapping)
            ]
            if failing or claims:
                instructions += (
                    " A REVISE must rule on every case in failing_test_ids and "
                    "in suite_defect_claims: for each, give a finding whose "
                    "test_id is that id exactly, with attribution candidate "
                    "(the candidate is wrong; say how to fix it) or "
                    "accepted_suite (no implementation satisfying "
                    "public_contract can pass it; say why in observed_behavior). "
                    "One finding per id. A case left unnamed is sent back once, "
                    "and a second miss parks the lane."
                )
            if claims:
                instructions += (
                    " The builder claims these cases are defects in the accepted "
                    "suite; rule on each against public_contract, not against "
                    "the builder's word: {0}"
                ).format(
                    " | ".join(
                        "{0}: {1}".format(item.get("test_id"), item.get("reason"))
                        for item in claims
                    )
                )
            unattributed = [str(item) for item in extra.get("unattributed_tests") or ()]
            if unattributed:
                instructions += (
                    " Your previous REVISE did not rule on these cases: {0}. "
                    "Re-review and give each one a finding whose test_id is that "
                    "id exactly, attributed candidate or accepted_suite."
                ).format(", ".join(unattributed))
            if extra.get("suite_findings_required") and not red:
                instructions += (
                    " Your previous answer for this candidate was REVISE "
                    "with no finding against public_contract; standards "
                    "findings cannot send a lane back. Return PASS, or "
                    "REVISE with at least one finding that is not on the "
                    "standards axis, naming a file in declared_outputs and "
                    "the public_contract clause it violates."
                )
            elif extra.get("suite_findings_required"):
                instructions += (
                    " Your previous answer for this candidate carried no "
                    "actionable finding while the suite was red. That "
                    "left the builder with nothing to change. Return REVISE "
                    "with at least one finding naming a file in "
                    "declared_outputs and the concrete change it needs."
                )
        elif role == "operator":
            instructions += (
                " Read current_ir_path and, for every lane in parked_lanes, "
                "its accepted suite under suite/<lane_id>/ and its gate "
                "table, reviews and final review under lanes/<lane_id>/. "
                "Write one revised IR answering every parked lane at "
                "revision_out_path and return that exact path plus one "
                "rationale per parked lane, except a LANE_FAULT lane you "
                "list under runtime_defects instead. Change only lanes in "
                "allowed_lane_ids. Run no maestro verb, no git command, and "
                "no planctl. A tests lane stalled on an undeclared binding is "
                "repaired by declaring extensions.maestro.interfaces entries "
                "for the paired build lane, naming each module, export and "
                "signature the suite may call. For every claim a tests lane "
                "discharges, make sure no two implementations can satisfy "
                "every decided_by example and the observation_seam yet differ "
                "observably: the independent reviewer asks exactly that."
            )
        elif role == _ATTEND_REVIEWER_ROLE:
            instructions += (
                " Read current_ir_path, revision_ir_path, question_surface_path, "
                "rationales_path, and for every lane in parked_lanes its "
                "evidence under lanes/<lane_id>/ and suite/<lane_id>/. "
                + att.TWO_IMPLEMENTATIONS_QUESTION
                + " Then return verdict PASS or REVISE; REVISE carries at "
                "least one finding with location, defect and required_change."
            )
        if mandate:
            instructions += self._probe_instruction(extra)
        if role in ("tester", "test-reviewer", "builder") and not acceptance:
            instructions += " " + self._PUBLIC_INTERFACE_RULE
        if (
            role in ("test-reviewer", "code-reviewer", "integration-reviewer")
            and not acceptance
        ):
            instructions += " PASS requires findings=[]. REVISE requires at least one actionable finding."
            rejected = getattr(ctx, "rejected_citation", "")
            if rejected:
                instructions += (
                    " Your previous answer was rejected: its violated_requirement "
                    "{0} is not a verbatim substring of the public contract. "
                    "Re-review, and for every finding copy the exact words of "
                    "the contract sentence it violates into violated_requirement, "
                    "character for character, at least 12 characters, with no "
                    "paraphrase, summary, or added words. A second finding that "
                    "does not quote the contract parks the lane (the integration "
                    "reviewer: stops the run)."
                ).format(json.dumps(rejected[:300]))
        if role == "integration-reviewer":
            instructions += " PASS requires affected_lanes=[]. REVISE requires a nonempty affected_lanes subset."
        if extra.get("envelope_error"):
            instructions += (
                " Your previous envelope was refused: {0}. Write a corrected "
                "envelope; this is the one correction turn."
            ).format(extra["envelope_error"])
        if extra.get("session_handoff"):
            instructions += (
                " This is a fresh session replacing one that grew past its "
                "size cap. session_handoff summarizes the earlier work; this "
                "checkout holds its current state. Rely on those and on this "
                "prompt, not on anything you assume was said before."
            )
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
        passes a scope: its test files must equal its declared outputs, so
        an unscoped sweep read a toolchain byproduct the role never declared
        -- a package manager's lockfile, a stray `__pycache__` entry -- as
        role output and refused TYPED_TEST_OUTPUTS for a file no role wrote.
        An untyped lane declares product paths its tester does not write to,
        and the unscoped sweep is how that lane's tests are delivered, so it
        passes no scope and keeps the whole-tree behaviour.

        A scope also reads its tracked, clean paths (`-c`). The declared
        outputs are the draft, whatever git thinks changed: a tester that
        rewrites an already-merged suite byte for byte leaves nothing
        modified, and dropping those paths refused the draft as empty before
        `write_test_draft` could admit it as the zero-delta `changed=false`
        edge. A declared path that exists nowhere is still absent, so a
        tester that wrote nothing still yields no files.
        """
        requested = tuple(str(item) for item in outputs)
        pathspec = ("--",) + requested if requested else ()
        tracked = ("-c",) if requested else ()
        listed = self._git(
            checkout,
            "ls-files",
            *tracked,
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

    def _commit_declared(
        self,
        checkout: Path,
        outputs: Sequence[str],
        base: str,
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
            pathspec = self._safe_role_pathspec(
                checkout,
                tuple(item for item in listed.split("\0") if item),
                self._deleted_tracked(checkout, *requested),
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
        return candidate, True

    def _restore_draft(
        self, checkout: Path, draft_sha: str, files: Sequence[str]
    ) -> None:
        """Write `files` as they are at `draft_sha` into the working tree only.

        The index stays at the refreshed head, so the restored files read as
        the role's uncommitted output exactly as if it had just written them.
        A path absent at `draft_sha` was deleted there, and is deleted here.
        """
        present = tuple(
            line
            for line in self._git(
                checkout, "ls-tree", "-r", "--name-only", draft_sha, "--", *files
            ).splitlines()
            if line
        )
        for rel in set(files) - set(present):
            target = _resolved_under(checkout, str(Path(rel).parent)) / Path(rel).name
            if target.is_file() or target.is_symlink():
                target.unlink()
        if not present:
            return
        self._git(checkout, "checkout", draft_sha, "--", *present)
        self._git(checkout, "reset", "-q", "--", *present)

    def _clean_checkout(self, checkout: Path) -> None:
        self._git(checkout, "clean", "-fdx", "-e", lch.ROLE_AGENT_DIR)

    def _with_fixtures(self, ctx: LaneContext, prepare: Callable[[Path], None]) -> Callable[[Path], None]:
        """`prepare`, then copy in the lane's plan-declared fixtures the tree lacks.

        A fixture that is untracked in the target working tree is not in any
        lane worktree, and the refresh's `git clean` removes a copy made
        earlier, so the copy is always the last step.
        """
        spec = self.lane_specs.get(ctx.lane.lane_id) or {}

        def run(path: Path) -> None:
            prepare(path)
            if not (path / ".git").exists():
                return
            for relative in rp.declared_fixture_paths(spec):
                if relative in ctx.lane.declared_outputs or (path / relative).exists():
                    continue
                try:
                    rp.supply_fixture(Path(self.target.target_repository_root), path, relative)
                except (FileNotFoundError, ValueError):
                    pass

        return run

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
    ) -> None:
        """Put the builder's checkout at `base`.

        `base` carries the lane's own accepted suite -- merged there by its
        tests lane, or put there for an untyped lane by the scheduler -- and
        it stays in the working tree for the whole turn: the builder reads
        it. What keeps the builder off it is not this checkout but admission:
        `admit_candidate` refuses a delta that names a protected test path,
        and the review verifies the suite's bytes before running it.
        """
        if not (checkout / ".git").exists():
            raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
        head = self._git(checkout, "rev-parse", "HEAD")
        parent = self._git(checkout, "rev-parse", "HEAD^", check=False)
        dirty = bool(self._git(checkout, "status", "--porcelain"))
        if not dirty and (head == base or parent == base):
            self._clean_checkout(checkout)
            return
        self._commit_declared(checkout, outputs, base)

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
            # source-workspace lookup, but never anchor a role tree there.
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
                        spec_digest=lane.spec_digest,
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

    def _failure_output(self, ctx: LaneContext) -> list[str]:
        """Failure lines carried on the prior code review, if it left any."""
        record = ctx.artifacts.get("CODE_REVIEW")
        payload = getattr(record, "payload", None) or {}
        if not isinstance(payload, Mapping):
            return []
        lines = payload.get("failure_output")
        if not isinstance(lines, Sequence) or isinstance(lines, str):
            return []
        return [str(item) for item in lines if str(item).strip()]

    def _payload_ok(self, role: str, payload: Mapping[str, Any]) -> bool:
        if role in (
            "test-reviewer",
            "code-reviewer",
            "integration-reviewer",
            _ATTEND_REVIEWER_ROLE,
        ):
            return payload.get("verdict") in (
                st.ReviewerVerdict.PASS.value,
                st.ReviewerVerdict.REVISE.value,
            )
        if role == "operator":
            # Asked again here rather than refused later: an envelope with no
            # revision_path is a turn the agent has not finished, and the
            # attend loop's refusal would end the whole verb over it. A turn
            # that declared only runtime defects owes no revision.
            return bool(
                str(payload.get("revision_path") or "").strip()
                or payload.get("runtime_defects")
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
        started = time.monotonic()
        waited = 0.0
        next_beat = 30.0
        activity = getattr(self.launcher, "turn_activity", None)
        limit = int(getattr(self, "dispatch_idle_timeout_s", 0) or 0)
        last_seen: object = None
        quiet_since = 0.0

        def transcript_bytes() -> object:
            if activity is None:
                return None
            try:
                seen = activity(handle)
            except Exception:
                return None
            return seen[1] if seen is not None else None

        def background_live() -> bool:
            if activity is None:
                return False
            try:
                seen = activity(handle)
            except Exception:
                return False
            return seen is not None and seen[0] == lch.BACKGROUND_TASK_STATUS

        def envelope_text() -> object:
            try:
                return _read_regular_text_under(bound, envelope)
            except (OSError, UnicodeError, ValueError):
                return None

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
                    before = transcript_bytes()
                    try:
                        wait(handle)
                    except lch.AgentNotInteractive as exc:
                        # An envelope is not the end of a turn. On run
                        # 2acd2f5f the tester declared, kept editing for a
                        # minute, and rewrote its envelope six seconds after
                        # this wait gave up, so the recorded draft missed its
                        # last edit. A transcript that grew, or an envelope
                        # that changed, during the wait is an agent still
                        # working: wait again. One that did neither is a slow
                        # composer over finished work, and the declaration
                        # stands.
                        if (
                            transcript_bytes() != before
                            or envelope_text() != raw
                            or background_live()
                        ):
                            self._say(
                                lane_id or "-",
                                "{0} still working after declaring".format(role),
                                str(exc),
                            )
                            continue
                        self._say(
                            lane_id or "-",
                            "{0} completion could not be confirmed".format(role),
                            str(exc),
                        )
                    except (
                        lch.HerdrCallError,
                        lch.LaunchRefused,
                    ) as exc:
                        # The declaration is already on disk and already valid;
                        # this wait merely leaves the composer ready for a
                        # possible correction. A vanished or unreadable agent
                        # cannot invalidate the declaration it already wrote.
                        self._say(
                            lane_id or "-",
                            "{0} completion could not be confirmed".format(role),
                            str(exc),
                        )
                    else:
                        # Idle now, and the envelope read above predates the
                        # wait: a turn that rewrote it while finishing is
                        # declared by the rewrite. Read it again.
                        if envelope_text() != raw:
                            continue
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
            #
            # Contract change (strengthening, new path): "visibly waiting" was
            # unbounded, and on run 2acd2f5f an attend operator whose prompt
            # never reached it logged "waiting on operator" for over an hour.
            # One bound is added, and it is not a single transport sample: the
            # agent must read `idle`, with no envelope, and a transcript that
            # has not grown, on every sample for `dispatch_idle_timeout_s`.
            # Any other status, any growth, or an unreadable agent restarts
            # it. The refusal is the typed launch failure every caller already
            # contains (a lane parks LANE_FAULT; attend retries once).
            time.sleep(0.1)
            # Wall time, not a count of sleeps: each pass also reads the
            # envelope and calls herdr, so summing 0.1s ticks ran 2.7x slow
            # on run 2acd2f5f and a 600s bound took 2030s.
            waited = time.monotonic() - started
            # A silent minute is indistinguishable from a hung agent. Say the
            # wait is still a wait, at a cadence that does not flood a terminal.
            if waited >= next_beat:
                next_beat = waited - waited % 30 + 30
                self._say(
                    lane_id or "-",
                    "waiting on {0}".format(role),
                    "{0}s elapsed".format(int(waited)),
                )
                if activity is None or limit <= 0:
                    continue
                try:
                    seen = activity(handle)
                except Exception:
                    seen = None
                if seen is None or seen[0] != "idle" or seen != last_seen:
                    last_seen = seen
                    quiet_since = waited
                    continue
                if waited - quiet_since >= limit:
                    raise LaunchFailed(
                        "DISPATCH_NOT_CONSUMED:{0} idle {1}s with no envelope "
                        "and no transcript activity".format(
                            role, int(waited - quiet_since)
                        ),
                        pane_created=True,
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
        except (lch.HerdrCallError, lch.LaunchRefused):
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

    def _prepared_cwd(
        self, cwd: Path, prepare_cwd: Callable[[Path], None], *, provision: bool = True
    ) -> None:
        """Materialize the tree, then install its dependencies. In that order.

        A tree is provisioned after its *final* materialization, never before
        one. Materializing is destructive by construction --
        `tm.refresh_materialized_commit` unlinks every child of the tree, and a
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
        if not provision or not argv:
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
        provision: bool = True,
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
        session = attempt / "session"
        session.mkdir(exist_ok=True)
        try:
            session_mode = session.lstat().st_mode
        except OSError as exc:
            raise FactoryRefused("ROLE_SESSION_UNSAFE") from exc
        if not stat.S_ISDIR(session_mode):
            raise FactoryRefused("ROLE_SESSION_UNSAFE")
        dispatch_identity = {
            "extra": st.json_ready(dict(extra)),
            "input_artifact_refs": {
                str(kind): str(getattr(record, "artifact_ref", ""))
                for kind, record in sorted(ctx.artifacts.items())
            },
            "input_digest": ctx.input_digest,
            "plan_revision": ctx.plan_revision,
            "rejected_citation": str(getattr(ctx, "rejected_citation", "") or ""),
            "role_key": list(key),
            "run_id": ctx.run_id,
            "stage": ctx.stage.value,
        }
        identity_bytes = st.canonical_bytes(dispatch_identity)

        def identity_path_for(identity_turn: int) -> Path:
            return session / "identity-{}.json".format(identity_turn)

        def same_dispatch(identity_turn: int) -> bool:
            try:
                persisted_identity = json.loads(
                    _read_regular_text_under(
                        session, identity_path_for(identity_turn)
                    )
                )
            except (FactoryRefused, OSError, UnicodeError, ValueError):
                return False
            return persisted_identity == dispatch_identity

        def harvested_payload(identity_turn: int) -> Mapping[str, Any] | None:
            if not same_dispatch(identity_turn):
                return None
            try:
                payload = json.loads(
                    _read_regular_text_under(
                        cwd, lch.role_result_path(cwd, identity_turn)
                    )
                )
            except (FactoryRefused, OSError, UnicodeError, ValueError):
                return None
            if not isinstance(payload, dict):
                return None
            if not self._payload_ok(role, payload):
                return None
            if role == "tester":
                try:
                    # `write_tests` parses this with the same conversion after
                    # a live dispatch. A malformed completed envelope cannot
                    # be adopted merely because it was left on disk.
                    dict(
                        payload.get("test_files")
                        or payload.get("private_files")
                        or {}
                    )
                except (TypeError, ValueError):
                    self._say(
                        ctx.lane.lane_id,
                        "discarded malformed harvested tester envelope",
                        "turn {0}: test_files/private_files cannot be converted "
                        "to a file mapping".format(identity_turn),
                    )
                    return None
            return payload

        highest_existing_turn: int | None = None
        while (
            lch.role_result_path(cwd, turn).exists()
            or lch.role_prompt_path(cwd, turn).exists()
        ):
            highest_existing_turn = turn
            turn += 1
        if highest_existing_turn is not None:
            payload = harvested_payload(highest_existing_turn)
            if payload is not None:
                handle = stored.handle if stored is not None else None
                recovered = stored or _RoleSession(
                    handle, cwd, attempt, None, ctx.run_id
                )
                recovered.turns = highest_existing_turn
                recovered.run_id = ctx.run_id
                self._roles[key] = recovered
                if handle is not None:
                    self._retain_completed(handle, key)
                return payload, handle, cwd
            # The same dispatch, sent and not yet answered: it is still that
            # turn, so it is asked again under that turn's number. Run
            # 2acd2f5f resumed a code reviewer whose turn 3 had no envelope
            # and allocated turn 4 with byte-identical identity; an agent that
            # finishes turn 3 after a resume writes `envelope-3`, which a
            # waiter on `envelope-4` never reads. Re-sending prompt 3 puts any
            # answer -- the one in flight, or a reply pointing at it -- on the
            # path being awaited.
            if (
                not lch.role_result_path(cwd, highest_existing_turn).exists()
                and same_dispatch(highest_existing_turn)
            ):
                turn = highest_existing_turn
        prompt_extra: Mapping[str, Any] = extra
        handoff = self._cap_reused_session(ctx, role, key, stored, cwd, session, turn)
        if handoff is not None:
            stored = None
            prompt_extra = {**extra, "session_handoff": handoff}
        if stored is not None:
            stored.turns = turn
            stored.run_id = ctx.run_id
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
        # while the tester had fixed both in round 2. Nothing could notice:
        # the ledger records the input artifact id rather than the bytes the
        # reader was given, so `input_digest` moved every round while the
        # file did not.
        try:
            self._prepared_cwd(cwd, prepare_cwd, provision=provision)
        except lch.LaunchRefused as refused:
            raise self._launch_failed(refused) from refused
        if _PREPARE_ONLY.get():
            raise _PreparedOnly(cwd)
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
            write_role_prompt(
                prompt, self._prompt(ctx, role, envelope, actual_cwd, prompt_extra)
            )

        def write_identity() -> None:
            identity = identity_path_for(turn)
            partial = identity.with_name(
                ".{0}.{1}.part".format(identity.name, uuid.uuid4().hex)
            )
            fd = -1
            try:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                fd = os.open(str(partial), flags, 0o600)
                with os.fdopen(fd, "wb") as identity_file:
                    fd = -1
                    identity_file.write(identity_bytes)
                    identity_file.flush()
                    os.fsync(identity_file.fileno())
                os.replace(partial, identity)
            except OSError as exc:
                raise FactoryRefused("ROLE_SESSION_WRITE_REFUSED") from exc
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    partial.unlink()
                except FileNotFoundError:
                    pass

        def prepare_adopted_cwd(actual_cwd: Path) -> None:
            adopted = Path(actual_cwd).resolve()
            if adopted != dispatch_prepared:
                self._prepared_cwd(adopted, prepare_cwd, provision=provision)
            self._materialize_role_instructions(
                adopted, role, route["route"], ctx.lane.lane_kind
            )
            envelope.parent.mkdir(parents=True, exist_ok=True)
            write_prompt(adopted)

        lane_id = ctx.lane.lane_id
        token = lch.role_session_token(ctx.run_id, lane_id, role)
        if not ctx.lane.spec_digest:
            raise FactoryRefused("ROLE_SESSION_SPEC_DIGEST_MISSING")

        def session_replaced(reason: str, detail: str) -> None:
            self._say(lane_id, "{0}: fresh {1} session".format(reason, role), detail)
            self._start_session_epoch(session, turn)

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
            spec_digest=ctx.lane.spec_digest,
            on_session_replaced=session_replaced,
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
        write_identity()
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

    @staticmethod
    def _session_epoch(session: Path) -> int:
        """First turn of the live session; `1` when it has never been replaced."""
        try:
            return max(1, int((session / _SESSION_EPOCH_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return 1

    @staticmethod
    def _start_session_epoch(session: Path, turn: int) -> None:
        """Record that a fresh session begins at `turn`: earlier turns no longer count."""
        try:
            (session / _SESSION_EPOCH_FILE).write_text(str(turn), encoding="utf-8")
        except OSError:
            pass

    def _session_tokens(self, cwd: Path, session: Path, turns: int) -> tuple[int, int]:
        """Size of the live role session: (estimated tokens, largest single prompt tokens).

        The tokens are the larger of its transcript and its traffic. The
        transcript is whatever `*.jsonl` the route keeps in `session` (a
        replaced session's is moved to `superseded-*`, out of the glob); a
        route that keeps none there is measured by the prompts sent and the
        envelopes returned, which is what the agent has read and written.
        Traffic counts only the turns since the last replacement, because
        the turn files outlive the session that read them.
        """
        def size(path: Path) -> int:
            try:
                return path.stat().st_size if path.is_file() else 0
            except OSError:
                return 0

        transcript = sum(size(path) for path in session.glob("*.jsonl"))
        live = range(self._session_epoch(session), turns + 1)
        traffic = sum(
            size(lch.role_prompt_path(cwd, turn)) + size(lch.role_result_path(cwd, turn))
            for turn in live
        )
        largest = max((size(lch.role_prompt_path(cwd, turn)) for turn in live), default=0)
        return (
            estimate_tokens_for_bytes(max(transcript, traffic)),
            estimate_tokens_for_bytes(largest),
        )

    def _cap_reused_session(
        self,
        ctx: LaneContext,
        role: str,
        key: tuple[str, str, str],
        stored: _RoleSession | None,
        cwd: Path,
        session: Path,
        turn: int,
    ) -> Mapping[str, Any] | None:
        """Retire a session that has grown past the cap; answer the handoff for its successor.

        Reuse under the cap is unchanged (same lane, role and spec digest).
        Past it the next dispatch starts a fresh session instead of piling
        onto the old one: the pane is closed, the transcript moved aside
        (kept, never deleted, and out of `-c`'s reach), and the new prompt
        carries a compact handoff. `None` when nothing was retired.
        """
        cap = self.role_session_max_tokens
        if cap <= 0 or turn <= 1:
            return None
        tokens, prompt_tokens = self._session_tokens(cwd, session, turn - 1)
        # A session holding one prompt larger than half the cap would be over
        # it again on every dispatch; replacing it cannot help, so the cap
        # yields to twice the biggest prompt.
        if tokens <= max(cap, 2 * prompt_tokens):
            return None
        lane_id = ctx.lane.lane_id
        self._say(
            lane_id,
            "{0} session over cap: fresh session".format(role),
            "~{0} tokens against role_session_max_tokens {1}".format(tokens, cap),
        )
        cancel = getattr(self.launcher, "cancel", None)
        if stored is not None and stored.handle is not None and cancel is not None:
            try:
                cancel(stored.handle, time.monotonic() + _SUPERSEDED_CLOSE_SECONDS)
            except Exception as exc:
                self._say(lane_id, "{0} session close refused".format(role), str(exc))
        self._roles.pop(key, None)
        self._start_session_epoch(session, turn)
        transcripts = sorted(session.glob("*.jsonl"))
        if transcripts:
            aside = session / "superseded-{0}".format(time.time_ns())
            aside.mkdir()
            for transcript in transcripts:
                os.replace(transcript, aside / transcript.name)
        return self._session_handoff(ctx, cwd, turn - 1, tokens)

    def _session_handoff(
        self, ctx: LaneContext, cwd: Path, turns: int, tokens: int
    ) -> dict[str, Any]:
        """What a fresh session needs of the one it replaces: last answer, tree state, obligations."""
        def trim(value: Any, depth: int = 0) -> Any:
            if isinstance(value, str):
                return value[:300]
            if isinstance(value, Mapping) and depth < 3:
                return {str(k): trim(v, depth + 1) for k, v in list(value.items())[:20]}
            if isinstance(value, (list, tuple)) and depth < 3:
                return [trim(v, depth + 1) for v in list(value)[:20]]
            return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:300]

        handoff: dict[str, Any] = {
            "reason": "ROLE_SESSION_OVER_CAP",
            "prior_turns": turns,
            "prior_tokens_estimate": tokens,
            "open_obligations": [str(item)[:300] for item in ctx.lane.public_acceptance][:20],
        }
        try:
            last = json.loads(lch.role_result_path(cwd, turns).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = None
        if isinstance(last, Mapping):
            files = last.get("test_files") or last.get("private_files")
            summary = {k: v for k, v in last.items() if k not in ("test_files", "private_files")}
            if isinstance(files, Mapping):
                summary["test_files"] = sorted(str(path) for path in files)
            handoff["last_answer"] = trim(summary)
        try:
            state = self._git(cwd, "status", "--short", check=False)
        except FactoryRefused:
            state = ""
        if state:
            handoff["tree_state"] = state.splitlines()[:40]
        return handoff

    def _prepare(
        self, ctx: LaneContext, role: str, *, sha: str | None
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
            return attempt, cwd
        if not sha:
            raise FactoryRefused("missing checkout sha")
        if precreated:
            _clear_precreated_role_cwd(cwd)
        self._add_worktree(cwd, sha)
        return attempt, cwd

    def _bind_checkout(
        self,
        key: tuple[str, str, str],
        attempt: Path,
        checkout: Path | None,
        cwd_used: Path,
    ) -> Path:
        used = cwd_used.resolve()
        prepared = checkout or attempt / "checkout"
        if prepared.resolve() != used:
            try:
                used.relative_to(attempt.resolve())
            except ValueError:
                self._safe_remove_attempt(attempt, checkout)
        stored = self._roles.get(key)
        if stored is None:
            return used
        stored.cwd = used
        stored.attempt = used.parent if used.name == "checkout" else used
        stored.checkout = used if (used / ".git").exists() else None
        return used

    def write_tests(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "tester")
        key = self._role_key(ctx, "tester")
        stored = self._roles.get(key)

        # Filled in below once the turn is known to be a review revise.
        revise_draft: list[tuple[str, tuple[str, ...]]] = []
        # The accepted suite a redraft starts from, when the lane has one.
        baseline = ctx.redraft_baseline or {}
        baseline_files: Mapping[str, str] = baseline.get("suite_files") or {}

        def refresh(path: Path) -> None:
            if (path / ".git").exists():
                self._refresh_git_checkout(path, sha)
                for rel, text in baseline_files.items():
                    target = _resolved_under(path, rel)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(text)
                for draft_sha, draft_files in revise_draft:
                    self._restore_draft(path, draft_sha, draft_files)

        prepare = self._with_fixtures(ctx, refresh)

        if stored is None:
            attempt, checkout = self._prepare(ctx, "tester", sha=sha)
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        extra: dict[str, Any] = {
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_acceptance": list(ctx.lane.public_acceptance),
            # The declared interface the suite binds to; the same bytes the
            # test reviewer, builder and code reviewer are graded against.
            "public_contract": ctx.public_contract,
        }
        findings = list(ctx.draft_correction or ())
        if not findings:
            review_findings = self._revise_findings(ctx, "TEST_REVIEW")
            if review_findings is not None:
                findings = review_findings
                # The refresh above resets the tree to the integration head,
                # which does not hold the draft under review, so the tester
                # would find its own files gone on the revise turn and rebuild
                # them from its envelope. Lay the draft's files back down,
                # uncommitted, so the revise edits the draft it was asked to.
                draft = ctx.artifacts.get("TEST_DRAFT")
                draft_payload = getattr(draft, "payload", None) or {}
                draft_sha = str(draft_payload.get("candidate_sha") or "")
                draft_files = tuple(sorted(dict(draft_payload.get("files") or {})))
                if draft_sha and draft_files:
                    revise_draft.append((draft_sha, draft_files))
        if findings:
            extra["revise_findings"] = findings
        if baseline_files:
            extra["accepted_suite_paths"] = sorted(baseline_files)
            if baseline.get("spec_diff") is not None:
                extra["spec_diff"] = baseline["spec_diff"]
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
        files = dict(payload.get("test_files") or payload.get("private_files") or {})
        if (cwd_used / ".git").exists():
            scope: Sequence[str] = ()
            if ctx.lane.lane_kind == st.LANE_KIND_TESTS:
                scope = ctx.lane.declared_outputs
            files.update(self._collect_uncommitted(cwd_used, scope))
        return {"test_files": files}

    def review_tests(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        # An ordinary checkout of the draft candidate: the integration head
        # plus the draft's files, the same way the code reviewer gets the
        # builder's candidate. The draft is a pinned commit in the run
        # repository, so a linked worktree at its sha is the whole answer.
        draft = ctx.artifacts.get("TEST_DRAFT")
        if draft is None:
            raise FactoryRefused("missing TEST_DRAFT")
        # The draft names its own candidate. Reading it off the context
        # instead would fall back to the integration head when the context
        # was built without one -- a tree that does not hold the draft, so
        # the reviewer would grade files that are not there.
        sha = str(draft.payload.get("candidate_sha") or "")
        if not sha:
            raise FactoryRefused("TEST_DRAFT carries no candidate_sha")
        key = self._role_key(ctx, "test-reviewer")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(ctx, "test-reviewer", sha=sha)
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        extra = {
            "candidate_sha": sha,
            "public_contract": ctx.public_contract,
            "test_files": sorted(
                str(path) for path in dict(draft.payload.get("files") or {})
            ),
        }
        payload, _handle, cwd_used = self._launch(
            ctx,
            "test-reviewer",
            cwd,
            extra,
            prepare_cwd=self._with_fixtures(ctx, lambda path: self._refresh_git_checkout(path, sha)),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        return self._review_payload(payload)

    def build(self, ctx: LaneContext) -> Mapping[str, Any]:
        sha = self._base_sha(ctx, "builder")
        extra: dict[str, Any] = {
            "builder_base_sha": sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            # The suite this lane is graded against, in the builder's own
            # checkout at these paths. Read, never written: a candidate whose
            # delta names one is refused at admission.
            "test_paths": list(ctx.protected_test_paths),
            "test_suite_digest": ctx.test_suite_digest,
        }
        suite = ctx.artifacts.get("ACCEPTED_TEST_SUITE")
        if suite is not None:
            extra["accepted_suite_id"] = suite.artifact_id
        if ctx.bound_surface is not None:
            # An index into the suite: module specifiers, exported symbols,
            # and result-object keys the assertions resolve against.
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
        failures = self._failure_output(ctx)
        if failures:
            # The runner's own failure lines, verbatim. Without these the
            # builder is told a count and has to guess which cases it names.
            extra["failure_output"] = failures
        key = self._role_key(ctx, "builder")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(ctx, "builder", sha=sha)
        else:
            attempt, checkout = stored.attempt, stored.checkout
        if checkout is None and stored is None:
            raise FactoryRefused("BUILDER_CHECKOUT_MISSING")
        cwd = checkout or stored.cwd
        # A CODE_REVISE or BASE_INVALIDATION turn edits the prior candidate.
        # A checkout that survived holds it committed on the base; a fresh one
        # is the bare base, so lay the candidate's delta back down, uncommitted.
        prior = getattr(ctx.artifacts.get("BUILDER_OUTPUT"), "payload", None) or {}
        prior_sha = str(prior.get("candidate_sha") or "")
        prior_files = tuple(sorted({
            path
            for entry in prior.get("tree_delta") or ()
            if isinstance(entry, Mapping)
            for path in (entry.get("old_path"), entry.get("new_path"))
            if path
        }))

        def refresh(path: Path) -> None:
            self._refresh_builder_checkout(path, ctx.lane.declared_outputs, sha)
            if (
                prior_sha
                and prior_files
                and self._git(path, "rev-parse", "HEAD") == sha
            ):
                self._restore_draft(path, prior_sha, prior_files)

        payload, _handle, cwd_used = self._launch(
            ctx,
            "builder",
            cwd,
            extra,
            prepare_cwd=self._with_fixtures(ctx, refresh),
        )
        cwd_used = self._bind_checkout(key, attempt, checkout, cwd_used)
        candidate_sha, changed = self._commit_declared(
            cwd_used, ctx.lane.declared_outputs, sha
        )
        result: dict[str, Any] = {"candidate_sha": candidate_sha, "changed": changed}
        if isinstance(payload, Mapping) and payload.get("suite_defects"):
            # Typed and ruled on by the scheduler, never read as prose here.
            result["suite_defects"] = payload["suite_defects"]
        return result

    def review_code(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]:
        extra: dict[str, Any] = {
            "candidate_sha": ctx.candidate_sha,
            "declared_outputs": list(ctx.lane.declared_outputs),
            "public_contract": ctx.public_contract,
            "test_paths": list(ctx.protected_test_paths),
            "test_suite_digest": ctx.test_suite_digest,
        }
        if ctx.suite_result_summary is not None:
            # The same five integers the builder receives as
            # public_result_summary.
            extra["suite_result_summary"] = dict(ctx.suite_result_summary)
        if ctx.suite_findings_required:
            extra["suite_findings_required"] = True
        # Each only when present, so a green review dispatches byte-identical
        # extra. The reviewer must name every id here in a finding's test_id.
        if ctx.failing_test_ids:
            extra["failing_test_ids"] = list(ctx.failing_test_ids)
        if ctx.suite_defect_claims:
            extra["suite_defect_claims"] = [dict(item) for item in ctx.suite_defect_claims]
        if ctx.unattributed_tests:
            extra["unattributed_tests"] = list(ctx.unattributed_tests)
        # Each key only when there is something to say, so a lane that has
        # never carried a probe dispatches byte-identical extra.
        existing = [
            {key: record.get(key) for key in _PROBE_VIEW_KEYS}
            for record in getattr(ctx, "existing_probes", ()) or ()
            if isinstance(record, Mapping)
        ]
        if existing:
            extra["existing_probes"] = existing
        rejected = [
            {"path": item.path, "code": item.code, "detail": item.detail}
            for item in getattr(ctx, "rejected_probes", ()) or ()
        ]
        if rejected:
            extra["rejected_probes"] = rejected
        sha = self._base_sha(ctx, "code-reviewer")
        key = self._role_key(ctx, "code-reviewer")
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(
                ctx, "code-reviewer", sha=sha
            )
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        payload, _handle, cwd_used = self._launch(
            ctx,
            "code-reviewer",
            cwd,
            extra,
            prepare_cwd=self._with_fixtures(ctx, lambda path: self._refresh_git_checkout(path, sha)),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        result = self._review_payload(payload)
        self._stash_probes(ctx, payload, cwd_used, existing)
        return result

    def _stash_probes(
        self,
        ctx: LaneContext,
        payload: Mapping[str, Any],
        root: Path,
        existing: Sequence[Mapping[str, Any]],
    ) -> None:
        """Parse and read what the code reviewer authored, keyed to this review.

        The envelope is validated before any file is read, then each listed
        file is read bounded and without following links from the reviewer's
        own checkout. Nothing here decides anything: the scheduler takes the
        result with `take_probes` and the `review_code` return is unchanged.
        """
        reserved = [
            *ctx.lane.declared_outputs,
            *ctx.protected_test_paths,
            *(str(record.get("path") or "") for record in existing),
        ]
        entries, rejections, challenges = probes.parse_envelope(
            payload, reserved=[item for item in reserved if item]
        )
        submissions, unread = probes.read_submissions(Path(root), entries)
        with self._probe_stash_lock:
            self._probe_stash[(ctx.lane.lane_id, ctx.input_digest)] = (
                submissions,
                rejections + unread,
                challenges,
            )

    def take_probes(
        self, ctx: LaneContext
    ) -> tuple[
        tuple[probes.ProbeSubmission, ...],
        tuple[probes.Rejection, ...],
        tuple[probes.Challenge, ...],
    ]:
        """What the last `review_code` for this lane and input authored.

        Removed on read: a completed stage is never reauthored from here, it is
        read from its artifact.
        """
        with self._probe_stash_lock:
            found = self._probe_stash.pop((ctx.lane.lane_id, ctx.input_digest), None)
        return found if found is not None else ((), (), ())

    def acceptor_for(
        self, ctx: LaneContext
    ) -> tuple[str, str, str] | probes.Rejection:
        """`probes.select_acceptor` over the declared vendors, verbatim.

        The `(role, author_vendor, acceptor_vendor)` triple, or the
        `PROBE_ACCEPTOR_NOT_INDEPENDENT` rejection when the code reviewer's
        vendor and an acceptor's are not both declared and different.
        """
        return probes.select_acceptor(self.role_vendors)

    def accept_probes(
        self,
        ctx: LaneContext,
        role: str,
        items: Sequence[Mapping[str, Any]],
        mode: str,
    ) -> Mapping[str, Any]:
        """One read-only acceptance pass over `items` by `role`; the raw envelope.

        Dispatched at the candidate checkout like `review_tests`. The session
        is this lane's own (`_role_key` carries the lane id), so it cannot be a
        tests lane's tester or test-reviewer. Parsing is the caller's:
        `probes.parse_acceptance` is the gate, not this envelope.
        """
        if role not in probes.ACCEPTOR_ROLES:
            raise FactoryRefused("PROBE_ACCEPTOR_ROLE_INVALID:{0}".format(role))
        if mode not in (probes.MODE_ACCEPT, probes.MODE_CHALLENGE):
            raise FactoryRefused("PROBE_ACCEPTANCE_MODE_INVALID:{0}".format(mode))
        sha = ctx.candidate_sha
        if not sha:
            raise FactoryRefused("PROBE_ACCEPTANCE_CANDIDATE_MISSING")
        key = self._role_key(ctx, role)
        stored = self._roles.get(key)
        if stored is None:
            attempt, checkout = self._prepare(ctx, role, sha=sha)
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        extra = {
            "mode": mode,
            "probes": [dict(item) for item in items],
            "candidate_sha": sha,
            "public_contract": ctx.public_contract,
        }
        payload, _handle, cwd_used = self._launch(
            ctx,
            role,
            cwd,
            extra,
            prepare_cwd=self._with_fixtures(ctx, lambda path: self._refresh_git_checkout(path, sha)),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        return payload

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
                ctx, "integration-reviewer", sha=sha
            )
            cwd = checkout or attempt / "checkout"
        else:
            attempt, checkout, cwd = stored.attempt, stored.checkout, stored.cwd
        payload, _handle, cwd_used = self._launch(
            ctx,
            "integration-reviewer",
            cwd,
            extra,
            prepare_cwd=self._with_fixtures(ctx, lambda path: self._refresh_git_checkout(path, sha)),
        )
        self._bind_checkout(key, attempt, checkout, cwd_used)
        verdict, findings = self._review_payload(payload)
        return verdict, findings, tuple(payload.get("affected_lanes") or ())

    #: Files the operator's and the attend reviewer's trees carry, relative
    #: to their CWD.
    _OPERATOR_INPUTS = "inputs"
    _OPERATOR_SUITE = "suite"
    _OPERATOR_OUT = "revisions"
    _ATTEND_REVIEWER = _ATTEND_REVIEWER_ROLE

    def _attend_checkout(self, ctx: LaneContext, cwd: Path) -> None:
        """Make `cwd` a detached `--no-checkout` worktree of the target repo.

        The same `_add_worktree` the lane trees and UI anchors use, so Claude
        Code resolves the tree to the target repository's already-trusted
        project instead of stopping at a folder-trust prompt nobody is there
        to answer (a plain directory has no project entry). `--no-checkout`
        leaves no product file in it.

        Whatever an earlier dispatch left -- `revisions/` named by
        ATTEND_REVIEW rows, a pre-worktree plain tree -- has its children
        moved aside and back, so every recorded path keeps its bytes. The
        directory itself is never renamed: a live agent's working directory
        is that directory, and renaming it carried the agent's pane to the
        temporary name, so the next launch refused BINDING_MISMATCH against
        it. `git worktree add` takes the emptied directory as it stands.
        """
        if (cwd / ".git").exists():
            return
        held = cwd.parent / ".checkout-{0}".format(uuid.uuid4().hex)
        if cwd.is_dir() and any(cwd.iterdir()):
            held.mkdir()
            for child in cwd.iterdir():
                child.rename(held / child.name)
        self._add_worktree(cwd, self._base_sha(ctx, "tester"), no_checkout=True)
        if held.is_dir():
            for child in held.iterdir():
                child.rename(cwd / child.name)
            held.rmdir()

    def _write_attend_tree(
        self,
        cwd: Path,
        lanes: Sequence[att.LaneEvidence],
        files: Mapping[str, bytes],
    ) -> None:
        """Materialize everything an attend agent reads, on every dispatch.

        On disk rather than in the prompt: the accepted suites and four rounds
        of findings per lane are the bulk of this handoff, and B13's size
        check is made against the route's window at launch. A prompt that
        carries them is a prompt that can overflow, and an overflowing agent
        answers about a different lane. Each lane's evidence sits under its
        own `lanes/<lane_id>/` and `suite/<lane_id>/` so one batch can carry
        every parked lane without their files colliding.
        """
        inputs = cwd / self._OPERATOR_INPUTS
        suite = cwd / self._OPERATOR_SUITE
        for directory in (inputs, suite):
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True, exist_ok=True)
        # Never wiped: each round's revision and findings have their own
        # path, and an ATTEND_REVIEW row's `revision_ref` must keep naming
        # the bytes that round reviewed. The dispatchers unlink the one path
        # they ask for, so a stale file cannot pass for a fresh answer.
        (cwd / self._OPERATOR_OUT).mkdir(parents=True, exist_ok=True)
        for name, data in sorted(files.items()):
            (inputs / name).write_bytes(data)
        for lane in lanes:
            lane_dir = _resolved_under(inputs / "lanes", lane.lane_id)
            lane_dir.mkdir(parents=True, exist_ok=True)
            (lane_dir / "lane_gates.txt").write_text(lane.lane_gates, encoding="utf-8")
            (lane_dir / "reviews.json").write_text(
                json.dumps(
                    {
                        "public_contract": st.json_ready(lane.public_contract),
                        "failure_output": list(lane.failure_output),
                        "reviews": [st.json_ready(row) for row in lane.reviews],
                        "round": lane.round_number,
                        "stage": lane.stage,
                        "wait_reason": lane.wait_reason,
                        "fault": st.json_ready(lane.fault),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (lane_dir / "final_review.json").write_text(
                json.dumps(st.json_ready(lane.final_review), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            lane_suite = _resolved_under(suite, lane.lane_id)
            for relative, text in sorted(lane.suite_files.items()):
                destination = _resolved_under(lane_suite, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8")
        lch.scratch_environment(cwd)

    def _attend_lanes_extra(
        self, cwd: Path, lanes: Sequence[att.LaneEvidence]
    ) -> list[dict[str, Any]]:
        return [
            {
                "lane_id": lane.lane_id,
                "wait_reason": lane.wait_reason,
                "evidence_dir": str(
                    (cwd / self._OPERATOR_INPUTS / "lanes" / lane.lane_id).resolve()
                ),
                "suite_dir": str((cwd / self._OPERATOR_SUITE / lane.lane_id).resolve()),
                "suite_files": sorted(lane.suite_files),
            }
            for lane in lanes
        ]

    def attend_operator(
        self, ctx: LaneContext, request: att.OperatorRequest
    ) -> Mapping[str, Any]:
        """Dispatch the operator agent that authors an attended amendment.

        Its tree is a detached `--no-checkout` worktree of the target repo,
        made by the lane trees' own helper: a plain directory has no trusted
        Claude Code project, and the agent stopped at the folder-trust prompt
        in it. `--no-checkout` keeps every product file out, because this role
        authors a plan, never a candidate. It gets its inputs as files and one
        path to write.
        """
        attempt = self._role_dir(ctx, "operator")
        cwd = attempt / "checkout"
        self._attend_checkout(ctx, cwd)
        inputs = cwd / self._OPERATOR_INPUTS
        files = {
            "current.ir.json": Path(request.ir_path).read_bytes(),
            "amendment_rules.md": request.amendment_rules.encode("utf-8"),
        }
        extra: dict[str, Any] = {
            "allowed_lane_ids": list(request.allowed_lane_ids),
            "amendment_rules_path": str((inputs / "amendment_rules.md").resolve()),
            "current_ir_path": str((inputs / "current.ir.json").resolve()),
            "next_plan_revision": request.next_plan_revision,
            "parked_lanes": self._attend_lanes_extra(cwd, request.lanes),
            "revision_out_path": request.revision_out_path,
            "review_round": request.review_round,
        }
        if request.reviewer_findings:
            files["reviewer_findings.json"] = json.dumps(
                {"findings": [dict(item) for item in request.reviewer_findings]},
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            extra["reviewer_findings_path"] = str(
                (inputs / "reviewer_findings.json").resolve()
            )
        if request.previous_revision_path:
            files["previous_revision.ir.json"] = Path(
                request.previous_revision_path
            ).read_bytes()
            extra["previous_revision_path"] = str(
                (inputs / "previous_revision.ir.json").resolve()
            )
        Path(request.revision_out_path).unlink(missing_ok=True)
        try:
            payload = self._attend_launch(
                ctx,
                "operator",
                attempt,
                cwd,
                extra,
                lambda path: self._write_attend_tree(path, request.lanes, files),
                lambda answer: att.read_operator_envelope(
                    answer, request, strict=True
                ),
            )
        finally:
            self._close_attend_pane(ctx, "operator", cwd)
        return payload

    def attend_reviewer(
        self, ctx: LaneContext, request: att.ReviewRequest
    ) -> Mapping[str, Any]:
        """Dispatch the independent reviewer of an attended revision.

        Beside `attend_operator` and shaped the same way: a `--no-checkout`
        worktree, its inputs as files, one path to write (`findings_out_path`,
        the answer to the independent review question that `planctl review`
        signs). It is a different agent on a different declared vendor, so the
        author never answers its own review.
        """
        attempt = self._role_dir(ctx, _ATTEND_REVIEWER_ROLE)
        cwd = attempt / "checkout"
        inputs = cwd / self._OPERATOR_INPUTS
        # The reviewer is handed exactly the bytes the round recorded, and
        # those are the only bytes `project` will accept after a PASS.
        revision = att.reviewed_bytes(
            Path(request.revision_ir_path), request.revision_sha256
        )
        self._attend_checkout(ctx, cwd)
        files = {
            "current.ir.json": Path(request.current_ir_path).read_bytes(),
            "revision.ir.json": revision,
            "question_surface.json": request.question_surface.encode("utf-8"),
            "rationales.json": json.dumps(
                {"rationales": [dict(item) for item in request.rationales]},
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
            "amendment_rules.md": request.amendment_rules.encode("utf-8"),
        }
        extra = {
            "allowed_lane_ids": list(request.allowed_lane_ids),
            "amendment_rules_path": str((inputs / "amendment_rules.md").resolve()),
            "current_ir_path": str((inputs / "current.ir.json").resolve()),
            "findings_out_path": request.findings_out_path,
            "next_plan_revision": request.next_plan_revision,
            "parked_lanes": self._attend_lanes_extra(cwd, request.lanes),
            "question_surface_path": str((inputs / "question_surface.json").resolve()),
            "rationales_path": str((inputs / "rationales.json").resolve()),
            "review_round": request.review_round,
            "revision_ir_path": str((inputs / "revision.ir.json").resolve()),
        }
        Path(request.findings_out_path).unlink(missing_ok=True)
        try:
            payload = self._attend_launch(
                ctx,
                _ATTEND_REVIEWER_ROLE,
                attempt,
                cwd,
                extra,
                lambda path: self._write_attend_tree(path, request.lanes, files),
                lambda answer: att.check_review_answer(answer, request),
            )
        finally:
            self._close_attend_pane(ctx, _ATTEND_REVIEWER_ROLE, cwd)
        return payload

    def _attend_launch(
        self,
        ctx: LaneContext,
        role: str,
        attempt: Path,
        cwd: Path,
        extra: Mapping[str, Any],
        prepare_cwd: Callable[[Path], None],
        check: Callable[[Mapping[str, Any]], Any],
    ) -> Mapping[str, Any]:
        """One attend dispatch, and one more turn in the same session if its
        envelope is refused.

        `check` is the attend loop's own shape check on the answer. Its exact
        refusal goes back to the agent that wrote the envelope, while its pane
        is still open, as `envelope_error`; whatever the second turn returns
        is the answer, and the loop refuses it if it is still malformed. An
        envelope the agent could have corrected used to end the whole verb.
        """
        key = self._role_key(ctx, role)
        payload, _handle, cwd_used = self._launch(
            ctx, role, cwd, extra, prepare_cwd=prepare_cwd,
            # A --no-checkout tree holds no project to install.
            provision=False,
        )
        # Records the worktree, so removal is the lanes' `git worktree remove`.
        self._bind_checkout(key, attempt, cwd, cwd_used)
        try:
            check(payload)
            return payload
        except att.AttendRefused as refused:
            error = "{0}: {1}".format(refused.code, refused.detail or refused.code)
        self._say(ctx.lane.lane_id, "{0} envelope refused, asking once more".format(role), error)
        payload, _handle, cwd_used = self._launch(
            ctx, role, cwd, {**extra, "envelope_error": error},
            prepare_cwd=prepare_cwd, provision=False,
        )
        # `_launch` records a fresh session for the turn; bind its tree again.
        self._bind_checkout(key, attempt, cwd, cwd_used)
        return payload

    def _close_attend_pane(self, ctx: LaneContext, role: str, cwd: Path) -> None:
        """Close the pane an attend dispatch launched, answered or failed.

        An attend role answers one file per dispatch, so its pane holds
        nothing a later dispatch needs, and one left open -- a `blocked` start
        keeps its pane -- is what the next launch reconnects to. The role's
        record keeps its tree for the run's own cleanup and drops only the
        handle. Best effort, as `_release_superseded` is: a pane Herdr would
        not close is reported, never a reason to fail the lane.
        """
        stored = self._roles.get(self._role_key(ctx, role))
        if stored is not None:
            stored.handle = None
        close = getattr(self.launcher, "close_agent", None)
        if close is None:
            return
        try:
            close(
                lch.role_session_token(ctx.run_id, ctx.lane.lane_id, role),
                self._launch_environment(cwd),
            )
        except Exception as exc:
            self._say(
                ctx.lane.lane_id,
                "{0} pane not closed".format(role),
                "{0}: {1}".format(type(exc).__name__, exc),
            )

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
            # `code` is harness-synthesized from a measured probe run. A
            # reviewer that writes one would let prose pose as a measurement.
            if any(key in finding for key in st.HARNESS_FINDING_KEYS):
                raise FactoryRefused(
                    "REVIEW_FINDINGS_MALFORMED:findings[{0}] carries {1}, "
                    "which only the harness sets".format(
                        index, ", ".join(st.HARNESS_FINDING_KEYS)
                    )
                )
        return st.ReviewerVerdict(payload["verdict"]), findings


def _max_probe_rounds(layout: Mapping[str, Any]) -> int:
    """`review.max_probe_rounds` as the loader read it. Absent means the default."""
    value = layout.get("review_max_probe_rounds")
    return st.DEFAULT_MAX_PROBE_ROUNDS if value is None else int(value)


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
    attend = layout.get("attend") or {}
    attend_route = attend.get("route") or {}
    reviewer_route = attend.get("reviewer_route") or {}
    # The operator and its reviewer are dispatched through the same launcher
    # as every other role, so their routes are admitted by the same executed
    # receipt. A deployment cannot opt into `run attend` on a route it never
    # captured.
    configured_routes = {binding["route"] for binding in role_routes.values()} | {
        binding
        for binding in (attend_route.get("route"), reviewer_route.get("route"))
        if binding
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
        suite_timeout_s=layout.get("suite_timeout_s"),
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
        attend_reviewer_route=reviewer_route or None,
        role_vendors=layout.get("role_vendors"),
        role_session_max_tokens=_config_role_session_max_tokens(
            layout.get("role_session_max_tokens"), "role_session_max_tokens"
        ),
        dispatch_idle_timeout_s=_config_dispatch_idle_timeout_s(
            layout.get("dispatch_idle_timeout_s"), "dispatch_idle_timeout_s"
        ),
    )


#: The AWS profile `planctl review` runs its EC2 mutation pass under.
_PLANCTL_RUNNER_PROFILE = "planctl-runner"
#: Where omp keeps a profile's auth, sessions and settings.
_OMP_PROFILES_DIR = Path.home() / ".omp" / "profiles"
#: Seconds a route capture gets per turn when the preflight mints a receipt.
_ROUTE_CAPTURE_TIMEOUT_S = 120.0


class _RunPreflight:
    """Every dependency `run start` / `run resume` can reach, before dispatch.

    Two halves, because route admission sits between them: the run-wide
    checks run before the actor is built (`_actor_for` refuses on the first
    unadmitted route, and a receipt is one of the things this provisions), and
    the per-lane checks run inside the scheduler's preflight hook, where the
    integration head, the accepted suites and the actor's own tree path exist.
    One record and one refusal cover both. See `adw_modules/run_preflight.py`
    for what is provisioned and what only refuses, and `docs/run-preflight.md`
    for the checklist.
    """

    def __init__(
        self,
        layout: Mapping[str, Any],
        runtime: RuntimeStateRoot,
        store: ArtifactStore,
        run_id: str,
        target: gitpub.TargetBinding,
        console: Any,
        *,
        verb: str,
        attend: bool,
        local: bool = False,
        plan_ir: Optional[Path] = None,
        plan_ir_current: Optional[Callable[[], Tuple[bool, str]]] = None,
        plan_ir_supply: Optional[Callable[[], str]] = None,
        planroot_check: Optional[Callable[[], Tuple[bool, str]]] = None,
        runner: rp.Runner = rp.real_runner,
        which: Callable[[str], Optional[str]] = shutil.which,
        file_issue: Optional[Callable[[str, str], Mapping[str, str]]] = None,
    ) -> None:
        self.layout = layout
        self.runtime = runtime
        self.store = store
        self.run_id = run_id
        self.target = target
        self.console = console
        self.verb = verb
        self.attend = attend
        self.local = local
        self.plan_ir = plan_ir
        self.plan_ir_current = plan_ir_current
        self.plan_ir_supply = plan_ir_supply
        self.planroot_check = planroot_check
        self.runner = runner
        self.which = which
        self.repo = Path(target.target_repository_root)
        self.file_issue = file_issue or (
            lambda title, body: _attend_file_issue(self.repo, title, body)
        )
        self.preflight_id = uuid.uuid4().hex
        self.items: tuple[rp.PreflightItem, ...] = ()
        self._added_packages: dict[str, list[str]] = {}

    # -- channel ----------------------------------------------------------

    def say(self, lane_id: str, message: str, detail: str = "") -> None:
        step = getattr(self.console, "step", None)
        if step is not None:
            step(lane_id or step_log.RUN_LANE_ID, message, detail)

    # -- run-wide ---------------------------------------------------------

    def _routes(self) -> list[tuple[str, Mapping[str, Any]]]:
        """Every (role, binding) the run can dispatch. Attend's only when on."""
        routes = [
            (str(role), binding)
            for role, binding in sorted((self.layout.get("role_routes") or {}).items())
        ]
        if self.attend:
            attend = self.layout.get("attend") or {}
            for role, key in (("operator", "route"), (_ATTEND_REVIEWER_ROLE, "reviewer_route")):
                binding = attend.get(key) or {}
                if binding.get("route"):
                    routes.append((role, binding))
        return routes

    def _executable(self, route: str) -> str:
        executables = self.layout.get("executables") or {}
        return str(executables.get(route) or route)

    def run_wide_checks(self) -> list[rp.Check]:
        layout = self.layout
        keys_dir = Path(layout["keys_dir"])
        checks: list[rp.Check] = [
            rp.binary_check("binary:uv", "uv", "install uv (https://docs.astral.sh/uv/)", which=self.which),
            rp.binary_check(
                "binary:herdr", self._executable("herdr"),
                "install herdr, or set executables.herdr", which=self.which,
            ),
            rp.command_check(
                "herdr:server", [self._executable("herdr"), "workspace", "list"],
                "start the herdr server (open herdr) and rerun", self.runner,
            ),
        ]
        checks.append(self._requires_python_check())
        seen_binaries: set[str] = set()
        seen_profiles: set[str] = set()
        seen_routes: dict[str, Mapping[str, Any]] = {}
        for role, binding in self._routes():
            route = str(binding.get("route") or "")
            binary = self._executable(route)
            if binary not in seen_binaries:
                seen_binaries.add(binary)
                checks.append(
                    rp.binary_check(
                        "binary:{0}".format(route), binary,
                        "install {0}, or set executables.{0}".format(route),
                        which=self.which,
                    )
                )
            profile = str(binding.get("profile") or "")
            if route == "omp" and profile and profile not in seen_profiles:
                seen_profiles.add(profile)
                # A profile holds the provider's login. Nothing here can log
                # in for anyone, so a missing one is a person's to create.
                checks.append(
                    rp.directory_check(
                        "omp-profile:{0}".format(profile), _OMP_PROFILES_DIR / profile,
                        "create it and log in: omp --profile {0}".format(profile),
                        human_secret=True,
                    )
                )
            seen_routes.setdefault(route, binding)
        verify_keys = tuple(Path(path) for path in layout.get("route_verify_keys") or ())
        for path in verify_keys:
            checks.append(
                rp.readable_file_check(
                    "key:route-verify:{0}".format(path.name), path,
                    "restore the route verify key; it is never regenerated",
                    human_secret=True,
                )
            )
        for route, binding in sorted(seen_routes.items()):
            checks.append(self._receipt_check(route, binding, verify_keys))
        checks.append(
            rp.readable_file_check(
                "key:reviewer-hmac", keys_dir / admission.REVIEWER_HMAC_KEY_FILE,
                "restore {0}; it is never regenerated".format(admission.REVIEWER_HMAC_KEY_FILE),
                human_secret=True,
            )
        )
        checks.extend(self._attend_run_checks())
        return checks

    def _requires_python_check(self) -> rp.Check:
        pyproject = Path(__file__).resolve().parent / "pyproject.toml"
        declared = tchain._requires_python(pyproject.parent) if pyproject.is_file() else None
        if not declared:
            return rp.Check(
                "python:runtime", lambda: (True, ""),
                skip="no requires-python is declared for the ADW runtime",
            )

        def verify() -> tuple[bool, str]:
            from packaging.specifiers import SpecifierSet

            version = "{0}.{1}.{2}".format(*sys.version_info[:3])
            return (version in SpecifierSet(declared), "python {0} vs {1}".format(version, declared))

        return rp.Check("python:runtime", verify, "run maestro under a python matching " + declared)

    def _receipt_check(
        self, route: str, binding: Mapping[str, Any], verify_keys: Sequence[Path]
    ) -> rp.Check:
        receipts = self.layout.get("route_receipts") or {}
        path = receipts.get(route)

        def verify() -> tuple[bool, str]:
            if not path:
                return False, "route_receipts.{0} is not configured".format(route)
            keys = tuple(load_public_key(Path(item)) for item in verify_keys)
            if not keys:
                return False, "route_verify_keys is not configured"
            admitted = load_admitted_routes({route: Path(path)}, verify_keys=keys)
            return admitted.admits(route), str(path)

        def mint() -> str:
            # The one minting path, signed with the deployment's existing
            # route seed. The seed is read, never created: a missing seed is
            # a key, and keys are a person's.
            seed_path = Path(self.layout["keys_dir"]) / "route.seed"
            seed = bytes.fromhex(seed_path.read_text(encoding="ascii").strip())
            session = self.runtime.path / "route-admission"
            spec = admission.RouteCaptureSpec(
                route=route,
                cwd=self.repo,
                herdr=Path(self._executable("herdr")),
                binary=Path(self._executable(route)),
                model=str(binding.get("model") or ""),
                effort=str(binding.get("effort") or ""),
                profile=binding.get("profile") or None,
                session_dir=session,
                timeout_s=_ROUTE_CAPTURE_TIMEOUT_S,
            )
            admission.admit_routes((spec,), {route: Path(path)}, route_seed=seed)
            return "captured and signed a {0} route receipt at {1}".format(route, path)

        missing_file = bool(path) and not Path(path).exists()
        return rp.Check(
            "route-receipt:{0}".format(route),
            verify,
            "capture it with route admission (needs keys_dir/route.seed); an "
            "existing invalid receipt is never overwritten",
            provision=mint if missing_file else None,
        )

    def _attend_run_checks(self) -> list[rp.Check]:
        if not self.attend:
            reason = "attend is disabled in this deployment"
            return [
                rp.Check(item, lambda: (True, ""), skip=reason)
                for item in ("attend:planctl", "attend:planroot-dir", "attend:plan-ir", "aws:planctl-runner", "gh:auth", "gh:repo-permission")
            ]
        policy = _attend_policy(self.layout)
        checks = [
            rp.readable_file_check(
                "attend:planctl", policy.planctl,
                "set attend.planctl to the plan-contract validator; there is no "
                "source the runtime can fetch it from",
            ),
            *(
                [
                    rp.Check(
                        "attend:planroot-dir",
                        self.planroot_check,
                        "set attend.planctl_root_dir to a durable directory "
                        "outside the home directory root, ~/.maestro and keys_dir: "
                        "planctl refuses to upload a root there",
                    )
                ]
                if self.planroot_check is not None
                else []
            ),
            self._plan_ir_check(),
        ]
        if self.local:
            checks.append(
                rp.Check("aws:planctl-runner", lambda: (True, ""), skip="run attend --local: planctl review runs here")
            )
        else:
            checks.append(
                rp.command_check(
                    "aws:planctl-runner",
                    ["aws", "sts", "get-caller-identity", "--profile", _PLANCTL_RUNNER_PROFILE],
                    "refresh the {0} AWS credentials (aws sso login --profile {0}), "
                    "or rerun with --local".format(_PLANCTL_RUNNER_PROFILE),
                    self.runner,
                    human_secret=True,
                )
            )
        checks.append(
            rp.command_check(
                "gh:auth", ["gh", "auth", "status", "--active"], "gh auth login", self.runner, human_secret=True
            )
        )
        checks.append(
            rp.command_check(
                "gh:repo-permission", ["gh", "repo", "view", "--json", "viewerPermission"],
                "grant this account triage or write on the run's repository",
                self.runner, human_secret=True, cwd=self.repo,
                accept=rp.gh_permission_accept,
            )
        )
        return checks

    def _plan_ir_check(self) -> rp.Check:
        """The revision's IR, in the planctl root attend owns, recorded.

        Supplied when absent: an IR attend did not write (`attend.plan_ir`,
        or a hand amendment's, which may sit in `/tmp`) is copied with its
        sources into the revision's root under `runtime_state_root`.
        The IR is the one the revision's plan names (`_attend_plan_ir`).
        """
        remedy = (
            "project the plan with plan_author_cli.py so it names its IR, or set "
            "attend.plan_ir to the IR whose sha256 the plan's receipt names"
        )
        check = rp.readable_file_check("attend:plan-ir", self.plan_ir, remedy, parse=json.loads)
        if self.plan_ir_current is None:
            return check
        return rp.Check(
            "attend:plan-ir", self.plan_ir_current, remedy, provision=self.plan_ir_supply
        )

    def run_wide(self) -> None:
        provisioning.set_tree_packages(())
        self.items = rp.evaluate(self.run_wide_checks(), self.say)

    def actor_for(self, compiled: st.CompiledPlan) -> StageActor:
        """`_actor_for`, with its refusal folded into this preflight's report."""
        try:
            return _actor_for(self.runtime, self.layout, self.target, self.run_id, compiled)
        except FactoryRefused as exc:
            self.items = self.items + (
                rp.PreflightItem(
                    "actor", rp.REFUSED, str(exc),
                    "fix the route items above; lane trees were not checked",
                ),
            )
            self.finish()
            raise

    # -- per lane ---------------------------------------------------------

    def __call__(self, scheduler: FactoryScheduler) -> None:
        actor = scheduler.actor
        trees: list[Path] = []
        try:
            lanes = scheduler.preflight_lanes()
            items = rp.evaluate(self._lane_checks(scheduler, actor, lanes, trees), self.say)
            items += rp.evaluate(self._attend_tree_checks(actor, trees), self.say)
            items += rp.evaluate(
                [
                    rp.Check(
                        "runners",
                        lambda: (scheduler._assert_runners_usable() or True, "every runner the plan names resolves"),
                        "fix provision_argv so the runner loads; the runner tree is kept for inspection",
                    )
                ],
                self.say,
            )
        finally:
            for tree in trees:
                self._remove_tree(tree)
        self.items = self.items + items
        self.finish(lanes)

    def _lane_checks(self, scheduler: FactoryScheduler, actor: Any, lanes: Sequence[Mapping[str, Any]], trees: list[Path]):
        """Lazily, so a later check can see whether the tree exists."""
        if not lanes:
            return
        base = scheduler._integration_head()
        tree = self.runtime.path / "worktrees" / "preflight-{0}".format(self.preflight_id) / "checkout"
        trees.append(tree)
        state = {"ready": False}

        def prepare() -> tuple[bool, str]:
            # A lane checkout exactly as a role gets one: a worktree at the
            # integration head, then the launch path's own `_prepared_cwd`,
            # which refreshes it and runs `provision_argv` in it.
            actor._add_worktree(tree, base)
            try:
                actor._prepared_cwd(tree, lambda path: actor._refresh_git_checkout(path, base))
            except lch.LaunchRefused as exc:
                return False, str(exc)
            state["ready"] = True
            return True, "worktree at {0} provisioned".format(base[:12])

        yield rp.Check("lane-tree", prepare, "fix provision_argv so it succeeds in a lane checkout")
        local = rp.tree_modules(tree) if tree.is_dir() else frozenset()
        outputs = rp.output_modules(
            path for lane in lanes for path in (*lane["declared_outputs"], *lane["suite_paths"])
        )
        for lane in lanes:
            lane_id = str(lane["lane_id"])
            needs = rp.python_needs(lane["python_specifiers"], local=local, outputs=outputs)
            gate = (lane["spec"] or {}).get("gate") or {}
            cwd = tree / str(gate.get("cwd") or ".")
            for module in needs:
                if not state["ready"]:
                    yield rp.Check("python:" + module, lambda: (True, ""), lane_id=lane_id, skip="lane tree could not be prepared")
                    continue
                yield rp.module_check(lane_id, module, tree, cwd, self.runner, lambda name, lane_id=lane_id: self._install(tree, name, lane_id))
            produced = set(lane["declared_outputs"]) | set(lane["suite_paths"])
            for path in rp.declared_fixture_paths(lane["spec"] or {}):
                if path in produced or any(path in set(other["declared_outputs"]) for other in lanes):
                    continue
                if not state["ready"]:
                    yield rp.Check("fixture:" + path, lambda: (True, ""), lane_id=lane_id, skip="lane tree could not be prepared")
                    continue
                yield rp.fixture_check(lane_id, path, tree, self.repo)

    def _install(self, tree: Path, module: str, lane_id: str) -> str:
        provisioning.install_tree_packages(tree, (module,), _resolved_provision_timeout(None))
        provisioning.set_tree_packages((*provisioning.tree_packages(), module))
        self._added_packages.setdefault(module, []).append(lane_id)
        return "installed {0} tree-local (uv pip install into the tree's .venv); it is not in the project's manifest".format(module)

    def _attend_tree_checks(self, actor: Any, trees: list[Path]):
        if not self.attend:
            yield rp.Check("attend-tree:operator", lambda: (True, ""), skip="attend is disabled in this deployment")
            yield rp.Check("attend-tree:reviewer", lambda: (True, ""), skip="attend is disabled in this deployment")
            return
        lane_id = "preflight-{0}".format(self.preflight_id)
        scratch = self.runtime.path / "worktrees" / self.run_id / lane_id
        # The attend trees are git worktrees: removed by `git worktree remove`
        # before their parent is deleted, or the target repo keeps the entries.
        trees.extend(
            (
                scratch / "operator" / "checkout",
                scratch / _ATTEND_REVIEWER_ROLE / "checkout",
                scratch,
            )
        )
        ctx = SimpleNamespace(
            run_id=self.run_id,
            lane=SimpleNamespace(lane_id=lane_id, spec_digest="0" * 64, lane_kind=None),
            artifacts={},
            input_digest="",
            plan_revision=0,
            stage=st.LaneStage.WAITING_FOR_USER,
            integration_head="",
        )
        ir = scratch / "preflight.ir.json"
        ir_bytes = b"{}"

        def dispatch(call: Callable[[], Any]) -> tuple[bool, str]:
            scratch.mkdir(parents=True, exist_ok=True)
            ir.write_bytes(ir_bytes)
            token = _PREPARE_ONLY.set(True)
            try:
                call()
            except _PreparedOnly as prepared:
                return True, "tree prepared by the launch path at {0}".format(prepared.cwd)
            except LaunchFailed as exc:
                return False, exc.detail
            finally:
                _PREPARE_ONLY.reset(token)
            return False, "the dispatch path returned without preparing a tree"

        def operator() -> tuple[bool, str]:
            return dispatch(
                lambda: actor.attend_operator(
                    ctx,
                    att.OperatorRequest(
                        run_id=self.run_id, plan_revision=0, next_plan_revision=1, lanes=(),
                        ir_path=str(ir), revision_out_path=str(scratch / "revision.ir.json"),
                        amendment_rules="", allowed_lane_ids=(),
                    ),
                )
            )

        def reviewer() -> tuple[bool, str]:
            return dispatch(
                lambda: actor.attend_reviewer(
                    ctx,
                    att.ReviewRequest(
                        run_id=self.run_id, plan_revision=0, next_plan_revision=1, review_round=1,
                        lanes=(), current_ir_path=str(ir), revision_ir_path=str(ir),
                        question_surface="{}", rationales=(),
                        findings_out_path=str(scratch / "findings.json"), amendment_rules="",
                        allowed_lane_ids=(), revision_sha256=att.sha256_hex(ir_bytes),
                    ),
                )
            )

        remedy = (
            "the attend tree is a --no-checkout worktree and must not be provisioned; "
            "a launch path that provisions it is a runtime defect, not configuration"
        )
        yield rp.Check("attend-tree:operator", operator, remedy)
        yield rp.Check("attend-tree:reviewer", reviewer, remedy)

    def _remove_tree(self, tree: Path) -> None:
        if (tree / ".git").exists():
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(tree)],
                capture_output=True, text=True, check=False,
            )
        root = tree.parent if tree.name == "checkout" else tree
        shutil.rmtree(root, ignore_errors=True)

    # -- record -----------------------------------------------------------

    def finish(self, lanes: Sequence[Mapping[str, Any]] = ()) -> None:
        """One ledger record, one step per lane, then refuse or return."""
        issues = []
        for module, lane_ids in sorted(self._added_packages.items()):
            title = "Run preflight installed {0} tree-local: missing from the project manifest".format(module)
            body = (
                "The run preflight found `{0}` imported by the accepted suite of "
                "{1}, not installed by `provision_argv`, and installed it into each "
                "lane tree's venv only. Add it to the project's dependency manifest "
                "through a reviewed lane or amendment.".format(module, ", ".join(sorted(lane_ids)))
            )
            issues.append({"module": module, **dict(self.file_issue(title, body))})
        by_lane: dict[str, dict[str, list[str]]] = {}
        for item in self.items:
            if item.status == rp.SKIPPED:
                continue
            record = by_lane.setdefault(
                item.lane_id or step_log.RUN_LANE_ID,
                {"needs": [], "verified": [], "added": [], "refused": []},
            )
            record["needs"].append(item.item_id)
            record[item.status].append(item.item_id)
        payload = {
            "verb": self.verb,
            "items": [item.as_payload() for item in self.items],
            "lanes": by_lane,
            "needs_manifest": sorted(self._added_packages),
            "issues": issues,
        }
        self.store.record_run_preflight(self.run_id, preflight_id=self.preflight_id, payload=payload)
        for lane in lanes:
            lane_id = str(lane["lane_id"])
            record = by_lane.get(lane_id) or {"verified": [], "added": [], "refused": []}
            self.say(
                lane_id, "preflight",
                "verified {0}, added {1}, refused {2}".format(
                    len(record["verified"]), len(record["added"]), len(record["refused"])
                ),
            )
        refused = rp.refused(self.items)
        if refused:
            raise RunPreflightRefused(refused, rp.render(self.items))


def _compile_plan(
    path: Path, *, revision: int, ref: str, bound_run: bool = False
) -> st.CompiledPlan:
    """Read and compile one plan artifact.

    ``bound_run`` is true only for a revision a run already holds, so an
    authoring obligation added after that run was bound (``OBLIGATION_UNDECIDED``)
    never refuses it mid-run. Start and amend compile strictly.

    Reading it is the only place a missing or unreadable *plan file* is a
    configuration fact, so the mapping belongs here rather than in `main`:
    every other `FileNotFoundError` a run can raise -- a missing `git`, `omp`
    or `claude` executable, a role checkout removed mid-turn, a state path
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
        stored, plan_revision=revision, plan_artifact_ref=ref, bound_run=bound_run
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
            # Read as bound first: this plan may already be a run's revision,
            # and resuming it must not re-judge authoring obligations.
            compiled = _compile_plan(
                plan_path, revision=1, ref=str(plan_path), bound_run=True
            )
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
                    compiled = _compile_plan(
                        plan_path, revision=1, ref=str(plan_path)
                    )
                    _require_approved_plan(plan_path, compiled, layout)
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


def _require_approved_plan(
    plan_path: Path, compiled: st.CompiledPlan, layout: Mapping[str, Any]
) -> None:
    """A new run binds only a plan bound to authenticated approval evidence.

    The approved projection (`plan_author_cli.py --from-plan-contract`, or
    `run attend`'s) writes an `approval` record: the plan digest and the
    planctl receipt it was projected from, both signed with this deployment's
    reviewer key. A compiler-valid plan without it, or one edited after
    projection, is refused `PLAN_UNAPPROVED`/`PLAN_APPROVAL_*` before a run
    exists. `run amend` does not call this: a scripted amendment is still
    accepted, and a run already bound is never re-checked.
    """
    try:
        document = json.loads(plan_path.read_bytes().decode("utf-8"))
        material = _reviewer_hmac_key(layout)
    except att.AttendRefused as exc:
        raise _RunRefused("PLAN_UNAPPROVED", "reviewer key: {0}".format(exc.detail)) from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise _RunRefused("PLAN_UNAPPROVED", str(exc)) from exc
    approval = document.get("approval") if isinstance(document, dict) else None
    try:
        plan_approval.verify_approval(
            approval, compiled.plan_digest, plan_approval.key_bytes(material)
        )
    except plan_approval.ApprovalRefused as exc:
        raise _RunRefused(
            exc.code,
            "run start binds only an approved projection: planctl review "
            "--findings, then plan_author_cli.py --from-plan-contract ({0})".format(exc),
        ) from exc


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
        _require_approved_plan(plan_path, compiled, layout)
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
            # The run exists and is pinned. With attend enabled, `run attend`
            # binds it like any other and runs the scheduler itself, once
            # this verb's store and runtime are closed.
            attend = _attend_policy(layout).enabled
            if not attend:
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
                preflight = _RunPreflight(
                    layout, runtime, store, run_id, target, console,
                    verb="start", attend=False,
                )
                preflight.run_wide()
                scheduler = FactoryScheduler(
                    store,
                    run_id,
                    preflight.actor_for(compiled),
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
                    max_probe_rounds=_max_probe_rounds(layout),
                    preflight=preflight,
                )
                status = scheduler.run()
                console.finished(run_id, status)
        finally:
            store.close()
    finally:
        runtime.close()
    if attend:
        return _run_attend(
            argparse.Namespace(run_id=run_id, local=False), verb="start"
        )
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
        # Before anything reads the run's artifacts: a run holding a pre-v5
        # suite artifact is restarted, and every verb says so by name.
        refuse_historical_artifacts(store, run_id)
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
            bound_run=True,
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
    if _attend_policy(layout).enabled:
        # A deployment that opted into attend never stops at a park an
        # amendment can answer; `run attend` rebinds the run itself.
        store.close()
        runtime.close()
        return _run_attend(args, verb="resume")
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
            preflight = _RunPreflight(
                layout, runtime, store, run_id, target, console,
                verb="resume", attend=False,
            )
            preflight.run_wide()
            actor = preflight.actor_for(compiled)
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
                max_probe_rounds=_max_probe_rounds(layout),
                preflight=preflight,
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
            try:
                apply_factory_amendment(
                    store,
                    run_id,
                    compiled,
                    runtime=runtime,
                    target=target,
                )
            except AmendmentAlreadyApplied as already:
                gitpub.revalidate_binding(target)
                current = FactoryScheduler(
                    store,
                    run_id,
                    _actor_for(runtime, layout, target, run_id, _previous),
                    runtime,
                    target,
                    compiled=_previous,
                    max_probe_rounds=_max_probe_rounds(layout),
                ).status()
                print(
                    json.dumps(
                        {
                            "detail": str(already),
                            "outcome": "AMENDMENT_ALREADY_APPLIED",
                            "run_id": run_id,
                            "status": current.value,
                        },
                        sort_keys=True,
                    )
                )
                return 0
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
                max_probe_rounds=_max_probe_rounds(layout),
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


def _attend_reviewer_id(route: Mapping[str, str]) -> str:
    """The attend reviewer's identity, read off the route admission bound.

    Not a name the agent gives itself -- an agent's claim about who it is is
    prose. The route, and its profile or model, is what the executed route
    receipt admitted and what the launcher dispatched. Shaped to planctl's
    identifier rule (`[A-Za-z][A-Za-z0-9._:-]*`), which `planctl review`
    refuses a reviewer id outside of.
    """
    if not route:
        return ""
    raw = "attend-reviewer:{0}:{1}".format(
        route.get("route"), route.get("profile") or route.get("model")
    )
    return re.sub(r"[^A-Za-z0-9._:-]", "-", raw)


def _attend_policy(layout: Mapping[str, Any]) -> att.AttendPolicy:
    raw = layout.get("attend") or {}
    reviewer_route = raw.get("reviewer_route") or {}
    return att.AttendPolicy(
        max_amendments_per_lane=int(raw.get("max_amendments_per_lane") or 0),
        max_amendments_per_run=int(raw.get("max_amendments_per_run") or 0),
        max_review_rounds=int(raw.get("max_review_rounds") or 3),
        route=raw.get("route") or {},
        reviewer_route=reviewer_route,
        planctl=raw.get("planctl"),
        plan_ir=raw.get("plan_ir"),
        planctl_root_dir=raw.get("planctl_root_dir"),
        validate_argv=tuple(raw.get("validate_argv") or ()),
        reviewer_id=_attend_reviewer_id(reviewer_route),
        reviewer_vendor=str(raw.get("reviewer_vendor") or ""),
    )


def _reviewer_hmac_key(layout: Mapping[str, Any]) -> str:
    """The plan-contract reviewer key, from the deployment's keys directory.

    `layout["keys_dir"]` is resolved once, in `_load_maestro_config`: the
    configured `keys_dir`, or `<runtime_state_root>/keys` where
    `route_admission.provision_keys` mints it. Ship, the `run start` approval
    gate and `run attend` all read it here and nowhere else. The key is never
    regenerated, because a new key silently invalidates every approval receipt
    already signed with the old one.
    """
    path = Path(layout["keys_dir"]) / admission.REVIEWER_HMAC_KEY_FILE
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


#: planctl's exit status for a verdict about the plan. Every other non-zero
#: status is planctl failing to give one.
_PLANCTL_VERDICT_EXIT = 1


def _run_planctl(
    binary: Path,
    argv: Sequence[str],
    *,
    key: Optional[str] = None,
    log: Optional[Path] = None,
) -> str:
    """One planctl subcommand, refused by exit status and nothing else.

    The refusal carries planctl's own output because a receipt or validation
    refusal names the IR field that is wrong, and that sentence is the whole
    value of running it. It is never parsed: the decision is the exit code.
    Exit 1 is planctl's verdict about the revision (`ATTEND_REVISION_REFUSED`).
    Any other failure -- an interpreter that cannot start, a signal, the
    interpreter's own exit 2 for a script it cannot open, planctl's exit 3
    for its own harness (remote runner, uncaught crash, a `--repo-root` that
    does not contain the IR) -- said nothing about
    the revision and is `ATTEND_PLANCTL_FAILED`, which attend contains.
    """
    environment = dict(os.environ)
    environment.pop(admission.REVIEWER_HMAC_KEY_ENV, None)
    if key is not None:
        environment[admission.REVIEWER_HMAC_KEY_ENV] = key
    try:
        result = subprocess.run(
            [sys.executable, str(binary), *argv],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    except OSError as exc:
        raise att.AttendRefused(
            att.PLANCTL_FAILED,
            "planctl {0} could not start: {1}: {2}".format(
                argv[0], type(exc).__name__, exc
            ),
        ) from exc
    if log is not None:
        # planctl prints its phase timings on stderr; success used to drop
        # them, so a 15-32 minute review could not be broken into phases.
        try:
            log.write_text(result.stderr or "", encoding="utf-8")
        except OSError:
            pass
    if result.returncode == 0:
        return result.stdout or ""
    detail = (result.stdout or "") + (result.stderr or "")
    message = "planctl {0} exited {1}: {2}".format(
        argv[0], result.returncode, detail.strip()[:2000]
    )
    if "review.remote_runner" in detail:
        # planctl runs its mutation pass on EC2 by default. Maestro never
        # falls back to a local run on its own; the operator opts in.
        message += (
            "\nhint: EC2 could not run the mutation pass; rerun with "
            "`maestro run attend --local` to run it on this machine"
        )
    if result.returncode != _PLANCTL_VERDICT_EXIT:
        raise att.AttendRefused(att.PLANCTL_FAILED, message)
    raise att.AttendRefused(att.REVISION_REFUSED, message)


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


def _attend_suite_files(
    store: ArtifactStore,
    repo: Path,
    run_id: str,
    lane: st.LaneProjection,
) -> dict[str, str]:
    """The accepted suite, read off the run repository for the operator agent.

    The same bytes the builder and every reviewer have in their checkouts,
    written into the operator's own `--no-checkout` worktree, which is
    never merged.
    """
    artifact_id = store.accepted_suite_artifact_id(run_id, lane.lane_id)
    if artifact_id is None:
        return {}
    record = store.get_lane_artifact(artifact_id)
    return tchain.read_suite(
        repo, tchain.suite_files(_record_as_lane_artifact(record, lane))
    )


def _plan_source_ir(pinned: Path) -> tuple[Optional[str], Optional[str]]:
    """The IR a pinned plan was projected from: `(sha256, recorded path)`.

    The sha256 is the signed receipt's `ir_sha256`; the path is
    `approval.source_ir.path`, which plans projected before it existed lack.
    Both are None for a plan with no approval (a scripted `run amend`).
    """
    try:
        document = json.loads(Path(pinned).read_bytes())
    except (OSError, ValueError):
        return None, None
    approval = document.get("approval") if isinstance(document, dict) else None
    if not isinstance(approval, Mapping):
        return None, None
    receipt = approval.get("receipt")
    expected = receipt.get("ir_sha256") if isinstance(receipt, Mapping) else None
    source = approval.get("source_ir")
    recorded = source.get("path") if isinstance(source, Mapping) else None
    return (
        expected if isinstance(expected, str) else None,
        recorded if isinstance(recorded, str) and recorded else None,
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _attend_configured_ir_conflict(
    policy: att.AttendPolicy, pinned: Path, revision: int
) -> str:
    """Why `attend.plan_ir` names another IR than this revision's, else "".

    The pinned plan's receipt names its IR by sha256; a configured path to
    different bytes is a run-scoped input read from deployment config.
    """
    expected, _ = _plan_source_ir(pinned)
    configured = policy.plan_ir
    if expected is None or configured is None or not configured.is_file():
        return ""
    found = _file_sha256(configured)
    if found == expected:
        return ""
    return (
        "attend.plan_ir {0} has sha256 {1}; revision {2} was projected from "
        "the IR with sha256 {3}".format(configured, found, revision, expected)
    )


def _attend_plan_ir(
    policy: att.AttendPolicy,
    plans_dir: Path,
    run_id: str,
    revision: int,
    pinned: Optional[Path] = None,
    repo: Optional[Path] = None,
) -> Path:
    """The Plan IR the live revision was projected from.

    An approved plan names its IR by the receipt's sha256, and a plan the
    projection authored also names its path (`approval.source_ir`). Places,
    in order: the copy `run attend` itself wrote for a revision it applied,
    the plan's recorded path under the run repository, then `attend.plan_ir`
    -- each taken only when its bytes are that sha256. `attend.plan_ir`
    naming other bytes refuses `ATTEND_PLAN_IR_MISMATCH`: it never stands in
    for the run's own IR. A plan with no receipt keeps the unverified order.
    None found is a refusal rather than a fallback -- the pinned plan
    artifact is a *projection* of an IR, and handing it to an agent asked to
    edit an IR would produce a revision the ingress cannot read.
    """
    written = plans_dir / "{0}.r{1}.ir.json".format(run_id, revision)
    expected, recorded = _plan_source_ir(pinned) if pinned is not None else (None, None)
    if expected is None:
        if written.is_file():
            return written
        if policy.plan_ir is not None and policy.plan_ir.is_file():
            return policy.plan_ir
        raise att.AttendRefused(
            att.PLAN_IR_UNRESOLVED,
            "no Plan IR for revision {0}: looked at {1} and attend.plan_ir "
            "({2})".format(revision, written, policy.plan_ir or "unset"),
        )
    conflict = _attend_configured_ir_conflict(policy, pinned, revision)
    if conflict:
        raise att.AttendRefused(att.PLAN_IR_MISMATCH, conflict)
    candidates = [written]
    if recorded is not None:
        candidates.append(Path(repo or ".") / recorded)
    if policy.plan_ir is not None:
        candidates.append(policy.plan_ir)
    for candidate in candidates:
        if candidate.is_file() and _file_sha256(candidate) == expected:
            return candidate
    raise att.AttendRefused(
        att.PLAN_IR_UNRESOLVED,
        "no Plan IR with sha256 {0} (revision {1}'s receipt): looked at {2}; "
        "re-project the plan with plan_author_cli.py so it records its IR, "
        "or set attend.plan_ir to that IR".format(
            expected, revision, ", ".join(str(c) for c in candidates)
        ),
    )


def _attend_planroot_dir(policy: att.AttendPolicy, runtime: Any) -> Path:
    """The durable directory attend keeps its planctl roots in.

    `attend.planctl_root_dir`, else a per-deployment directory in the user's
    own state location. Never under `runtime_state_root`: planctl uploads a
    revision's root to its mutation runner and refuses one that is the home
    directory, contains it, or sits in `~/.maestro`, which holds the signing
    keys. The root is durable (it outlives the scratch trees an operator
    amends from) and uploadable, so what it holds is the IR, its render, the
    findings and the sha256-verified pinned sources -- never a key.
    """
    if policy.planctl_root_dir is not None:
        return Path(policy.planctl_root_dir)
    name = Path(runtime.path).name
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        state = os.environ.get("XDG_STATE_HOME")
        base = Path(state) if state and os.path.isabs(state) else Path.home() / ".local" / "state"
    return base / "maestro" / name / "planroots"


def _planroot_unsafe(root: Path, keys_dir: Optional[Path] = None) -> str:
    """Why planctl would refuse to upload `root`, or "" when it would not.

    The rule is planctl-ec2.sh `upload_root_is_unsafe`: the root is not the
    home directory, does not contain it, and is not `~/.maestro` or inside
    it. A root that is, contains or sits in `keys_dir`, or that holds a file
    with the bytes of one of its files, is refused too.
    """
    home = Path(os.environ.get("HOME") or Path.home()).resolve()
    resolved = Path(root).resolve()
    maestro_home = home / ".maestro"
    if resolved == home or home.is_relative_to(resolved):
        return "{0} is, or contains, the home directory".format(root)
    if resolved == maestro_home or resolved.is_relative_to(maestro_home):
        return "{0} is inside {1}, which holds signing keys".format(root, maestro_home)
    if keys_dir is None:
        return ""
    keys = Path(keys_dir).resolve()
    if resolved == keys or resolved.is_relative_to(keys) or keys.is_relative_to(resolved):
        return "{0} overlaps the keys directory {1}".format(root, keys)
    if keys.is_dir() and resolved.is_dir():
        secrets = {
            hashlib.sha256(item.read_bytes()).hexdigest()
            for item in keys.rglob("*")
            if item.is_file()
        }
        for item in resolved.rglob("*"):
            if item.is_file() and hashlib.sha256(item.read_bytes()).hexdigest() in secrets:
                return "{0} holds a copy of a key from {1}".format(item, keys)
    return ""


def _attend_planroot_base(
    policy: att.AttendPolicy, runtime: Any, layout: Mapping[str, Any]
) -> Path:
    """`_attend_planroot_dir`, refused PLANROOT_UNSAFE when planctl would refuse it."""
    base = _attend_planroot_dir(policy, runtime)
    keys = layout.get("keys_dir")
    reason = _planroot_unsafe(base, Path(keys) if keys else None)
    if reason:
        raise att.AttendRefused(
            att.PLANROOT_UNSAFE,
            "{0}; set attend.planctl_root_dir to a directory outside the home "
            "directory root, ~/.maestro and keys_dir".format(reason),
        )
    return base


def _attend_planroot_check(
    policy: att.AttendPolicy, runtime: Any, layout: Mapping[str, Any]
) -> tuple[bool, str]:
    """Preflight's reading of `_attend_planroot_base`: the typed reason, not a raise."""
    try:
        return True, str(_attend_planroot_base(policy, runtime, layout))
    except att.AttendRefused as refused:
        return False, "{0}: {1}".format(refused.code, refused.detail)


def _attend_root(base: Path, run_id: str, revision: int) -> Path:
    """The planctl root attend owns for one revision, under `base`.

    The revision's IR, everything planctl writes beside it, and a copy of
    every pinned source the IR cites live here, and attend passes this
    directory as `--repo-root`. It is a function of the run and the
    revision and never of where some earlier IR happened to be written: on
    run 2acd2f5f the r6 IR was written next to a hand amendment's r5 IR in
    `/tmp`, outside the `--repo-root` attend passed, and planctl refused it.
    `base` is `_attend_planroot_base`, never `runtime_state_root`: planctl
    refuses to upload a root inside `~/.maestro`.
    """
    return Path(base) / run_id / "r{0}".format(int(revision))


def _attend_owned_ir(base: Path, run_id: str, revision: int) -> Path:
    return _attend_root(base, run_id, revision) / "{0}.r{1}.ir.json".format(
        run_id, int(revision)
    )


def _attend_root_record(base: Path, run_id: str, revision: int) -> Path:
    """Which plan the owned IR is the IR of, by the pinned plan's sha256.

    Without it an IR a refused attempt left in the root would read as the
    revision's own once a hand amendment applied that revision number.
    """
    return _attend_root(base, run_id, revision) / "{0}.r{1}.root.json".format(
        run_id, int(revision)
    )


def _attend_record_root(runtime: Any, base: Path, run_id: str, revision: int) -> None:
    ir = _attend_owned_ir(base, run_id, revision)
    pinned = _pinned_plan_artifact(runtime, run_id, revision)
    _attend_root_record(base, run_id, revision).write_text(
        json.dumps(
            {
                "plan_revision": int(revision),
                "repo_root": str(ir.parent),
                "ir": ir.name,
                "ir_sha256": hashlib.sha256(ir.read_bytes()).hexdigest(),
                "plan_sha256": hashlib.sha256(pinned.read_bytes()).hexdigest(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _attend_root_current(
    runtime: Any,
    base: Path,
    run_id: str,
    revision: int,
    policy: Optional[att.AttendPolicy] = None,
) -> tuple[bool, str]:
    """The owned IR exists and its record names the revision's pinned plan."""
    ir = _attend_owned_ir(base, run_id, revision)
    record_path = _attend_root_record(base, run_id, revision)
    pinned = _pinned_plan_artifact(runtime, run_id, revision)
    if not ir.is_file() or not record_path.is_file():
        return False, "{0} is not recorded".format(ir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not pinned.is_file() or record.get("plan_sha256") != hashlib.sha256(
        pinned.read_bytes()
    ).hexdigest():
        return False, "{0} records another plan than {1}".format(record_path, pinned)
    if record.get("ir_sha256") != hashlib.sha256(ir.read_bytes()).hexdigest():
        return False, "{0} changed after it was recorded".format(ir)
    expected, _ = _plan_source_ir(pinned)
    if expected is not None and record.get("ir_sha256") != expected:
        return False, "{0} holds the IR with sha256 {1}; {2} was projected from {3}".format(
            ir, record.get("ir_sha256"), pinned, expected
        )
    if policy is not None:
        conflict = _attend_configured_ir_conflict(policy, pinned, revision)
        if conflict:
            return False, conflict
    json.loads(ir.read_bytes())
    return True, str(ir)


def _attend_migrate_roots(runtime: Any, base: Path, run_id: str) -> list[Path]:
    """Move roots an earlier release laid out under `runtime_state_root`.

    Those roots sat in `runtime_state_root/plans/<run>/r<N>/` beside the
    pinned plan, which is inside `~/.maestro`, so planctl refused to upload
    them. Everything but the pinned plan moves to `base`, which keeps the
    revision's own layout, and a moved record's `repo_root` follows it. A
    file the destination already holds is left where it is.
    """
    old = Path(runtime.path) / "plans" / run_id
    moved: list[Path] = []
    if not old.is_dir():
        return moved
    for source in sorted(old.glob("r[0-9]*")):
        if not source.is_dir():
            continue
        revision = source.name[1:]
        destination = Path(base) / run_id / source.name
        for item in sorted(source.rglob("*")):
            if not item.is_file() or item.relative_to(source) == Path(_PLAN_ARTIFACT_NAME):
                continue
            target = destination / item.relative_to(source)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))
            moved.append(target)
        record = destination / "{0}.r{1}.root.json".format(run_id, revision)
        if record in moved:
            data = json.loads(record.read_text(encoding="utf-8"))
            data["repo_root"] = str(destination)
            record.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return moved


def _attend_copy_sources(
    document: Mapping[str, Any], root: Path, bases: Sequence[Path]
) -> list[str]:
    """Copy the IR's pinned sources and link targets into `root`.

    A source is taken from the first base whose bytes match the sha256 the
    IR declares, so a base is somewhere to find bytes and never an authority
    over them. An absolute path is read by planctl where it is and copied
    nowhere. Returns the required sources no base holds; planctl names
    them itself when the caller lets it run.
    """
    resolved_root = root.resolve()

    def destination(raw: Any) -> Optional[Path]:
        if not isinstance(raw, str) or not raw or os.path.isabs(raw):
            return None
        target = (root / raw).resolve()
        if not target.is_relative_to(resolved_root):
            raise att.AttendRefused(
                att.PLANCTL_FAILED,
                "source path {0} escapes the planctl root {1} attend lays "
                "out".format(raw, root),
            )
        return target

    def place(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_bytes(data)
        scratch.replace(target)

    missing = []
    for source in document.get("source_artifacts") or ():
        if not isinstance(source, Mapping):
            continue
        raw = source.get("path")
        target = destination(raw)
        if target is None:
            continue
        for base in bases:
            candidate = Path(base) / raw
            if not candidate.is_file():
                continue
            data = candidate.read_bytes()
            if hashlib.sha256(data).hexdigest() == source.get("sha256"):
                if candidate.resolve() != target:
                    place(target, data)
                break
        else:
            if source.get("required"):
                missing.append(str(raw))
    for link in document.get("links") or ():
        if not isinstance(link, Mapping):
            continue
        raw = link.get("canonical_target")
        target = destination(raw)
        if target is None or target.is_file():
            continue
        for base in bases:
            candidate = Path(base) / raw
            if candidate.is_file():
                place(target, candidate.read_bytes())
                break
    return missing


def _attend_clear_foreign_root(
    base: Path, run_id: str, revision: int, ir_sha256: str
) -> None:
    """Empty a revision's root recorded for another IR before re-laying it.

    A root an earlier release supplied from a deployment-wide `attend.plan_ir`
    holds that IR's sources too; laying the run's own IR over it would leave
    them beside it, for planctl to upload and an agent to read.
    """
    record_path = _attend_root_record(base, run_id, revision)
    if not record_path.is_file():
        return
    try:
        recorded = json.loads(record_path.read_text(encoding="utf-8")).get("ir_sha256")
    except ValueError:
        recorded = None
    if recorded != ir_sha256:
        shutil.rmtree(_attend_root(base, run_id, revision))


def _attend_supply_root(
    policy: att.AttendPolicy,
    runtime: Any,
    base: Path,
    store: ArtifactStore,
    repo: Path,
    run_id: str,
    revision: int,
) -> str:
    """Lay out the owned root of a revision attend did not write itself.

    Roots an earlier release kept under `runtime_state_root` are moved to
    `base` first (`_attend_migrate_roots`); a revision that leaves recorded
    needs no more.

    The IR is found where `_attend_plan_ir` always looked -- beside the
    revision's recorded plan artifact, then `attend.plan_ir` -- and copied
    in with its sources, which are read from that IR's directory and its
    ancestors (planctl's root contains its IR) and then the run repository.
    """
    moved = _attend_migrate_roots(runtime, base, run_id)
    if moved:
        if _attend_root_current(runtime, base, run_id, revision, policy)[0]:
            return "moved {0} files from runtime_state_root into {1}".format(
                len(moved), _attend_root(base, run_id, revision)
            )
    legacy = Path(plan_artifact_ref_for(store, run_id, revision)).resolve().parent
    found = _attend_plan_ir(
        policy, legacy, run_id, revision,
        pinned=_pinned_plan_artifact(runtime, run_id, revision), repo=repo,
    )
    data = found.read_bytes()
    document = json.loads(data)
    if not isinstance(document, dict):
        raise att.AttendRefused(att.PLAN_IR_UNRESOLVED, "{0} is not an object".format(found))
    owned = _attend_owned_ir(base, run_id, revision)
    _attend_clear_foreign_root(base, run_id, revision, _file_sha256(found))
    owned.parent.mkdir(parents=True, exist_ok=True)
    bases = [found.resolve().parent, *found.resolve().parent.parents, repo]
    missing = _attend_copy_sources(document, owned.parent, bases)
    if missing:
        raise att.AttendRefused(
            att.PLAN_IR_UNRESOLVED,
            "no copy of required sources {0} with the sha256 {1} declares".format(
                ", ".join(missing), found
            ),
        )
    owned.write_bytes(data)
    _attend_record_root(runtime, base, run_id, revision)
    return "copied {0} and its sources into {1}".format(found, owned.parent)


def _attend_final_review(
    store: ArtifactStore, run_id: str, wait: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The final integration review an `AMENDMENT_REQUIRED` park answers.

    Read by the artifact id the lane's own `USER_WAIT` names, so the author
    and reviewer see the review that parked this lane and no other.
    """
    if not isinstance(wait, Mapping):
        return {}
    if wait.get("wait_reason") != st.WaitReason.AMENDMENT_REQUIRED.value:
        return {}
    artifact_id = wait.get("final_review_artifact_id")
    for row in store.run_artifacts_of_kind(
        run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW
    ):
        if row["artifact_id"] == artifact_id:
            return dict(row["payload"])
    return {}


def _attend_lane_evidence(
    store: ArtifactStore,
    runtime: RuntimeStateRoot,
    layout: Mapping[str, Any],
    run_id: str,
    lane_id: str,
    wait_reason: str,
    compiled: st.CompiledPlan,
) -> att.LaneEvidence:
    """What one parked lane contributes to a batch, assembled from the ledger."""
    lane = next(item for item in compiled.lanes if item.lane_id == lane_id)
    stage = store.lane_stage(run_id, lane_id)
    repo = Path(str(store._run(run_id)["target_repository_root"]))
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
    latest_review = store.latest_lane_artifact_payload(
        run_id, lane_id, st.ArtifactKind.CODE_REVIEW
    )
    if isinstance(latest_review, Mapping):
        raw = latest_review.get("failure_output")
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            failures = tuple(str(item) for item in raw)
    wait = store.latest_lane_artifact_payload(
        run_id, lane_id, st.ArtifactKind.USER_WAIT
    )
    return att.LaneEvidence(
        lane_id=lane_id,
        wait_reason=wait_reason,
        stage=stage.value,
        round_number=len(reviews),
        public_contract=contract,
        reviews=tuple(reviews),
        failure_output=failures,
        lane_gates=_attend_lane_gates(
            runtime,
            run_id,
            lane_id,
            bool(layout.get("stall_regression_on_findings")),
        ),
        suite_files=_attend_suite_files(store, repo, run_id, lane),
        final_review=_attend_final_review(store, run_id, wait),
        fault=dict((wait or {}).get("fault") or {}),
    )


def _attend_round_stem(next_revision: int, session_id: str, review_round: int) -> str:
    """`r<N>.<session>.round<k>`: one path per round per attend session.

    The session is in the name because two attend sessions on one run can
    author the same revision number; without it the second would overwrite
    the first's round files and leave an `ATTEND_REVIEW.revision_ref` naming
    bytes it never reviewed.
    """
    if not session_id:
        raise att.AttendRefused(att.NO_REVISION, "no attend session id")
    return "r{0}.{1}.round{2}".format(next_revision, session_id, review_round)


def _attend_request(
    store: ArtifactStore,
    runtime: RuntimeStateRoot,
    layout: Mapping[str, Any],
    run_id: str,
    parked: Sequence[tuple[str, str]],
    compiled: st.CompiledPlan,
    ir_path: Path,
    operator_cwd: Path,
    review_round: int = 1,
    reviewer_findings: Sequence[Mapping[str, Any]] = (),
    previous_revision: str = "",
    *,
    session_id: str,
) -> att.OperatorRequest:
    """Everything the operator agent reads for one batch, from the ledger."""
    next_revision = compiled.plan_revision + 1
    return att.OperatorRequest(
        run_id=run_id,
        plan_revision=compiled.plan_revision,
        next_plan_revision=next_revision,
        lanes=tuple(
            _attend_lane_evidence(
                store, runtime, layout, run_id, lane_id, reason, compiled
            )
            for lane_id, reason in parked
        ),
        ir_path=str(ir_path),
        revision_out_path=str(
            operator_cwd
            / HerdrStageActor._OPERATOR_OUT
            / (_attend_round_stem(next_revision, session_id, review_round) + ".ir.json")
        ),
        amendment_rules=ATTEND_AMENDMENT_RULES,
        allowed_lane_ids=att.batch_scope(compiled.lanes, parked),
        review_round=review_round,
        reviewer_findings=tuple(reviewer_findings),
        previous_revision_path=previous_revision,
    )


def _attend_review_request(
    policy: att.AttendPolicy,
    request: att.OperatorRequest,
    revision: Path,
    rationales: Sequence[Mapping[str, Any]],
    review_round: int,
    revision_sha256: str,
    reviewer_cwd: Path,
    *,
    session_id: str,
) -> att.ReviewRequest:
    """Everything the attend reviewer reads for one round.

    The revision is snapshotted from the bytes whose sha256 the round
    recorded, and the question surface -- planctl's own rendering, the bytes
    `planctl review` binds the reviewer's findings to -- is computed over
    that snapshot, not over the author's live file.
    """
    out = reviewer_cwd / HerdrStageActor._OPERATOR_OUT
    out.mkdir(parents=True, exist_ok=True)
    stem = _attend_round_stem(request.next_plan_revision, session_id, review_round)
    snapshot = out / (stem + ".reviewed.ir.json")
    snapshot.write_bytes(att.reviewed_bytes(revision, revision_sha256))
    surface = _run_planctl(
        _planctl_binary(policy), ["question-surface", str(snapshot), "--json"]
    )
    return att.ReviewRequest(
        run_id=request.run_id,
        plan_revision=request.plan_revision,
        next_plan_revision=request.next_plan_revision,
        review_round=review_round,
        lanes=request.lanes,
        current_ir_path=request.ir_path,
        revision_ir_path=str(snapshot),
        revision_sha256=revision_sha256,
        question_surface=surface,
        rationales=tuple(rationales),
        findings_out_path=str(out / (stem + ".review-findings.json")),
        amendment_rules=request.amendment_rules,
        allowed_lane_ids=request.allowed_lane_ids,
    )


def _attend_file_issue(repo: Path, title: str, body: str) -> dict[str, str]:
    """File one GitHub issue in the run's repository, deduplicated by title.

    The search is a quoted phrase restricted to titles (`"<title>" in:title`),
    and it is still a search, so a hit counts only when its title is exactly
    this one. Any `gh` failure is reported, never raised:
    the amendment is already applied, and an issue that could not be filed
    must not undo it.
    """

    def gh(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["gh", *argv],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )

    def failed(result: subprocess.CompletedProcess) -> dict[str, str]:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        return {
            "status": att.ISSUE_FAILED,
            "detail": lines[0] if lines else "gh exited {0}".format(result.returncode),
        }

    try:
        listed = gh(
            [
                "issue", "list", "--state", "open",
                "--search", '"{0}" in:title'.format(title.replace('"', "")),
                "--json", "title,url",
            ]
        )
        if listed.returncode != 0:
            return failed(listed)
        for item in json.loads(listed.stdout or "[]"):
            if isinstance(item, Mapping) and item.get("title") == title:
                return {"status": att.ISSUE_EXISTS, "url": str(item.get("url") or "")}
        created = gh(["issue", "create", "--title", title, "--body", body])
    except (OSError, ValueError) as exc:
        return {"status": att.ISSUE_FAILED, "detail": "{0}: {1}".format(type(exc).__name__, exc)}
    if created.returncode != 0:
        return failed(created)
    return {"status": att.ISSUE_CREATED, "url": (created.stdout or "").strip()}


def _write_review_findings(
    binary: Path,
    ir: Path,
    out: Path,
    policy: att.AttendPolicy,
    review_findings: Sequence[Any],
) -> None:
    """Bind the reviewer's findings to the revision and to the reviewer.

    planctl refuses a findings file whose plan id, question surface or
    reviewer does not match what it is signing, so a stale or foreign answer
    cannot ride on a later revision.
    """
    try:
        surface = json.loads(
            _run_planctl(binary, ["question-surface", str(ir), "--json"])
        )
    except ValueError as exc:
        # planctl exited 0 and printed something that is not its JSON: that
        # is planctl failing, not a verdict about the revision.
        raise att.AttendRefused(
            att.PLANCTL_FAILED, "question surface: {0}".format(exc)
        ) from exc
    out.write_text(
        json.dumps(
            {
                "schema_version": "plan-contract-review-findings.v2",
                "plan_id": surface.get("plan_id"),
                "question_surface_sha256": surface.get("question_surface_sha256"),
                "reviewer_id": policy.reviewer_id,
                "reviewer_vendor": policy.reviewer_vendor,
                "findings": list(review_findings),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _attend_project(
    policy: att.AttendPolicy,
    layout: Mapping[str, Any],
    repo: Path,
    plans_dir: Path,
    run_id: str,
    revision_bytes: bytes,
    revision: int,
    review_findings: Sequence[Any],
    local: bool = False,
    bases: Optional[Sequence[Path]] = None,
) -> tuple[st.CompiledPlan, Path]:
    """Validate, approve, project and compile one reviewed revision.

    The same four steps the human ran by hand on an earlier run, in the same
    order and against the same binary. `revision_bytes` are exactly the bytes
    the reviewer passed. They are written into `plans_dir`, the revision's
    own planctl root (`_attend_root`), with the sources they cite copied in
    from `bases` (default: the run repository), and `plans_dir` is the
    `--repo-root` every planctl step is given: planctl refuses an IR outside
    its root, and a revision that is applied has to survive the operator
    agent's scratch tree.

    `approval.receipt_path` is where planctl requires the receipt to be
    written, relative to the IR. Where Maestro writes the receipt is
    Maestro's fact, not the author's, so it is set here on the copy before
    findings, render and review -- an author that copied the previous
    revision's value would otherwise be refused `receipt.path` every time.
    `approval` is outside the question surface, so the reviewer's findings
    still bind.

    `review_findings` is the independent reviewer agent's answer to the
    review question, and only the reviewer's: the author never answers its
    own review. `planctl review` signs as that reviewer.
    """
    if not policy.reviewer_id or not policy.reviewer_vendor:
        raise att.AttendRefused(
            att.REVIEWER_UNCONFIGURED,
            "attend.reviewer_route and attend.reviewer_vendor name no reviewer",
        )
    binary = _planctl_binary(policy)
    key = _reviewer_hmac_key(layout)
    plans_dir.mkdir(parents=True, exist_ok=True)
    stem = "{0}.r{1}".format(run_id, revision)
    ir = plans_dir / (stem + ".ir.json")
    rendered = plans_dir / (stem + ".html")
    receipt = plans_dir / (stem + ".receipt.json")
    plan_out = plans_dir / (stem + ".plan.json")
    findings = plans_dir / (stem + ".review-findings.json")
    for stale in (rendered, receipt, plan_out, findings):
        if stale.exists():
            stale.unlink()
    try:
        document = json.loads(revision_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise att.AttendRefused(
            att.REVISION_REFUSED, "revision IR is not JSON: {0}".format(exc)
        ) from exc
    if not isinstance(document, dict):
        raise att.AttendRefused(att.REVISION_REFUSED, "revision IR is not an object")
    approval = document.get("approval")
    if not isinstance(approval, dict):
        approval = {}
    document["approval"] = {**approval, "receipt_path": receipt.name}
    _attend_copy_sources(document, plans_dir, (repo,) if bases is None else bases)
    ir.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    root = ["--repo-root", str(plans_dir)]
    _write_review_findings(binary, ir, findings, policy, review_findings)
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
            "--findings",
            str(findings),
            *root,
            *(["--local"] if local else []),
        ],
        key=key,
        log=plans_dir / (stem + ".planctl-review.log"),
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
        # `--require-approved` verifies the receipt's HMAC, so validate needs
        # the same key review signed with; without it planctl refuses
        # `receipt.key_missing` on every revision.
        key=key,
    )
    try:
        ingress.author_from_plan_contract(
            ir, receipt, plan_out, repo, rendered,
            reviewer_key=plan_approval.key_bytes(key),
        )
    except Exception as exc:
        raise att.AttendRefused(
            att.REVISION_REFUSED, "{0}: {1}".format(type(exc).__name__, exc)
        ) from exc
    try:
        compiled = _compile_plan(plan_out, revision=revision, ref=str(plan_out))
    except PlanCompileError as exc:
        raise att.AttendRefused(att.REVISION_REFUSED, str(exc)) from exc
    return compiled, plan_out


def _run_attend(args: argparse.Namespace, verb: str = "attend") -> int:
    """Run the factory and author the amendment its attendable parks need.

    Everything about a lane's lifecycle is unchanged. This verb runs the same
    scheduler `run resume` runs, and when that scheduler settles with lanes
    parked `NO_PROGRESS`, `AMENDMENT_REQUIRED` or `LANE_FAULT` it does what
    the operator would have done by hand instead of returning to a prompt:
    dispatches one agent to author one plan revision for all of them, has a
    second agent of a different declared vendor review it, validates and
    projects it exactly as `run amend` requires, and applies it through
    `apply_factory_amendment`. A `LANE_FAULT` the author declares a runtime
    defect files an issue and stays parked. `PAUSE`, and any lane past its
    bound, park as they do today. `run start` and `run resume` end here
    whenever the deployment enabled attend; `verb` is which one ran.
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
    console = step_log.RunReporter(run_id, runtime.path)
    state: dict[str, Any] = {"passes": 0}

    def plan_base() -> Path:
        # Unvalidated: preflight reports an unsafe directory as a refused item.
        return _attend_planroot_dir(policy, runtime)

    def build_scheduler(plan: st.CompiledPlan) -> tuple[Any, Any]:
        preflight = None
        if state["passes"]:
            actor = _actor_for(runtime, layout, target, run_id, plan)
        else:
            # Once per invocation, before the first dispatch: a later pass
            # runs after an amendment this session applied, in the same
            # environment the first pass already proved.
            revision = plan.plan_revision
            preflight = _RunPreflight(
                layout, runtime, store, run_id, target, console,
                verb=verb, attend=True, local=bool(getattr(args, "local", False)),
                plan_ir=_attend_owned_ir(plan_base(), run_id, revision),
                plan_ir_current=lambda: _attend_root_current(
                    runtime, _attend_planroot_base(policy, runtime, layout),
                    run_id, revision, policy,
                ),
                plan_ir_supply=lambda: _attend_supply_root(
                    policy, runtime, _attend_planroot_base(policy, runtime, layout),
                    store, repo, run_id, revision,
                ),
                planroot_check=lambda: _attend_planroot_check(policy, runtime, layout),
            )
            preflight.run_wide()
            actor = preflight.actor_for(plan)
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
            max_probe_rounds=_max_probe_rounds(layout),
            preflight=preflight,
        )
        return actor, scheduler

    def run_scheduler(
        plan: st.CompiledPlan, held: Sequence[str] = ()
    ) -> st.RunStatus:
        actor, scheduler = build_scheduler(plan)
        state["actor"] = actor
        actor.restore_layout(run_id, store.active_projection(run_id))
        # Attend answers a NO_PROGRESS park; resuming it here first would
        # regrant rounds the lane has already shown it cannot use (#331).
        scheduler.resume_waiting(held, hold_no_progress=True)
        status = scheduler.run()
        state["passes"] = state["passes"] + 1
        return status

    def role_cwd_for(lane_id: str, role: str) -> Path:
        return runtime.path / "worktrees" / run_id / lane_id / role / "checkout"

    def request_for(
        parked: Sequence[tuple[str, str]],
        plan: st.CompiledPlan,
        review_round: int,
        reviewer_findings: Sequence[Mapping[str, Any]],
        previous_revision: str,
    ) -> att.OperatorRequest:
        base = _attend_planroot_base(policy, runtime, layout)
        current, detail = _attend_root_current(
            runtime, base, run_id, plan.plan_revision
        )
        if not current:
            raise att.AttendRefused(att.PLAN_IR_UNRESOLVED, detail)
        return _attend_request(
            store,
            runtime,
            layout,
            run_id,
            parked,
            plan,
            _attend_owned_ir(base, run_id, plan.plan_revision),
            role_cwd_for(parked[0][0], "operator"),
            review_round,
            reviewer_findings,
            previous_revision,
            session_id=session_id,
        )

    def review_request_for(
        request: att.OperatorRequest,
        revision: Path,
        rationales: Sequence[Mapping[str, Any]],
        review_round: int,
        revision_sha256: str,
    ) -> att.ReviewRequest:
        return _attend_review_request(
            policy,
            request,
            revision,
            rationales,
            review_round,
            revision_sha256,
            role_cwd_for(request.lane_id, HerdrStageActor._ATTEND_REVIEWER),
            session_id=session_id,
        )

    def attend_ctx(request: Any, role: str) -> LaneContext:
        lane = next(
            item
            for item in store.active_projection(run_id)
            if item.lane_id == request.lane_id
        )
        return LaneContext(
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
                    "review_round": request.review_round,
                    "role": role,
                    "schema_version": st.CANONICAL_SCHEMA_VERSION,
                }
            ),
            stage=st.LaneStage(request.stage),
            artifacts={},
        )

    def dispatch(request: att.OperatorRequest) -> Mapping[str, Any]:
        propose = getattr(state.get("actor"), "attend_operator", None)
        if propose is None:
            raise att.AttendRefused(
                att.OPERATOR_FAILED, "actor cannot dispatch an operator agent"
            )
        return propose(attend_ctx(request, "operator"), request)

    def review(request: att.ReviewRequest) -> Mapping[str, Any]:
        judge = getattr(state.get("actor"), "attend_reviewer", None)
        if judge is None:
            raise att.AttendRefused(
                att.REVIEWER_FAILED, "actor cannot dispatch an attend reviewer"
            )
        return judge(attend_ctx(request, HerdrStageActor._ATTEND_REVIEWER), request)

    def project(
        revision_bytes: bytes, revision: int, review_findings: list
    ) -> st.CompiledPlan:
        # Sources come from the root of the revision this one amends, which
        # preflight or this session's last apply recorded, then the repo.
        base = _attend_planroot_base(policy, runtime, layout)
        plan, plan_out = _attend_project(
            policy, layout, repo, _attend_root(base, run_id, revision), run_id,
            revision_bytes, revision, review_findings,
            local=bool(getattr(args, "local", False)),
            bases=(_attend_root(base, run_id, revision - 1), repo),
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
        _attend_record_root(
            runtime, _attend_planroot_base(policy, runtime, layout),
            run_id, plan.plan_revision,
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
    # Bound before the loop: the STOP record below runs on every exit, and a
    # scheduler that raises anything but AttendRefused (FDAdb run be064e58:
    # LaunchFailed from restore_layout) otherwise hid its own traceback behind
    # `UnboundLocalError: applied`.
    applied: tuple[att.AppliedAmendment, ...] = ()
    status = st.RunStatus.WAITING
    parks: tuple[tuple[str, str], ...] = ()
    escalated: tuple[att.RuntimeDefect, ...] = ()
    try:
        try:
            console.opened(
                verb,
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
                    review_request_for=review_request_for,
                    review=review,
                    project=project,
                    apply_amendment=apply,
                    file_issue=lambda title, body: _attend_file_issue(
                        repo, title, body
                    ),
                    deviations_path=runtime.path / "plans" / run_id
                    / "{0}.deviations.md".format(run_id),
                    say=console.step,
                )
                status = outcome.status
                stop_reason = outcome.stop_reason
                applied = outcome.applied
                parks = outcome.parks
                escalated = outcome.escalated
            except att.AttendRefused as refused:
                stop_reason = refused.code
                raise
            except BaseException as exc:
                stop_reason = type(exc).__name__
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
                        "revisions_applied": len(
                            {item.amendment_artifact_id for item in applied}
                        ),
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
                "revisions_applied": len(
                    {item.amendment_artifact_id for item in applied}
                ),
                # Every lane the run is still waiting on, and why: what the
                # operator has to answer, since nothing here will.
                "parks": [
                    {"lane_id": lane_id, "wait_reason": reason}
                    for lane_id, reason in parks
                ],
                "runtime_defects": [
                    {
                        "lane_id": item.lane_id,
                        "fault": dict(item.fault),
                        "issue": dict(item.issue),
                    }
                    for item in escalated
                ],
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
                max_probe_rounds=_max_probe_rounds(layout),
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
    attend.add_argument(
        "--local",
        action="store_true",
        help="run planctl's mutation pass on this machine instead of EC2",
    )
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
    except RunPreflightRefused as exc:
        # The structured list beside the rendered report: one refusal names
        # every missing item, each with its lane and remedy.
        print(
            json.dumps(
                {
                    "detail": exc.report,
                    "outcome": exc.code,
                    "refused": [item.as_payload() for item in exc.items],
                },
                sort_keys=True,
            )
        )
        return 3
    except FactoryRefused as exc:
        return _RunRefused(exc.code, str(exc)).emit()
    except st.LiveRunCutoverRequired as exc:
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
    except rc.ReviewContractError as exc:
        # A review tree that cannot be provisioned, or a project no available
        # interpreter satisfies, is a fault of this machine. It is already kept
        # out of the builder's findings by being raised rather than recorded;
        # this keeps it out of a traceback too, so the frozen operator surface
        # answers with the same typed JSON it answers every other refusal with.
        # Every other `ReviewContractError` names a factory invariant, not an
        # environment, and is deliberately left to surface as it does today.
        detail = cr.suite_environment_detail(exc)
        if detail is None:
            raise
        return _RunRefused(cr.SUITE_ENVIRONMENT_OUTCOME, detail).emit()
    finally:
        _INVOCATION_WORKSPACE.reset(invocation)


if __name__ == "__main__":
    raise SystemExit(main())
