#!/usr/bin/env python3
"""Maestro public operator workflow (§11)."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
from contextlib import redirect_stdout as _redirect_stdout
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml
from rich.console import Console as RichConsole
from rich.markup import escape as rich_escape
from rich import box as rich_box
from rich.panel import Panel as RichPanel
from rich.table import Table as RichTable
from rich.text import Text as RichText

from adw_modules import agent_pi
from adw_modules import code_review
from adw_modules import coordinator as coordinator
from adw_modules import coordinator_store
from adw_modules import deliver as deliver_module
from adw_modules import finalization
from adw_modules import finalization_window
from adw_modules import launcher
from adw_modules import lifecycle as lc
from adw_modules import participant
from adw_modules import plan_author
from adw_modules import plan_contract_ingress
from adw_modules import runner_resolution
from adw_modules import plan_amendment
from adw_modules import plan_digest
from adw_modules import plan_model
from adw_modules import plan_validate as pv
from adw_modules import publication
from adw_modules import receipt_crypto
from adw_modules import retry_policy
from adw_modules import review_convergence
from adw_modules import review_findings
from adw_modules import route_admission
from adw_modules import route_receipts
from adw_modules import scheduler
from adw_modules import scheduler_types
from adw_modules import watchdog
from adw_modules import worktree
from adw_modules import salvage
from adw_modules import attempt_identity

from adw_modules import workspace_author
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


class _RunRefused(RuntimeError):
    """A run verb's refusal, named by the refusal vocabulary rather than by
    whatever Python class happened to carry it.

    `_run_start` and `_run_resume` both ended in
    `_refusal(type(exc).__name__.upper(), str(exc))`, which is not a
    vocabulary: it prints `FILENOTFOUNDERROR`, `VALUEERROR` or `OSERROR` as an
    operator-visible `outcome`, none of which is declared anywhere, none of
    which an operator or a caller can branch on, and each of which changes
    whenever an implementation detail changes what it raises. Two conditions on
    the run path already *had* names — `RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE` and
    `RUN_RECEIPT_NOT_PASS` — and were smuggled through `ValueError` messages, so
    the name reached the operator as prose in `detail` while `outcome` read
    `VALUEERROR`. §19 M16 quotes the intended one as the outcome; it was never
    printed as one.

    `fields` carries any additional *typed* discriminator, in the shape
    `_typed_refusal` established: a fact a caller must branch on travels as a
    field, never as a sentence (§1.2).
    """

    def __init__(self, outcome: str, detail: str, **fields: Any) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.fields = fields

    def emit(self) -> int:
        if self.fields:
            return _typed_refusal({"outcome": self.outcome, **self.fields}, self.detail)
        return _refusal(self.outcome, self.detail)


class _RunSelectionError(ValueError):
    """Nothing in the ledger matches the run the operator named.

    Deliberately not a configuration error: the installation is fine, the
    question simply has no answer, and an operator told `MAESTRO_CONFIGURATION_
    INVALID` goes looking for a broken config file instead of a run id.
    """


class _MaestroConfigurationError(ValueError):
    """The repository-local Maestro configuration is absent or unsafe."""


class _MaestroEnvironmentError(_MaestroConfigurationError):
    """A configuration-directed environment variable is unavailable."""


_MAESTRO_CONFIG_FILE = Path("adws") / "maestro.config.yaml"
_MAESTRO_SCHEMA = "maestro-config.v1"
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The plan-contract pipeline. Every path below is derived from the plan name and
# the configured plans_dir so an operator never types one.
_PLAN_CONTRACT_IR_SUFFIX = ".plan.json"
_PLAN_CONTRACT_RENDERED_SUFFIX = ".html"
_PLAN_CONTRACT_RECEIPT_SUFFIX = ".plan-review.json"
_PLAN_CONTRACT_SCRIPT = Path("scripts") / "planctl.py"
_PLAN_CONTRACT_REPOSITORY_SKILL = (
    Path(".claude") / "skills" / "plan-contract" / _PLAN_CONTRACT_SCRIPT
)
_PLAN_CONTRACT_SKILL_ENV = "PLAN_CONTRACT_SKILL_PATH"
_PLAN_CONTRACT_AUTHOR_OPTION = "--from-plan-contract"
_PLAN_CONTRACT_RECEIPT_OPTION = "--plan-contract-receipt"
_PLAN_CONTRACT_RENDERED_OPTION = "--plan-contract-rendered"

# planctl reads the reviewer key from exactly this variable. Maestro owns the
# key's whole lifecycle and injects it per subprocess; an operator shell never
# holds it.
_REVIEWER_HMAC_KEY_ENV = route_admission.REVIEWER_HMAC_KEY_ENV
_REVIEWER_HMAC_KEY_FILE = route_admission.REVIEWER_HMAC_KEY_FILE
_REVIEWER_HMAC_KEY_MINIMUM_BYTES = 32
_REVIEWER_HMAC_KEY_MINTED_BYTES = 32

# Every step streams into a log a visible Herdr pane tails, so an operator can
# watch work that would otherwise happen behind captured pipes. The log path
# travels in the environment; only it, never a key, is ever expanded here.
_PLAN_STEP_LOG_ENV = "MAESTRO_PLAN_STEP_LOG"
_PLAN_STEP_SHELL = (
    "/bin/sh",
    "-c",
    'exec >>"$' + _PLAN_STEP_LOG_ENV + '" 2>&1; exec "$0" "$@"',
)


class _StrictYamlLoader(yaml.SafeLoader):
    """Safe YAML plus duplicate-key refusal for operator configuration."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "configuration mapping keys must be strings",
                key_node.start_mark,
            )
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate configuration key: " + key, key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)



def _omp_profile_default_model(route: str, profile: Optional[str]) -> str:
    """The model an `omp --profile <name>` launch will actually run.

    omp keeps it in the profile's own config as `defaultModel`, which is the
    single source of truth for that launch -- nothing on the command line can
    override it, because `build_omp_argv` passes no model at all. Reading it
    here lets a lane be declared the way it is actually run, as a profile and
    nothing more.
    """
    if route != "omp" or not profile:
        raise _MaestroConfigurationError(
            "tester.model is required unless the lane is route omp with a profile"
        )
    config = (
        Path.home() / ".omp" / "profiles" / profile / "agent" / "config.yml"
    )
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise _MaestroConfigurationError(
            "tester.profile {!r} has no readable omp config at {}".format(
                profile, config
            )
        ) from exc
    found = _find_default_model(raw)
    if not found:
        raise _MaestroConfigurationError(
            "omp profile {!r} declares no defaultModel in {}; give the lane an "
            "explicit model:".format(profile, config)
        )
    return found


def _find_default_model(node: object) -> str:
    """`defaultModel` wherever the profile nests it."""
    if isinstance(node, dict):
        value = node.get("defaultModel")
        if isinstance(value, str) and value.strip():
            return value.strip()
        for child in node.values():
            found = _find_default_model(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_default_model(child)
            if found:
                return found
    return ""


def _config_mapping(value, label, required, optional=()):
    if not isinstance(value, dict):
        raise _MaestroConfigurationError(label + " must be a mapping")
    allowed = set(required) | set(optional)
    missing = set(required) - set(value)
    unknown = set(value) - allowed
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise _MaestroConfigurationError(label + " has " + "; ".join(detail) + " keys")
    return value


def _config_string(value, label):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise _MaestroConfigurationError(label + " must be a non-empty string")
    return value


def _config_positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _MaestroConfigurationError(label + " must be positive")
    return float(value)


def _config_positive_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _MaestroConfigurationError(label + " must be a positive integer")
    return value


def _config_nonnegative_integer(value, label):
    """A count that is allowed to be zero.

    Separate from `_config_positive_integer` because zero is the *point* of
    one of these: §7.5 gives `LauncherFailure.CREDENTIAL` a budget of zero, and
    a validator that refuses zero would make the one row of the retry table
    whose whole purpose is not to retry unexpressible in configuration.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MaestroConfigurationError(label + " must be a non-negative integer")
    return value


def _config_bool(value, label):
    if not isinstance(value, bool):
        raise _MaestroConfigurationError(label + " must be a boolean")
    return value


def _config_reject_grade(value) -> "code_review.FindingGrade":
    """§3.6 A9's rejection threshold, validated at load rather than at review.

    A misspelt grade must refuse the installation now, not silently become a
    threshold nothing matches on the night a lane is being reviewed.
    """
    try:
        return code_review.parse_reject_grade(value)
    except code_review.UnknownGrade as exc:
        raise _MaestroConfigurationError(
            "execution.review_reject_grade: " + str(exc)
        ) from exc


def _config_argv(value, label) -> Tuple[str, ...]:
    """A command as a real argv list, never a shell string.

    The same rule the plan's gates already live under (`docs/plan-authoring.md`,
    "Pass the real argv, never a script alias"): a string would have to be
    split by something, and whatever split it would be a shell this process
    does not run.
    """
    if not isinstance(value, list) or not value:
        raise _MaestroConfigurationError(
            label + " must be a non-empty list of argv strings"
        )
    return tuple(
        _config_string(item, label + "[{}]".format(index))
        for index, item in enumerate(value)
    )


def _path_is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


_REGISTRY_RELATIVE = Path(".maestro") / "registry.json"


def _registry_path() -> Path:
    override = os.environ.get("MAESTRO_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / _REGISTRY_RELATIVE


def _register_installation(layout: Dict[str, Any]) -> None:
    """Record this installation so the dashboard can find it without being told.

    An operator runs several factories at once, each keeping its ledger beside
    its own repository in a directory the dashboard cannot guess. Naming every
    one of them by environment variable at start-up restates what Maestro
    already knows, and caps the dashboard at what fits on a command line.

    This is observability bookkeeping and nothing else: no lifecycle transition
    reads it, and a failure to write it is not allowed to affect a run, so the
    whole thing is best-effort by construction.
    """
    entry = {
        "repository": str(layout["repo"]),
        "database": str(layout["database"]),
        "plans_dir": str(layout["plans_dir"]),
        "state": str(layout["repository_state"]),
    }
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        installations = []
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(
                loaded.get("installations"), list
            ):
                installations = [
                    item
                    for item in loaded["installations"]
                    if isinstance(item, dict)
                    and item.get("database") != entry["database"]
                    and item.get("repository") != entry["repository"]
                ]
        installations.insert(0, entry)
        # Written beside the destination and renamed, so a dashboard reading
        # concurrently sees either the old file or the new one, never a
        # half-written one.
        scratch = path.with_name(path.name + ".tmp")
        scratch.write_text(
            json.dumps({"installations": installations}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        scratch.replace(path)
    except (OSError, UnicodeError, ValueError):
        return


def _repository_identity_root(repo: Path) -> Path:
    """The main worktree of `repo`, which is what names the factory's state.

    State was anchored to the checkout directory, so every linked worktree of
    one repository became a *different* factory: its own ledger, its own
    receipt store, its own admitted routes, and no memory of anything the
    repository had already finalized. A plan finalized in the main checkout
    then failed in a worktree with `no receipt for <digest>` — a refusal that
    named a missing file when the truth was a second, empty state directory.

    A linked worktree spells its `.git` as a file pointing into the main
    repository's `.git/worktrees/<name>`; the main checkout spells it as a
    directory. Reading that is enough to recover the identity, and it needs no
    subprocess, so config loading stays a pure filesystem operation.
    """
    marker = repo / ".git"
    try:
        if not marker.is_file():
            return repo
        pointer = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return repo
    if not pointer.startswith("gitdir:"):
        return repo
    git_dir = Path(pointer.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    for parent in git_dir.parents:
        # `<main>/.git/worktrees/<name>` -> `<main>`. Anything else (a bare or
        # relocated git dir) has no main worktree to name, so the checkout
        # keeps naming itself rather than guessing.
        if parent.name == ".git":
            return parent.parent.resolve()
    return repo


def _installed_config_path() -> Path:
    """The configuration of the repository this command was issued inside.

    Configuration was resolved as `Path.cwd() / adws/maestro.config.yaml`, so
    every configured verb silently required the operator to be standing in the
    repository root. One directory down — in `adws/` itself, in `plans/`, in
    any source tree — the identical command found no configuration at all and
    fell back to reading the plan *name* as a plan *path*, which fails with a
    message about a missing file rather than about where the operator stood.
    It is the same defect as state keyed to the checkout directory: behaviour
    depending on where somebody happened to be, rather than on the repository.

    The repository owns the configuration, so the repository is what answers.
    The checkout is discovered by walking up from the invocation directory to
    the nearest ancestor that carries an installation, stopping at the first
    `.git` marker because a repository without a configuration is unconfigured
    and does not inherit whatever encloses its directory. Reading the marker
    rather than asking git keeps this a pure filesystem operation, the same
    property `_repository_identity_root` preserves resolving identity in the
    other direction. Finding nothing returns the invocation-relative path, so
    an unconfigured tree refuses exactly as it did before, naming that path.
    """
    origin = Path.cwd().resolve()
    for directory in (origin, *origin.parents):
        candidate = directory / _MAESTRO_CONFIG_FILE
        if os.path.lexists(str(candidate)):
            return candidate
        if os.path.lexists(str(directory / ".git")):
            break
    return origin / _MAESTRO_CONFIG_FILE


def _repository_path(repo: Path, value, label, *, inside: bool) -> Path:
    raw = Path(_config_string(value, label))
    if raw.is_absolute():
        raise _MaestroConfigurationError(label + " must be repository-relative")
    resolved = (repo / raw).resolve()
    within_repo = _path_is_within(resolved, repo)
    if inside and not within_repo:
        raise _MaestroConfigurationError(label + " must resolve inside the repository")
    if not inside and within_repo:
        raise _MaestroConfigurationError(label + " must resolve outside the repository")
    return resolved


def _state_root_path(repo: Path, value) -> Path:
    """Resolve a central state holder, including a home-relative path.

    Repository data belongs outside every checkout. Unlike `plans_dir`, its
    configured holder may therefore be absolute; `~/.maestro` is the portable
    operator-level spelling shared by every repository and linked worktree.
    The repository name is appended later, so repositories remain isolated
    below that central holder.
    """
    raw = Path(_config_string(value, "state_root")).expanduser()
    resolved = (raw if raw.is_absolute() else repo / raw).resolve()
    if _path_is_within(resolved, repo):
        raise _MaestroConfigurationError(
            "state_root must resolve outside the repository"
        )
    return resolved


def _config_relative_path(value, label) -> Path:
    path = Path(_config_string(value, label))
    if path.is_absolute():
        raise _MaestroConfigurationError(label + " must be relative")
    return path


def _resolve_binary(value, label) -> str:
    binary = _config_string(value, label)
    resolved = shutil.which(binary)
    if resolved is None:
        raise _MaestroConfigurationError(label + " is not an executable on PATH")
    path = Path(resolved).resolve()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise _MaestroConfigurationError(label + " is not an executable file")
    return str(path)


def _resolve_key_environment(
    name,
    label,
    expected_size: int,
    *,
    fallback: Optional[Path] = None,
) -> str:
    environment_name = _config_string(name, label)
    if _ENVIRONMENT_NAME.fullmatch(environment_name) is None:
        raise _MaestroConfigurationError(label + " must name an environment variable")
    value = os.environ.get(environment_name)
    if not value and fallback is not None and fallback.is_file():
        try:
            value = fallback.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise _MaestroEnvironmentError(
                "key material file is unreadable: " + str(fallback)
            ) from exc
    if not value:
        raise _MaestroEnvironmentError(
            "required environment variable is unset: " + environment_name
        )
    try:
        material = bytes.fromhex(value)
    except ValueError as exc:
        raise _MaestroEnvironmentError(
            "environment variable is not hexadecimal: " + environment_name
        ) from exc
    if len(material) != expected_size:
        raise _MaestroEnvironmentError(
            "environment variable has invalid key length: " + environment_name
        )
    return value


#: Every `SchedulerConfig` field that declares a default, and that default.
#:
#: Read off the dataclass rather than restated, so there is exactly one of each
#: number. `_run_configuration` uses it for the settings an operator may leave
#: unspecified, and looking up a field that has no default raises `KeyError`
#: rather than inventing one — a required field with no argument is the
#: `missing run configuration` refusal's job, not a fallback's.
_SCHEDULER_CONFIG_DEFAULTS: Dict[str, Any] = {
    field.name: field.default
    for field in dataclasses.fields(scheduler_types.SchedulerConfig)
    if field.default is not dataclasses.MISSING
}


def _validate_review_clocks(
    reviewer: Mapping[str, Any], execution: Mapping[str, Any]
) -> None:
    """Refuse review windows that the remaining live bound cannot hold.

    `execution.turn_timeout_s` is *not* compared to
    `reviewer.finalization_timeout_s`. After §19 M15 the builder turn clock
    is disarmed once the attempt has an ACCEPTED result, so a 600s review
    against a 300s turn clock is the shipping configuration, not an
    inconsistency. The clocks that still apply during review are the
    reviewer's own window and the run-level backstop. Review starts after
    the node attempt, so a healthy sequential path spends
    `node_timeout_s + finalization_timeout_s` before the next lifecycle
    transition. Observed worst case: 461s / 64 turns; the template's 600s
    window holds that with 139s of margin, and backstop_t_s=7200 holds
    1800+600.
    """
    finalization_s = reviewer["finalization_timeout_s"]
    reviewer_turn_s = reviewer["turn_timeout_s"]
    backstop_s = execution["backstop_t_s"]
    node_timeout_s = execution["node_timeout_s"]
    if reviewer_turn_s >= finalization_s:
        raise _MaestroConfigurationError(
            "LIVENESS_BOUND_UNSATISFIED: reviewer.turn_timeout_s must be "
            "less than reviewer.finalization_timeout_s, or a single silent "
            "turn consumes the whole review window. "
            "reviewer_turn={0}, finalization={1}".format(
                reviewer_turn_s, finalization_s
            )
        )
    if finalization_s >= backstop_s:
        raise _MaestroConfigurationError(
            "LIVENESS_BOUND_UNSATISFIED: reviewer.finalization_timeout_s "
            "must be less than execution.backstop_t_s, or the run-level "
            "backstop fires inside a healthy review. "
            "finalization={0}, backstop={1}".format(finalization_s, backstop_s)
        )
    sequential_s = node_timeout_s + finalization_s
    if sequential_s >= backstop_s:
        raise _MaestroConfigurationError(
            "LIVENESS_BOUND_UNSATISFIED: execution.node_timeout_s plus "
            "reviewer.finalization_timeout_s must be less than "
            "execution.backstop_t_s, or the run-level backstop fires on a "
            "healthy sequential node-and-review path. "
            "node_timeout={0}, finalization={1}, sequential={2}, "
            "backstop={3}".format(
                node_timeout_s, finalization_s, sequential_s, backstop_s
            )
        )


def _load_maestro_layout(repo: Path, config_path: Path) -> Dict[str, Any]:
    """Repository layout, binaries, and route destinations. No key material."""
    try:
        with config_path.open(encoding="utf-8") as handle:
            raw = yaml.load(handle, Loader=_StrictYamlLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _MaestroConfigurationError(
            "cannot load " + str(_MAESTRO_CONFIG_FILE)
        ) from exc
    root = _config_mapping(
        raw,
        "maestro configuration",
        (
            "schema",
            "plans_dir",
            "state_root",
            "keys",
            "executables",
            "route_receipts",
            "reviewer",
            "execution",
        ),
        ("plan_contract", "author", "runners", "tester"),
    )
    if root["schema"] != _MAESTRO_SCHEMA:
        raise _MaestroConfigurationError("schema must be " + _MAESTRO_SCHEMA)

    plans_dir = _repository_path(repo, root["plans_dir"], "plans_dir", inside=True)
    if not plans_dir.is_dir():
        raise _MaestroConfigurationError("plans_dir is not a directory")
    # Anchored to the repository, not the checkout: a worktree shares the
    # ledger, receipts, and admitted routes of the repository it belongs to.
    # `plans_dir` stays bound to `repo` above, because the plan being run is
    # whatever this checkout has on disk.
    identity = _repository_identity_root(repo)
    state_root = _state_root_path(identity, root["state_root"])
    if _path_is_within(state_root, repo):
        raise _MaestroConfigurationError(
            "state_root must resolve outside the repository"
        )
    repository_state = (state_root / identity.name).resolve()
    if not _path_is_within(repository_state, state_root):
        raise _MaestroConfigurationError(
            "repository state must remain below state_root"
        )

    keys = _config_mapping(
        root["keys"],
        "keys",
        ("verify_key_env", "signing_seed_env", "route_verify_key_env"),
        ("reviewer_hmac_key_env",),
    )
    if "reviewer_hmac_key_env" in keys:
        reviewer_key_env = _config_string(
            keys["reviewer_hmac_key_env"], "keys.reviewer_hmac_key_env"
        )
        if _ENVIRONMENT_NAME.fullmatch(reviewer_key_env) is None:
            raise _MaestroConfigurationError(
                "keys.reviewer_hmac_key_env must name an environment variable"
            )

    executable_values = _config_mapping(
        root["executables"], "executables", ("herdr", "omp", "claude")
    )
    executables = {
        name: _resolve_binary(executable_values[name], "executables." + name)
        for name in ("herdr", "omp", "claude")
    }

    # The gate runner, declared the same way every other external binary
    # Maestro shells out to already is. Optional, so an existing configuration
    # keeps loading; when a runner is named here the declaration is binding and
    # discovery never runs. Deliberately *not* resolved with `_resolve_binary`:
    # a runner is legitimately spelled as a repository-relative path
    # (`.venv/bin/pytest`), which is not on PATH, and its usability is decided
    # by the capability probe in `runner_resolution`, not by `shutil.which`.
    runners: Dict[str, str] = {}
    if "runners" in root:
        runner_values = _config_mapping(
            root["runners"], "runners", (), tuple(plan_model.RUNNERS)
        )
        for name in plan_model.RUNNERS:
            if name in runner_values:
                runners[name] = _config_string(runner_values[name], "runners." + name)

    receipts = root["route_receipts"]
    if not isinstance(receipts, dict) or not receipts:
        raise _MaestroConfigurationError("route_receipts must be a non-empty mapping")
    route_paths = {}
    runs_root = repository_state / "runs"
    for route, value in receipts.items():
        route_name = _config_string(route, "route_receipts route")
        if "=" in route_name:
            raise _MaestroConfigurationError(
                "route_receipts route must not contain '='"
            )
        relative = _config_relative_path(value, "route_receipts." + route_name)
        path = (repository_state / relative).resolve()
        if not _path_is_within(path, repository_state):
            raise _MaestroConfigurationError(
                "route receipt must remain below repository state"
            )
        if _path_is_within(path, runs_root):
            raise _MaestroConfigurationError(
                "route receipt must not be in a participant run boundary"
            )
        route_paths[route_name] = path

    reviewer_raw = _config_mapping(
        root["reviewer"],
        "reviewer",
        (
            "route",
            "model",
            "effort",
            "finalization_timeout_s",
            "turn_timeout_s",
            "poll_interval_s",
        ),
        ("profile", "id", "vendor"),
    )
    execution_raw = _config_mapping(
        root["execution"],
        "execution",
        (
            "route",
            "model",
            "effort",
            "concurrency",
            "node_timeout_s",
            "turn_timeout_s",
            "final_acceptance_timeout_s",
            "backstop_t_s",
            "semantic_ceiling",
        ),
        (
            "profile",
            "vendor",
            "review_ceiling",
            "test_review_ceiling",
            "review_reject_grade",
            "provision",
            "environmental_retries",
            "launcher_retries",
            "credential_retries",
            "restrict_actor_tools",
        ),
    )

    reviewer = {
        "route": _config_string(reviewer_raw["route"], "reviewer.route"),
        "model": _config_string(reviewer_raw["model"], "reviewer.model"),
        "effort": _config_string(reviewer_raw["effort"], "reviewer.effort"),
        "profile": (
            _config_string(reviewer_raw["profile"], "reviewer.profile")
            if "profile" in reviewer_raw
            else None
        ),
        "id": (
            _config_string(reviewer_raw["id"], "reviewer.id")
            if "id" in reviewer_raw
            else None
        ),
        "vendor": (
            _config_string(reviewer_raw["vendor"], "reviewer.vendor")
            if "vendor" in reviewer_raw
            else None
        ),
        "finalization_timeout_s": _config_positive_number(
            reviewer_raw["finalization_timeout_s"], "reviewer.finalization_timeout_s"
        ),
        "turn_timeout_s": _config_positive_number(
            reviewer_raw["turn_timeout_s"], "reviewer.turn_timeout_s"
        ),
        "poll_interval_s": _config_positive_number(
            reviewer_raw["poll_interval_s"], "reviewer.poll_interval_s"
        ),
    }
    execution = {
        "route": _config_string(execution_raw["route"], "execution.route"),
        "model": _config_string(execution_raw["model"], "execution.model"),
        "effort": _config_string(execution_raw["effort"], "execution.effort"),
        "profile": (
            _config_string(execution_raw["profile"], "execution.profile")
            if "profile" in execution_raw
            else None
        ),
        "concurrency": _config_positive_integer(
            execution_raw["concurrency"], "execution.concurrency"
        ),
        "node_timeout_s": _config_positive_number(
            execution_raw["node_timeout_s"], "execution.node_timeout_s"
        ),
        "turn_timeout_s": _config_positive_number(
            execution_raw["turn_timeout_s"], "execution.turn_timeout_s"
        ),
        "final_acceptance_timeout_s": _config_positive_number(
            execution_raw["final_acceptance_timeout_s"],
            "execution.final_acceptance_timeout_s",
        ),
        "backstop_t_s": _config_positive_number(
            execution_raw["backstop_t_s"], "execution.backstop_t_s"
        ),
        "semantic_ceiling": _config_positive_integer(
            execution_raw["semantic_ceiling"], "execution.semantic_ceiling"
        ),
        "vendor": (
            _config_string(execution_raw["vendor"], "execution.vendor")
            if "vendor" in execution_raw
            else None
        ),
        # Secondary hatch. Default off: the omp profile is the tool policy.
        # True appends permissions.route_capability_argv on every launch.
        "restrict_actor_tools": (
            _config_bool(
                execution_raw["restrict_actor_tools"], "execution.restrict_actor_tools"
            )
            if "restrict_actor_tools" in execution_raw
            else False
        ),
        # Separate from the semantic ceiling by construction. Defaulted rather
        # than required so an existing config keeps working, but the default is
        # a real bound, not "unlimited".
        "review_ceiling": (
            _config_positive_integer(
                execution_raw["review_ceiling"], "execution.review_ceiling"
            )
            if "review_ceiling" in execution_raw
            else 3
        ),
        # The test reviewer's own ceiling. Separate from `review_ceiling` for
        # the reason that one is separate from `semantic_ceiling`: they bound
        # different loops, and one counter would let a lane whose tests took
        # three rounds to become strong reach its implementation review with
        # nothing left.
        "test_review_ceiling": (
            _config_positive_integer(
                execution_raw["test_review_ceiling"],
                "execution.test_review_ceiling",
            )
            if "test_review_ceiling" in execution_raw
            else 3
        ),
        # §3.6 A9's grading threshold, beside the ceiling because it answers
        # the half of A9 the ceiling cannot: the ceiling decides how many
        # rejections a node may collect, this decides what counts as one. A
        # property of the installation, never of the plan (§6.2), so a plan
        # cannot raise or lower its own bar.
        "review_reject_grade": (
            _config_reject_grade(execution_raw["review_reject_grade"])
            if "review_reject_grade" in execution_raw
            else code_review.DEFAULT_REJECT_GRADE
        ),
        # §8.3/§9.3's provision step: the ecosystem's setup, run in every
        # attempt's fresh worktree after `git worktree add` and before the
        # pre-node gate and the baseline inventory. `npm ci` for a JavaScript
        # repository; absent for a pytest one, which is §9.3's stated no-op
        # default. Deployment-specific for the same reason `execution.model`
        # is — it names the ecosystem this installation builds — so it lives
        # here rather than being inferred from a runner.
        #
        # Without it §7.4's falsifiable gate claims nothing in any repository
        # with an install step: a pre-node gate red because `node_modules` is
        # absent is not red for the intended reason, and its post-node partner
        # can never go green, so every agent node blocks on a fact about the
        # tree rather than about the work.
        "provision": (
            _config_argv(execution_raw["provision"], "execution.provision")
            if "provision" in execution_raw
            else ()
        ),
        # §7.5's two non-semantic budgets and CREDENTIAL's zero. The defaults
        # are read off `SchedulerConfig` rather than restated here: a literal
        # in this file would be a second representation of a number the
        # dataclass already declares, which is RC1's shape and the reason
        # these three keys were unreachable in the first place.
        "environmental_retries": (
            _config_nonnegative_integer(
                execution_raw["environmental_retries"],
                "execution.environmental_retries",
            )
            if "environmental_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["environmental_retries"]
        ),
        "launcher_retries": (
            _config_nonnegative_integer(
                execution_raw["launcher_retries"], "execution.launcher_retries"
            )
            if "launcher_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["launcher_retries"]
        ),
        "credential_retries": (
            _config_nonnegative_integer(
                execution_raw["credential_retries"], "execution.credential_retries"
            )
            if "credential_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["credential_retries"]
        ),
    }
    # `author` is optional so an installation that never runs `maestro deliver`
    # keeps working unchanged; `deliver` refuses when it is absent rather than
    # inventing a lane nobody configured.
    author = None
    if "author" in root:
        author_raw = _config_mapping(
            root["author"],
            "author",
            (
                "route",
                "model",
                "effort",
                "author_timeout_s",
                "turn_timeout_s",
                "poll_interval_s",
            ),
            ("profile",),
        )
        author = {
            "route": _config_string(author_raw["route"], "author.route"),
            "model": _config_string(author_raw["model"], "author.model"),
            "effort": _config_string(author_raw["effort"], "author.effort"),
            "profile": (
                _config_string(author_raw["profile"], "author.profile")
                if "profile" in author_raw
                else None
            ),
            "author_timeout_s": _config_positive_number(
                author_raw["author_timeout_s"], "author.author_timeout_s"
            ),
            "turn_timeout_s": _config_positive_number(
                author_raw["turn_timeout_s"], "author.turn_timeout_s"
            ),
            "poll_interval_s": _config_positive_number(
                author_raw["poll_interval_s"], "author.poll_interval_s"
            ),
        }

    tester = None
    if "tester" in root:
        # `model` and `effort` are OPTIONAL on an omp lane that names a
        # profile, because omp does not accept either one.
        # `launcher.build_omp_argv` sends `--profile` and `--session-dir` and
        # nothing else: the profile owns the model, and demanding a `model:`
        # key here made the config state a fact the launcher never uses --
        # then refused the run when that unused string failed to resolve
        # (run-6357251adc7d41dc9b2a72645f778c9c, seven attempts, zero turns,
        # on `deepseek/deepseek-v4-flash:auto`).
        #
        # The string is still needed for ONE thing: B13 reads the model's
        # context window to prove the handoff fits. So when the config omits
        # it, take it from the profile's own `defaultModel` -- the same model
        # omp will actually run -- rather than making the operator restate it.
        tester_raw = _config_mapping(
            root["tester"],
            "tester",
            ("route",),
            ("model", "effort", "profile", "vendor"),
        )
        tester_profile = (
            _config_string(tester_raw["profile"], "tester.profile")
            if "profile" in tester_raw
            else None
        )
        tester_route = _config_string(tester_raw["route"], "tester.route")
        if "model" in tester_raw:
            tester_model = _config_string(tester_raw["model"], "tester.model")
        else:
            tester_model = _omp_profile_default_model(tester_route, tester_profile)
        tester = {
            "route": tester_route,
            "model": tester_model,
            "effort": (
                _config_string(tester_raw["effort"], "tester.effort")
                if "effort" in tester_raw
                else ""
            ),
            "profile": tester_profile,
            "vendor": (
                _config_string(tester_raw["vendor"], "tester.vendor")
                if "vendor" in tester_raw
                else None
            ),
        }

    sections = [("reviewer", reviewer), ("execution", execution)]
    if author is not None:
        sections.append(("author", author))
    if tester is not None:
        sections.append(("tester", tester))
    for label, section in sections:
        if section["route"] not in route_paths:
            raise _MaestroConfigurationError(label + ".route has no route receipt")

    # B12's *equality* half applies to every verb that loads a config: two
    # blocks naming the same vendor is a misconfiguration whenever it is
    # written, not only when a run reads it. The *absence* half is enforced at
    # the run branch instead, because a config with no vendors is legal for
    # `plan validate`, `bootstrap`, and the workspace verbs — none of them
    # launches a reviewer, so none of them can be self-judging.
    if execution.get("vendor") and reviewer.get("vendor"):
        try:
            code_review.require_distinct_vendor(execution["vendor"], reviewer["vendor"])
        except code_review.SelfJudgeRefused as exc:
            raise _MaestroConfigurationError(str(exc)) from exc

    # Sibling clocks. After §19 M15, execution.turn_timeout_s is disarmed
    # for an attempt that already holds an ACCEPTED result, so a review
    # window of 600s against a builder turn clock of 300s is legal — the
    # old inequality would refuse the shipping template for a closed bug.
    # The remaining live bound during review is the run-level backstop,
    # which node_timeout defers to once a result exists. The per-turn
    # reviewer silence clock must also sit inside the review window.
    _validate_review_clocks(reviewer, execution)

    data_dir = repository_state / "data"
    receipt_dir = repository_state / "receipts"
    database = repository_state / "lifecycle.sqlite3"
    for label, path in (
        ("data directory", data_dir),
        ("receipt store", receipt_dir),
        ("lifecycle database", database),
    ):
        if _path_is_within(path, repo) or _path_is_within(path, runs_root):
            raise _MaestroConfigurationError(
                label + " is inside a repository or participant boundary"
            )
    if _path_is_within(receipt_dir, data_dir) or _path_is_within(data_dir, receipt_dir):
        raise _MaestroConfigurationError(
            "receipt store and data directory must be separate"
        )
    plan_contract = None
    if "plan_contract" in root:
        raw_contract = Path(_config_string(root["plan_contract"], "plan_contract"))
        plan_contract = (
            raw_contract if raw_contract.is_absolute() else repo / raw_contract
        ).resolve()
    return {
        "repo": repo,
        "plan_contract": plan_contract,
        "plans_dir": plans_dir,
        "repository_state": repository_state,
        "receipt_dir": receipt_dir,
        "data_dir": data_dir,
        "database": database,
        "key_env": keys,
        "executables": executables,
        "runners": runners,
        "route_paths": route_paths,
        "reviewer": reviewer,
        "execution": execution,
        "author": author,
        "tester": tester,
    }


def _bind_maestro_keys(layout: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve signing and route keys from env, then bootstrapped state files."""
    keys = layout["key_env"]
    key_dir = layout["repository_state"] / "keys"
    verify_key = _resolve_key_environment(
        keys["verify_key_env"],
        "keys.verify_key_env",
        receipt_crypto.PUBLIC_KEY_SIZE,
        fallback=key_dir / "signing.pub",
    )
    signing_seed = _resolve_key_environment(
        keys["signing_seed_env"],
        "keys.signing_seed_env",
        receipt_crypto.SEED_SIZE,
        fallback=key_dir / "signing.seed",
    )
    route_verify_key = _resolve_key_environment(
        keys["route_verify_key_env"],
        "keys.route_verify_key_env",
        receipt_crypto.PUBLIC_KEY_SIZE,
        fallback=key_dir / "route.pub",
    )
    bound = dict(layout)
    bound["verify_key"] = verify_key
    bound["signing_seed"] = signing_seed
    bound["route_verify_key"] = route_verify_key
    return bound


def _load_maestro_config(repo: Path, config_path: Path) -> Dict[str, Any]:
    return _bind_maestro_keys(_load_maestro_layout(repo, config_path))


def _is_plan_name(name: Any) -> bool:
    """The one-path-component rule, asked as a question rather than raised.

    Mode selection needs the same predicate `_named_plan_name` enforces, so it
    lives here once: a second spelling of "what counts as a plan name" is how
    the resolver and the mode selector come to disagree about the same string.
    """
    return (
        isinstance(name, str)
        and bool(name)
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _named_plan_name(name: str) -> str:
    if not _is_plan_name(name):
        raise _MaestroConfigurationError("plan name must be one path component")
    return name


def _named_plan_file(config: Dict[str, Any], name: str) -> Path:
    path = (config["plans_dir"] / _named_plan_name(name) / "maestro-plan.v1").resolve()
    if not _path_is_within(path, config["plans_dir"]):
        raise _MaestroConfigurationError("named plan resolves outside plans_dir")
    if not path.is_file():
        raise _MaestroConfigurationError("named plan does not exist: " + name)
    return path


def _named_plan_output(config: Dict[str, Any], name: str) -> Path:
    path = (config["plans_dir"] / _named_plan_name(name) / "maestro-plan.v1").resolve()
    if not _path_is_within(path, config["plans_dir"]):
        raise _MaestroConfigurationError("named plan resolves outside plans_dir")
    return path


#: Run verbs served by the repository layout alone. None of them verifies a
#: receipt or launches anything, so none of them needs key material — asking
#: `run status` for a signing seed is how a read verb becomes unusable on a
#: machine that merely wants to watch a run.
_RUN_LEDGER_COMMANDS = (
    "status",
    "list",
    "pause",
    "cancel",
    "convergence",
    "findings",
    # A read by default. `--migrate --apply` writes, and it writes only
    # to the ledger it was pointed at -- no receipt is verified and
    # nothing is launched, so it needs no key material either.
    "test-strength",
)

#: Flags that select *which* run to read and how to print it. They are the only
#: flags a configured run verb accepts, because they override no derived path;
#: any other flag means the operator is driving every path by hand. `--discard`
#: belongs here for the same reason: it selects which of `run cancel`'s two
#: behaviours the operator meant, and derives no path at all.
_RUN_SELECTION_OPTIONS = frozenset(
    {"--run-id", "--json", "--discard", "--migrate", "--apply",
     "--policy", "--no-backup"}
)

#: Run-execution flags that override a *setting* rather than a *path*, mapped
#: to the `argparse` destination each one writes.
#:
#: The all-or-nothing rule above is about identity: `--plan-file` typed by hand
#: beside a `--digest` bound from configuration is two halves describing
#: different runs, and there is no way to tell which half is wrong. None of the
#: flags below can produce that disagreement. A retry budget, a concurrency
#: limit or a model name names no file, resolves no run and is compared against
#: nothing — it is the one number the operator meant to change for this
#: invocation.
#:
#: Treating them as identity flags is what made `run resume <id>
#: --environmental-retries 6` refuse with fifteen missing values (#91): the
#: single tuning flag switched binding off, and the fourteen paths it had
#: nothing to do with then had to be retyped. There was no supported
#: command-line way to raise a retry budget for one resume at all; the only
#: route was editing `execution:` in a deployment-owned `maestro.config.yaml`,
#: a persistent change made to express a one-run intent.
#:
#: So these bind from configuration like everything else and are then written
#: back from what the operator typed, which is `_bind_salvage_configuration`'s
#: rule (#83) applied to the run verbs: only the options the operator did not
#: type are derived, and an explicit flag still wins.
#:
#: The executables (`--herdr`, `--omp`, `--claude`) are deliberately *not*
#: here. They name paths, and a run's route receipts attest the launcher it was
#: admitted with, so an executable swapped under a configured route is the
#: half-disagreement this rule exists to refuse.
_RUN_TUNING_OPTIONS: Dict[str, str] = {
    "--concurrency": "concurrency",
    "--node-timeout-s": "node_timeout_s",
    "--turn-timeout-s": "turn_timeout_s",
    "--final-acceptance-timeout-s": "final_acceptance_timeout_s",
    "--backstop-t-s": "backstop_t_s",
    "--semantic-ceiling": "semantic_ceiling",
    "--review-ceiling": "review_ceiling",
    "--test-review-ceiling": "test_review_ceiling",
    "--review-start-deadline-s": "review_start_deadline_s",
    "--review-quiescence-confirm-s": "review_quiescence_confirm_s",
    "--environmental-retries": "environmental_retries",
    "--launcher-retries": "launcher_retries",
    "--credential-retries": "credential_retries",
    "--provision": "provision_argv",
    "--agent-route": "agent_route",
    "--agent-model": "agent_model",
    "--agent-effort": "agent_effort",
    "--agent-profile": "agent_profile",
}

#: Named once so the refusal can quote a few of them, rather than telling an
#: operator that "tuning flags" exist without saying which ones they are.
_RUN_TUNING_EXAMPLES = (
    "--concurrency",
    "--environmental-retries",
    "--launcher-retries",
    "--review-ceiling",
)

#: Run-execution flags that name neither a path nor a setting, but an operator
#: *decision* about this one invocation. `--allow-exhausted-node` names a node
#: in the plan configuration already resolved, derives nothing, and is
#: compared against nothing, so it cannot produce the half-configured
#: disagreement the all-or-nothing rule exists to refuse. Without this it
#: would be classified as a runtime flag: on `run start` the invocation would
#: be refused outright, and on `run resume` it would silently switch
#: configuration binding off and demand the other fifteen paths by hand (#91)
#: -- which is to say the escape §3.6 B10 requires would not have been
#: reachable on a configured repository at all.
_RUN_ESCAPE_OPTIONS = frozenset({"--allow-exhausted-node"})

#: Plan verbs whose positional argument is overloaded — a plan *name* the
#: installed configuration resolves, or a filesystem path to the plan bytes.
#: `author` is excluded because it also binds executables and *writes* the
#: named plan, and `run` because its positional selects a run, not a file.
_PLAN_FILE_VERBS = ("validate", "finalize")


def _spells_its_own_plan_file(args: argparse.Namespace) -> bool:
    """Whether the operator typed a plan *path* instead of an installed name.

    A plan name is one path component; anything carrying a separator is a path,
    and a path is a configuration the operator wrote out by hand. Binding the
    repository's configuration over it answers a different question than the
    one asked. It also refuses the wrong thing: because a configured named-plan
    verb accepts no runtime flags, a manual `plan validate <path> --receipt-dir
    … --verify-key …` issued from inside any installed repository never reached
    `_plan_validate` at all, and a forged or wrong-key receipt was reported as
    `MAESTRO_CONFIGURATION_INVALID` rather than `RECEIPT_VERIFICATION_FAILED`
    — a refusal whose exit code was right and whose vocabulary named the wrong
    cause. Selecting the manual mode here is what lets the receipt gate speak.
    """
    return (
        args.command == "plan"
        and args.plan_command in _PLAN_FILE_VERBS
        and not _is_plan_name(getattr(args, "plan_name", None))
    )


def _configured_command(args: argparse.Namespace) -> bool:
    return (
        args.command == "bootstrap"
        or (
            args.command == "plan"
            and args.plan_command in ("validate", "finalize", "author")
        )
        or args.command == "run"
    )


def _bind_layout_executables(args: argparse.Namespace, layout: Dict[str, Any]) -> None:
    args.repo = str(layout["repo"])
    args.herdr = layout["executables"]["herdr"]
    args.omp = layout["executables"]["omp"]
    args.claude = layout["executables"]["claude"]
    args.route_receipt = [
        route + "=" + str(path) for route, path in sorted(layout["route_paths"].items())
    ]
    args.repository_state = str(layout["repository_state"])
    args.runners = dict(layout.get("runners") or {})
    args.layout = layout


def _apply_repository_config(args: argparse.Namespace, argv: Sequence[str]) -> None:
    """Bind named-plan and bootstrap entrypoints to installed repository state."""
    if (
        args.command == "attempt"
        and getattr(args, "attempt_command", None) == "salvage"
    ):
        _bind_salvage_configuration(args, argv)
        return
    if not _configured_command(args):
        return
    config_path = _installed_config_path()
    if not os.path.lexists(str(config_path)) or _spells_its_own_plan_file(args):
        if args.command == "plan":
            args.plan_file = args.plan_name
        elif args.command == "run" and args.run_command == "start":
            args.digest = args.plan_name
        return
    if not config_path.is_file():
        raise _MaestroConfigurationError(
            str(_MAESTRO_CONFIG_FILE) + " is not a regular file"
        )
    options = tuple(item.split("=", 1)[0] for item in argv if item.startswith("-"))
    supplied = frozenset(options)
    tuning: Dict[str, Any] = {}
    if args.command == "run" and args.run_command != "start":
        manual = tuple(
            sorted(
                supplied
                - _RUN_SELECTION_OPTIONS
                - frozenset(_RUN_TUNING_OPTIONS)
                - _RUN_ESCAPE_OPTIONS
            )
        )
        if manual:
            # A fully manual invocation. Every path it works on came from a
            # flag, and binding the other half from configuration is exactly
            # how the two halves come to disagree about which run this is.
            #
            # Recorded rather than merely acted on, because the operator has to
            # be told *which* flag did this. Fifteen missing values read as a
            # broken installation; they are the consequence of one word on the
            # command line, and `_missing_run_configuration_detail` is where
            # that word is finally said (#91). An option this partition does
            # not classify lands here too — the safe side, since an unknown
            # flag may well name a path.
            args.manual_run_options = manual
            return
        # Not manual, so configuration binds — but what the operator typed must
        # survive it. The `run` branch below assigns every one of these
        # unconditionally, so without this snapshot a tuning flag that no
        # longer disables binding would instead be silently overwritten by the
        # configured value, which is the same defect wearing a quieter face.
        tuning = {
            attribute: getattr(args, attribute, None)
            for option, attribute in _RUN_TUNING_OPTIONS.items()
            if option in supplied
        }
    elif options:
        # The same class as the branch above, refusing loudly rather than
        # silently, and it named the rule without ever naming the word that
        # broke it. Quote the flags: an operator reading "do not accept
        # runtime flags" beside a command line of their own has to guess which
        # of them counted as runtime.
        #
        # The escape options are exempt on the run verbs and nowhere else: a
        # `plan` verb has no run to admit a node into, so an escape flag there
        # is as much a mistake as any other runtime flag.
        runtime = sorted(
            supplied - (_RUN_ESCAPE_OPTIONS if args.command == "run" else frozenset())
        )
        if runtime:
            raise _MaestroConfigurationError(
                "configured named-plan commands do not accept runtime flags: "
                + ", ".join(runtime)
            )

    repo = config_path.parent.parent.resolve()
    if args.command == "bootstrap" or (
        args.command == "plan" and args.plan_command == "author"
    ):
        layout = _load_maestro_layout(repo, config_path)
        _bind_layout_executables(args, layout)
        _register_installation(layout)
        if args.command == "plan":
            args.plan_file = str(_named_plan_output(layout, args.plan_name))
        return

    if args.command == "run" and args.run_command in _RUN_LEDGER_COMMANDS:
        layout = _load_maestro_layout(repo, config_path)
        _bind_run_ledger_configuration(args, layout)
        _register_installation(layout)
        return

    config = _load_maestro_config(repo, config_path)
    _register_installation(config)
    resumed_run_id = None
    if args.command == "run" and args.run_command == "resume":
        resumed_run_id, plan_file = _resolve_resume_target(args, config)
    else:
        plan_file = _named_plan_file(config, args.plan_name)
    args.repo = str(config["repo"])
    args.plan_file = str(plan_file)
    args.receipt_dir = str(config["receipt_dir"])
    args.data_dir = str(config["data_dir"])
    args.verify_key = [config["verify_key"]]
    args.signing_seed = config["signing_seed"]
    args.route_verify_key = [config["route_verify_key"]]
    args.route_receipt = [
        route + "=" + str(path) for route, path in sorted(config["route_paths"].items())
    ]
    args.herdr = config["executables"]["herdr"]
    args.omp = config["executables"]["omp"]
    args.claude = config["executables"]["claude"]

    # `plan finalize` binds nothing further. It dispatches no reviewer, so it
    # needs no route, no session, no report path and no window clocks -- only
    # the receipt key material bound above.
    if args.command == "run":
        execution = config["execution"]
        # `start` mints; `resume` re-enters the run it just resolved. Minting
        # for `resume` would hand the scheduler an empty ledger, a fresh
        # integration worktree, and no memory of what already merged.
        run_id = resumed_run_id or ("run-" + uuid.uuid4().hex)
        run_root = config["repository_state"] / "runs" / run_id
        args.digest = plan_digest.digest_of(plan_file.read_bytes())
        args.db = str(config["database"])
        # The `runners:` block binds here or it binds nowhere. Only
        # `_bind_layout_executables` used to set it, and no run-execution verb
        # goes through that function, so `_resolve_run_runners` saw an empty
        # declaration on every `run start` and `run resume` and fell through to
        # discovery -- printing the adoption notice for a runner the repository
        # had already declared, and pinning nothing.
        args.runners = dict(config.get("runners") or {})
        # The same defect as `runners:` above, one field over, and it disabled a
        # whole refusal rather than a notice. `_configured_runs_root` reads this
        # and returns `None` when it is unset, and `None` is the *deliberate*
        # answer for a run spelled out by hand on the command line -- a run root
        # nothing declares cannot prove a worktree is this system's own. A
        # configured `run start` is not that case, but it reached the same
        # branch, so `_reclaim_stranded_integration_worktree` skipped its
        # containment test on every configured run and never reclaimed
        # anything. What the operator saw was the fallback refusal telling them
        # a checkout under Maestro's own run root was "not among" the ones it
        # reclaims, and to go move it by hand -- the precise sentence the
        # reclaim exists to make unnecessary, about the precise case it was
        # written for.
        args.repository_state = str(config["repository_state"])
        args.run_id = run_id
        args.integration_path = str(run_root / "integration")
        args.worktrees_root = str(run_root / "worktrees")
        args.scratch_root = str(run_root / "scratch")
        args.concurrency = execution["concurrency"]
        args.node_timeout_s = execution["node_timeout_s"]
        args.turn_timeout_s = execution["turn_timeout_s"]
        args.final_acceptance_timeout_s = execution["final_acceptance_timeout_s"]
        args.backstop_t_s = execution["backstop_t_s"]
        args.semantic_ceiling = execution["semantic_ceiling"]
        args.review_ceiling = execution["review_ceiling"]
        args.test_review_ceiling = execution["test_review_ceiling"]
        # A9's other half, and the one `execution:` key that stops here.
        #
        # Every budget and bound around it continues into `SchedulerConfig`,
        # and this one deliberately does not, because `SchedulerConfig` is the
        # *scheduler's* contract — the fields it reads to decide readiness,
        # concurrency, timeouts and retries. The reject threshold is read by
        # `_code_review_runner`'s closure, which the scheduler is handed as a
        # dependency and never inspects. Routing it through `SchedulerConfig`
        # would add a field with zero readers, and §3.6 B15's rule is that a
        # field with zero readers is a build failure, not a convenience: it
        # looks checked and is not. `TheProjectionIsTotalTest` is keyed on
        # `SchedulerConfig`'s fields for exactly that reason, so it cannot see
        # this key — `TheRejectGradeReachesTheReviewerTest` covers this path
        # end to end instead, and the coverage is not optional just because the
        # destination differs.
        #
        # Set here rather than read with a default at the review site, so a run
        # whose config never named it still carries an explicit threshold
        # instead of one inferred from an absent attribute.
        args.review_reject_grade = execution["review_reject_grade"]
        # §7.5's non-semantic budgets. Present in `SchedulerConfig` and in
        # `maestro.config.yaml` for as long as both existed, and connected by
        # nothing: a deployment that set them changed no run, because the
        # projection onto `SchedulerConfig` never named them and every budget
        # stayed at its dataclass default.
        args.environmental_retries = execution["environmental_retries"]
        args.launcher_retries = execution["launcher_retries"]
        args.credential_retries = execution["credential_retries"]
        # §8.3's provision argv, consumed by `_run_provisioner`.
        args.provision_argv = list(execution["provision"])
        args.agent_route = execution["route"]
        args.agent_model = execution["model"]
        args.agent_effort = execution["effort"]
        args.agent_profile = execution["profile"]
        args.restrict_actor_tools = execution["restrict_actor_tools"]

        # The reviewer bindings, which until now existed only in the `plan
        # finalize` branch above. That asymmetry was the whole defect: the run
        # path had a fully configured `reviewer:` block in
        # `maestro.config.yaml` and never read one field of it, so every lane
        # merged on gate results alone and no model ever read a diff.
        reviewer = config["reviewer"]
        review_root = run_root / "review"
        args.reviewer_route = reviewer["route"]
        args.reviewer_model = reviewer["model"]
        args.reviewer_effort = reviewer["effort"]
        args.reviewer_profile = reviewer["profile"]
        args.reviewer_vendor = reviewer["vendor"]
        args.execution_vendor = execution["vendor"]
        tester = config.get("tester") or {}
        args.tester_route = tester.get("route")
        args.tester_model = tester.get("model")
        args.tester_effort = tester.get("effort")
        args.tester_profile = tester.get("profile")
        args.tester_vendor = tester.get("vendor")
        # Per-attempt subdirectories are minted under this root by the runner;
        # each review gets a fresh session directory, because §6.5's structural
        # half of "independent review is recorded" refuses a reused one.
        args.review_root = str(review_root)
        args.review_receipt_dir = str(review_root / "receipts")
        args.review_timeout_s = reviewer["finalization_timeout_s"]
        args.reviewer_turn_timeout_s = reviewer["turn_timeout_s"]
        args.reviewer_poll_interval_s = reviewer["poll_interval_s"]

        # Last, so it wins over every configured value assigned above. This is
        # the whole of "an explicit flag still wins" for the run verbs: the
        # settings the operator typed are put back exactly as parsed, and no
        # path was rederived on their account.
        for attribute, value in tuning.items():
            setattr(args, attribute, value)


def _named_plan_digests(layout: Dict[str, Any]) -> Dict[str, str]:
    """Every installed plan name against the digest of the bytes on disk now.

    The ledger stores a plan *digest* and nothing else — it has never heard of
    a plan name — so this mapping is the whole bridge between what an operator
    types and what `runs.plan_digest` holds, in both directions. Rebuilt on
    every call rather than cached because the plan file is what an author
    edits between runs, and a stale digest here silently resolves to the wrong
    run.
    """
    digests: Dict[str, str] = {}
    for candidate in sorted(layout["plans_dir"].iterdir()):
        stored = candidate / "maestro-plan.v1"
        if stored.is_file():
            digests[candidate.name] = plan_digest.digest_of(stored.read_bytes())
    return digests


#: Salvage options the installed configuration can answer for itself. Each
#: names a path `run start` already mints from `repository_state`, so the verb
#: held both halves of the derivation and asked the operator for the answer
#: anyway -- at the one moment an operator is least able to reconstruct it, and
#: with a silent failure mode: a wrong `--worktrees-root` reports
#: `SALVAGE_WORKTREE_ABSENT`, which reads as "the work is gone" when the work
#: is fine and the path was wrong (#83).
#:
#: `--record-dir` and `--signing-seed` are deliberately absent. The record
#: directory is where signed evidence lands and is an operator's choice, and a
#: verb that quietly finds its own signing key is worse than one that asks.
_SALVAGE_DERIVED_OPTIONS = ("--worktrees-root", "--scratch-root", "--db")


def _bind_salvage_configuration(args: argparse.Namespace, argv: Sequence[str]) -> None:
    """Derive salvage's run-directory paths exactly as `run start` mints them.

    Salvage takes the run id positionally and reads the same
    `maestro.config.yaml`, so `repository_state / "runs" / <run_id>` is the
    same derivation on both sides -- spelled once here rather than retyped at
    the prompt. Only the options the operator did not type are bound, so an
    explicit flag still wins: a run directory that was relocated or copied is
    precisely the case a derived default cannot answer.

    Only the layout is loaded, never the keys. A stranded attempt is recovered
    with the operator's own seed on the command line, and reading key material
    here would be the verb finding its own key.
    """
    supplied = {item.split("=", 1)[0] for item in argv if item.startswith("-")}
    if all(option in supplied for option in _SALVAGE_DERIVED_OPTIONS):
        return
    config_path = _installed_config_path()
    if not os.path.lexists(str(config_path)):
        # Unconfigured tree. The handler refuses by flag name, which is a
        # better message than a configuration error about a file nobody
        # installed.
        return
    if not config_path.is_file():
        raise _MaestroConfigurationError(
            str(_MAESTRO_CONFIG_FILE) + " is not a regular file"
        )
    layout = _load_maestro_layout(config_path.parent.parent.resolve(), config_path)
    runs_root = (layout["repository_state"] / "runs").resolve()
    run_root = (runs_root / str(args.run_id)).resolve()
    if not _path_is_within(run_root, runs_root):
        raise _MaestroConfigurationError(
            "run id does not name a directory inside the run boundary"
        )
    if "--worktrees-root" not in supplied:
        args.worktrees_root = str(run_root / "worktrees")
    if "--scratch-root" not in supplied:
        args.scratch_root = str(run_root / "scratch")
    if "--db" not in supplied:
        args.db = str(layout["database"])


def _bind_run_ledger_configuration(
    args: argparse.Namespace, layout: Dict[str, Any]
) -> None:
    """Point a read verb at the configured ledger, and nothing more."""
    args.repo = str(layout["repo"])
    args.db = str(layout["database"])
    args.repository_state = str(layout["repository_state"])
    args.plan_digests = _named_plan_digests(layout)
    # The one execution setting a read verb needs, and the reason it is not
    # "nothing more": `run convergence` measures how many review attempts a
    # lane actually took, and that number means nothing without the ceiling it
    # is being compared against. Read from the layout rather than from
    # `_load_maestro_config`, so the comparison costs a read verb no key
    # material (#30).
    args.review_ceiling = layout["execution"]["review_ceiling"]


def _select_run(
    reader: "lc.LifecycleReader", args: argparse.Namespace
) -> "lc.RunRecord":
    """The single rule that turns what an operator typed into one run.

    Three accepted shapes, in falling order of explicitness: `--run-id`, a run
    id typed positionally, and a plan name — which resolves to that plan's
    *most recent* run, because the run an operator means while a run is going
    is the one that is going. Every other run for the plan stays reachable
    through `run list` and `--run-id`, so defaulting to the newest loses
    nothing.
    """
    requested = getattr(args, "run_id", None)
    if requested:
        record = reader.run(requested)
        if record is None:
            raise _RunSelectionError("no run in the ledger has id " + requested)
        return record
    selector = getattr(args, "selector", None)
    if not selector:
        raise _RunSelectionError("name a plan or a run id, or pass --run-id")
    record = reader.run(selector)
    if record is not None:
        return record
    digest = (getattr(args, "plan_digests", None) or {}).get(selector)
    if digest is None:
        raise _RunSelectionError(
            selector + " is neither a run id in the ledger nor an installed plan name"
        )
    records = reader.runs(digest)
    if not records:
        raise _RunSelectionError(
            "no run has been started for plan "
            + selector
            + " at its current contents (digest "
            + digest[:12]
            + ")"
        )
    return records[0]


def _retained_plan_path(config: Dict[str, Any], run_id: str, digest: str) -> Path:
    """Where a run's own retained plan bytes are materialised for execution.

    Under the run's state directory rather than beside the installed plans:
    these bytes belong to one run and must not be mistaken for, or overwritten
    by, anything `plan ship` manages. Named by digest so a run that has amended
    its plan keeps every version it executed under on disk beside the ledger
    rows that record them.

    The file is a *rendering* of the durable record, never the record itself —
    `run_plan_versions` holds the bytes, and this is recreated from them
    whenever it is missing or does not hash to the digest it claims.
    """
    return (
        Path(config["data_dir"]) / "runs" / run_id / "plans" / "{0}.json".format(digest)
    )


def _resolve_resume_target(
    args: argparse.Namespace, config: Dict[str, Any]
) -> Tuple[str, Path]:
    """The existing run `resume` re-enters, and the plan bytes it ran on.

    Resume is the verb that most needs *not* to invent an identity: a fresh
    run id would give the scheduler an empty ledger and a new integration
    worktree, discarding every node the original run already merged. The plan
    file is chosen by the resolved run's own `plan_digest` rather than by the
    name typed at the prompt, so resuming a run whose plan has since been
    edited refuses at validation instead of resuming a different plan.
    """
    args.plan_digests = _named_plan_digests(config)
    reader = _open_reader(config["database"])
    try:
        record = _select_run(reader, args)
        retained = reader.current_plan(record.run_id)
    finally:
        reader.close()
    if retained is not None:
        # The run kept its own plan bytes, so nothing on disk needs to match.
        # This does not relax the rule below — it removes the reason for it.
        # The refusal exists so a resume cannot silently run *different* bytes;
        # reading the run's own retained record satisfies that more directly
        # than searching for a file that happens to hash the same, and it holds
        # after `plan ship` has overwritten the file the run started from.
        digest, stored = retained
        materialised = _retained_plan_path(config, record.run_id, digest)
        materialised.parent.mkdir(parents=True, exist_ok=True)
        if (
            not materialised.is_file()
            or plan_digest.digest_of(materialised.read_bytes()) != digest
        ):
            materialised.write_bytes(stored)
        return record.run_id, materialised
    for name, digest in args.plan_digests.items():
        if digest == record.plan_digest:
            return record.run_id, _named_plan_file(config, name)
    raise _RunSelectionError(
        "run "
        + record.run_id
        + " ran plan digest "
        + record.plan_digest[:12]
        + ", which no installed plan currently matches; the plan file has "
        "changed since the run started"
    )


def _open_reader(database) -> "lc.LifecycleReader":
    try:
        return lc.LifecycleReader.open(database)
    except lc.LedgerUnavailable as exc:
        raise _RunSelectionError(str(exc)) from exc


def _refusal(outcome: str, detail: str) -> int:
    print(json.dumps({"detail": detail, "outcome": outcome}, sort_keys=True))
    return 3


def _quiescence_refusal(exc: BaseException) -> int:
    """`RUN_QUIESCENCE_UNPROVEN`, carrying the code the harness declared.

    `HarnessQuiescenceError`'s message is not prose. Every raise site puts a
    declared code first — `HARNESS_CONTEXT_QUIESCENCE_UNPROVEN`, or
    `HERDR_QUIESCENCE_UNPROVEN:<handle token>` naming the handle that could not
    be proven gone. Under the untyped arm the operator got that declared code
    as `detail` and the *Python class name* as `outcome`, which is the two
    halves exactly the wrong way round: the typed fact travelled as prose and
    the implementation detail travelled as the field a caller branches on.

    This is not a `RUN_EXECUTION_FAILED`. §8.3's quiesce is a correctness
    obligation — an unproven-absent process group is why a settle must not
    proceed — and an operator reading it needs to know that a process may
    still be alive, which the generic name does not say.
    """
    text = str(exc)
    code = text.partition(":")[0]
    return _typed_refusal(
        {"outcome": "RUN_QUIESCENCE_UNPROVEN", "quiescence_code": code}, text
    )


def _typed_refusal(payload: Dict[str, Any], detail: str) -> int:
    """A refusal whose discriminating facts travel as typed fields.

    `_refusal` carries an outcome and a sentence, which is enough while the
    outcome is the whole answer. `RUNNER_UNUSABLE` is not: an operator needs to
    know *which* of unresolved, incapable, or ambiguous fired, what was tried,
    and what the probe returned, and a caller needs to branch on that without
    parsing prose (§1.2). So the payload carries them as fields and the
    sentence stays a sentence.
    """
    body = dict(payload)
    body["detail"] = detail
    print(json.dumps(body, sort_keys=True))
    return 3


def _bootstrap(args: argparse.Namespace) -> int:
    layout = getattr(args, "layout", None)
    if layout is None:
        return _refusal(
            "MAESTRO_CONFIGURATION_INVALID",
            "bootstrap requires an installed " + str(_MAESTRO_CONFIG_FILE),
        )
    keys_dir = Path(layout["repository_state"]) / "keys"
    try:
        keys = route_admission.provision_keys(keys_dir)
        # Two files, because one file put the reviewer's key into the author's
        # shell. `maestro.env` is what an operator sources for ordinary
        # author-side work and it carries no reviewer binding;
        # `reviewer-hmac.env` carries that binding and nothing else, and
        # nothing in the supported path reads it -- `plan review` injects the
        # key itself. Both paths are reported so an operator can see which is
        # which without opening either.
        env_file = route_admission.write_env_file(
            keys,
            verify_key_env=layout["key_env"]["verify_key_env"],
            signing_seed_env=layout["key_env"]["signing_seed_env"],
            route_verify_key_env=layout["key_env"]["route_verify_key_env"],
        )
        reviewer_env_file = route_admission.write_reviewer_env_file(
            keys,
            reviewer_hmac_key_env=layout["key_env"].get(
                "reviewer_hmac_key_env", _REVIEWER_HMAC_KEY_ENV
            ),
        )
        # A receipt is per route while several lanes may ride one route, so the
        # capture spec comes from the first configured lane naming it. The
        # order preserves the precedence the previous conditional expressed —
        # execution before reviewer — and extends it to the authoring lane.
        # `author` was omitted here while `_load_maestro_layout` already
        # required `author.route` to have a receipt, so any config with an
        # `author:` block refused bootstrap with ROUTE_MODEL_UNCONFIGURED and
        # left the run unable to load the very receipt the config demanded.
        lanes = [layout["execution"], layout["reviewer"]]
        if layout.get("author") is not None:
            lanes.append(layout["author"])
        specs = []
        for route, path in sorted(layout["route_paths"].items()):
            section = next((lane for lane in lanes if lane["route"] == route), None)
            if section is None:
                raise route_admission.AdmissionError(
                    "ROUTE_MODEL_UNCONFIGURED:{}".format(route)
                )
            timeout = section.get("turn_timeout_s") or 180.0
            specs.append(
                route_admission.RouteCaptureSpec(
                    route=route,
                    cwd=Path(layout["repo"]),
                    herdr=Path(layout["executables"]["herdr"]),
                    binary=Path(layout["executables"][route]),
                    model=section["model"],
                    effort=section["effort"],
                    profile=section.get("profile"),
                    session_dir=(
                        Path(layout["repository_state"]) / "admission" / route
                    ),
                    timeout_s=float(timeout),
                )
            )
        written = route_admission.admit_routes(
            specs, layout["route_paths"], route_seed=keys.route_seed
        )
    except route_admission.AdmissionError as exc:
        return _refusal("ROUTE_ADMISSION_FAILED", str(exc))
    payload = {
        "outcome": "ROUTES_ADMITTED",
        "env_file": str(env_file),
        "reviewer_env_file": str(reviewer_env_file),
        "keys_dir": str(keys.keys_dir),
        "receipts": {item.route: str(item.path) for item in written},
        "reused": [item.route for item in written if item.reused],
        "routes": [item.route for item in written],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _plan_author(args: argparse.Namespace) -> int:
    destination = Path(args.plan_file)
    draft = getattr(args, "from_file", None)
    contract = getattr(args, "from_plan_contract", None)
    repo = Path(getattr(args, "repo", None) or ".")
    try:
        if contract:
            receipt = getattr(args, "plan_contract_receipt", None)
            if not receipt:
                return _refusal("PLAN_AUTHORING_FAILED", "RECEIPT_REQUIRED")
            rendered = getattr(args, "plan_contract_rendered", None)
            stored, _trace = plan_contract_ingress.author_from_plan_contract(
                Path(contract),
                Path(receipt),
                destination,
                repo,
                Path(rendered) if rendered else None,
            )
            draft_path = Path(contract)
        else:
            if draft:
                draft_path = Path(draft)
            else:
                draft_path = plan_author.find_draft(destination.parent)
            stored = plan_author.author_from_draft(draft_path, destination, repo)
    except (plan_author.AuthoringError, plan_contract_ingress.IngressError) as exc:
        return _refusal("PLAN_AUTHORING_FAILED", str(exc))
    payload = {
        "outcome": "PLAN_AUTHORED",
        "digest": plan_digest.digest_of(stored),
        "draft": str(draft_path),
        "plan": str(destination),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


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
                    "are required when validating a superseded plan"
                ),
            )
        return self._store

    def has_receipt(self, digest: str) -> bool:
        try:
            receipt = self._receipt_store().load(digest)
        except FileNotFoundError:
            return False
        except (
            finalization.ReceiptInvalid,
            finalization.SignatureMissing,
            finalization.SignatureInvalid,
            UnicodeError,
            ValueError,
            KeyError,
        ) as exc:
            raise _PlanReceiptVerificationError(str(exc)) from exc
        if receipt.plan_digest != digest:
            raise _PlanReceiptVerificationError(
                "the receipt stored for {0} names {1}".format(
                    digest, receipt.plan_digest
                )
            )
        return True


def _plan_collector(args: argparse.Namespace) -> "pv.SubprocessCollector":
    """The collector every plan verb uses, carrying the declared runners.

    The gate runner used to be `argv[0]` taken from the `Gate.runner` literal
    and executed against an inherited `PATH`, so `plan validate` enumerated a
    plan's gates with whatever interpreter the operator's shell exposed. On a
    machine whose `PATH` pytest cannot import the repository's `conftest.py`,
    every gate collected zero cases and the plan was blocked as if its authored
    bytes were wrong. Handing the collector the declared `runners:` block is
    what lets `runner_resolution` decide the binary instead.
    """
    # `resolver` is passed rather than defaulted so the seam has a production
    # writer: `tests/test_no_dead_seams.py` convicts a field production reads
    # and only tests write, and a resolver that only ever arrived from a test
    # would be exactly that.
    return pv.SubprocessCollector(
        declared=dict(getattr(args, "runners", {}) or {}),
        resolver=runner_resolution.resolve,
    )


def _plan_validate(args: argparse.Namespace) -> int:
    try:
        result = pv.validate_plan(
            Path(args.plan_file).read_bytes(),
            args.repo,
            receipts=_VerifiedReceipts(args),
            collector=_plan_collector(args),
        )
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    payload = {
        "outcome": result.outcome.value,
        "digest": result.digest,
        "blockers": [
            {
                "obligation": row.obligation.value,
                "pointer": row.pointer,
                "message": row.message,
            }
            for row in result.blockers
        ],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.eligible else 2


def _finalization_store(args: argparse.Namespace) -> finalization.ReceiptStore:
    if (
        not args.receipt_dir
        or not args.data_dir
        or not args.verify_key
        or not args.signing_seed
    ):
        raise _PlanReceiptConfigurationError(
            "--receipt-dir, --data-dir, --verify-key, and --signing-seed "
            "are required to write to the receipt store"
        )
    try:
        verify_keys = tuple(bytes.fromhex(value) for value in args.verify_key)
        signing_seed = bytes.fromhex(args.signing_seed)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "finalization key material must be hexadecimal"
        ) from exc
    if (
        not verify_keys
        or any(len(key) != receipt_crypto.PUBLIC_KEY_SIZE for key in verify_keys)
        or len(signing_seed) != receipt_crypto.SEED_SIZE
    ):
        raise _PlanReceiptConfigurationError(
            "finalization keys must be Ed25519 public keys and a 32-byte seed"
        )
    try:
        return finalization.ReceiptStore(
            args.receipt_dir,
            repo_paths=(args.repo,),
            data_dir=args.data_dir,
            verify_keys=verify_keys,
            signing_seed=signing_seed,
        )
    except (
        finalization.ReceiptStoreLocationError,
        finalization.SigningKeyUnavailable,
    ) as exc:
        raise _PlanReceiptConfigurationError(str(exc)) from exc


def _session_tab_id(session: Any) -> str:
    """Read optional placement identity from current and legacy session rows."""
    return str(getattr(session, "tab_id", "") or "")


def _code_review_runner(
    args: argparse.Namespace,
    runner: "launcher.HerdrLauncher",
    lifecycle_store: Optional["lc.LifecycleStore"] = None,
):
    """Build the persistent derived-review callback for every build lane."""
    try:
        code_review.require_distinct_vendor(
            getattr(args, "execution_vendor", "") or "",
            getattr(args, "reviewer_vendor", "") or "",
        )
    except code_review.SelfJudgeRefused as exc:
        raise _PlanReceiptConfigurationError(str(exc)) from exc

    review_root = Path(args.review_root)
    receipt_store = finalization.ReceiptStore(
        Path(args.review_receipt_dir),
        repo_paths=(args.repo,),
        data_dir=args.data_dir,
        verify_keys=tuple(bytes.fromhex(k) for k in args.verify_key),
        signing_seed=bytes.fromhex(args.signing_seed),
    )
    handles: Dict[str, launcher.LaunchHandle] = {}

    def lane_id(node: Any) -> str:
        return str(getattr(node, "review_of", None) or node.node_id)

    def active_session(build_node_id: str) -> Any:
        if lifecycle_store is None:
            return None
        session = lifecycle_store.current_actor_session(
            args.run_id, build_node_id, "reviewer"
        )
        if (
            session is None
            or session.state is not scheduler_types.ActorSessionState.ACTIVE
        ):
            return None
        return session

    def next_generation(build_node_id: str, record: Any) -> int:
        floor = int(getattr(record, "attempt_no", 1))
        if lifecycle_store is None:
            return floor
        sessions = lifecycle_store.actor_sessions(
            args.run_id, build_node_id, actor_role="reviewer", limit=10_000
        )
        return max(
            floor, max((session.generation for session in sessions), default=0) + 1
        )

    def close(build_node_id: str) -> None:
        handle = handles.pop(build_node_id, None)
        session = active_session(build_node_id)
        if handle is None and session is not None:
            persisted = launcher.PersistedActorHandle(
                correlation_token=session.correlation_token,
                pane_id=session.pane_id,
                agent_name=launcher.agent_name_for(session.correlation_token),
                launched_cwd=Path(args.repo),
                transcript_path=Path(session.session_path),
                workspace_id=launcher.workspace_of(_session_tab_id(session)),
                tab_id=_session_tab_id(session),
                lane_key=build_node_id,
            )
            try:
                handle = runner.adopt(persisted)
            except launcher.HandleAbsent:
                # Adoption already proved this persisted pane's cwd and
                # placement before finding its actor record absent. Retire the
                # remaining shell before closing the durable actor generation.
                runner.close_actorless_pane(persisted)
                lifecycle_store.close_actor_session(
                    args.run_id,
                    build_node_id,
                    "reviewer",
                    generation=session.generation,
                )
                return
            except launcher.HandleAdoptionRefused:
                # A live actor in a superseded run layout still requires
                # identity-proven physical retirement before its row closes.
                runner.retire_for_replacement(
                    persisted, finalization_window.time.monotonic() + 5.0
                )
                lifecycle_store.close_actor_session(
                    args.run_id,
                    build_node_id,
                    "reviewer",
                    generation=session.generation,
                )
                return
        if handle is None:
            return
        runner.cancel(handle, finalization_window.time.monotonic() + 5.0)
        if session is not None:
            lifecycle_store.close_actor_session(
                args.run_id, build_node_id, "reviewer", generation=session.generation
            )

    def review(
        attempt,
        node,
        record,
        base_sha: str,
        output_sha: str,
        resume_existing_dispatch: bool = False,
    ):
        build_node_id = lane_id(node)
        # The rubric is a property of the node kind (§1.1 item 4). It is also
        # a component of the review digest, so a tests node and a build node
        # cannot share a cached verdict, and changing either rubric
        # invalidates only its own cached answers.
        rubric = code_review.rubric_for(node.kind)
        digest = code_review.review_digest(
            run_id=args.run_id,
            node_id=build_node_id,
            base_sha=base_sha,
            output_sha=output_sha,
            rubric_version=rubric.version,
        )
        subject_root = review_root / digest
        report_path = subject_root / "report.json"
        ledger_path = subject_root / code_review.FINDING_LEDGER_FILENAME
        prompt_path = subject_root / "prompt.md"
        # Actor material belongs to the lane, not one immutable candidate. The
        # candidate's prompt/report/receipt remain under its digest.
        lane_root = review_root / build_node_id
        session_dir = lane_root / "session"
        scratch_dir = lane_root / "scratch"

        diff, changed = code_review.read_diff(Path(args.repo), base_sha, output_sha)
        objects = code_review.review_objects(changed, output_sha)
        matrix = finalization.compute_matrix(rubric, digest, objects)
        # The measurement the acceptance decision will rest on, not a fresh
        # one: the reviewer must be judging the same evidence, and a second
        # measurement here would be a second answer to a settled fact.
        measured = None
        if lifecycle_store is not None and node.kind is scheduler_types.NodeKind.TESTS:
            recorded = lifecycle_store.test_gate_evidence(
                args.run_id, build_node_id, output_sha
            )
            measured = dict(recorded[-1].evidence) if recorded else None
        handoff = code_review.build_handoff(
            subject_digest=digest,
            run_id=args.run_id,
            node=node,
            base_sha=base_sha,
            output_sha=output_sha,
            diff=diff,
            matrix=matrix,
            rubric=rubric,
            report_path=report_path,
            test_evidence=measured,
        )
        text = handoff.render()
        _preflight_prompt(text, args.reviewer_route, args.reviewer_model)

        def window_factory(_matrix):
            subject_root.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(text, encoding="utf-8")
            # A durable dispatch may leave a syntactically valid draft while
            # its reviewer is still writing.  Only the common poller's
            # completeness contract may adopt a persisted report.
            persisted_report = _poll_reviewer_report(report_path)
            # What the ledger says about this exact (node, output_sha) review,
            # and it is the only thing consulted. `DISPATCHED` means an offer
            # was issued to a reviewer; `PUBLISHED` means one was not. Named
            # `recorded` rather than `proven` on purpose -- the edge is written
            # when the prompt is offered, not when the actor is observed to
            # have taken it, and calling that "proven" is the precise mistake
            # §16.3 item 136 is about.
            dispatch_is_recorded = resume_existing_dispatch
            # Resuming a row the ledger left PUBLISHED. Its one reader is the
            # stale-report decision below: a report on disk under this digest
            # may belong to an earlier generation, and only the poller's
            # completeness contract may adopt one.
            recovering_unproven_dispatch = False
            if lifecycle_store is not None:
                durable_review = lifecycle_store.candidate_review(
                    args.run_id,
                    "{}::review".format(build_node_id),
                    output_sha,
                )
                if durable_review is not None:
                    dispatch_is_recorded = (
                        durable_review.state
                        is scheduler_types.CandidateReviewState.DISPATCHED
                    )
                    recovering_unproven_dispatch = (
                        resume_existing_dispatch
                        and durable_review.state
                        is scheduler_types.CandidateReviewState.PUBLISHED
                    )
            if not dispatch_is_recorded and not recovering_unproven_dispatch:
                _clear_stale_reviewer_report(report_path)
                persisted_report = None
            submitted = False

            def mark_dispatched(handle) -> None:
                """Record that this generation was offered the review prompt.

                **Its precondition is an offer, not a proof, and that is
                deliberate.** `submit_agent_prompt` no longer refuses a lane
                prompt it could not prove was taken: turn length is unbounded
                (§7.6 measured omp's transcript at TURN granularity and says a
                turn doing real work runs far longer), so the end of any window
                over "has the actor recorded this yet" is a fact about the
                window. A reviewer turn runs 46-461s (§3.6) -- exactly as
                unbounded as a build turn -- so the reviewer path is uniform
                with the lane path rather than special.

                What follows is that this edge is written on the offer, and a
                failed offer is caught later by the finalization window
                observing a reviewer that never reports. That ordering is now
                load-bearing: it is what lets `PUBLISHED` mean "never offered"
                and `DISPATCHED` mean "do not offer again", which is what
                removed the duplicate-review window this path used to carry.
                Do not "tighten" it by moving the write back behind a proof --
                there is no bounded proof to move it behind.
                """
                nonlocal dispatch_is_recorded, recovering_unproven_dispatch
                if lifecycle_store is not None:
                    # Direct callback consumers (including offline contract
                    # tests) may use actor-session persistence without the
                    # scheduler's candidate ledger. The scheduler owns that
                    # ledger and records the same edge before terminal CAS.
                    if (
                        lifecycle_store.candidate_review(
                            args.run_id,
                            "{}::review".format(build_node_id),
                            output_sha,
                        )
                        is None
                    ):
                        dispatch_is_recorded = True
                        recovering_unproven_dispatch = False
                        return
                    session = active_session(build_node_id)
                    if (
                        session is None
                        or session.correlation_token != handle.correlation_token
                    ):
                        raise scheduler.AttemptOwnershipLost(
                            "reviewer dispatch actor binding changed"
                        )
                    durable = lifecycle_store.candidate_review(
                        args.run_id,
                        "{}::review".format(build_node_id),
                        output_sha,
                    )
                    if (
                        durable is not None
                        and durable.reviewer_generation != session.generation
                    ):
                        lifecycle_store.recover_review_dispatch(
                            args.run_id,
                            "{}::review".format(build_node_id),
                            output_sha,
                            expected_reviewer_generation=durable.reviewer_generation,
                            reviewer_generation=session.generation,
                        )
                    lifecycle_store.mark_review_dispatched(
                        args.run_id,
                        "{}::review".format(build_node_id),
                        output_sha,
                        reviewer_generation=session.generation,
                    )
                dispatch_is_recorded = True
                recovering_unproven_dispatch = False

            def launch_reviewer():
                nonlocal submitted
                handle = handles.get(build_node_id)
                if handle is not None:
                    if not dispatch_is_recorded and not submitted:
                        # LEDGER FIRST, THEN THE PROMPT. Do not reorder these.
                        #
                        # `CandidateReviewState` is a two-phase log: PUBLISHED
                        # is written *before* prompt submission and DISPATCHED
                        # after it, so a crash between them used to leave
                        # PUBLISHED meaning either "never offered" or "offered,
                        # and the process died before it could say so". The
                        # ledger could not tell those apart, and what stood in
                        # for it was a 10s look for the prompt's record in the
                        # actor's transcript, with the expiry read as "never
                        # offered" and answered with a second prompt.
                        #
                        # That is the same defect as §16.3 item 136, one
                        # consequence milder: the transcript is written at TURN
                        # granularity, turn length is unbounded, and no window
                        # over it can be sized correctly. A reviewer turn runs
                        # 46-461s (§3.6), so the 10s window expired on working
                        # reviewers and bought a duplicate review.
                        #
                        # Writing the edge first removes the ambiguity instead
                        # of guessing at it. PUBLISHED now means the offer was
                        # never begun, which is the only case that may be
                        # offered; DISPATCHED means an offer was issued, and an
                        # issued offer is never repeated. Nothing here consults
                        # an artifact the actor writes, so nothing here can be
                        # wrong about how long the actor takes to write it.
                        #
                        # The residual is a crash between this write and the
                        # prompt landing: the reviewer never receives it, and
                        # the finalization window adjudicates the silence. That
                        # is the same trade already accepted for recording
                        # dispatch on the offer rather than on proof, and it is
                        # the safe direction -- a review that does not happen
                        # is detectable, a duplicated review turn overwrites
                        # the report of the one that did.
                        #
                        # `mark_dispatched` also re-verifies the actor binding,
                        # so running it first means ownership is proven before
                        # any text is typed into the pane.
                        mark_dispatched(handle)
                        runner.resubmit(
                            handle,
                            prompt_path,
                            route=args.reviewer_route,
                            expected_token=handle.correlation_token,
                        )
                        submitted = True
                else:
                    session = active_session(build_node_id)
                    adopted_existing = False
                    if session is not None:
                        persisted = launcher.PersistedActorHandle(
                            correlation_token=session.correlation_token,
                            pane_id=session.pane_id,
                            agent_name=launcher.agent_name_for(
                                session.correlation_token
                            ),
                            launched_cwd=Path(args.repo),
                            transcript_path=Path(session.session_path),
                            envelope_path=report_path,
                            workspace_id=launcher.workspace_of(
                                _session_tab_id(session)
                            ),
                            tab_id=_session_tab_id(session),
                            lane_key=build_node_id,
                        )
                        try:
                            handle = runner.adopt(persisted)
                            adopted_existing = True
                        except (
                            launcher.HandleAbsent,
                            launcher.HandleAdoptionRefused,
                        ) as exc:
                            if isinstance(exc, launcher.HandleAbsent):
                                runner.close_actorless_pane(persisted)
                            else:
                                runner.retire_for_replacement(
                                    persisted,
                                    finalization_window.time.monotonic() + 5.0,
                                )
                            generation = session.generation + 1
                            handle = _typed_launch_pane(
                                runner,
                                launcher.LaunchSpec(
                                    correlation_token=(
                                        "review-{}-{}-a{}".format(
                                            args.run_id, build_node_id, generation
                                        )
                                    ),
                                    worktree=Path(args.repo),
                                    prompt_path=prompt_path,
                                    envelope_path=report_path,
                                    route=args.reviewer_route,
                                    model=args.reviewer_model,
                                    effort=args.reviewer_effort,
                                    profile=args.reviewer_profile,
                                    session_dir=session_dir,
                                    context_window_tokens=_route_context_window(
                                        args.reviewer_route, args.reviewer_model
                                    ),
                                    workspace_label=getattr(
                                        runner, "workspace_label", ""
                                    ),
                                    lane_key=build_node_id,
                                    lane_label=build_node_id,
                                    pane_role="reviewer",
                                    attempt_no=generation,
                                    pane_group_size=3,
                                    restrict_tools=getattr(
                                        args, "restrict_actor_tools", False
                                    ),
                                    environment=worktree.launch_env(
                                        scratch_dir,
                                        concurrency=getattr(args, "concurrency", None),
                                    ),
                                ),
                            )
                            if lifecycle_store is not None:
                                _require_session_path(handle, build_node_id, generation)
                            if lifecycle_store is not None:
                                recovered = lifecycle_store.recover_actor_session(
                                    args.run_id,
                                    build_node_id,
                                    "reviewer",
                                    expected_generation=session.generation,
                                    generation=generation,
                                    pane_id=handle.pane_id,
                                    session_path=str(handle.transcript_path),
                                    correlation_token=handle.correlation_token,
                                    tab_id=handle.tab_id,
                                )
                                if not recovered.recovered:
                                    runner.cancel(
                                        handle,
                                        finalization_window.time.monotonic() + 5.0,
                                    )
                                    raise scheduler.AttemptOwnershipLost(
                                        "reviewer generation changed"
                                    )
                            submitted = True
                    else:
                        generation = next_generation(build_node_id, record)
                        handle = _typed_launch_pane(
                            runner,
                            launcher.LaunchSpec(
                                correlation_token="review-{}-{}-a{}".format(
                                    args.run_id, build_node_id, generation
                                ),
                                worktree=Path(args.repo),
                                prompt_path=prompt_path,
                                envelope_path=report_path,
                                route=args.reviewer_route,
                                model=args.reviewer_model,
                                effort=args.reviewer_effort,
                                profile=args.reviewer_profile,
                                session_dir=session_dir,
                                context_window_tokens=_route_context_window(
                                    args.reviewer_route, args.reviewer_model
                                ),
                                workspace_label=getattr(runner, "workspace_label", ""),
                                lane_key=build_node_id,
                                lane_label=build_node_id,
                                pane_role="reviewer",
                                attempt_no=generation,
                                pane_group_size=3,
                                restrict_tools=getattr(
                                    args, "restrict_actor_tools", False
                                ),
                                environment=worktree.launch_env(
                                    scratch_dir,
                                    concurrency=getattr(args, "concurrency", None),
                                ),
                            ),
                        )
                        submitted = True
                        if lifecycle_store is not None:
                            _require_session_path(handle, build_node_id, generation)
                        if lifecycle_store is not None:
                            lifecycle_store.register_actor_session(
                                args.run_id,
                                build_node_id,
                                "reviewer",
                                generation=generation,
                                pane_id=handle.pane_id,
                                session_path=str(handle.transcript_path),
                                correlation_token=handle.correlation_token,
                                tab_id=handle.tab_id,
                            )
                    if adopted_existing:
                        if not dispatch_is_recorded:
                            # Ledger first, then the prompt -- see the branch
                            # above for why this order is the fix rather than
                            # an accident of it.
                            mark_dispatched(handle)
                            runner.resubmit(
                                handle,
                                prompt_path,
                                route=args.reviewer_route,
                                expected_token=handle.correlation_token,
                            )
                            submitted = True
                    if not dispatch_is_recorded and submitted:
                        mark_dispatched(handle)
                    handles[build_node_id] = handle
                return finalization_window.ReviewerSession(
                    route=args.reviewer_route,
                    model=args.reviewer_model,
                    session_id=handle.pane_id,
                    session_dir=str(session_dir),
                    harness_owned_group=handle.process_group is not None,
                    pid=handle.process_group or handle.liveness_pid,
                )

            def poll_report():
                return (
                    persisted_report
                    if persisted_report is not None
                    else _poll_reviewer_report(report_path)
                )

            def read_status(_session):
                handle = handles.get(build_node_id)
                return runner.agent_status(handle) if handle is not None else None

            return finalization_window.FinalizationWindow(
                config=finalization_window.FinalizationConfig(
                    finalization_timeout_s=args.review_timeout_s,
                    turn_timeout_s=args.reviewer_turn_timeout_s,
                    poll_interval_s=args.reviewer_poll_interval_s,
                    start_deadline_s=(
                        args.review_start_deadline_s
                        if getattr(args, "review_start_deadline_s", None) is not None
                        else finalization_window.DEFAULT_START_DEADLINE_S
                    ),
                    quiescence_confirm_s=(
                        args.review_quiescence_confirm_s
                        if getattr(args, "review_quiescence_confirm_s", None)
                        is not None
                        else finalization_window.DEFAULT_QUIESCENCE_CONFIRM_S
                    ),
                ),
                launch=launch_reviewer,
                poll_report=poll_report,
                kill=lambda _s: close(build_node_id),
                actor_status=read_status,
            )

        return code_review.review_attempt(
            subject_digest=digest,
            handoff=handoff,
            objects=objects,
            rubric=rubric,
            store=receipt_store,
            window_factory=window_factory,
            occupancy_reader=_reviewer_occupancy,
            reject_at=args.review_reject_grade,
            ledger_path=ledger_path,
        )

    # Scheduler holds the terminal edge; an accepted/rejected candidate keeps
    # this callable's actor open until that edge explicitly invokes `close`.
    review.close = close
    review.receipt_path_for = lambda digest: str(receipt_store.path_for(digest))
    return review


#: How a builder should look things up, appended to every agent-node prompt.
#:
#: An agent that reads whole files and greps the tree to find them spends its
#: context on bytes it does not need, and a builder that runs out of context
#: mid-attempt produces a partial change rather than an error. The routing
#: skill costs one line here and is measured in tokens the attempt does not
#: spend.
#:
#: This is guidance about tooling, not a term the attempt is judged on: it
#: names no path, no gate and no output, so nothing here can change what
#: verification asks. It is appended last for the same reason -- an agent that
#: stops reading early has already been told everything it is judged on.
AGENT_DISCOVERY_ROUTING = (
    "Use the code-intel-routing skill to decide how to look something up "
    "before you search: codemap for a known file's structure, LSP for a "
    "symbol's definitions and references, codebase-memory for \"where does "
    'this happen", and grep only for an exact literal in a known path. It is '
    "faster than reading files whole, and it leaves you the context to finish."
)


def _agent_node_prompt(node: Any, envelope: Path, retry_prompt: Optional[str]) -> str:
    """The instruction, plus every term the attempt is actually judged on.

    Verification asks four questions of an agent node -- did it write a typed
    envelope, was its gate red before and green after, and did it write only
    what it declared. An agent sent the instruction alone is told none of that,
    so it cannot satisfy terms it was never given: it works, stops, writes no
    envelope, and the attempt fails clause 1 with the work discarded.

    The prompt ends with `AGENT_DISCOVERY_ROUTING`, which is guidance rather
    than a term: nothing hashes this text -- `plan_digest` takes no prompt
    input and B13 measures only its byte size -- so appending to it changes no
    identity and invalidates no receipt.
    """
    lines = [node.instruction, ""]
    outputs = list(getattr(node, "outputs", ()) or ())
    if outputs:
        lines.append(
            "Write only these paths, relative to the repository root. A change "
            "to anything else fails the attempt:"
        )
        lines.extend("  " + path for path in outputs)
        lines.append("")
    gate = getattr(node, "gate", None)
    if gate is not None:
        lines.append(
            "Your work is judged by this command, run from {0!r}. It fails now "
            "and must pass, collecting at least {1} case(s), when you are "
            "done:".format(gate.cwd, gate.min_cases)
        )
        lines.append("  " + " ".join([gate.runner, *gate.argv]))
        lines.append("")
    if retry_prompt:
        lines.extend(["Retry guidance:", retry_prompt, ""])
    lines.extend(
        [
            "When you have finished, write this file and then stop:",
            "  " + str(envelope),
            '  {"success": true, "summary": "<what you did>"}',
            "",
            'Use "success": false if you could not finish, with the reason in '
            "the summary. The attempt is not verified without this file, and "
            "nothing else ends it.",
            "",
            AGENT_DISCOVERY_ROUTING,
        ]
    )
    return "\n".join(lines)


def _tests_node_prompt(node: Any, envelope: Path, retry_prompt: Optional[str]) -> str:
    """Goal, produces, acceptance — tests only, no implementation."""
    lines = [node.instruction, ""]
    outputs = list(getattr(node, "outputs", ()) or ())
    if outputs:
        lines.append(
            "Write only these test files, relative to the repository root. "
            "Do not write implementation. A change to anything else fails "
            "the attempt:"
        )
        lines.extend("  " + path for path in outputs)
        lines.append("")
    gate = getattr(node, "gate", None)
    if gate is not None:
        lines.append(
            "Maestro will collect these tests and require every new case to "
            "fail at the parent commit (implementation does not exist yet). "
            "A case that passes at the parent is refused. A collection "
            "error or an import crash of the test file is not a red:"
        )
        lines.append("  " + " ".join([gate.runner, *gate.argv]))
        lines.append("")
    if retry_prompt:
        lines.extend(["Retry guidance:", retry_prompt, ""])
    lines.extend(
        [
            "When you have finished, write this file and then stop:",
            "  " + str(envelope),
            '  {"success": true, "summary": "<what you did>"}',
            "",
            AGENT_DISCOVERY_ROUTING,
        ]
    )
    return "\n".join(lines)


def _append_needed_tests(prompt: str, worktree: Path, node: Any, plan: Any) -> str:
    """Attach already-merged test file bytes to a build node's handoff.

    The tests node merged first; those files sit in this worktree and are
    not this node's outputs. Size-checked by the existing B13 chokepoint
    after this function returns.
    """
    from adw_modules.tests_chain import is_test_path

    by_id = plan.node_by_id()
    chunks = []
    for need in getattr(node, "needs", ()) or ():
        needed = by_id.get(need)
        if not isinstance(needed, plan_model.TestsNode):
            continue
        for rel in needed.outputs:
            if not is_test_path(rel):
                continue
            target = Path(worktree) / rel
            if not target.is_file():
                continue
            chunks.append("### " + rel)
            chunks.append(target.read_text(encoding="utf-8"))
    if not chunks:
        return prompt
    return (
        prompt
        + "\nThe tests you must make pass are already in the tree:\n\n"
        + "\n".join(chunks)
        + "\n"
    )


def _worktree_holding_branch(repo: Path, branch: str) -> Optional[Path]:
    """The worktree that already has `branch` checked out, if any.

    Read from `git worktree list --porcelain` rather than inferred from a
    failed `worktree add`, so the refusal can name the checkout standing in
    the way instead of echoing git's exit status.
    """
    # §7.5: a git failure is a fact about the machine, never about the
    # repository. `return None` here reads to every caller as "no worktree
    # holds this branch" — a repository fact — so a git that failed for any
    # reason silently authorised the run to take a branch that may well be
    # checked out, and the operator got a raw `worktree add` error instead of
    # the refusal this function exists to phrase. `_execute_run` turns the
    # raised error into a typed refusal.
    try:
        listed = subprocess.run(
            ("git", "-C", str(repo), "worktree", "list", "--porcelain"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("GIT_READ_FAILED:worktree list in {}".format(repo)) from exc
    if listed.returncode != 0:
        raise RuntimeError(
            "GIT_READ_FAILED:worktree list in {} exited {}".format(
                repo, listed.returncode
            )
        )
    wanted = "refs/heads/" + branch
    path: Optional[Path] = None
    for line in (listed.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and line[len("branch ") :].strip() == wanted:
            return path
    return None


def _configured_runs_root(args: argparse.Namespace) -> Optional[Path]:
    """This installation's run root, or `None` when nothing declares one.

    `repository_state` is bound only when a run resolves through installed
    repository configuration. A run spelled out by hand on the command line has
    no declared run root, so no worktree can be *proven* to be this system's
    own, and the reclaim below stays out of it and lets the refusal speak.
    """
    state = getattr(args, "repository_state", None)
    return (Path(state) / "runs") if state else None


class _RunStateStillHeld(RuntimeError):
    """State a run still needs is not this system's litter to take back.

    Raised rather than returned so no caller can reach the removal by
    ignoring a value. The message names the run and the recorded state that
    refused, because "some run" is not a diagnosis an operator can act on.
    """


def _run_owning_worktree(occupant: Path, runs_root: Optional[Path]) -> Optional[str]:
    """The run id a checkout under this system's run root belongs to.

    The run root is laid out `<runs_root>/<run_id>/<...>`, so the first path
    component below the root *is* the run identity. This is the same
    structural fact `_reclaim_stranded_integration_worktree`'s containment
    check already relies on, read one component further, rather than a second
    naming convention nobody maintains.
    """
    if runs_root is None:
        return None
    try:
        relative = occupant.resolve().relative_to(runs_root.resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def _recorded_run_is_over(database: Any, run_id: str) -> Tuple[bool, str]:
    """Whether `run_id` can never be resumed, and the state that says so.

    The predicate is `lifecycle.resume_run`'s, inverted and read from the same
    columns: a run this answers `True` for is exactly a run a resume would
    refuse. There is one definition of "over for good" in this system and it
    is the one that decides whether an operator can come back to the work --
    a second definition here is how a verb starts deleting checkouts belonging
    to runs the operator has every right to resume.

    Both facts are typed values a scheduler wrote at the point the run stopped
    (§7.3), read from `runs` exactly as `_deliver_accepted_run` reads it. §1.2:
    nothing here consults a report, a pane, an envelope field, or an operator's
    memory of which verb they typed.

    Every unreadable case answers `False`. A missing ledger, an absent row, a
    schema without the cause column, a database that will not open -- none of
    them is evidence that a run finished, and the cost of guessing wrong is the
    operator's merged work: `scheduler.py` re-proves every MERGED node's
    `output_sha` against the integration head on resume and raises
    `DurableOutputIdentityError` when it is not an ancestor, so a destroyed
    checkout fails the run later rather than at the moment it was destroyed.
    """
    if database is None or not Path(database).is_file():
        return False, "no lifecycle ledger"
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(database), uri=True, timeout=5.0
        )
    except sqlite3.Error:
        return False, "unreadable lifecycle ledger"
    try:
        row = connection.execute(
            "SELECT latest_outcome FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        outcome = str(row[0]) if row and row[0] is not None else ""
        cause = ""
        if outcome == scheduler_types.RunOutcome.CANCELLED.value:
            cause_row = connection.execute(
                "SELECT cancel_cause FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            cause = str(cause_row[0]) if cause_row and cause_row[0] is not None else ""
    except sqlite3.Error:
        return False, "unreadable lifecycle ledger"
    finally:
        connection.close()
    if not outcome:
        # NULL is not "finished quietly": it is a run no scheduler ever
        # declared quiescence for, which includes every run that is live
        # right now (lifecycle.py, `runs.latest_outcome`).
        return False, "NULL (no declared outcome)"
    if outcome == scheduler_types.RunOutcome.ACCEPTED.value:
        return True, outcome
    if outcome == scheduler_types.RunOutcome.CANCELLED.value:
        reopenable = tuple(
            member.value for member in scheduler_types.REOPENABLE_CANCEL_CAUSES
        )
        if cause in reopenable:
            return False, outcome + " (" + cause + ")"
        return True, outcome + " (" + (cause or "unrecorded cause") + ")"
    # BLOCKED and STUCK are the resumable outcomes: §11.3's operator escapes
    # are legal only against those two, which is the same statement as "an
    # operator is still expected to come back to this run".
    return False, outcome


def _reclaim_stranded_integration_worktree(
    repo: Path,
    runs_root: Optional[Path],
    branch: str,
    database: Any,
    discard_live: bool = False,
) -> Optional[Path]:
    """Take back an integration checkout this system's own run root still holds.

    Permanent Maestro semantics, not a repair for one installation: an operator
    is never asked to hand-move a checkout to unblock a run that Maestro itself
    stranded. Maestro reclaims what Maestro created, and refuses only for
    checkouts it did not create. Every installation gets this, because the
    boundary below is derived from configuration and names no plan, no
    repository, and no deployment.

    One rule, one representation: `run start` and `deliver`'s release verb both
    reach the same question -- something holds the integration branch, may it
    be removed? -- and a second copy of the answer is how the two drift into
    disagreeing about whose worktree they are deleting.

    Two predicates, and both must hold. *Whose* is path containment against the
    configured run root, read from `git worktree list`. It is deliberately not
    a claim, a message, or a naming convention (§1.2): a worktree *under this
    repository's own runs directory* was created by this system and is a
    leftover of a previous run; anything else may be the operator's own
    checkout and is left exactly where it is, so the caller still refuses and
    explains itself.

    *Whether it is still in use* is `_recorded_run_is_over` against the run
    that owns the checkout. Containment alone says the worktree is Maestro's;
    it says nothing about whether Maestro is done with it. A run declared
    BLOCKED or STUCK is one an operator can resume, a run with no declared
    outcome may have a scheduler attached to it right now, and this function's
    own caller describes its subject as "the backlog" -- neither of those is
    backlog. The refusal is a raise, so no caller reaches the removal by
    ignoring a return value, and it carries the run id and the recorded state.

    `discard_live` is the operator's escape for the case they genuinely mean:
    a deliberate discard, asked for at the command line. It is not a default
    and never inferred, because a flag nobody typed is not an intention (§11.3).

    Removal is **unforced**. `--force` discards uncommitted content silently,
    and a tree that is over should have nothing to discard -- so git's refusal
    is evidence about the checkout rather than noise to override, exactly as
    `worktree.remove_attempt_worktree` treats it. A refusal is reported on
    stderr and the worktree is left for the operator.

    Only the checkout holding the integration branch is ever named here.
    Attempt worktrees -- including the blocked ones §8.8 retains for
    post-mortem -- hold their own attempt branches, never this one, so they are
    outside what this function can even select. `worktree prune` drops
    administrative records of directories that are already gone and never
    removes a worktree that still exists; it does not run on the refusal path,
    because a verb that declines to touch something touches nothing.

    Returns the path released, or `None` when nothing was.
    Raises `_RunStateStillHeld` when the owning run is not over.
    """
    occupant = _worktree_holding_branch(repo, branch)
    released: Optional[Path] = None
    if (
        occupant is not None
        and runs_root is not None
        and _path_is_within(occupant.resolve(), runs_root.resolve())
    ):
        run_id = _run_owning_worktree(occupant, runs_root)
        if run_id is None:
            over, state = False, "no run identity in its path"
        elif discard_live:
            over, state = True, "discarded at operator request"
        else:
            over, state = _recorded_run_is_over(database, run_id)
        if not over:
            raise _RunStateStillHeld(
                "the integration branch "
                + branch
                + " is checked out at "
                + str(occupant)
                + ", which belongs to run "
                + (run_id or "(unidentified)")
                + " whose recorded state is "
                + state
                + ". That run is resumable, and removing its "
                "integration checkout would take the merges every MERGED node "
                "is re-proved against on resume. Resume that run, or end it "
                "for good with `run cancel --discard`"
            )
        result = subprocess.run(
            ("git", "-C", str(repo), "worktree", "remove", str(occupant)),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            released = occupant
        else:
            # Unforced, so this is the ordinary way a non-empty tree says so.
            # The operator learns what is in the way instead of losing it.
            print(
                "integration worktree not reclaimed: {}: {}".format(
                    occupant, (result.stderr or result.stdout or "").strip()
                ),
                file=sys.stderr,
            )
    subprocess.run(
        ("git", "-C", str(repo), "worktree", "prune"),
        capture_output=True,
        text=True,
        check=False,
    )
    return released


def _release_run_integration_worktree(repo: Path, path: Optional[Path]) -> None:
    """Give up a run's own integration checkout, keeping its branch.

    Only the checkout this invocation added is ever passed here. The
    INTEGRATION_BRANCH_CHECKED_OUT refusal returns while somebody else's
    worktree -- possibly the operator's own -- holds the branch, and releasing
    that one would delete work this process never created.

    `--force`, because the integration gate executes inside this checkout and
    leaves untracked artifacts behind (§8.8); an unforced removal fails in the
    ordinary case, which would leave in place the leak this exists to close.
    Nothing merged is lost: `worktree remove` takes the checkout and never the
    branch, so every merged commit stays reachable for the operator to
    publish, and Maestro still performs no remote git operation at all (§8.8).
    Attempt worktrees -- including the blocked ones retained for post-mortem
    -- are not this function's business and are never named here.

    Never raises. It runs in the run's `finally`, where an exception would
    replace the outcome the run actually reached.
    """
    if path is None:
        return
    try:
        released = subprocess.run(
            ("git", "-C", str(repo), "worktree", "remove", "--force", str(path)),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        detail = str(exc)
    else:
        if released.returncode == 0:
            return
        detail = (released.stderr or released.stdout or "").strip()
    # stdout carries the run's report, so a failed release is reported beside
    # it rather than inside it: the operator learns there is a worktree left to
    # clean up, and the run still says exactly what it did.
    print(
        "integration worktree release failed: {}: {}".format(path, detail),
        file=sys.stderr,
    )


def _reviewer_occupancy(
    session: finalization_window.ReviewerSession,
) -> Optional[float]:
    """How full the reviewer's context window was after its last valid turn.

    The reading comes from the transcript the route wrote, not from the report
    the reviewer returned: the report is the thing being judged, so it cannot
    also be the evidence that the reviewer had room to judge it. Anything that
    cannot be read stays `None`, because `finalization.check_occupancy`
    convicts on a NULL row -- an unmeasured window is not a passing one.

    Occupancy is the last VALID assistant turn's window usage, exactly as
    `agent_pi` records it live: an aborted or errored turn reports usage that
    cannot be trusted and must not overwrite a good reading.
    """
    directory = session.session_dir
    if not directory:
        return None
    tokens = 0
    provider = ""
    model = ""
    for transcript in sorted(Path(directory).glob("*.jsonl")):
        try:
            lines = transcript.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                message = event
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            turn = agent_pi._context_tokens(usage)
            if turn and message.get("stopReason") not in ("aborted", "error"):
                tokens = turn
                provider = str(message.get("provider") or provider)
                model = str(message.get("model") or model)
    if not tokens:
        return None
    if not provider or not model:
        # The configured binding is `provider/model`; it is the fallback only,
        # because what actually ran is what the window belongs to.
        provider, _, model = str(session.model).partition("/")
    window = agent_pi.context_window(provider, model) if provider and model else 0
    if not window:
        return None
    return tokens / window


#: The rubric label a deterministically finalized plan carries. It is not a
#: rubric: no check was asked of anything, and the label exists so a reader of
#: a receipt can tell which authority wrote it without inspecting `cells`.
#: `Receipt.to_bytes` treats any label other than "maestro-rubric.v1" as the
#: graded shape, so this one round-trips through the closed receipt schema
#: unchanged -- which is why the deterministic path adds no receipt field. A
#: boolean would have had to join the schema's fixed key set, and that set is
#: compared exactly, so it would have rejected every receipt already signed.
DETERMINISTIC_RUBRIC_VERSION = "maestro-deterministic.v1"


def _deterministic_receipt(digest: str) -> finalization.Receipt:
    """The receipt an eligible plan earns, with no reviewer in the path.

    §1.2 forbids a lifecycle transition caused by an agent's prose. A plan
    becoming runnable is such a transition, and until this verb was rewritten
    it was caused by a reviewer's per-cell answers about a matrix of check ids
    -- a reviewer that was never shown the plan, only the matrix, the digest,
    and where to write its report. What replaces it is `plan_validate`'s
    deterministic obligations, every one re-derivable from git objects alone,
    which have already run by the time this is reached.

    So the receipt records who actually judged: route "deterministic", model
    "plan_validate", and the digest as the session id, because the computation
    is a pure function of those bytes and there is no session to name. `cells`
    is empty for the same reason -- no cell was answered -- and an empty cell
    list is the one case the receipt's per-cell key check skips, so the shape
    is already legal under the schema every existing signed receipt was
    written to.
    """
    return finalization.Receipt(
        plan_digest=digest,
        rubric_version=DETERMINISTIC_RUBRIC_VERSION,
        verdict=finalization.Verdict.PASS,
        cells=(),
        reviewer=finalization.ReviewerIdentity(
            route="deterministic", model="plan_validate", session_id=digest
        ),
        created_at_epoch=time.time(),
        reject_at=None,
    )


def _plan_finalize(args: argparse.Namespace) -> int:
    """`maestro plan finalize` (§11.1) -- deterministic, and dispatches nothing.

    Eligibility is `plan_validate`'s, unchanged. What used to follow it was a
    launched reviewer inside a bounded window; what follows it now is the
    signed receipt itself. The create-once and replay semantics of §6.5 are
    untouched, because they never belonged to the reviewer: `store.recover`
    finishes any interrupted publication, `store.has` short-circuits a digest
    that already carries a receipt, and a `ReceiptExists` raised by a
    concurrent writer replays that writer's receipt rather than overwriting
    it.
    """
    try:
        stored = Path(args.plan_file).read_bytes()
        validation = pv.validate_plan(
            stored,
            args.repo,
            receipts=_VerifiedReceipts(args),
            collector=_plan_collector(args),
        )
        if not validation.eligible:
            print(
                json.dumps(
                    {
                        "outcome": validation.outcome.value,
                        "digest": validation.digest,
                        "blockers": [
                            {
                                "obligation": row.obligation.value,
                                "pointer": row.pointer,
                                "message": row.message,
                            }
                            for row in validation.blockers
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 2
        store = _finalization_store(args)
        store.recover(validation.digest)
        if store.has(validation.digest):
            receipt, replayed = store.load(validation.digest), True
        else:
            receipt = _deterministic_receipt(validation.digest)
            try:
                store.write(receipt)
                replayed = False
            except finalization.ReceiptExists:
                receipt, replayed = store.load(validation.digest), True
    except _PlanReceiptConfigurationError as exc:
        return _refusal("FINALIZATION_CONFIGURATION_REQUIRED", str(exc))
    except (
        _PlanReceiptVerificationError,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        finalization.ReceiptInvalid,
    ) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (
        finalization.ReceiptStoreLocationError,
        receipt_crypto.KeyMaterialError,
        route_receipts.ReceiptInvalid,
        ValueError,
        OSError,
    ) as exc:
        return _refusal("FINALIZATION_FAILED", str(exc))
    print(
        json.dumps(
            {
                "outcome": "FINALIZED",
                "digest": receipt.plan_digest,
                "verdict": receipt.verdict.value,
                "replayed": replayed,
            },
            sort_keys=True,
        )
    )
    return 0


def _receipt_root(args: argparse.Namespace) -> Optional[Path]:
    raw = getattr(args, "receipt_dir", None) or os.environ.get("MAESTRO_RECEIPT_DIR")
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
                receipt_crypto.PUBLIC_KEY_SIZE
            )
        )
    try:
        return finalization.ReceiptStore(
            root,
            repo_paths=(getattr(args, "repo", "."),),
            data_dir=data_dir,
            verify_keys=verify_keys,
            create=False,
        )
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
                "are required for receipt access"
            ),
        )
        receipt = store.load(args.digest)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except FileNotFoundError:
        return _refusal("FINALIZED_PLAN_NOT_FOUND", args.digest)
    except (
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        UnicodeError,
        ValueError,
        KeyError,
    ) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(receipt.to_bytes().decode("utf-8"))
    return 0


def _plan_set_aside(args: argparse.Namespace) -> int:
    """`maestro plan set-aside` — §3.6 B10's operator escape.

    A FAIL receipt is terminal for those bytes, and that is correct while
    the FAIL is a fact about the plan. When it is a fact about Maestro —
    a rubric, a projection, or a reviewer harness defect — the plan is
    correct and unrunnable, and the only route back used to be a full
    authoring cycle whose entire purpose was to change the digest (§19
    M16). This verb sets the receipt aside instead, retaining it, and
    records who did it and why.

    Which of those two a given FAIL was is a judgment, so the verb makes
    the judgment attributable rather than making it for the operator.
    """
    try:
        store = _finalization_store(args)
        record = store.set_aside(
            args.digest, invoked_by=args.invoked_by, reason=args.reason
        )
    except _PlanReceiptConfigurationError as exc:
        return _refusal("SET_ASIDE_CONFIGURATION_REQUIRED", str(exc))
    except finalization.SetAsideRefused as exc:
        return _refusal("SET_ASIDE_REFUSED", str(exc))
    except (
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        UnicodeError,
    ) as exc:
        return _plan_receipt_verification_refusal(exc)
    except (ValueError, KeyError, OSError) as exc:
        return _refusal("SET_ASIDE_FAILED", str(exc))
    print(record.to_bytes().decode("utf-8"))
    return 0


def _plan_set_aside_log(args: argparse.Namespace) -> int:
    """Every escape ever invoked against a digest, with the receipt each
    one superseded — the half of B10 that makes the escape auditable
    rather than a hole."""
    try:
        store = _plan_receipt_store(
            args,
            missing_detail=(
                "--receipt-dir, --data-dir, and at least one --verify-key "
                "are required for receipt access"
            ),
        )
        entries = [
            {
                "record": json.loads(record.to_bytes().decode("utf-8")),
                "superseded_receipt": json.loads(
                    store.load_set_aside_receipt(args.digest, record.sequence)
                    .to_bytes()
                    .decode("utf-8")
                ),
            }
            for record in store.set_aside_records(args.digest)
        ]
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except (
        FileNotFoundError,
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        UnicodeError,
        ValueError,
        KeyError,
    ) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(json.dumps(entries, sort_keys=True))
    return 0


def _plan_list(args: argparse.Namespace) -> int:
    try:
        store = _plan_receipt_store(
            args,
            missing_detail=(
                "--receipt-dir, --data-dir, and at least one --verify-key "
                "are required for receipt access"
            ),
        )
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    digests = sorted(
        path.stem
        for path in store.root.glob("*.json")
        if len(path.stem) == 64
        and all(character in "0123456789abcdef" for character in path.stem)
    )
    try:
        for digest in digests:
            store.load(digest)
    except (
        FileNotFoundError,
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        UnicodeError,
        ValueError,
        KeyError,
    ) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(json.dumps(digests))
    return 0


def _store(args: argparse.Namespace) -> Optional[lc.LifecycleStore]:
    return lc.LifecycleStore(args.db) if getattr(args, "db", None) else None


def _run_start(args: argparse.Namespace) -> int:
    if not getattr(args, "plan_file", None):
        return _refusal(
            "RUN_CONFIGURATION_REQUIRED",
            "repository, finalized receipt, launcher roster, and liveness bounds are required",
        )
    try:
        return _execute_run(args, resuming=False)
    except _RunRefused as exc:
        return exc.emit()
    except launcher.HarnessQuiescenceError as exc:
        return _quiescence_refusal(exc)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RUN_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
    ) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except Exception as exc:
        # Everything with no better name, under the one it already had. The
        # arm that used to sit above this printed `type(exc).__name__.upper()`
        # and differed from it in nothing else, so the class name was the whole
        # of what it added — and a class name is not a refusal vocabulary. It
        # keeps its diagnostic value in `detail`, which is where prose belongs.
        return _refusal(
            "RUN_EXECUTION_FAILED", "{0}: {1}".format(type(exc).__name__, exc)
        )


def _missing_run_configuration_detail(
    args: argparse.Namespace, missing: Sequence[str]
) -> str:
    """The `missing run configuration` refusal, plus the rule that caused it.

    A list of fifteen absent values is a symptom, and on a configured
    repository it is never the operator's actual mistake: every one of them
    binds from `adws/maestro.config.yaml`. What happened is that one flag on
    the command line named a path by hand, so `_apply_repository_config`
    declined to bind the other half rather than let two halves describe
    different runs. The old message said none of that, so the reading it
    invited was "my installation is broken" — and the remedy, deleting one
    word, was not derivable from anything printed (#91).

    Same shape as #78, and the same correction: the refusal is right, the
    vocabulary named a consequence instead of the rule. Name the flag, name
    the rule, name both ways out.
    """
    detail = "missing run configuration: {}".format(", ".join(missing))
    manual = tuple(getattr(args, "manual_run_options", ()) or ())
    if not manual:
        return detail
    named = ", ".join(manual)
    return (
        detail
        + "; configuration binding was disabled by "
        + named
        + " because it names a path, key or executable by hand, and a run "
        "half-derived from " + str(_MAESTRO_CONFIG_FILE) + " and half typed "
        "at the prompt cannot be trusted to name one run. Either drop "
        + named
        + " and let every value above bind from configuration, or "
        "supply all of them as flags. Tuning flags ("
        + ", ".join(_RUN_TUNING_EXAMPLES)
        + " and the other settings in "
        "execution:) override a value without disabling binding."
    )


def _run_configuration(args: argparse.Namespace) -> scheduler_types.SchedulerConfig:
    required = (
        "plan_file",
        "repo",
        "receipt_dir",
        "data_dir",
        "verify_key",
        "digest",
        "db",
        "run_id",
        "integration_path",
        "worktrees_root",
        "scratch_root",
        "concurrency",
        "node_timeout_s",
        "turn_timeout_s",
        "final_acceptance_timeout_s",
        "backstop_t_s",
        "semantic_ceiling",
    )
    missing = [name for name in required if not getattr(args, name, None)]
    if missing:
        raise _PlanReceiptConfigurationError(
            _missing_run_configuration_detail(args, missing)
        )
    # Every field of `SchedulerConfig` is named here, and
    # `test_every_scheduler_config_field_is_projected` fails if one is not.
    # This is the projection §7.4 describes: the one that copied a gate's
    # runner, argv and selector and dropped its threshold, because a
    # field-by-field copy has no way to notice the field it did not copy.
    return scheduler_types.SchedulerConfig(
        concurrency=args.concurrency,
        node_timeout_s=args.node_timeout_s,
        turn_timeout_s=args.turn_timeout_s,
        final_acceptance_timeout_s=args.final_acceptance_timeout_s,
        backstop_t_s=args.backstop_t_s,
        semantic_ceiling=args.semantic_ceiling,
        review_ceiling=_scheduler_setting(args, "review_ceiling"),
        test_review_ceiling=_scheduler_setting(args, "test_review_ceiling"),
        environmental_retries=_scheduler_setting(args, "environmental_retries"),
        launcher_retries=_scheduler_setting(args, "launcher_retries"),
        credential_retries=_scheduler_setting(args, "credential_retries"),
    )


def _scheduler_setting(args: argparse.Namespace, name: str) -> Any:
    """One optional `SchedulerConfig` setting, from the run's arguments.

    Not `getattr(args, name, 2)`. A literal here would be a second
    representation of a number `SchedulerConfig` already declares, and the two
    would drift the first time either changed — the shape RC1 names. The
    fallback is the dataclass's own default, so there is exactly one of it, and
    a field with no default raises `KeyError` rather than acquiring an invented
    one.

    A configured run never reaches the fallback: `_apply_repository_config`
    writes all four of these from `maestro.config.yaml`, which resolves its own
    absent keys against the same table. The fallback exists for the manual
    `maestro run start --concurrency ...` invocation, where every one of these
    is an unsupplied `argparse` option.
    """
    value = getattr(args, name, None)
    if value is None:
        return _SCHEDULER_CONFIG_DEFAULTS[name]
    return value


def _paths_share_inode(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev,
        right_stat.st_ino,
    )


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


def _refuse_base_commit_divergence(
    args: argparse.Namespace, plan: plan_model.Plan
) -> Optional[int]:
    """Single-repo twin of workspace_runtime.prepare_candidate's SHA check.

    Attempt worktrees still branch from the integration head so `needs` works
    (§8.1). This only asserts that, at run start, that head still *is* the
    recorded authoring base. Resume skips it: merges in the same run are
    supposed to move the head. A fixture or double without those fields is
    left to the existing worktree-add refusal rather than invented here.

    Identity is the resolved commit object, not the spelling of the revision
    the plan recorded. Ingress accepts any nonempty Git revision that
    resolves to a commit, including an abbreviated SHA or a tag. A recorded
    base that cannot resolve is a refusal: failing open here used to create
    attempt worktrees from whatever the integration head currently is (#32).
    """
    branch = getattr(getattr(plan, "merge_policy", None), "integration_branch", None)
    base = getattr(plan, "base_commit", None)
    if not branch or not base:
        return None
    try:
        head = worktree.resolve_commit(Path(args.repo), branch)
        base_sha = worktree.resolve_commit(Path(args.repo), str(base))
    except worktree.WorktreeError as exc:
        return _refusal(
            "BASE_COMMIT_UNRESOLVABLE",
            "plan.base_commit {0} or integration branch {1} could not be "
            "resolved, so recorded-base identity cannot be verified: {2}".format(
                base, branch, exc
            ),
        )
    if head.lower() != base_sha.lower():
        return _refusal(
            "BASE_COMMIT_DIVERGED",
            "integration branch {0} is at {1}, plan.base_commit {2} "
            "resolves to {3}. The single-repo path used to create attempt "
            "worktrees against whatever {0} pointed at and never compared "
            "the two.".format(branch, head, base, base_sha),
        )
    return None


def _refuse_uncommittable_outputs(
    args: argparse.Namespace, plan: plan_model.Plan
) -> Optional[int]:
    """Fail closed when a node declares an output git will not commit.

    A gitignored path is outside `git ls-files --cached --others
    --exclude-standard`, so it is outside the measured delta, outside §8.3's
    permission check, and outside §8.4's commit. The node writes the file, the
    gate may even pass over it, and the attempt commits nothing — a silent
    empty success that costs a whole attempt to discover. `git check-ignore`
    answers the question from the plan alone, before any worktree is created.

    Placed beside `_refuse_base_commit_divergence` and, like it, at run start
    only. Both are preflights over the plan as authored, and the answer here
    is a function of the plan's declared outputs and the repository's ignore
    rules — neither of which the run itself writes, so re-asking on resume
    could only change its answer if an operator edited `.gitignore` mid-run.
    Refusing a resume for that would strand a run whose plan cannot be edited
    (the plan hash is checked at resume), with no repair but abandon. The
    resumed case is not left unguarded: `worktree.existing_ignored_outputs`
    runs at every attempt settle regardless of resume and blocks with the same
    `DECLARED_OUTPUT_UNCOMMITTABLE`, paying one attempt for the discovery
    instead of none.

    Globs are not asked about here — `check-ignore` names paths, not patterns
    — so a glob that happens to cover ignored files is caught at settle.
    """
    nodes = getattr(plan, "nodes", None) or ()
    ignored = []
    try:
        for node in nodes:
            outputs = getattr(node, "outputs", None) or ()
            hits = worktree.outputs_ignored_in_repo(Path(args.repo), outputs)
            for path in hits:
                ignored.append((getattr(node, "node_id", "?"), path))
    except worktree.WorktreeError:
        return None
    if not ignored:
        return None
    detail = "; ".join(
        "{0} declares {1}, which git check-ignore excludes".format(nid, path)
        for nid, path in ignored
    )
    return _refusal("DECLARED_OUTPUT_UNCOMMITTABLE", detail)


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
            "lifecycle database cannot be statted: {}".format(database)
        ) from exc
    if database_stat is not None and database_stat.st_nlink != 1:
        raise _RunPathConfigurationError(
            "lifecycle database has no single canonical inode: {}".format(database)
        )

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
                "lifecycle database is inside the {}: {}".format(label, boundary)
            )


#: The plan schema versions this runtime will execute (§6.3, §19 M26).
#:
#: An **allowlist**, not a denylist. A version registered later and not added
#: here is refused rather than run, which is the direction a fail-closed
#: check has to fail; a denylist would run every version nobody remembered to
#: list. Adding an entry is a decision that the projection which emits that
#: version produces instructions a reviewer can judge against — that is the
#: whole content of the v1/v2 distinction, and no structural check can
#: substitute for it.
_RUNNABLE_PLAN_SCHEMA_VERSIONS = frozenset(
    {plan_model.SCHEMA_V2, plan_model.SCHEMA_V3, plan_model.SCHEMA_V4}
)


def _refuse_unrunnable_plan_schema(args: argparse.Namespace, stored: bytes) -> None:
    """Refuse a plan whose schema version this runtime does not execute.

    **Why a version can refuse a run at all.** `plan_contract_ingress` used to
    map a lane's `title` onto `nodes[].instruction` and drop
    `requirements[].text`, so every builder prompt and every reviewer contract
    carried a summary of the lane's contract in place of the contract
    (§3.6 B9, §19 M26). The field was *populated* the whole time, which is
    precisely why nothing downstream could convict it: a totality guard sees a
    value, a reader sweep sees a reader, and a reviewer relays what it is
    given. The repair widened the field — and the projection went on emitting
    `maestro-plan.v1`, the same string the degenerate projection emitted, so a
    plan shipped before the repair and a plan shipped after it were
    indistinguishable to a runtime carrying every fix. Four shipped plans and
    51 agent nodes in the lexgenius-pipeline deployment are in the first
    state. This is the check that stops one of them starting.

    **Where it sits, and why here.** `_load_runnable_plan` is the one function
    that turns plan bytes into a plan a run will execute, and it is called
    from exactly one place — `_execute_run`, which `run start` and
    `run resume` both enter, and which the workspace coordinator reaches by
    invoking `maestro run start` as a participant subprocess. So the coverage
    claim ("no run starts against an unrunnable plan version") is a property
    of the call graph rather than of a list of verbs somebody has to keep
    updating. §19 M6 is the recorded cost of the alternative: B13's size
    preflight was installed on one launch path, a second route reached the
    same agent without crossing it, and the guarantee decayed with nothing
    going red.

    **What it keys on.** The `schema_version` string in the stored bytes, read
    *before* the bytes become a model. That is format selection, the way a
    magic number selects a decoder, and it is the one carve-out §5.3 grants to
    its stored-value prohibition; reading the version off a parsed `Plan`
    would be the forbidden direction, and nothing here does. Prose, pane text,
    and agent self-report are nowhere on this path (§1.2).

    **What it deliberately declines to answer.** Bytes with no
    `schema_version`, or bytes that are not JSON, get no opinion here —
    `plan_model.parse_bytes` already refuses both with its own typed errors
    (`SchemaVersionUnknown`, `PlanParseError`), and naming one defect in two
    vocabularies teaches an operator that the second one is optional. Nothing
    that could actually run escapes through the gap: bytes that reach a
    scheduler have parsed, and bytes that have parsed declared a registered
    version, and every registered version is either in the allowlist above or
    refused right here.
    """
    try:
        declared = json.loads(stored.decode("utf-8")).get("schema_version")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return
    if not isinstance(declared, str):
        return
    if declared in _RUNNABLE_PLAN_SCHEMA_VERSIONS:
        return
    runnable = ", ".join(sorted(_RUNNABLE_PLAN_SCHEMA_VERSIONS))
    raise _RunRefused(
        "RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE",
        "{plan} declares schema_version {declared!r}; this runtime executes "
        "{runnable}. A {declared} plan was projected by a `plan_contract_"
        "ingress` that wrote the lane title into nodes[].instruction and "
        "dropped requirements[].text, so its reviewers judge the work against "
        "a summary of its contract rather than the contract (§19 M26), and "
        "the field is populated either way so nothing downstream can tell. "
        "There is no upgrade function and no in-place edit (§6.3): re-ship "
        "the plan from its IR with `maestro plan ship <plan_name>` "
        "(docs/plan-authoring.md), which re-projects it at {runnable}, "
        "re-validates, and re-finalizes it under a new digest.".format(
            plan=args.plan_file, declared=declared, runnable=runnable
        ),
        declared_schema_version=declared,
        runnable_schema_versions=sorted(_RUNNABLE_PLAN_SCHEMA_VERSIONS),
    )


def _refuse_uncontracted_tests_nodes(
    args: argparse.Namespace, plan: plan_model.Plan
) -> None:
    """Refuse to **create** a run whose tests nodes prove nothing (§TS).

    The rollout invariant in two halves, and this is the second one. The first
    is that an existing run keeps the contract it was created under: it is
    resumable, its terminal nodes stay terminal, and its legacy tests are
    classified rather than reopened. This is the other side — a *new* run may
    not be created under those rules, because "the new lifecycle is mandatory
    for newly created runs" is only true if something refuses the alternative.

    Keyed on the plan's tests nodes rather than on its version string, so a
    plan that declares a contract for every tests node is admitted whatever it
    calls itself, and a plan with no tests nodes at all is admitted trivially.

    Resume is deliberately not on this path. `_execute_run` calls this only
    when starting, and a v3 run already in flight would otherwise be refused
    by the very check that exists to protect it.
    """
    uncontracted = [
        node.node_id
        for node in (getattr(plan, "tests_nodes", ()) or ())
        if getattr(node, "test_strength", None) is None
    ]
    if not uncontracted:
        return
    raise _RunRefused(
        "RUN_TEST_STRENGTH_CONTRACT_ABSENT",
        "these tests nodes declare no test-strength contract: {nodes}. A tests "
        "node accepted without one is accepted on its case count, and "
        "run-8d1a71f463e4430f92a125a8f8b3731d is what that permits: a tests "
        "node reached MERGED on four non-skipped cases while every "
        "implementation candidate it existed to gate was independently "
        "rejected. There is no upgrade function and no in-place edit (§6.3): "
        "re-ship the plan at {version} with `maestro plan ship <plan_name>`, "
        "declaring for each tests node the coverage obligations its cases must "
        "discharge and the falsifiability strategy its negative control will "
        "execute (docs/plan-authoring.md). An existing run of this plan is "
        "unaffected and stays resumable.".format(
            nodes=", ".join(sorted(uncontracted)),
            version=plan_model.SCHEMA_V4,
        ),
        uncontracted_tests_nodes=sorted(uncontracted),
        required_schema_version=plan_model.SCHEMA_V4,
    )


def _load_runnable_plan(args: argparse.Namespace) -> plan_model.Plan:
    stored = Path(args.plan_file).read_bytes()
    # First, because it is the most specific thing wrong with these bytes and
    # every check below would pass over a v1 plan and say nothing. An operator
    # whose run is refused for a stale receipt learns the wrong lesson about a
    # plan whose real defect is that its instructions are titles.
    _refuse_unrunnable_plan_schema(args, stored)
    receipts = _VerifiedReceipts(args)
    validation = pv.validate_plan(
        stored, args.repo, receipts=receipts, collector=_plan_collector(args)
    )
    if not validation.eligible or validation.digest != args.digest:
        raise _RunRefused(
            "RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE",
            "the plan bytes are not canonical at {0}, or the plan is not "
            "eligible to run".format(args.digest),
        )
    store = receipts._receipt_store()
    try:
        receipt = store.load(args.digest)
    except FileNotFoundError as exc:
        raise _receipt_absent(store, args.digest) from exc
    if receipt.verdict is not finalization.Verdict.PASS:
        raise _RunRefused(
            "RUN_RECEIPT_NOT_PASS",
            "the finalization receipt for {0} records {1}".format(
                args.digest, receipt.verdict.value
            ),
        )
    # The exact bytes this run executes, carried forward to be retained on the
    # run itself. Taken here rather than re-read later because these are the
    # bytes that were validated as canonical and whose digest the PASS receipt
    # above covers -- a second read could pick up a file edited in between.
    args.plan_bytes = stored
    return plan_model.parse_bytes(stored)


def _receipt_absent(
    store: "finalization.ReceiptStore", plan_digest: str
) -> _RunRefused:
    """`RUN_RECEIPT_ABSENT`, and which of its two causes this is.

    A run needs a PASS receipt, and there are two ways for the digest not to
    have one. It was never finalized — the ordinary case, and the one an
    operator meets by running a plan they have not shipped. Or `plan set-aside`
    took it: §3.6 B10's escape retains the FAILed receipt under an archival
    name and frees the live slot, so the *absence* is what admits a fresh
    review, and the absence is therefore deliberate rather than an oversight.

    The store can tell them apart — `set_aside_records` is non-empty for the
    second — so the refusal says which, as a typed field rather than as prose,
    because an operator staring at a set-aside digest and an operator staring
    at an unfinalized one need to do different things. A store that cannot be
    read at all answers `NEVER_FINALIZED`: the receipt is absent either way,
    and inventing a set-aside from a failed read would be the defaulting
    failure §19.2 names.
    """
    try:
        records = store.set_aside_records(plan_digest)
    except (
        OSError,
        ValueError,
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
    ):
        records = ()
    if records:
        return _RunRefused(
            "RUN_RECEIPT_ABSENT",
            "the finalization receipt for {0} was set aside {1} time(s); "
            "run `maestro plan finalize` to review those bytes afresh".format(
                plan_digest, len(records)
            ),
            cause="SET_ASIDE",
            set_aside_count=len(records),
        )
    return _RunRefused(
        "RUN_RECEIPT_ABSENT",
        "no finalization receipt exists for {0}".format(plan_digest),
        cause="NEVER_FINALIZED",
        set_aside_count=0,
    )


def _refuse_cross_run_node_budget(
    args: argparse.Namespace, plan: plan_model.Plan
) -> None:
    """Refuse a run whose nodes have already spent their fix-loop budget in
    earlier runs of the same plan.

    **The hole this closes.** `semantic_ceiling` is enforced by
    `Scheduler._semantic_ceiling_reached`, which counts
    `retry_policy.semantic_attempts_total` over `(run_id, node_id)`. That scope
    is deliberate and correct *within* a run — but a fresh `run start` mints a
    new `run_id`, `create_run` seeds a `node_lifecycle` row with an empty
    history, and the same node against the same plan bytes is handed a whole
    new ceiling. A node that cannot be made to pass can therefore be
    re-attempted without limit, one run at a time, and no budget in the system
    ever says so. That is #92's shape: a debt no amount of spending pays off,
    and no counter that can see it. It is also the measured shape of
    `lane-p5-gap-policy` — 39 attempts over four `run_id`s, no run of which
    exceeded its own in-run budget.

    **What it counts, and where the count comes from.** The cumulative total
    is `retry_policy.semantic_attempts_across_runs`, which sums
    `semantic_attempts_total` per prior run rather than restating its predicate
    (RC1: the second copy of this rule that had no caller disagreed with the
    enforced one by exactly one attempt). Prior runs are every `runs` row
    carrying this plan's digest except the one this invocation is about to
    execute — fresh on `run start`, and on `run resume` the run being
    re-entered, whose own spend the scheduler's in-run ceiling already owns.

    It counted **review rejections** until §19 M35, and that was the right
    predicate for exactly as long as a review rejection was the failure that
    repeated. Review no longer fails an attempt, so the failure that repeats is
    a content failure — a red gate, a clause-4 conviction, §7.4's post-work
    falsification refusal — and every one of those is a SEMANTIC row.

    **Grants are honoured.** `retry --force` / `retry --grant N` raise
    `node_lifecycle.granted_extra_attempts` on the run they were typed
    against, so a node an operator deliberately widened in run A would
    otherwise be refused at the start of run B by a rule reading the very
    rejections the grant absorbed. The effective ceiling is the configured one
    plus every grant standing on the prior runs' rows for that node.

    **The escape (§3.6 B10).** A budget with no operator exit would be the
    first in this system, and B10 makes the exit a requirement rather than a
    convenience. `--allow-exhausted-node <node_id>` admits the node it names,
    and its use is recorded as a typed transition on the run — never as a flag
    remembered only by a process that has exited (§1.2).

    **Where it sits.** In `_execute_run`, immediately after
    `_load_runnable_plan` and before any `LifecycleStore` write, worktree
    creation or launch — so a refused run leaves the ledger and the filesystem
    exactly as it found them. `_execute_run` is entered by `run start`, by
    `run resume`, and by the workspace coordinator's participant subprocess,
    so the coverage claim is a property of the call graph rather than of a
    list of verbs somebody has to keep updating (§19 M6).
    """
    # `to_plan_nodes()` rather than `node_by_id()`: these are the node ids
    # `create_run` is about to project, which is the same set the prior runs'
    # `dag_nodes` rows were projected from, so the two sides of the comparison
    # are derived from one definition rather than two.
    known = tuple(node.node_id for node in plan.to_plan_nodes())
    allowed = tuple(getattr(args, "allow_exhausted_nodes", None) or ())
    unknown = sorted(set(allowed) - set(known))
    if unknown:
        raise _RunRefused(
            "ALLOW_EXHAUSTED_NODE_UNKNOWN",
            "--allow-exhausted-node named {0}, which {1} does not contain. "
            "The plan's nodes are {2}. A misspelled node id would admit "
            "nothing and refuse nothing, so it is refused here rather than "
            "silently ignored.".format(
                ", ".join(unknown), args.plan_file, ", ".join(sorted(known))
            ),
            unknown_node_ids=unknown,
            plan_node_ids=sorted(known),
        )
    database = getattr(args, "db", None)
    ceiling = getattr(args, "semantic_ceiling", None)
    digest = getattr(args, "digest", None)
    if not database or not digest or ceiling is None or not Path(database).is_file():
        # No ledger means no prior run, which is the ordinary first start.
        # The reader would refuse an absent database, and creating one to
        # discover it is empty is exactly the side effect `LedgerUnavailable`
        # exists to prevent.
        #
        # An absent digest declines the question rather than widening it: the
        # digest is what scopes "the same node" to one plan, and `runs(None)`
        # would count every run of every plan in the repository against a lane
        # id that merely matches by name.
        return
    reader = _open_reader(database)
    try:
        excluded = getattr(args, "run_id", None)
        attempts_by_run = reader.attempts_by_run_for_plan(
            digest, exclude_run_id=excluded
        )
        granted = reader.granted_extra_attempts_for_plan(
            digest, exclude_run_id=excluded
        )
    finally:
        reader.close()

    exhausted: List[Dict[str, Any]] = []
    admitted: List[Dict[str, Any]] = []
    for node_id in sorted(known):
        total, run_ids = retry_policy.semantic_attempts_across_runs(
            attempts_by_run, node_id
        )
        effective = int(ceiling) + int(granted.get(node_id, 0))
        if total <= effective:
            continue
        record = {
            "node_id": node_id,
            "cumulative_semantic_attempts": total,
            "effective_ceiling": effective,
            "run_ids": list(run_ids),
        }
        (admitted if node_id in allowed else exhausted).append(record)

    if exhausted:
        raise _RunRefused(
            "NODE_BUDGET_EXHAUSTED_ACROSS_RUNS",
            "the fix-loop budget for {0} is already spent across earlier "
            "runs of {1}: {2}. `semantic_ceiling` is {3}, and a fresh run "
            "would hand each of these nodes the whole ceiling again, which is "
            "how a node that cannot be made to pass is re-attempted without "
            "limit. Read the series with `maestro run convergence <run_id>` "
            "and re-author the node's instruction, or admit it deliberately "
            "with `--allow-exhausted-node <node_id>`.".format(
                ", ".join(record["node_id"] for record in exhausted),
                digest,
                "; ".join(
                    "{0} spent {1} attempt(s) over {2}".format(
                        record["node_id"],
                        record["cumulative_semantic_attempts"],
                        ", ".join(record["run_ids"]),
                    )
                    for record in exhausted
                ),
                ceiling,
            ),
            plan_digest=digest,
            semantic_ceiling=int(ceiling),
            nodes=exhausted,
        )

    if admitted:
        store = lc.LifecycleStore(database)
        try:
            for record in admitted:
                store.record_budget_allowance(
                    args.run_id,
                    record["node_id"],
                    cumulative_semantic_attempts=record["cumulative_semantic_attempts"],
                    effective_ceiling=record["effective_ceiling"],
                    run_ids=record["run_ids"],
                )
        finally:
            store.close()


def _run_workspace_label(args: argparse.Namespace, plan: Any, *, resuming: bool) -> str:
    """Return the one persisted, authored workspace label for this run."""
    authored = str(getattr(plan, "title", "") or "")
    if not resuming:
        return authored or str(args.run_id)
    reader = lc.LifecycleReader.open(args.db)
    try:
        record = reader.run(args.run_id)
    finally:
        reader.close()
    if record is None:
        raise _RunSelectionError("unknown run {}".format(args.run_id))
    return str(record.plan_name or authored or args.run_id)


def _runtime_launcher(
    args: argparse.Namespace, workspace_label: str
) -> launcher.HerdrLauncher:
    required = (
        args.herdr,
        args.omp,
        args.claude,
        args.agent_route,
        args.agent_model,
        args.agent_effort,
        args.route_receipt,
        args.route_verify_key,
    )
    if not all(required):
        raise _PlanReceiptConfigurationError(
            "Herdr launcher route, verified route receipt, and agent model "
            "configuration are required for agent nodes"
        )
    try:
        keys = tuple(bytes.fromhex(value) for value in args.route_verify_key)
        paths = dict(item.split("=", 1) for item in args.route_receipt)
    except (TypeError, ValueError) as exc:
        raise _PlanReceiptConfigurationError(
            "route receipts are ROUTE=PATH and route keys are hexadecimal"
        ) from exc
    admitted = route_receipts.load_admitted_routes(
        {route: Path(path) for route, path in paths.items()}, verify_keys=keys
    )
    return launcher.HerdrLauncher(
        herdr_path=Path(args.herdr),
        omp_path=Path(args.omp),
        claude_path=Path(args.claude),
        admitted_routes=admitted,
        # §9.3's sixth operation. The adapter has implemented it since it was
        # written; nothing had ever handed it the argv to run, so it returned
        # immediately for every attempt of every run.
        provision_argv=tuple(getattr(args, "provision_argv", None) or ()),
        # One workspace per plan. The validated label is resolved once by
        # `_execute_run`; resume reads the persisted run row rather than
        # deriving another label from argparse state.
        workspace_label=workspace_label,
    )


def _restore_run_placement(
    runner: "launcher.HerdrLauncher",
    store: "lc.LifecycleStore",
    run_id: str,
    lane_ids: Iterable[str],
) -> None:
    """Seed a resumed launcher from each lane's newest live durable pane.

    Actor adoption is intentionally not the placement authority. A completed
    builder may have advanced to a later session generation while the node
    attempt being recovered retains its original generation. The pane and tab
    IDs still identify the run workspace and lane tab, so prove and restore
    those before any resumed reviewer is allowed to launch.
    """
    for lane_id in sorted(set(lane_ids)):
        restored = False
        for actor_role in ("builder", "reviewer"):
            sessions = reversed(
                store.actor_sessions(
                    run_id, lane_id, actor_role=actor_role, limit=10_000
                )
            )
            for session in sessions:
                tab_id = _session_tab_id(session)
                if not session.pane_id or not tab_id:
                    continue
                try:
                    runner.restore_placement(
                        workspace_id=launcher.workspace_of(tab_id),
                        lane_key=lane_id,
                        tab_id=tab_id,
                        pane_id=session.pane_id,
                        environment={},
                    )
                except launcher.HandleAbsent:
                    continue
                restored = True
                break
            if restored:
                break


class _ConfiguredProvisioner:
    """§9.3's `provision(worktree)` for a run that launches no agent.

    Provision is a runner-adapter operation, and a plan of code nodes alone has
    no runner adapter: the scheduler starts those commands itself (§8.3). The
    tree they run in still needs provisioning — §8.3's baseline is the
    provisioned tree for a code node exactly as it is for an agent node, and a
    code node's command in an unprovisioned JavaScript checkout fails on the
    absent install, not on the work.

    `HerdrLauncher.provision` is reused rather than restated. A second copy of
    "run the configured argv in the worktree, refuse on a nonzero exit" is one
    behaviour with two representations, and the two would answer differently
    the first time either changed.
    """

    provision = launcher.HerdrLauncher.provision

    def __init__(self, provision_argv: Sequence[str]) -> None:
        self.provision_argv = tuple(provision_argv)


def _run_provisioner(args: argparse.Namespace, route_runner):
    """§8.3's provision step, for whichever kind of run this is.

    Returns `None` only when nothing is configured to run, which is §9.3's
    stated default of a no-op and is what a pytest repository wants.
    """
    if route_runner is not None:
        return route_runner.provision
    argv = tuple(getattr(args, "provision_argv", None) or ())
    if not argv:
        return None
    return _ConfiguredProvisioner(argv).provision


# ── §7.5's launcher triggers, from the adapter's own typed error class ───────
#
# The chain reads no text at any step: `launcher.classify_error` dispatches on
# an exception's *type*, `ErrorClass` is a closed enum, and so is
# `LauncherFailure`. §7.5 names exception type among the facts a classifier may
# read and forbids it reading process output, so a refusal whose code is
# embedded in a message string — `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:...`
# — is classified by what it *is* rather than by matching its prefix. Matching
# the prefix would be the lexical shortcut §7.5 forbids, and the launcher has
# already settled the same question for itself (`HerdrCallError`: branch on
# `.code`, never on the message).
#
# Coarse on purpose. §7.5 closes the retry classes at three, so a
# *deterministic* refusal — a missing scratch redirect, a missing session path
# — still spends the launcher budget and blocks LAUNCHER_BUDGET_EXHAUSTED
# rather than blocking at once. That is a stated cost, not an oversight:
# separating it needs a fourth non-retryable reason and typed refusal codes at
# the launcher boundary, which is a design change rather than this repair.
_LAUNCHER_FAILURE_BY_ERROR_CLASS = {
    launcher.ErrorClass.AUTHENTICATION: retry_policy.LauncherFailure.CREDENTIAL,
    launcher.ErrorClass.TRANSIENT: retry_policy.LauncherFailure.TRANSPORT,
    launcher.ErrorClass.CONFIGURATION: retry_policy.LauncherFailure.STARTUP,
    launcher.ErrorClass.PROTOCOL: retry_policy.LauncherFailure.STARTUP,
    launcher.ErrorClass.EXECUTION: retry_policy.LauncherFailure.STARTUP,
}

#: Exceptions that mean something other than "the launch failed" and must
#: reach their own handlers untouched: cancellation, the quiescence proof, and
#: a failure already carrying its type.
_LAUNCH_PASSTHROUGH = (
    launcher.HarnessCancelled,
    launcher.HarnessQuiescenceError,
    scheduler.AttemptCancelled,
    scheduler.AttemptOwnershipLost,
    scheduler.QuiescenceFailure,
    scheduler.LaunchFailed,
)


def _launcher_failure_for(
    adapter: Any, exc: BaseException
) -> retry_policy.LauncherFailure:
    """The typed launcher class for one failed launch or poll.

    STARTUP is the fall-through rather than a raise: an `ErrorClass` this
    mapping has not met is still a launch that failed, and refusing to name it
    would drop the attempt back to the ENVIRONMENTAL default — the exact
    misclassification this function exists to end.
    """
    return _LAUNCHER_FAILURE_BY_ERROR_CLASS.get(
        adapter.classify(exc), retry_policy.LauncherFailure.STARTUP
    )


def _typed_launch(adapter: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one launcher call, converting its failure into a typed one.

    `LauncherAdapter.classify` has been part of the adapter contract since the
    seam was written and had no production caller: the runtime asked the
    launcher to launch and never asked it what kind of failure it had just
    had. This is the call site that was missing, and without it every launcher
    fault arrived at the scheduler as a bare exception carrying its code in
    prose (§16.3 item 42).
    """
    try:
        return operation(*args, **kwargs)
    except _LAUNCH_PASSTHROUGH:
        raise
    except Exception as exc:
        raise scheduler.LaunchFailed(
            _launcher_failure_for(adapter, exc),
            "{0}: {1}".format(type(exc).__name__, exc),
        ) from exc


def _typed_launch_pane(runner: Any, spec: "launcher.LaunchSpec") -> Any:
    """Open one agent pane, converting a refusal into a typed failure.

    Named for the operation rather than for any one caller: its three callers
    are the finalization window's reviewer, the node reviewer, and the plan
    author, and only the first two are reviewers.

    **The cause is already fixed; this closes the classification gap.** Every
    `LaunchSpec` in this module now carries a redirected environment, so
    `SCRATCH_REDIRECT_MISSING` cannot fire at these sites again. What was
    still open is what happens to the *next* refusal here, whatever it turns
    out to be: as a bare exception it reached the scheduler with nothing
    structural in it and silently spent the ENVIRONMENTAL budget. Through
    this it spends the launcher budget and names itself (§7.5). That makes
    this a different defect at the same seam, not a second guard over a fixed
    one.

    Uniformity is the other reason it exists. With every `.launch(` in this
    module behind one path, the structural guard in
    `tests/test_launcher_classification.py` needs no allowlist — and an
    allowlist with one entry on day one is how such a guard starts rotting.

    Deliberately *not* the place the target's context window is attached. Every
    caller here has already resolved that window to size-check its own prompt,
    so filling it in centrally would only hide an omission: a dispatch site that
    never measured anything would be handed a window it did not ask for and
    would launch. Left off the spec, it reaches `preflight_launch_prompt` as
    "nobody measured this" and is refused (§3.6 B13). Omission fails closed.
    """
    return _typed_launch(runner, runner.launch, spec)


def _require_session_path(handle: Any, node_id: str, attempt_no: int) -> None:
    """Refuse a launch that produced no session file, with its class attached.

    A handle without a transcript path produced no agent to watch: §7.6's
    turn-count signal has nothing to read, so the attempt cannot be supervised
    even if something is alive behind it. Module level so the refusal is
    reachable from a test — as a bare `RuntimeError` inside the run-node
    closure it was both unclassifiable and untestable, and it is the failure
    that took every attempt of run-f31686ea41b44c33b117f64e3b319317.

    STARTUP rather than the ENVIRONMENTAL default: this is the launcher's
    output being incomplete, not the machine misbehaving, so it spends the
    launcher budget (§7.5).
    """
    if handle.transcript_path is not None:
        return
    raise scheduler.LaunchFailed(
        retry_policy.LauncherFailure.STARTUP,
        "SESSION_PATH_MISSING:{0}#{1}".format(node_id, attempt_no),
    )


def _launch_attempt_extra(
    session_path: str,
    *,
    vendor: object = None,
    model: object = None,
    route: object = None,
) -> Dict[str, str]:
    """session_path plus the identity keys the launcher holds at dispatch.

    Adds to the existing mapping. Replacing it would drop
    `watchdog.SESSION_PATH_KEY` and break the transcript signal.
    """
    extra = {watchdog.SESSION_PATH_KEY: session_path}
    extra.update(
        attempt_identity.launch_identity_extra(vendor=vendor, model=model, route=route)
    )
    return extra


def _preflight_prompt(text: str, route: str, model_spec: str) -> Optional[int]:
    """Size-check one assembled prompt against its target's window (B13).

    Returns the token estimate when the route publishes a window and the prompt
    fits, and ``None`` when the route publishes no window at all. Raises
    `code_review.HandoffTooLarge` when the prompt does not fit, and when a route
    that has a catalog does not carry this model — an unmeasured window on a
    measurable route is not a passing one.

    The model is resolved through the route's own catalog rather than split on a
    slash. `x-ai/grok-4.6` is a *pattern*, and reading its two halves as a
    provider and an id looks it up under a provider the catalog does not have:
    the window comes back 0, and a check that fails closed on 0 refuses every
    launch of a correctly configured lane. Resolution is the catalog's job and
    it already does it.

    This lives at the CLI rather than beside `preflight_handoff` because
    reading a catalog means importing `agent_pi`, and `enforcement.py`'s
    `base-execution-import` check convicts any `adw_modules` policy module that
    does. The rule and the arithmetic stay in `code_review`; the window is
    fetched here and passed in.

    Nothing here reads the prompt's content. A length in bytes against a
    ceiling in tokens is arithmetic on two integers, which is what §1.2 permits.
    """
    window = _route_context_window(route, model_spec)
    if window is None:
        return None
    return code_review.preflight_handoff(text, window)


def _route_context_window(route: str, model_spec: str) -> Optional[int]:
    """The window B13 measures against, or `None` if the route publishes none.

    Split out of `_preflight_prompt` because the number has a second reader:
    `launcher.preflight_launch_prompt` runs the same check at the dispatch
    chokepoint and cannot read a catalog itself, so the window travels to it on
    the `LaunchSpec`. One resolution, two enforcement points — rather than the
    launcher re-deriving a window and the two disagreeing about which model a
    pattern names.

    Raises `HandoffTooLarge` when a route that *has* a catalog does not carry
    this model: an unmeasured window on a measurable route is not a passing one.
    """
    if not code_review.route_publishes_a_window(route):
        return None
    try:
        provider, model_id = agent_pi.resolve_model(str(model_spec))
    except ValueError as exc:
        raise code_review.HandoffTooLarge(
            "model {0!r} does not resolve in the {1} catalog, so the prompt "
            "cannot be shown to fit any window: {2} (B13)".format(
                model_spec, route, exc
            )
        ) from exc
    return agent_pi.context_window(provider, model_id)


#: Paths inside an attempt's scratch that only a dispatched attempt creates.
#: The prompt is written immediately before the launch and the envelope is the
#: agent's own declaration, so either one present means the handoff was made.
_DISPATCH_RESIDUE_FILES: Tuple[str, ...] = (
    "agent-prompt.txt",
    "agent-envelope.json",
    "repair-prompt.md",
    "repair-acknowledgement.json",
)


def _undispatched_attempt_residue(
    args: argparse.Namespace, store: Any, node_id: str, attempt_no: int
) -> Tuple[str, ...]:
    """Everything the runtime can find that a never-dispatched attempt cannot have.

    The ledger's half of this proof is `undispatched_quiescence_attempts`, and
    it is a statement about rows. This is the half over the paths the runtime
    owns and the ledger cannot see, and it is deliberately a *finder* rather
    than a predicate: an attempt that fails it stays blocked and the operator
    is told which artefact refused it, instead of being told "ineligible".

    Empty means proven clean. Anything else means the attempt is not provably
    undispatched, whatever the rows say, and it keeps its block.
    """
    residue: List[str] = []
    scratch = Path(args.scratch_root) / worktree.worktree_dirname(
        args.run_id, node_id, attempt_no
    )
    for name in _DISPATCH_RESIDUE_FILES:
        if (scratch / name).exists():
            residue.append(name)
    session_dir = scratch / "session"
    if session_dir.is_dir() and any(session_dir.iterdir()):
        # A route writes its transcript here, and it does so on launch. A
        # directory that exists but is empty is the launch environment's own
        # skeleton, which `worktree.launch_env` makes before any dispatch.
        residue.append("session/")
    if not worktree.attempt_worktree_exists(
        Path(args.worktrees_root), args.run_id, node_id, attempt_no
    ):
        # §8.8's cleanup already took it. Nothing to measure and nothing that
        # could hold output; the durable half already showed none was recorded.
        return tuple(residue)
    try:
        record = store.get_attempt(args.run_id, node_id, attempt_no)
        reopened = worktree.reopen_attempt_worktree(
            Path(args.repo),
            args.run_id,
            node_id,
            attempt_no,
            record.base_sha,
            Path(args.worktrees_root),
            Path(args.scratch_root),
        )
        identity = worktree.check_at_create(reopened)
        if not identity.ok:
            residue.append("worktree identity: " + "; ".join(identity.detail))
            return tuple(residue)
        baseline = store.attempt_baseline(args.run_id, node_id, attempt_no)
    except lc.BaselineUnrecorded:
        # The bracket never opened, so there is no before-side to measure this
        # checkout against, and a bare `git status` cannot tell a provisioned
        # path from a produced one. Unmeasurable is not clean.
        residue.append("worktree present with no recorded baseline")
        return tuple(residue)
    except (lc.LifecycleError, worktree.WorktreeError) as exc:
        residue.append("{0}: {1}".format(type(exc).__name__, exc))
        return tuple(residue)
    # The bracket's own comparison, against the bracket's own before-side.
    # A provisioned path the baseline already measured is not output; a path
    # whose tuple diverges is, whatever it is called (§8.3).
    verdict = worktree.compare_to_expected(reopened.path, baseline, "report")
    if not verdict.clean:
        residue.extend(
            "worktree {0}: {1}".format(divergence.kind, divergence.path)
            for divergence in verdict.divergences[:5]
        )
    return tuple(residue)


def _clear_stale_reviewer_report(report_path: Path) -> None:
    """Remove a report left by an earlier reviewer before launching a new one.

    The window completes on the presence of a report, so a report already on
    disk ends the new reviewer's window on its first poll -- with the bytes
    some previous reviewer wrote. The receipt would then bind the new
    session's route, model, and session id to a report that session did not
    produce, which is exactly the identity the receipt exists to establish.

    `_deliver_lane` already clears its envelope for the same reason ("a stale
    envelope from a previous attempt would end this one before it began").
    Replay is unaffected: `finalization.finalize` short-circuits on a stored
    receipt for the digest before any window is built, so anything reaching
    here has no receipt and is genuinely being reviewed afresh.
    """
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass


def _poll_reviewer_report(report_path: Path) -> Optional[Any]:
    """One poll of a reviewer's report file: the payload, or None while the
    reviewer is still writing it.

    Module level and shared by both reviewer windows because the failure it
    prevents is a race, and a race reproduced in a test needs the production
    reader, not a paraphrase of it. Two ways a poll can land mid-write:

    * the bytes do not parse yet -- a partial flush;
    * the bytes parse and the report is not finished -- fewer cells present
      than the `pair_count` the reviewer itself declared.

    The second one is what condemned `cmo-consolidation-l` on 2026-08-18: the
    window accepted a draft carrying a handful of cells, `verify_report`
    rejected it for a `CELL_SET` the reviewer was still filling in, and the
    complete 136-cell report landed on disk moments later. A rejection is
    terminal for the plan's bytes, so a read race became a verdict.

    Neither test reads prose. `report_is_complete` compares a declared count
    against a length (S1.2).
    """
    if not report_path.is_file():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not finalization.report_is_complete(payload):
        return None
    return payload


def _clear_turn_artifact(path: Path) -> None:
    """Remove one prior turn's terminal artifact before interactive reuse."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@dataclasses.dataclass(frozen=True)
class _ActorDispatch:
    """Which actor a lane's pane launches as, and on whose route.

    One resolution read by every site that opens a pane for a lane: the
    first attempt in `run_node`, and the repair. Before this existed
    `launch_replacement` hardcoded `args.agent_route`, `args.agent_model`
    and `pane_role="builder"` for whatever node it was handed, so a repaired
    tests node would have opened a *builder* pane on the build lane's
    vendor. That is the wrong actor on the wrong route wearing the wrong
    label: `tester_vendor` is bound to `lane_vendor` and is a separate route
    on purpose, because the information barrier between the lane that writes
    the tests and the lane judged by them is the node split (B12, §19 M41).
    """

    route: str
    model: str
    effort: str
    profile: Optional[str]
    vendor: Optional[str]


def _lane_actor_dispatch(args: Any, node_kind: Any) -> _ActorDispatch:
    """Resolve one lane kind's actor route, falling back to the agent route."""
    if node_kind is scheduler_types.NodeKind.TESTS:
        return _ActorDispatch(
            route=getattr(args, "tester_route", None) or args.agent_route,
            model=getattr(args, "tester_model", None) or args.agent_model,
            effort=getattr(args, "tester_effort", None) or args.agent_effort,
            profile=getattr(args, "tester_profile", None) or args.agent_profile,
            # B15: tester.vendor is loaded onto args and recorded on the
            # attempt row. `LaunchSpec` has no vendor field, so this travels
            # through `_launch_attempt_extra` rather than through the spec.
            vendor=getattr(args, "tester_vendor", None),
        )
    return _ActorDispatch(
        route=args.agent_route,
        model=args.agent_model,
        effort=args.agent_effort,
        profile=args.agent_profile,
        vendor=getattr(args, "execution_vendor", None),
    )


def _repair_prompt_text(
    repair_prompt: str,
    rejected_candidate_sha: str,
    builder_generation: int,
    acknowledgement_path: Path,
    *,
    same_session: bool = True,
) -> str:
    """Render the exact durable handoff plus a schema-bound acknowledgement.

    `same_session` is false for a lane whose actor is one-shot -- a tests
    node's tester, whose pane is cancelled when its attempt settles. Telling
    a freshly opened actor to "keep working in this existing session" names
    a session it has never had, and the worktree it inherits is the only
    continuity there is.
    """
    continuation = (
        "Keep working in this existing worktree and session."
        if same_session
        else (
            "This is a fresh actor turn in the existing worktree of the "
            "rejected attempt: read the tree rather than assuming any prior "
            "session's context."
        )
    )
    return (
        "# Persistent repair handoff\n\n"
        "Rejected candidate SHA: `{}`\n"
        "Builder generation: `{}`\n\n"
        "{}\n\n"
        "Before making the repair, write exactly this JSON object to "
        "`{}` (no additional keys):\n"
        '```json\n{{"builder_generation": {}, "kind": '
        '"repair_acknowledgement", "rejected_candidate_sha": "{}"}}\n```\n'
        "{} Commit a distinct "
        "descendant candidate, then write the normal terminal envelope."
    ).format(
        rejected_candidate_sha,
        builder_generation,
        repair_prompt,
        acknowledgement_path,
        builder_generation,
        rejected_candidate_sha,
        continuation,
    )


def _read_repair_acknowledgement(
    acknowledgement_path: Path, rejected_candidate_sha: str, builder_generation: int
) -> bool:
    """Accept only the exact acknowledgement for this handoff generation."""
    try:
        payload = json.loads(acknowledgement_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    expected = {
        "kind": "repair_acknowledgement",
        "rejected_candidate_sha": rejected_candidate_sha,
        "builder_generation": builder_generation,
    }
    return isinstance(payload, dict) and payload == expected


def _poll_agent_execution(
    adapter: Any,
    handle: Any,
    envelope_path: Path,
    record: Any,
    cancel_requested: Any,
    quiesce_attempt: Any,
    sleep: Any = time.sleep,
) -> "scheduler.NodeExecution":
    """Poll one launched agent until it settles, and never spin on GONE.

    Module level rather than a closure inside `_execute_run` because the real
    adapter path was otherwise untestable: every existing test that reaches a
    scheduler supplies its own `run_node`, the golden scenario included, which
    is how §16.3 item 42's defect survived a green suite.

    **The GONE branch is the repair.** `PollState.GONE` had no branch at all,
    so a vanished agent fell through to the sleep below and the loop re-polled
    at 20Hz forever. The worker thread never returned, its executor slot was
    never released, and the watchdog's conviction could not stop it — the
    worker checks nothing until the runner returns, and `cancel_requested` is
    the run-level flag rather than this attempt's. With concurrency equal to
    the pane limit, enough vanished agents wedge the run outright, and it ends
    at §11.2's liveness backstop rather than at any node timeout. Returning
    here ends the attempt carrying a typed launcher class, which is what makes
    it a LAUNCHER_TRANSIENT retry rather than an ENVIRONMENTAL one (§7.5).
    """
    while True:
        state = _typed_launch(adapter, adapter.poll, handle)
        if state.state is launcher.PollState.EXITED:
            parsed = False
            payload = None
            try:
                declared = json.loads(envelope_path.read_text(encoding="utf-8"))
                parsed = True
                # §7.7's result payload. This object was already being parsed
                # here and thrown away, which is the whole of why the
                # `results` table had a live reader in `run status` and no
                # writer anywhere: not a missing mechanism, a dropped value.
                # Only a mapping is carried — `ResultRecord` stores a payload,
                # and a bare list or scalar is a parseable envelope that
                # declares nothing this row can hold.
                if isinstance(declared, dict):
                    payload = declared
            except (OSError, ValueError, UnicodeError):
                pass
            return scheduler.NodeExecution(
                envelope_parsed=parsed,
                exit_code=state.exit_code or 1,
                launched_pid=handle.process_group,
                launch_detail=state.detail,
                envelope_payload=payload,
            )
        if state.state is launcher.PollState.GONE:
            # TRANSPORT rather than STARTUP: the agent launched and then its
            # record vanished, so what was lost is the channel to it, not its
            # start. The budget is identical for all three non-CREDENTIAL
            # members, so the choice between them is diagnostic only — the
            # ledger reads this, never a branch.
            return scheduler.NodeExecution(
                envelope_parsed=False,
                exit_code=1,
                launched_pid=handle.process_group,
                launcher_failure=retry_policy.LauncherFailure.TRANSPORT,
                launch_detail=state.detail,
            )
        if cancel_requested():
            quiesce_attempt(record, "cancel")
            return scheduler.NodeExecution(
                envelope_parsed=False, exit_code=1, launched_pid=handle.process_group
            )
        sleep(0.05)


def _late_agent_execution(
    attempt: worktree.AttemptWorktree,
) -> Optional[scheduler.NodeExecution]:
    """Read a successful declaration from an existing attempt; never launch."""
    envelope = attempt.scratch / "agent-envelope.json"
    if not envelope.is_file():
        return None
    try:
        payload = json.loads(envelope.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    return scheduler.NodeExecution(
        envelope_parsed=True, envelope_payload=payload, exit_code=0
    )


def _running_late_agent_execution(
    args: argparse.Namespace,
    route_runner: Any,
    store: "lc.LifecycleStore",
    node_id: str,
    attempt_no: int,
    *,
    allow_live_builder: bool = False,
) -> Optional[scheduler.NodeExecution]:
    """Validate a dead scheduler's completed RUNNING generation."""
    scratch = Path(args.scratch_root) / worktree.worktree_dirname(
        args.run_id, node_id, attempt_no
    )
    envelope = scratch / "agent-envelope.json"
    if not envelope.is_file():
        return None
    try:
        declared = json.loads(envelope.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(declared, dict) or declared.get("success") is not True:
        return None

    token = "{}-{}-{}".format(args.run_id, node_id, attempt_no)
    probe = getattr(route_runner, "agent_presence", None)
    presence = probe(token) if callable(probe) else None
    if presence is True and not allow_live_builder:
        raise _RunRefused(
            "RESUME_AGENT_STILL_LIVE",
            "{} attempt {} still has live agent {}; resume left the run "
            "unchanged".format(node_id, attempt_no, token),
        )
    if presence is None:
        raise _RunRefused(
            "RESUME_AGENT_LIVENESS_UNKNOWN",
            "{} attempt {} could not be proven absent; resume left the run "
            "unchanged".format(node_id, attempt_no),
        )

    record = store.get_attempt(args.run_id, node_id, attempt_no)
    try:
        reopened = worktree.reopen_attempt_worktree(
            Path(args.repo),
            args.run_id,
            node_id,
            attempt_no,
            record.base_sha,
            Path(args.worktrees_root),
            Path(args.scratch_root),
        )
        identity = worktree.check_at_create(reopened)
        baseline = store.attempt_baseline(args.run_id, node_id, attempt_no)
        ignored = store.attempt_ignored_at_base(args.run_id, node_id, attempt_no)
        execution = _late_agent_execution(reopened)
    except (lc.LifecycleError, worktree.WorktreeError) as exc:
        raise _RunRefused(
            "LATE_ENVELOPE_RECOVERY_INVALID",
            "{} attempt {} cannot recover: {}".format(node_id, attempt_no, exc),
        ) from exc
    if not identity.ok or ignored is None or execution is None:
        detail = (
            "; ".join(identity.detail)
            if not identity.ok
            else "baseline identity or successful envelope is missing"
        )
        raise _RunRefused(
            "LATE_ENVELOPE_RECOVERY_INVALID",
            "{} attempt {} cannot recover: {}".format(node_id, attempt_no, detail),
        )
    if baseline is None:
        raise _RunRefused(
            "LATE_ENVELOPE_RECOVERY_INVALID",
            "{} attempt {} has no recorded baseline".format(node_id, attempt_no),
        )
    return execution


def _resolve_run_runners(
    args: argparse.Namespace, plan: plan_model.Plan
) -> Dict[str, "runner_resolution.ResolvedRunner"]:
    """Resolve every gate runner this run will execute, before it starts.

    Keyed by the runner literal alone rather than by `(runner, cwd)`, and that
    is a fact about where gates run rather than a simplification: the
    projection deliberately drops `Gate.cwd` (`plan_model._GATE_PROJECTION`
    maps it to `None`, because a node's command runs in that attempt's own
    worktree), and `run_integration_gate` is handed the integration checkout.
    Every gate on this path therefore executes at its tree root, so the
    repository root is the directory capability must be established in.

    The probe runs under the same environment builder the gate will run under.
    Proving capability under an environment the gate will not have proves
    nothing, and the two paths could already disagree: `worktree.launch_env`
    sets `PYTEST_ADDOPTS`, and the collector on the validate path did not.
    """
    declared = dict(getattr(args, "runners", {}) or {})
    scratch = Path(args.scratch_root) / "runner-probe"
    env = worktree.launch_env(scratch, concurrency=getattr(args, "concurrency", None))
    wanted = {node.gate.runner for node in plan.agent_nodes}
    wanted.update(node.gate.runner for node in getattr(plan, "tests_nodes", ()) or ())
    wanted.add(plan.merge_policy.integration_gate.runner)
    return {
        runner: runner_resolution.resolve(
            runner, Path(args.repo), ".", declared=declared.get(runner), env=env
        )
        for runner in sorted(wanted)
    }


def _legacy_receipt_findings(
    receipt: finalization.Receipt,
) -> Tuple[Mapping[str, Any], ...]:
    """Render only signed receipt cells for a migrated review ledger."""
    return tuple(
        {
            "check_id": cell.check_id,
            "object_id": cell.object_id,
            "status": cell.status.value,
            "severity": cell.severity.value,
            "message": cell.message,
            "canary": None if cell.canary is None else cell.canary.value,
            "grade": cell.grade,
        }
        for cell in receipt.cells
    )


def _migrate_legacy_inline_reviews(
    args: argparse.Namespace,
    store: lc.LifecycleStore,
    nodes: Sequence[scheduler_types.PlanNode],
) -> Tuple[lc.LegacyReviewMigration, ...]:
    """Import only receipt-proven reviews written before candidate ledgers."""
    receipt_store = finalization.ReceiptStore(
        Path(args.review_receipt_dir),
        repo_paths=(args.repo,),
        data_dir=args.data_dir,
        verify_keys=tuple(bytes.fromhex(key) for key in args.verify_key),
        create=False,
    )
    receipts: List[Tuple[str, finalization.Receipt]] = []
    if receipt_store.root.is_dir():
        for path in receipt_store.root.glob("*.json"):
            digest = path.stem
            if len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                continue
            try:
                receipts.append((digest, receipt_store.load(digest)))
            except Exception:
                # A candidate with no verified match below becomes a typed
                # migration block; unrelated receipt debris does not.
                continue

    def receipt_matches(item: lc.LegacyReviewEvidence) -> bool:
        try:
            receipt = receipt_store.load(item.review_digest)
            expected = code_review.review_digest(
                run_id=args.run_id,
                node_id=item.build_node_id,
                base_sha=item.base_sha,
                output_sha=item.candidate_sha,
                rubric_version=receipt.rubric_version,
            )
            verdict = (
                scheduler_types.ReviewVerdict.PASS
                if receipt.verdict is finalization.Verdict.PASS
                else scheduler_types.ReviewVerdict.REJECTED
            )
            return (
                expected == item.review_digest
                and Path(item.receipt_path).resolve()
                == receipt_store.path_for(item.review_digest).resolve()
                and item.verdict is verdict
                and tuple(item.findings) == _legacy_receipt_findings(receipt)
            )
        except Exception:
            return False

    evidence: List[lc.LegacyReviewEvidence] = []
    reviewable = {
        node.node_id
        for node in nodes
        if node.kind in (scheduler_types.NodeKind.AGENT, scheduler_types.NodeKind.CODE)
    }
    attempts = store.attempts_for(args.run_id)
    for build_node_id in sorted(reviewable):
        node = store.get_node(args.run_id, build_node_id)
        retry_spend_floor = store.retry_spend_floor(args.run_id, build_node_id)
        # Operator retry establishes a durable attempt boundary. Attempts at
        # or below that floor belong to superseded build cycles even after the
        # replacement attempt has left PENDING and is RUNNING/BLOCKED. Legacy
        # inline review evidence must never cross that boundary.
        build_attempts = sorted(
            (
                record
                for record in attempts
                if record.node_id == build_node_id
                and record.attempt_no > retry_spend_floor
            ),
            key=lambda record: record.attempt_no,
        )
        # An explicit operator retry starts a new build cycle. Its historical
        # attempts remain audit evidence, but they are no longer candidate
        # evidence for the current cycle. Re-importing their inline reviews
        # would recreate a migration block after every operator recovery and
        # make the reset impossible to resume.
        if (
            node.state is scheduler_types.NodeState.PENDING
            and node.pending_cause is scheduler_types.PendingCause.OPERATOR_RETRY
            and node.output_sha is None
        ):
            continue
        candidates: List[Tuple[str, str]] = []
        for record in build_attempts:
            candidate_sha = (record.extra or {}).get("review_output_sha")
            if isinstance(candidate_sha, str):
                candidates.append((candidate_sha, record.base_sha))
        if node.output_sha and node.output_sha not in {
            candidate_sha for candidate_sha, _base_sha in candidates
        }:
            final_attempt = next(
                (
                    record
                    for record in build_attempts
                    if record.attempt_no == node.attempt_no
                ),
                None,
            )
            candidates.append(
                (
                    node.output_sha,
                    final_attempt.base_sha if final_attempt is not None else "",
                )
            )
        for sequence, (candidate_sha, base_sha) in enumerate(candidates, start=1):
            matches = [
                (digest, receipt)
                for digest, receipt in receipts
                if code_review.review_digest(
                    run_id=args.run_id,
                    node_id=build_node_id,
                    base_sha=base_sha,
                    output_sha=candidate_sha,
                    rubric_version=receipt.rubric_version,
                )
                == digest
            ]
            if matches:
                for digest, receipt in matches:
                    evidence.append(
                        lc.LegacyReviewEvidence(
                            build_node_id=build_node_id,
                            candidate_seq=sequence,
                            candidate_sha=candidate_sha,
                            base_sha=base_sha,
                            review_digest=digest,
                            receipt_path=str(receipt_store.path_for(digest)),
                            verdict=(
                                scheduler_types.ReviewVerdict.PASS
                                if receipt.verdict is finalization.Verdict.PASS
                                else scheduler_types.ReviewVerdict.REJECTED
                            ),
                            findings=_legacy_receipt_findings(receipt),
                        )
                    )
                continue
            # This placeholder has no authority: the validator rejects it and
            # persistently fences the lane rather than redispatching a SHA that
            # may already have received an inline review.
            digest = code_review.review_digest(
                run_id=args.run_id,
                node_id=build_node_id,
                base_sha=base_sha,
                output_sha=candidate_sha,
                rubric_version=code_review.CODE_RUBRIC.version,
            )
            evidence.append(
                lc.LegacyReviewEvidence(
                    build_node_id=build_node_id,
                    candidate_seq=sequence,
                    candidate_sha=candidate_sha,
                    base_sha=base_sha,
                    review_digest=digest,
                    receipt_path=str(receipt_store.path_for(digest)),
                    verdict=scheduler_types.ReviewVerdict.PASS,
                    findings=(),
                )
            )
    if not evidence:
        return ()

    def is_descendant(parent_sha: str, child_sha: str) -> bool:
        return (
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(args.repo),
                    "merge-base",
                    "--is-ancestor",
                    parent_sha,
                    child_sha,
                ),
                check=False,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )

    return store.migrate_legacy_inline_reviews(
        args.run_id,
        evidence,
        evidence_validator=receipt_matches,
        ancestry_validator=is_descendant,
    )


def _keeps_builder_session_open(
    node_id: str, phase: str, builder_node_ids: frozenset[str]
) -> bool:
    """Only build/review turns retain an interactive actor after completion."""
    return phase in ("candidate-idle", "repair-idle") and node_id in builder_node_ids


def _lane_placement_by_node(plan: Any) -> Dict[str, str]:
    """Map each interactive authored node to the lane tab that owns it.

    A tests node is authored independently but visually belongs beside the
    single build lane that directly depends on it. Shared or unconsumed tests
    keep their own tab: one pane cannot truthfully appear in two lane tabs,
    and guessing an owner would hide shared work under an arbitrary build.
    """
    tests = {node.node_id: node for node in (getattr(plan, "tests_nodes", ()) or ())}
    owners: Dict[str, List[str]] = {node_id: [] for node_id in tests}
    placement = {
        node.node_id: node.node_id for node in (getattr(plan, "agent_nodes", ()) or ())
    }
    for build in getattr(plan, "agent_nodes", ()) or ():
        for dependency in getattr(build, "needs", ()) or ():
            if dependency in owners:
                owners[dependency].append(build.node_id)
    for test_node_id, build_node_ids in owners.items():
        placement[test_node_id] = (
            build_node_ids[0] if len(build_node_ids) == 1 else test_node_id
        )
    return placement


class _RunProgress:
    """Rich stderr narrative for a foreground scheduler invocation.

    Stdout remains the command's single JSON result.  The observer owns a
    read-only SQLite connection, so reporting can neither share the scheduler's
    connection across threads nor delay lifecycle writes.
    """

    HEARTBEAT_SECONDS = 10.0
    POLL_SECONDS = 0.25

    def __init__(
        self, db_path: Path, run_id: str, *, resuming: bool, announce: bool = True
    ):
        import threading

        self.db_path = Path(db_path)
        self.run_id = run_id
        self.action = "resume" if resuming else "start"
        self.console = RichConsole(file=sys.stderr, highlight=False, soft_wrap=True)
        self._stop = threading.Event()
        self.announce = announce
        self._thread = threading.Thread(
            target=self._observe, name="maestro-run-progress", daemon=True
        )
        self._cursor = 0
        self._last_change = time.monotonic()

    @staticmethod
    def _short(value: Any, limit: int = 72) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _max_transition_id(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM transitions WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _state_style(state: str) -> str:
        if state in {"MERGED", "COMPLETED", "ACCEPTED", "PASS"}:
            return "bold green"
        if state in {"BLOCKED", "FAILED", "REJECTED", "CANCELLED"}:
            return "bold red"
        if state in {"PENDING", "PUBLISHED", "DISPATCHED", "WAITING"}:
            return "bold yellow"
        if state in {"RUNNING", "REVIEWING"}:
            return "bold cyan"
        return "bold white"

    def _frontier_rows(
        self,
        conn: sqlite3.Connection,
    ) -> List[Tuple[str, str, str, str, str]]:
        rows: List[Tuple[str, str, str, str, str]] = []
        for row in conn.execute(
            "SELECT node_id,state,attempt_no,lane_phase,block_reason "
            "FROM node_lifecycle WHERE run_id=? "
            "AND state IN ('RUNNING','BLOCKED') ORDER BY node_id",
            (self.run_id,),
        ):
            node_id = str(row["node_id"])
            role = "tester" if node_id.endswith("-tests") else "builder"
            detail = (
                row["block_reason"]
                if str(row["state"]) == "BLOCKED"
                else row["lane_phase"]
            )
            rows.append(
                (
                    node_id,
                    role,
                    "a{}".format(row["attempt_no"]),
                    str(row["state"]),
                    self._short(detail or ""),
                )
            )
        for row in conn.execute(
            "SELECT review_node_id,candidate_sha,state,reviewer_generation "
            "FROM candidate_reviews WHERE run_id=? "
            "AND state IN ('PUBLISHED','DISPATCHED') ORDER BY review_node_id",
            (self.run_id,),
        ):
            review_node_id = str(row["review_node_id"])
            state = str(row["state"])
            candidate = self._short(row["candidate_sha"], limit=12)
            evidence = (
                "candidate {} · dispatch pending".format(candidate)
                if state == "PUBLISHED"
                else "candidate {} · signed verdict pending".format(candidate)
            )
            rows.append(
                (
                    review_node_id.removesuffix("::review"),
                    "reviewer",
                    "r{}".format(row["reviewer_generation"]),
                    state,
                    evidence,
                )
            )
        return rows

    def _frontier(self, conn: sqlite3.Connection) -> str:
        parts = []
        for node, _role, attempt, state, detail in self._frontier_rows(conn):
            suffix = " · {}".format(detail) if detail else ""
            parts.append("{} {} {}{}".format(node, state, attempt, suffix))
        return "; ".join(parts) if parts else "scheduler evaluating frontier"

    def _print_frontier(self, conn: sqlite3.Connection, heading: str) -> None:
        rows = self._frontier_rows(conn)
        if not rows:
            body: Any = RichText(
                "No active lane transitions; evaluating the DAG frontier.", style="dim"
            )
        else:
            table = RichTable(
                box=None,
                expand=True,
                padding=(0, 1),
                header_style="bold bright_cyan",
                show_edge=False,
            )
            table.add_column("LANE", style="bold white", no_wrap=True)
            table.add_column("ACTOR", style="magenta", no_wrap=True)
            table.add_column("TRY", style="bright_black", no_wrap=True)
            table.add_column("STATE", no_wrap=True)
            table.add_column("PHASE / EVIDENCE", style="white")
            for node, role, attempt, state, detail in rows:
                table.add_row(
                    node,
                    role,
                    attempt,
                    RichText(state, style=self._state_style(state)),
                    detail or "—",
                )
            body = table
        self.console.print(
            RichPanel(
                body,
                title=RichText(heading.upper(), style="bold bright_cyan"),
                border_style="cyan",
                box=rich_box.ROUNDED,
                padding=(0, 1),
            )
        )

    def _emit_transition(self, row: sqlite3.Row) -> None:
        node = str(row["node_id"] or "run")
        try:
            payload = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        reason = str(row["reason"])
        if reason == "lane-phase" and payload.get("from") and payload.get("to"):
            line = RichText()
            line.append("● ", style="cyan")
            line.append(node, style="bold white")
            line.append("  PHASE  ", style="bold cyan")
            line.append(
                "{} → {}".format(
                    self._short(payload["from"]), self._short(payload["to"])
                ),
                style="cyan",
            )
            self.console.print(line)
            return
        destination = str(row["to_state"] or "EVENT")
        phase = payload.get("phase") or payload.get("lane_phase")
        line = RichText()
        line.append("● ", style=self._state_style(destination))
        line.append(node, style="bold white")
        line.append("  ")
        line.append(destination, style=self._state_style(destination))
        line.append("  ")
        line.append(self._short(reason), style="bold magenta")
        if phase:
            line.append("  ·  {}".format(self._short(phase)), style="cyan")
        self.console.print(line)

    def _observe(self) -> None:
        try:
            conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            self.console.print(
                "[yellow]progress observer unavailable:[/yellow] {}".format(
                    rich_escape(self._short(exc))
                )
            )
            return
        try:
            while not self._stop.wait(self.POLL_SECONDS):
                try:
                    rows = conn.execute(
                        "SELECT id,node_id,to_state,reason,detail_json "
                        "FROM transitions WHERE run_id=? AND id>? ORDER BY id",
                        (self.run_id, self._cursor),
                    ).fetchall()
                    for row in rows:
                        self._cursor = int(row["id"])
                        self._last_change = time.monotonic()
                        self._emit_transition(row)
                    if time.monotonic() - self._last_change >= self.HEARTBEAT_SECONDS:
                        self._print_frontier(
                            conn,
                            "waiting · no transition for {}s".format(
                                int(self.HEARTBEAT_SECONDS)
                            ),
                        )
                        self._last_change = time.monotonic()
                except sqlite3.Error as exc:
                    self.console.print(
                        "[yellow]progress read delayed:[/yellow] {}".format(
                            rich_escape(self._short(exc))
                        )
                    )
                    self._last_change = time.monotonic()
        finally:
            conn.close()

    def __enter__(self) -> "_RunProgress":
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0
            )
            conn.row_factory = sqlite3.Row
            self._cursor = self._max_transition_id(conn)
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            conn = None
        if self.announce:
            self.console.print(
                RichPanel(
                    RichText(self.run_id, style="bold white"),
                    title=RichText(
                        "MAESTRO {}".format(self.action.upper()),
                        style="bold bright_cyan",
                    ),
                    border_style="bright_cyan",
                    box=rich_box.HEAVY,
                    padding=(0, 1),
                )
            )
        if conn is not None:
            self._print_frontier(conn, "current frontier")
            conn.close()
        else:
            self.console.print(
                "[bold yellow]◌ scheduler state is initializing[/bold yellow]"
            )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if exc is None:
            self.console.print("[bold green]✓ Maestro command finished[/bold green]")
        else:
            self.console.print(
                "[bold red]✗ scheduler stopped[/bold red]  {}".format(
                    rich_escape(self._short(exc))
                )
            )


def _execute_run(args: argparse.Namespace, *, resuming: bool) -> int:
    worktree.bind_lane_concurrency(getattr(args, "concurrency", None))
    action = "resume" if resuming else "start"
    run_console = RichConsole(file=sys.stderr, highlight=False, soft_wrap=True)
    run_console.print(
        "[bold cyan]Maestro {}[/bold cyan] [bold]{}[/bold]\n"
        "[dim]preflight · loading and validating plan[/dim]".format(
            action, rich_escape(args.run_id)
        )
    )
    try:
        from threading import RLock

        config = _run_configuration(args)
        plan = _load_runnable_plan(args)
        if not resuming:
            # Only on start. A run already in flight is pinned to the contract
            # it was created under, and refusing its resume here would break
            # the half of the rollout invariant that protects it.
            _refuse_uncontracted_tests_nodes(args, plan)
        run_console.print("[green]✓[/green] plan and configuration loaded")

        # Before the ledger is opened for writing and before any worktree exists,
        # because a run refused for a spent cross-run review budget must leave
        # nothing behind (§3.6 B10's escape is the only way past it).
        _refuse_cross_run_node_budget(args, plan)
        _validate_run_paths(args, plan)
        run_console.print("[green]✓[/green] paths and cross-run budgets validated")
        run_console.print("[dim]preflight · resolving declared runners[/dim]")
        # A run precondition, in the same family as the integration branch already
        # being checked out: decided before the scheduler exists, so an unusable
        # runner launches no pane, writes no attempt row, and reaches no retry
        # classifier. Discovering it at attempt time instead is what made a
        # permanently wrong interpreter look like a transient environmental fault
        # and burn a node's whole environmental budget on identical re-runs.
        try:
            resolved_runners = _resolve_run_runners(args, plan)
        except runner_resolution.RunnerUnusable as exc:
            return _typed_refusal(exc.payload(), exc.detail)
        run_console.print("[green]✓[/green] runners resolved")
        builder_handles: Dict[str, launcher.LaunchHandle] = {}
        runner_resolution.write_record(
            Path(args.integration_path).parent / "runner-resolution.json",
            resolved_runners.values(),
        )
        notice = runner_resolution.adoption_notice(resolved_runners.values())
        if notice:
            # stderr, because this verb's stdout is one JSON document and an
            # operator hint is not part of it.
            print(notice, file=sys.stderr)
        tests_nodes = getattr(plan, "tests_nodes", ()) or ()
        workspace_label = _run_workspace_label(args, plan, resuming=resuming)
        if plan.agent_nodes or tests_nodes:
            run_console.print(
                "[dim]preflight · connecting to the persisted Herdr workspace[/dim]"
            )
        route_runner = (
            _runtime_launcher(args, workspace_label)
            if plan.agent_nodes or tests_nodes
            else None
        )
        run_console.print("[green]✓[/green] runtime launcher ready")
        interactive_node_ids = {
            node.node_id for node in (*plan.agent_nodes, *tests_nodes)
        }
        lane_placement = _lane_placement_by_node(plan)
        builder_node_ids = frozenset(node.node_id for node in plan.agent_nodes)
        store = lc.LifecycleStore(args.db)
        if resuming and route_runner is not None:
            _restore_run_placement(route_runner, store, args.run_id, builder_node_ids)
        run_console.print(
            "[dim]preflight · reconciling durable attempts and actor sessions[/dim]"
        )
        progress = _RunProgress(
            Path(args.db), args.run_id, resuming=resuming, announce=False
        )
        progress.__enter__()
        handles = {}
        proven_absent = set()
        #: `(node_id, attempt_no)` for every attempt this process has leased
        #: and for which it has **not yet entered** the one call that can
        #: create a pane or a process. The front-side twin of `proven_absent`,
        #: and the same kind of fact: positive knowledge recorded by the frame
        #: that holds it, never a default inferred from a missing handle.
        #:
        #: `handles` is populated only after a launch has succeeded, so every
        #: failure raised inside `run_node` before that call arrives at
        #: `quiesce_attempt` with no handle. Without this set the quiescer
        #: could only answer `PROCESS_GROUP_UNTRACKED` -- "absence unproven" --
        #: about a process group that was never created, and the scheduler
        #: turns that answer into a terminal `QUIESCENCE_UNPROVEN` that buries
        #: the failure which actually happened. That is not hypothetical: it
        #: blocked `lane-routing-chemical-tests` in
        #: run-8d1a71f463e4430f92a125a8f8b3731d over a B13 refusal
        #: (`HandoffTooLarge`, an unresolvable lane model) raised two
        #: statements before the launch.
        #:
        #: `scheduler._launch_left_nothing_to_reap` covers one exception type
        #: -- a typed `LaunchFailed` that reports `pane_created` false -- and
        #: deliberately no others (§16.3 item 45). This covers the other half
        #: of the same shape structurally: every pre-dispatch exit, of every
        #: exception type, for every node kind. A key is discarded at the
        #: launch call and never after it, so a launch that creates something
        #: and then fails is outside the exemption and still owes the proof.
        undispatched = set()
        handles_lock = RLock()
        # The integration checkout this invocation added, and nothing else. The
        # refusal below returns while another worktree still holds the branch, and
        # a release that did not distinguish the two would delete that worktree --
        # which may be the operator's own. `None` until `worktree add` succeeds.
        created_integration_path: Optional[Path] = None
        try:
            late_envelope_recovery = []
            proven_late_envelope_recovery = []
            undispatched_resume = []
            if resuming:
                # A completed interactive declaration belongs to its original
                # generation. Validate every durable input, then either prove
                # the actor absent or reclaim that exact retained actor. A live
                # completed builder must advance to review without relaunching
                # the build; an unknown actor identity still leaves the run
                # unchanged.
                durable_undispatched = set(
                    store.undispatched_quiescence_attempts(args.run_id)
                )
                for node_id, attempt_no in store.quiescence_blocked_attempts(
                    args.run_id
                ):
                    if (node_id, attempt_no) in durable_undispatched:
                        # Asked before any liveness probe, because a probe over
                        # a correlation token no launch ever created answers
                        # about nothing -- which is the same mistake, one layer
                        # up, as the quiescer that wrote this block.
                        residue = _undispatched_attempt_residue(
                            args, store, node_id, attempt_no
                        )
                        if residue:
                            run_console.print(
                                "[yellow]{0} attempt {1} stays blocked: {2}[/yellow]".format(
                                    node_id, attempt_no, "; ".join(residue)
                                )
                            )
                            continue
                        undispatched_resume.append((node_id, attempt_no))
                        continue
                    if node_id not in interactive_node_ids:
                        continue
                    token = "{}-{}-{}".format(args.run_id, node_id, attempt_no)
                    probe = getattr(route_runner, "agent_presence", None)
                    presence = probe(token) if callable(probe) else None
                    if presence is None:
                        return _refusal(
                            "RESUME_AGENT_LIVENESS_UNKNOWN",
                            "{} attempt {} could not be proven absent; resume "
                            "left the run unchanged".format(node_id, attempt_no),
                        )
                    scratch = Path(args.scratch_root) / worktree.worktree_dirname(
                        args.run_id, node_id, attempt_no
                    )
                    envelope = scratch / "agent-envelope.json"
                    if not envelope.is_file():
                        continue
                    record = store.get_attempt(args.run_id, node_id, attempt_no)
                    try:
                        reopened = worktree.reopen_attempt_worktree(
                            Path(args.repo),
                            args.run_id,
                            node_id,
                            attempt_no,
                            record.base_sha,
                            Path(args.worktrees_root),
                            Path(args.scratch_root),
                        )
                        identity = worktree.check_at_create(reopened)
                        baseline = store.attempt_baseline(
                            args.run_id, node_id, attempt_no
                        )
                        ignored = store.attempt_ignored_at_base(
                            args.run_id, node_id, attempt_no
                        )
                        execution = _late_agent_execution(reopened)
                    except (lc.LifecycleError, worktree.WorktreeError) as exc:
                        return _refusal(
                            "LATE_ENVELOPE_RECOVERY_INVALID",
                            "{} attempt {} cannot recover: {}".format(
                                node_id, attempt_no, exc
                            ),
                        )
                    if not identity.ok or ignored is None or execution is None:
                        detail = (
                            "; ".join(identity.detail)
                            if not identity.ok
                            else "baseline identity or successful envelope is missing"
                        )
                        return _refusal(
                            "LATE_ENVELOPE_RECOVERY_INVALID",
                            "{} attempt {} cannot recover: {}".format(
                                node_id, attempt_no, detail
                            ),
                        )
                    # Force the durable baseline decoder before mutation. The
                    # scheduler re-reads it after claiming the same attempt.
                    if baseline is None:
                        return _refusal(
                            "LATE_ENVELOPE_RECOVERY_INVALID",
                            "{} attempt {} has no recorded baseline".format(
                                node_id, attempt_no
                            ),
                        )
                    if presence is True:
                        session = store.current_actor_session(
                            args.run_id, node_id, "builder"
                        )
                        if (
                            session is None
                            or session.state
                            is not scheduler_types.ActorSessionState.ACTIVE
                            or session.generation != attempt_no
                        ):
                            return _refusal(
                                "LIVE_BUILDER_IDENTITY_INVALID",
                                "{} attempt {} has no matching active builder "
                                "generation".format(node_id, attempt_no),
                            )
                        try:
                            handle = route_runner.adopt(
                                launcher.PersistedActorHandle(
                                    correlation_token=(session.correlation_token),
                                    pane_id=session.pane_id,
                                    agent_name=launcher.agent_name_for(
                                        session.correlation_token
                                    ),
                                    launched_cwd=reopened.path,
                                    transcript_path=Path(session.session_path),
                                    envelope_path=envelope,
                                    environment=worktree.launch_env(
                                        reopened.scratch,
                                        concurrency=getattr(args, "concurrency", None),
                                    ),
                                    workspace_id=launcher.workspace_of(
                                        _session_tab_id(session)
                                    ),
                                    tab_id=_session_tab_id(session),
                                    lane_key=node_id,
                                )
                            )
                            route_runner.wait_for_idle(handle)
                        except RuntimeError as exc:
                            return _refusal(
                                "LIVE_BUILDER_ADOPTION_INVALID",
                                "{} attempt {} cannot reclaim completed "
                                "builder: {}".format(node_id, attempt_no, exc),
                            )
                        key = (node_id, attempt_no)
                        with handles_lock:
                            handles[key] = handle
                            builder_handles[node_id] = handle
                            proven_absent.discard(key)
                    else:
                        proven_absent.add((node_id, attempt_no))
                    late_envelope_recovery.append((node_id, attempt_no))
                # A scheduler can die after a fast agent has declared success
                # but before launch returns a handle, or after preserving that
                # candidate while exhausting a reviewer infrastructure budget.
                # Both states need the same evidence gate before `resume_run`;
                # otherwise the accepted declaration is discarded and the
                # original builder assignment is relaunched from its base.
                recoverable_attempts = set(store.running_attempts(args.run_id))
                recoverable_attempts.update(
                    store.retry_budget_blocked_attempts(args.run_id)
                )
                for node_id, attempt_no in sorted(recoverable_attempts):
                    if node_id not in interactive_node_ids:
                        continue
                    try:
                        execution = _running_late_agent_execution(
                            args,
                            route_runner,
                            store,
                            node_id,
                            attempt_no,
                            allow_live_builder=node_id in builder_node_ids,
                        )
                    except _RunRefused as refused:
                        return refused.emit()
                    if execution is not None:
                        proven_late_envelope_recovery.append((node_id, attempt_no))
                        token = "{}-{}-{}".format(args.run_id, node_id, attempt_no)
                        presence = route_runner.agent_presence(token)
                        key = (node_id, attempt_no)
                        if presence is False:
                            with handles_lock:
                                proven_absent.add(key)
                        if node_id in builder_node_ids and presence is True:
                            session = store.current_actor_session(
                                args.run_id, node_id, "builder"
                            )
                            if (
                                session is None
                                or session.state
                                is not scheduler_types.ActorSessionState.ACTIVE
                                or session.generation != attempt_no
                            ):
                                return _refusal(
                                    "LIVE_BUILDER_BINDING_MISSING",
                                    "{} attempt {} has a live completed "
                                    "agent but no matching durable builder "
                                    "binding".format(node_id, attempt_no),
                                )
                            record = store.get_attempt(args.run_id, node_id, attempt_no)
                            reopened = worktree.reopen_attempt_worktree(
                                Path(args.repo),
                                args.run_id,
                                node_id,
                                attempt_no,
                                record.base_sha,
                                Path(args.worktrees_root),
                                Path(args.scratch_root),
                            )
                            try:
                                handle = route_runner.adopt(
                                    launcher.PersistedActorHandle(
                                        correlation_token=(session.correlation_token),
                                        pane_id=session.pane_id,
                                        agent_name=launcher.agent_name_for(
                                            session.correlation_token
                                        ),
                                        launched_cwd=reopened.path,
                                        transcript_path=Path(session.session_path),
                                        envelope_path=(
                                            reopened.scratch / "agent-envelope.json"
                                        ),
                                        environment=worktree.launch_env(
                                            reopened.scratch,
                                            concurrency=getattr(
                                                args, "concurrency", None
                                            ),
                                        ),
                                        workspace_id=launcher.workspace_of(
                                            _session_tab_id(session)
                                        ),
                                        tab_id=_session_tab_id(session),
                                        lane_key=node_id,
                                    )
                                )
                                route_runner.wait_for_idle(handle)
                            except (launcher.HandleAbsent, RuntimeError) as exc:
                                return _refusal(
                                    "LIVE_BUILDER_ADOPTION_INVALID",
                                    "{} attempt {} cannot reclaim completed "
                                    "builder: {}".format(node_id, attempt_no, exc),
                                )
                            with handles_lock:
                                handles[key] = handle
                                builder_handles[node_id] = handle
                                proven_absent.discard(key)
                store.resume_run(
                    args.run_id,
                    late_envelope_attempts=proven_late_envelope_recovery,
                    undispatched_attempts=undispatched_resume,
                )
                for node_id, attempt_no in late_envelope_recovery:
                    store.prepare_late_envelope_recovery(
                        args.run_id, node_id, attempt_no
                    )
            else:
                refused = _refuse_base_commit_divergence(args, plan)
                if refused is not None:
                    return refused
                refused = _refuse_uncommittable_outputs(args, plan)
                if refused is not None:
                    return refused
            # Not `elif`: a resumed run whose predecessor released the checkout has
            # to take the branch back, because every attempt is based on its head.
            if not Path(args.integration_path).exists():
                branch = plan.merge_policy.integration_branch
                # A previous run's integration checkout is this system's own litter,
                # and telling an operator to go clean it up by hand is a refusal
                # over a state Maestro created and can prove it created. Reclaim it
                # under exactly the boundary the release verb already applies -- and
                # under no other -- so an operator's checkout of the same branch
                # still reaches the refusal below untouched.
                try:
                    _reclaim_stranded_integration_worktree(
                        Path(args.repo),
                        _configured_runs_root(args),
                        branch,
                        getattr(args, "db", None),
                    )
                except _RunStateStillHeld as held:
                    # The same defect at the same seam: whose was checked here
                    # from the day this reclaim was written, whether-in-use never
                    # was. A run this one is about to start has no more right to
                    # take another run's merges than the release verb does, so it
                    # gets the same predicate and a refusal that names the run
                    # holding the branch rather than the generic one below, which
                    # would say "not among them" about a worktree that is.
                    return _refusal("INTEGRATION_WORKTREE_RUN_NOT_OVER", str(held))
                occupant = _worktree_holding_branch(Path(args.repo), branch)
                if occupant is not None:
                    return _refusal(
                        "INTEGRATION_BRANCH_CHECKED_OUT",
                        "the integration branch "
                        + branch
                        + " is checked out at "
                        + str(occupant)
                        + ", and git gives a branch to one worktree "
                        "at a time. The run's integration worktree must hold it, "
                        "because every attempt is based on that branch's head and a "
                        "detached copy would never advance it. Maestro reclaims the "
                        "stranded integration checkouts inside its own run root "
                        "without asking, and this one is not among them, so it has "
                        "been left exactly as it is. Move that checkout to another "
                        "branch and start the run again",
                    )
                subprocess.run(
                    (
                        "git",
                        "-C",
                        str(args.repo),
                        "worktree",
                        "add",
                        str(args.integration_path),
                        branch,
                    ),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                created_integration_path = Path(args.integration_path)

            def run_node(
                attempt, node, record, retry_prompt, on_launch, cancel_requested
            ):
                key = (node.node_id, record.attempt_no)
                with handles_lock:
                    # Before the first statement that can raise, so no
                    # pre-dispatch failure can outrun the fact that this frame
                    # has created nothing yet.
                    undispatched.add(key)
                launch_environment = worktree.launch_env(
                    attempt.scratch, concurrency=getattr(args, "concurrency", None)
                )
                if node.kind is scheduler_types.NodeKind.CODE:
                    with handles_lock:
                        # The chokepoint for a code node. Cleared before the
                        # call, never after: a `Popen` that starts a process
                        # and then raises has left a group to reap.
                        undispatched.discard(key)
                    process = subprocess.Popen(
                        node.command,
                        cwd=attempt.path,
                        env=launch_environment,
                        start_new_session=True,
                    )
                    with handles_lock:
                        handles[key] = process
                        proven_absent.discard(key)
                    on_launch(process.pid)
                    while process.poll() is None:
                        if cancel_requested():
                            quiesce_attempt(record, "cancel")
                            process.wait(timeout=1)
                            return scheduler.NodeExecution(
                                exit_code=process.returncode or 1
                            )
                        time.sleep(0.05)
                    return scheduler.NodeExecution(exit_code=process.returncode or 0)
                assert route_runner is not None
                prompt = attempt.scratch / "agent-prompt.txt"
                envelope = attempt.scratch / "agent-envelope.json"
                plan_node = plan.node_by_id()[node.node_id]
                # One resolution for both the first attempt and the repair,
                # so the two cannot disagree about which actor a lane opens.
                dispatch = _lane_actor_dispatch(args, node.kind)
                if node.kind is scheduler_types.NodeKind.TESTS:
                    prompt_text = _tests_node_prompt(plan_node, envelope, retry_prompt)
                else:
                    prompt_text = _agent_node_prompt(plan_node, envelope, retry_prompt)
                    prompt_text = _append_needed_tests(
                        prompt_text, attempt.path, node, plan
                    )
                lane_route = dispatch.route
                lane_model = dispatch.model
                lane_effort = dispatch.effort
                lane_profile = dispatch.profile
                lane_vendor = dispatch.vendor
                # B13: the build handoff now carries the tests as well as the
                # goal, so it is strictly larger. Same chokepoint as before.
                _preflight_prompt(prompt_text, lane_route, lane_model)
                prompt.write_text(prompt_text, encoding="utf-8")
                # When the launcher first held this attempt's durable identity,
                # so the `mark_launched` after `launch` returns re-asserts that
                # instant instead of overwriting it with the later one. Same
                # attempt, one launch, one launched_at.
                identity_at: List[float] = []

                def record_launch_identity(handle: Any) -> None:
                    """Write pane identity the moment the launcher holds it.

                    Runs inside `launch`, before the prompt-submission proof.
                    Until this existed the row stayed blank for the 30s-2min a
                    real pane was already open, so §7.6's process-alive and
                    turn-count signals were disarmed over exactly the window
                    where a launch goes wrong, and a refusal in the submission
                    path orphaned a pane no durable record named.

                    `pid` is `None` on purpose: the pane's foreground group is
                    only meaningful after submission, and `attempt_liveness`
                    reads a missing pid as *unknown* rather than as dead
                    (§1.2), so arming early cannot manufacture a PROCESS_DEAD.
                    The call after `launch` returns fills the pid in.
                    """
                    identity_at.append(time.time())
                    store.mark_launched(
                        args.run_id,
                        node.node_id,
                        record.attempt_no,
                        None,
                        launched_at=identity_at[0],
                        extra=_launch_attempt_extra(
                            str(handle.transcript_path),
                            vendor=lane_vendor,
                            model=lane_model,
                            route=lane_route,
                        ),
                    )

                # Bound to a name rather than passed inline so the chokepoint
                # is one statement. `_route_context_window` resolves a lane
                # model through its route's catalog and raises when it does not
                # resolve, and as an inline argument that raise happened
                # *between* the mark being cleared and the launch being
                # entered -- inside the window the mark exists to describe, and
                # therefore invisible to it.
                spec = launcher.LaunchSpec(
                    correlation_token="{}-{}-{}".format(
                        args.run_id, node.node_id, record.attempt_no
                    ),
                    worktree=attempt.path,
                    prompt_path=prompt,
                    envelope_path=envelope,
                    route=lane_route,
                    model=lane_model,
                    effort=lane_effort,
                    profile=lane_profile,
                    session_dir=attempt.scratch / "session",
                    context_window_tokens=_route_context_window(
                        lane_route, lane_model
                    ),
                    workspace_label=workspace_label,
                    lane_key=lane_placement[node.node_id],
                    lane_label=lane_placement[node.node_id],
                    pane_role=(
                        "tester"
                        if node.kind is scheduler_types.NodeKind.TESTS
                        else "builder"
                    ),
                    attempt_no=record.attempt_no,
                    pane_group_size=3,
                    restrict_tools=getattr(args, "restrict_actor_tools", False),
                    environment=launch_environment,
                    on_identity=record_launch_identity,
                )
                with handles_lock:
                    # The one call that can open a pane for this attempt.
                    undispatched.discard(key)
                handle = _typed_launch_pane(route_runner, spec)
                with handles_lock:
                    handles[key] = handle
                    proven_absent.discard(key)
                _require_session_path(handle, node.node_id, record.attempt_no)
                if node.kind is scheduler_types.NodeKind.AGENT:
                    try:
                        store.register_actor_session(
                            args.run_id,
                            node.node_id,
                            "builder",
                            generation=record.attempt_no,
                            pane_id=handle.pane_id,
                            session_path=str(handle.transcript_path),
                            correlation_token=handle.correlation_token,
                            tab_id=handle.tab_id,
                        )
                    except BaseException:
                        with handles_lock:
                            handles.pop(key, None)
                        route_runner.cancel(
                            handle, finalization_window.time.monotonic() + 5.0
                        )
                        raise
                    with handles_lock:
                        builder_handles[node.node_id] = handle
                liveness_pid = handle.process_group
                if liveness_pid is None:
                    liveness_pid = handle.liveness_pid
                store.mark_launched(
                    args.run_id,
                    node.node_id,
                    record.attempt_no,
                    liveness_pid,
                    # Re-asserted, not recomputed: `record_launch_identity`
                    # already wrote this row when the pane became real, and
                    # `launched_at` names that instant rather than whenever
                    # the submission path happened to finish.
                    launched_at=identity_at[0] if identity_at else None,
                    extra=_launch_attempt_extra(
                        str(handle.transcript_path),
                        vendor=lane_vendor,
                        model=lane_model,
                        route=lane_route,
                    ),
                )
                on_launch(liveness_pid, identity_at[0] if identity_at else None)
                return _poll_agent_execution(
                    route_runner,
                    handle,
                    envelope,
                    record,
                    cancel_requested,
                    quiesce_attempt,
                )

            def quiesce_attempt(record, phase):
                key = (record.node_id, record.attempt_no)
                with handles_lock:
                    if key in proven_absent:
                        return
                    handle = handles.get(key)
                    if handle is None:
                        if phase == "pre-baseline":
                            return
                        if key in undispatched:
                            # Absent by construction, for any phase: this
                            # process leased the attempt and has not entered
                            # the call that could have created anything for
                            # it. Answering `PROCESS_GROUP_UNTRACKED` here
                            # reports "absence unproven" about a group that
                            # provably does not exist, and the scheduler
                            # blocks the node terminally on that answer --
                            # destroying the pre-dispatch failure that caused
                            # the quiesce in the first place.
                            return
                        raise RuntimeError(
                            "PROCESS_GROUP_UNTRACKED:{}:{}#{}".format(
                                phase, record.node_id, record.attempt_no
                            )
                        )
                    if _keeps_builder_session_open(
                        record.node_id, phase, builder_node_ids
                    ):
                        if (
                            not isinstance(handle, launcher.LaunchHandle)
                            or route_runner is None
                        ):
                            raise RuntimeError(
                                "ACTOR_IDLE_UNPROVEN:{}:{}#{}".format(
                                    phase, record.node_id, record.attempt_no
                                )
                            )
                        try:
                            route_runner.wait_for_idle(handle)
                        except RuntimeError as exc:
                            raise RuntimeError(
                                "ACTOR_IDLE_UNPROVEN:{}:{}".format(
                                    phase, handle.correlation_token
                                )
                            ) from exc
                        return
                    if isinstance(handle, subprocess.Popen):
                        process_group = handle.pid
                        launcher.quiesce_process_group(
                            process_group, time.monotonic() + 1.0
                        )
                        if not launcher._process_group_absent(process_group):
                            raise RuntimeError(
                                "PROCESS_GROUP_STILL_OWNED:{}:{}".format(
                                    phase, process_group
                                )
                            )
                    else:
                        if route_runner is None:
                            raise RuntimeError(
                                "PROCESS_GROUP_UNTRACKED:{}:{}#{}".format(
                                    phase, record.node_id, record.attempt_no
                                )
                            )
                        route_runner.cancel(handle, time.monotonic() + 1.0)
                        if route_runner.reclaim(handle.correlation_token):
                            raise RuntimeError(
                                "PROCESS_GROUP_STILL_OWNED:{}:{}".format(
                                    phase, handle.correlation_token
                                )
                            )
                        if builder_handles.get(record.node_id) is handle:
                            session = store.current_actor_session(
                                args.run_id, record.node_id, "builder"
                            )
                            if (
                                session is not None
                                and session.state
                                is scheduler_types.ActorSessionState.ACTIVE
                            ):
                                store.close_actor_session(
                                    args.run_id,
                                    record.node_id,
                                    "builder",
                                    generation=session.generation,
                                )
                            builder_handles.pop(record.node_id, None)
                    handles.pop(key)
                    proven_absent.add(key)

            def mark_repair_launched(node, record, handle, dispatch):
                """Re-point the attempt row at the pane that will repair it.

                `attempts.extra` is written once per attempt, at first
                launch, and a repair opens a *new* pane inside that same
                attempt. Without this the row keeps naming the rejected
                actor's transcript, pid, and start epoch for the rest of the
                attempt: §7.6's transcript signal reads the wrong pane and
                `attempt_liveness` measures a process that was cancelled,
                over exactly the round -- rejected, repaired, merged -- that
                anyone goes back to read. It is silent, because the stale
                path still resolves and a transcript is still there.

                The same `store.mark_launched` the first attempt calls, with
                the replacement's identity in place of the original's.
                `launched_at` is deliberately left to stamp now: it is what
                arms those two signals, and the instant they became
                measurable again is this launch, not the rejected one.
                """
                liveness_pid = handle.process_group
                if liveness_pid is None:
                    liveness_pid = handle.liveness_pid
                store.mark_launched(
                    args.run_id,
                    node.node_id,
                    record.attempt_no,
                    liveness_pid,
                    extra=_launch_attempt_extra(
                        str(handle.transcript_path),
                        vendor=dispatch.vendor,
                        model=dispatch.model,
                        route=dispatch.route,
                    ),
                )

            def continue_tests_node(
                attempt,
                node,
                record,
                repair_prompt,
                rejected_candidate_sha,
                builder_generation,
                cancel_requested,
            ):
                """Open a fresh tester on one tests-node repair handoff.

                A tests node's tester is one-shot by construction: nothing
                registers a durable actor session for it and its pane is
                cancelled when the attempt settles, so `continue_node`'s
                adopt/resubmit/replace machinery has no session to act on and
                its two guards both refuse -- which is how a REJECTED tests
                node reached a `REPAIRING` phase that no code path could
                service, and then stopped existing
                (run-36dd33d262d9485ca815aea5001b2ce2, `lane-wp6-tests`).

                The repair is therefore a new tester turn against the *same*
                attempt worktree, carrying the reviewer's findings, placed in
                the lane's existing Herdr tab by `lane_key`. Making testers
                durable instead would be the larger change and is not this
                one. §19 M41 requires a tests node to carry the full review
                **and repair** apparatus; only its review half had shipped.
                """
                key = (node.node_id, record.attempt_no)
                envelope_path = attempt.scratch / "agent-envelope.json"
                acknowledgement_path = attempt.scratch / "repair-acknowledgement.json"
                prompt_path = attempt.scratch / "repair-prompt.md"
                launch_environment = worktree.launch_env(
                    attempt.scratch, concurrency=getattr(args, "concurrency", None)
                )
                handoff = store.repair_handoff(
                    args.run_id, node.node_id, rejected_candidate_sha
                )
                generation = builder_generation
                # A fresh actor cannot own the rejected turn's envelope or a
                # previous acknowledgement; this turn writes both itself.
                _clear_turn_artifact(envelope_path)
                _clear_turn_artifact(acknowledgement_path)
                prompt_text = _repair_prompt_text(
                    repair_prompt,
                    rejected_candidate_sha,
                    generation,
                    acknowledgement_path,
                    same_session=False,
                )
                dispatch = _lane_actor_dispatch(args, node.kind)
                # B13 at the same chokepoint every other dispatched prompt
                # crosses: a repair handoff carries the reviewer's findings on
                # top of the goal, so it is strictly larger than the first
                # tester prompt that already passed this check.
                _preflight_prompt(prompt_text, dispatch.route, dispatch.model)
                prompt_path.write_text(prompt_text, encoding="utf-8")
                # A second rejection reaches this frame with the *previous*
                # repair tester still tracked under this key, and overwriting
                # the entry below would leave that pane open with nothing
                # naming it -- one leaked rectangle per review round, up to the
                # test-review ceiling. Proven absent through the harness's own
                # quiescer rather than a bespoke cancel, so the absence is the
                # same proof every other path takes.
                #
                # Conditioned on a tracked handle rather than called
                # unconditionally, and that is not a shortcut: `quiesce_attempt`
                # answers `PROCESS_GROUP_UNTRACKED` for a key it has never
                # seen, and the scheduler blocks a node terminally on that
                # answer. A recovered attempt reaches a repair without ever
                # having entered `run_node`, so its key is untracked by
                # construction and there is nothing to prove absent.
                with handles_lock:
                    stale = handles.get(key)
                if stale is not None:
                    quiesce_attempt(record, "repair-relaunch")
                # The round, not just the generation. A tests node's repair
                # rounds all happen inside one attempt, so `generation` is
                # constant across them and a token built from it alone names
                # two different panes; the rejected candidate is distinct per
                # round by construction, so it is what separates them -- in
                # the Herdr agent id this token hashes to, and in the session
                # directory below.
                round_key = "g{}-{}".format(generation, rejected_candidate_sha[:12])
                spec = launcher.LaunchSpec(
                    correlation_token="{}-{}-tester-{}".format(
                        args.run_id, node.node_id, round_key
                    ),
                    worktree=attempt.path,
                    prompt_path=prompt_path,
                    envelope_path=envelope_path,
                    route=dispatch.route,
                    model=dispatch.model,
                    effort=dispatch.effort,
                    profile=dispatch.profile,
                    session_dir=(
                        attempt.scratch / "session-tester-{}".format(round_key)
                    ),
                    context_window_tokens=_route_context_window(
                        dispatch.route, dispatch.model
                    ),
                    workspace_label=workspace_label,
                    # The tab key, not the node id: `_tab_for` caches on
                    # `lane_key`, so the repair tester splits into the tab the
                    # lane already owns instead of opening a new one.
                    lane_key=lane_placement[node.node_id],
                    lane_label=lane_placement[node.node_id],
                    pane_role="tester",
                    attempt_no=generation,
                    pane_group_size=3,
                    restrict_tools=getattr(args, "restrict_actor_tools", False),
                    environment=launch_environment,
                )
                handle = _typed_launch_pane(route_runner, spec)
                try:
                    _require_session_path(handle, node.node_id, generation)
                    if handoff is not None:
                        submission = store.mark_handoff_submitted(
                            args.run_id,
                            node.node_id,
                            rejected_candidate_sha,
                            builder_generation=generation,
                        )
                        if (
                            not submission.submitted
                            or submission.handoff.builder_generation != generation
                        ):
                            raise scheduler.AttemptOwnershipLost(
                                "{}: tester handoff submission generation lost".format(
                                    node.node_id
                                )
                            )
                except BaseException:
                    route_runner.cancel(
                        handle, finalization_window.time.monotonic() + 5.0
                    )
                    raise
                mark_repair_launched(node, record, handle, dispatch)
                with handles_lock:
                    # Tracked for quiescence, and deliberately *not* placed in
                    # `builder_handles`: a tester is not a retained session, so
                    # `repair-idle` must cancel and reclaim its pane rather
                    # than wait for it to go idle and leave it open.
                    handles[key] = handle
                    proven_absent.discard(key)
                execution = _poll_agent_execution(
                    route_runner,
                    handle,
                    envelope_path,
                    record,
                    cancel_requested,
                    quiesce_attempt,
                )
                acknowledged = (
                    rejected_candidate_sha
                    if _read_repair_acknowledgement(
                        acknowledgement_path, rejected_candidate_sha, generation
                    )
                    else ""
                )
                return scheduler.RepairExecution(
                    execution=execution,
                    acknowledged_rejected_sha=acknowledged,
                    builder_generation=generation,
                )

            def continue_node(
                attempt,
                node,
                record,
                repair_prompt,
                rejected_candidate_sha,
                builder_generation,
                cancel_requested,
            ):
                """Deliver one repair handoff to the actor its lane kind owns.

                Widened rather than paralleled: all three of the scheduler's
                `REPAIRING` writers already funnel into this one callback, and
                what the scheduler asks for -- "deliver this prompt to this
                lane and return a `RepairExecution`" -- is kind-agnostic. Only
                the *delivery mechanism* differs by kind, and the delivery
                mechanism is exactly what this closure owns. A second
                `SchedulerDeps` field would have put a kind test at three
                scheduler call sites and in every test fake instead of here.
                """
                if route_runner is None:
                    raise scheduler.UnserviceableHandoff(
                        "{}: the launcher is unavailable, so no repair handoff "
                        "can be delivered".format(node.node_id)
                    )
                if node.kind is scheduler_types.NodeKind.TESTS:
                    return continue_tests_node(
                        attempt,
                        node,
                        record,
                        repair_prompt,
                        rejected_candidate_sha,
                        builder_generation,
                        cancel_requested,
                    )
                if node.kind is not scheduler_types.NodeKind.AGENT:
                    raise scheduler.UnserviceableHandoff(
                        "{} is a {} node, which has no repair route".format(
                            node.node_id, node.kind.value
                        )
                    )
                session = store.current_actor_session(
                    args.run_id, node.node_id, "builder"
                )
                prior_sessions = store.actor_sessions(
                    args.run_id,
                    node.node_id,
                    actor_role="builder",
                    limit=10_000,
                )
                if session is None:
                    session = next(
                        (
                            prior
                            for prior in reversed(prior_sessions)
                            if prior.generation == builder_generation
                        ),
                        None,
                    )
                if session is None or session.generation != builder_generation:
                    raise scheduler.AttemptOwnershipLost(
                        "{}: builder generation changed".format(node.node_id)
                    )
                replacement_generation = (
                    max(
                        (prior.generation for prior in prior_sessions),
                        default=session.generation,
                    )
                    + 1
                )
                key = (node.node_id, record.attempt_no)
                envelope_path = attempt.scratch / "agent-envelope.json"
                acknowledgement_path = attempt.scratch / "repair-acknowledgement.json"
                prompt_path = attempt.scratch / "repair-prompt.md"
                launch_environment = worktree.launch_env(
                    attempt.scratch, concurrency=getattr(args, "concurrency", None)
                )
                handoff = store.repair_handoff(
                    args.run_id, node.node_id, rejected_candidate_sha
                )
                resuming_submitted_handoff = (
                    handoff is not None
                    and handoff.builder_generation == builder_generation
                    and handoff.state
                    in (
                        scheduler_types.RepairHandoffState.SUBMITTED,
                        scheduler_types.RepairHandoffState.ACKNOWLEDGED,
                    )
                )

                def write_prompt(generation):
                    prompt_path.write_text(
                        _repair_prompt_text(
                            repair_prompt,
                            rejected_candidate_sha,
                            generation,
                            acknowledgement_path,
                        ),
                        encoding="utf-8",
                    )

                def mark_handoff_submitted(generation):
                    if handoff is None:
                        return
                    submission = store.mark_handoff_submitted(
                        args.run_id,
                        node.node_id,
                        rejected_candidate_sha,
                        builder_generation=generation,
                    )
                    if (
                        not submission.submitted
                        or submission.handoff.builder_generation != generation
                    ):
                        raise scheduler.AttemptOwnershipLost(
                            "{}: builder handoff submission generation lost".format(
                                node.node_id
                            )
                        )

                if not resuming_submitted_handoff:
                    _clear_turn_artifact(envelope_path)
                    _clear_turn_artifact(acknowledgement_path)
                    write_prompt(builder_generation)

                def launch_replacement():
                    nonlocal session, builder_generation
                    generation = replacement_generation
                    # A proven-absent session cannot own its old envelope or
                    # acknowledgement; its replacement gets a fresh turn.
                    _clear_turn_artifact(envelope_path)
                    _clear_turn_artifact(acknowledgement_path)
                    write_prompt(generation)
                    # Resolved from the node's own kind rather than hardcoded
                    # to `args.agent_*` and `"builder"`. This frame is reached
                    # only for AGENT lanes today, so the two agree; they must
                    # keep agreeing if another kind is ever routed here,
                    # because a replacement that silently swaps a lane's actor
                    # for the build vendor's is a B12 barrier failure that no
                    # gate would catch.
                    replacement_dispatch = _lane_actor_dispatch(args, node.kind)
                    replacement_role = (
                        "tester"
                        if node.kind is scheduler_types.NodeKind.TESTS
                        else "builder"
                    )
                    replacement = _typed_launch_pane(
                        route_runner,
                        launcher.LaunchSpec(
                            correlation_token=(
                                "{}-{}-{}-g{}".format(
                                    args.run_id,
                                    node.node_id,
                                    replacement_role,
                                    generation,
                                )
                            ),
                            worktree=attempt.path,
                            prompt_path=prompt_path,
                            envelope_path=envelope_path,
                            route=replacement_dispatch.route,
                            model=replacement_dispatch.model,
                            effort=replacement_dispatch.effort,
                            profile=replacement_dispatch.profile,
                            session_dir=(
                                attempt.scratch
                                / "session-{}-g{}".format(
                                    replacement_role, generation
                                )
                            ),
                            context_window_tokens=_route_context_window(
                                replacement_dispatch.route,
                                replacement_dispatch.model,
                            ),
                            workspace_label=workspace_label,
                            lane_key=lane_placement[node.node_id],
                            lane_label=lane_placement[node.node_id],
                            # Repeated as a literal rather than read off
                            # `replacement_role`: `test_pane_placement` reads
                            # the role off this call site's AST, and a name
                            # here makes the site invisible to it.
                            pane_role=(
                                "tester"
                                if node.kind is scheduler_types.NodeKind.TESTS
                                else "builder"
                            ),
                            attempt_no=generation,
                            pane_group_size=3,
                            restrict_tools=getattr(args, "restrict_actor_tools", False),
                            environment=launch_environment,
                        ),
                    )
                    try:
                        _require_session_path(replacement, node.node_id, generation)
                        if handoff is None:
                            recovered = store.recover_actor_session(
                                args.run_id,
                                node.node_id,
                                "builder",
                                expected_generation=session.generation,
                                generation=generation,
                                pane_id=replacement.pane_id,
                                session_path=str(replacement.transcript_path),
                                correlation_token=replacement.correlation_token,
                                tab_id=replacement.tab_id,
                            )
                        else:
                            recovered = store.recover_builder_handoff(
                                args.run_id,
                                node.node_id,
                                rejected_candidate_sha,
                                expected_generation=session.generation,
                                generation=generation,
                                pane_id=replacement.pane_id,
                                session_path=str(replacement.transcript_path),
                                correlation_token=replacement.correlation_token,
                                tab_id=replacement.tab_id,
                            )
                    except BaseException:
                        route_runner.cancel(
                            replacement, finalization_window.time.monotonic() + 5.0
                        )
                        raise
                    if not recovered.recovered:
                        route_runner.cancel(
                            replacement, finalization_window.time.monotonic() + 5.0
                        )
                        raise scheduler.AttemptOwnershipLost(
                            "{}: builder replacement generation lost".format(
                                node.node_id
                            )
                        )
                    session = recovered.session
                    builder_generation = generation
                    mark_handoff_submitted(generation)
                    mark_repair_launched(
                        node, record, replacement, replacement_dispatch
                    )
                    with handles_lock:
                        handles[key] = replacement
                        builder_handles[node.node_id] = replacement
                        proven_absent.discard(key)
                    return replacement

                with handles_lock:
                    handle = builder_handles.get(node.node_id)
                launched_replacement = False
                if handle is None:
                    persisted = launcher.PersistedActorHandle(
                        correlation_token=session.correlation_token,
                        pane_id=session.pane_id,
                        agent_name=launcher.agent_name_for(session.correlation_token),
                        launched_cwd=attempt.path,
                        transcript_path=Path(session.session_path),
                        envelope_path=envelope_path,
                        environment=launch_environment,
                        workspace_id=launcher.workspace_of(_session_tab_id(session)),
                        tab_id=_session_tab_id(session),
                        lane_key=node.node_id,
                    )
                    try:
                        handle = route_runner.adopt(persisted)
                    except (
                        launcher.HandleAbsent,
                        launcher.HandleAdoptionRefused,
                    ) as exc:
                        if isinstance(exc, launcher.HandleAdoptionRefused):
                            route_runner.retire_for_replacement(
                                persisted,
                                finalization_window.time.monotonic() + 5.0,
                            )
                        handle = launch_replacement()
                        launched_replacement = True
                    with handles_lock:
                        handles[key] = handle
                        builder_handles[node.node_id] = handle
                        proven_absent.discard(key)
                if handle.correlation_token != session.correlation_token:
                    raise scheduler.AttemptOwnershipLost(
                        "{}: builder handle token changed".format(node.node_id)
                    )
                if not launched_replacement and not resuming_submitted_handoff:
                    try:
                        route_runner.resubmit(
                            handle,
                            prompt_path,
                            route=args.agent_route,
                            expected_token=session.correlation_token,
                        )
                    except launcher.HandleAbsent:
                        handle = launch_replacement()
                        launched_replacement = True
                    except scheduler.AttemptOwnershipLost:
                        raise
                    except Exception as exc:
                        # Submission proof can race a prompt that did land. If
                        # the retained actor is visibly working or has already
                        # declared, consume that turn instead of creating a
                        # duplicate attempt.
                        status = route_runner.agent_status(handle)
                        if not envelope_path.exists() and status != "working":
                            raise scheduler.LaunchFailed(
                                _launcher_failure_for(route_runner, exc),
                                "{}: {}".format(type(exc).__name__, exc),
                            ) from exc
                        mark_handoff_submitted(builder_generation)
                    else:
                        mark_handoff_submitted(builder_generation)
                elif resuming_submitted_handoff:
                    # Fence the adopted durable dispatch before consuming its
                    # existing turn; this is idempotent for SUBMITTED/ACKED.
                    mark_handoff_submitted(builder_generation)
                execution = _poll_agent_execution(
                    route_runner,
                    handle,
                    envelope_path,
                    record,
                    cancel_requested,
                    quiesce_attempt,
                )
                acknowledged = (
                    rejected_candidate_sha
                    if _read_repair_acknowledgement(
                        acknowledgement_path, rejected_candidate_sha, builder_generation
                    )
                    else ""
                )
                return scheduler.RepairExecution(
                    execution=execution,
                    acknowledged_rejected_sha=acknowledged,
                    builder_generation=builder_generation,
                )

            run_gate, run_integration_gate = _scheduler_gate_deps(
                plan, resolved_runners
            )
            review_attempt = (
                _code_review_runner(args, route_runner, store)
                if route_runner is not None and getattr(args, "review_root", None)
                else None
            )
            close_review = (
                getattr(review_attempt, "close", None)
                if review_attempt is not None
                else None
            )
            receipt_path_for = (
                getattr(review_attempt, "receipt_path_for", None)
                if review_attempt is not None
                else None
            )

            def actor_status(attempt):
                """Raw per-pane status for the watchdog turn clock. Never observe()."""
                key = (attempt.node_id, attempt.attempt_no)
                with handles_lock:
                    handle = handles.get(key)
                if handle is None or isinstance(handle, subprocess.Popen):
                    return None
                if route_runner is None:
                    return None
                return route_runner.agent_status(handle)

            deps = scheduler.SchedulerDeps(
                store=store,
                repo=Path(args.repo),
                integration_path=Path(args.integration_path),
                integration_branch=plan.merge_policy.integration_branch,
                worktrees_root=Path(args.worktrees_root),
                scratch_root=Path(args.scratch_root),
                run_node=run_node,
                recover_node=_late_agent_execution,
                run_gate=run_gate,
                run_integration_gate=run_integration_gate,
                # The bytes this run executes, retained on the run itself so a
                # later `plan ship` over the installed file cannot leave it
                # unresumable. Read from the plan file the run is actually
                # about to execute, which is the same bytes `_load_runnable_
                # plan` validated and whose digest the receipt covers.
                # `None` when the plan did not come through
                # `_load_runnable_plan` -- an offline harness or a test double
                # constructing deps directly -- and retention is then skipped,
                # which is the same no-op a run predating the table sees.
                plan_bytes=getattr(args, "plan_bytes", None),
                # The revert target for a `controlled_mutation` negative
                # control. Carried from the plan the run is executing, so a
                # control reverts to the state the plan declared as its
                # starting point rather than to whatever HEAD happens to be.
                #
                # Read the way `_refuse_base_commit_divergence` reads it, and
                # for the reason stated there: a fixture or double without the
                # field is left to the refusal that already covers it rather
                # than given an invented base here. The empty default is not a
                # fallback -- `_prove_test_strength` refuses a revert_paths
                # control by name when it has no target, instead of reverting
                # against HEAD.
                plan_base_commit=getattr(plan, "base_commit", "") or "",
                quiesce_attempt=quiesce_attempt,
                # §8.8's single integration gate, adjudicated at the number the
                # plan declared for it. Omitting this is what left final
                # acceptance counting to 1 while the plan asked for 70.
                integration_min_cases=(plan.merge_policy.integration_gate.min_cases),
                # §8.3's provision step, which the scheduler has always called and
                # nothing had ever supplied. Omitting it measured every baseline
                # against an unprovisioned tree and left §7.4's pre-gate red for
                # the ecosystem's missing install rather than for the node's
                # missing work.
                provision=_run_provisioner(args, route_runner),
                kill_attempt=lambda record: quiesce_attempt(record, "watchdog-kill"),
                review_attempt=review_attempt,
                continue_node=continue_node,
                close_review=close_review,
                receipt_path_for=receipt_path_for,
                actor_status=actor_status,
            )
            try:
                runtime = scheduler.Scheduler(
                    args.run_id,
                    plan.to_plan_nodes(),
                    config,
                    deps,
                    plan_digest=args.digest,
                    plan_name=getattr(plan, "title", None),
                )
                # Projection creates the derived review DAG rows that legacy
                # evidence must bind to, but does not dispatch a node.
                runtime.project()
                if resuming:
                    migrations = _migrate_legacy_inline_reviews(
                        args, store, plan.to_plan_nodes()
                    )
                    blocks = tuple(
                        migration for migration in migrations if migration.blocked
                    )
                    if not blocks:
                        blocks = store.legacy_review_migrations(args.run_id)
                    if blocks:
                        return _refusal(
                            "LEGACY_REVIEW_MIGRATION_BLOCKED",
                            "; ".join(
                                "{}:{}".format(
                                    migration.build_node_id, migration.reason
                                )
                                for migration in blocks
                            ),
                        )
                    for migration in migrations:
                        if not migration.reviews:
                            continue
                        accepted = migration.reviews[-1]
                        if accepted.verdict is not scheduler_types.ReviewVerdict.PASS:
                            continue
                        review_node_id = "{}::review".format(migration.build_node_id)
                        review_lifecycle = store.get_node(args.run_id, review_node_id)
                        if (
                            review_lifecycle.state
                            is not scheduler_types.NodeState.ACCEPTED
                        ):
                            store.mark_review_accepted(
                                args.run_id, review_node_id, accepted.candidate_sha
                            )
                        build_lifecycle = store.get_node(
                            args.run_id, migration.build_node_id
                        )
                        if build_lifecycle.lane_phase is None:
                            store.set_lane_phase(
                                args.run_id,
                                migration.build_node_id,
                                scheduler_types.LanePhase.ACCEPTED,
                                expected=None,
                            )
                report = runtime.run()
            except scheduler.RunPaused:
                # Not an outcome, and deliberately not printed as one: nothing was
                # declared, no node moved, and `run resume` is legal from here.
                print(
                    json.dumps(
                        {
                            "outcome": "PAUSED",
                            "run_id": args.run_id,
                        },
                        sort_keys=True,
                    )
                )
                return 0
        finally:
            # Progress and the store both stop before the integration checkout
            # is released. Each cleanup still runs if the preceding one fails.
            try:
                try:
                    progress.__exit__(*sys.exc_info())
                finally:
                    store.close()
            finally:
                _release_run_integration_worktree(
                    Path(args.repo), created_integration_path
                )
        print(
            json.dumps(
                {
                    "outcome": report.outcome.value,
                    "run_id": args.run_id,
                    "merged": list(report.merged),
                    "blocked": [
                        {"node_id": node, "reason": reason.value}
                        for node, reason in report.blocked
                    ],
                    # The findings that exhausted a node's review budget. A
                    # bare REVIEW_BUDGET_EXHAUSTED names the rule that fired
                    # and nothing an operator can act on. Read defensively:
                    # the scheduler is a seam tests substitute, and a run's
                    # exit status must not depend on a stand-in carrying
                    # every field of the real report.
                    "review_findings": dict(
                        getattr(report, "review_findings", {}) or {}
                    ),
                    # Findings-per-attempt for every reviewed node, in
                    # order. `review_findings` answers "what did the
                    # reviewer object to"; this answers "was it objecting
                    # less each time", which is the question behind
                    # `review_ceiling` and the one nothing in the run
                    # could answer once the process exited.
                    "review_convergence": {
                        node_id: list(counts)
                        for node_id, counts in dict(
                            getattr(report, "review_convergence", {}) or {}
                        ).items()
                    },
                },
                sort_keys=True,
            )
        )
        return 0

    finally:
        worktree.bind_lane_concurrency(None)


def _scheduler_gate_deps(plan: plan_model.Plan, runners):
    """Adapt the frozen plan gates without dropping cancellation ownership.

    `node.gate_command` stays the abstract `(runner,) + argv` it has always
    been — it is inside the plan's canonical bytes and `plan_model` asserts
    exactly that shape, so a resolved interpreter must never be baked into it
    or the resolved binary becomes part of the approved identity and a plan
    stops being portable between machines. The binary is supplied here, at
    execution, from the `ResolvedRunner` the run's preflight probed.
    """
    integration_gate = plan.merge_policy.integration_gate

    def run_gate(attempt, node, phase, cancel_requested):
        command = tuple(node.gate_command)
        return worktree.run_node_gate(
            attempt,
            runners[command[0]],
            command[1:],
            node.gate_selector,
            cancel_requested,
            label="{}-{}".format(node.node_id, phase),
        )

    def run_integration_gate(integration_path, specs, cancel_requested):
        # `specs` is the union of merged node specs, recorded on the
        # acceptance result. It is not the integration command: appending
        # it would make this gate the union of what the lanes already ran,
        # which is the §8.8 gap. The executed argv is the plan's command
        # with every selector stripped, so a named subset cannot hide a
        # test no lane owned.
        del specs
        return worktree.run_integration_gate(
            integration_path,
            runners[integration_gate.runner],
            plan_model.unscoped_argv(integration_gate.argv),
            Path(integration_path).parent / ".maestro",
            cancel_requested,
            label="integration-gate",
        )

    return run_gate, run_integration_gate


def _epoch(stamp: Optional[str]) -> Optional[float]:
    """An ISO ledger stamp as epoch seconds, or None when it cannot be read.

    None rather than 0.0, unlike the scheduler's backstop: this is a display
    path, and a zero here would render as "elapsed 57 years" instead of
    admitting the timestamp is unreadable.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _since(stamp: Optional[str], now: float) -> Optional[float]:
    moment = _epoch(stamp)
    return None if moment is None else max(0.0, now - moment)


def _duration(seconds: Optional[float]) -> str:
    """A wall-clock gap an operator can read at a glance."""
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 60:
        return "{}s".format(total)
    if total < 3600:
        return "{}m{:02d}s".format(total // 60, total % 60)
    if total < 86400:
        return "{}h{:02d}m".format(total // 3600, (total % 3600) // 60)
    return "{}d{:02d}h".format(total // 86400, (total % 86400) // 3600)


def _integration_head(path: Path) -> Optional[Dict[str, Any]]:
    """The integration worktree's branch and head, as git reports them.

    The run's whole output accumulates on this branch, so "which commit is the
    integration worktree on" is the single most load-bearing fact `run status`
    can print that the ledger does not store.
    """
    if not path.is_dir():
        return None
    read = {
        "branch": ("rev-parse", "--abbrev-ref", "HEAD"),
        "head": ("rev-parse", "HEAD"),
        "subject": ("log", "-1", "--format=%s"),
    }
    found: Dict[str, Any] = {"path": str(path)}
    # A key is present when git answered and absent when it did not. The
    # ternary this replaces wrote `None` on every nonzero exit, which reads as
    # "the repository has no head" rather than "this process could not read
    # one" — §7.5's conflation, in the diagnostics rather than in an
    # obligation. The renderer already prints `?` for a key it does not find,
    # so an unread field looks unread, and `unreadable` says so outright
    # rather than leaving an operator to infer it from a null.
    unreadable = []
    for key, command in read.items():
        completed = subprocess.run(
            ("git", "-C", str(path)) + command, capture_output=True, text=True
        )
        if completed.returncode == 0:
            found[key] = completed.stdout.strip()
        else:
            unreadable.append(key)
    if unreadable:
        found["unreadable"] = sorted(unreadable)
    return found


def _attempt_history(
    transitions: Sequence[Dict[str, Any]],
) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    """Every node transition filed under the attempt it happened to.

    The ledger numbers attempts in `attempts` and narrates them in
    `transitions`, and nothing joins the two — `transitions` carries no
    attempt number. The join is positional and reliable for one reason: the
    scheduler writes exactly one `attempt-start` per attempt, in order, so the
    nth `attempt-start` for a node opens attempt n. Anything before the first
    one belongs to no attempt and is dropped rather than guessed at.
    """
    open_attempt: Dict[str, int] = {}
    history: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for row in transitions:
        node_id = row.get("node_id")
        if node_id is None:
            continue
        if row.get("reason") == "attempt-start":
            open_attempt[node_id] = open_attempt.get(node_id, 0) + 1
            continue
        attempt_no = open_attempt.get(node_id)
        if attempt_no is None:
            continue
        history.setdefault((node_id, attempt_no), []).append(
            {
                "reason": row.get("reason"),
                "from_state": row.get("from_state"),
                "to_state": row.get("to_state"),
                "actor": row.get("actor"),
                "at": row.get("created_at"),
                "detail": row.get("detail", {}),
            }
        )
    return history


def _attempt_verdict(entries: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The sentence that explains why an attempt ended, when there is one."""
    for entry in entries:
        detail = entry.get("detail")
        if not isinstance(detail, Mapping):
            continue
        verdict = detail.get("verdict")
        if verdict:
            return str(verdict)
    return None


def _merge_cause_prefix(merge_cause: Optional[str]) -> str:
    """What precedes the output SHA in `run status`'s DETAIL column.

    `MERGED` stays the state, because it *is* the state: the frontier, the
    readiness predicate, and the run outcome all key on it, and renaming it
    for one provenance would be a display inventing a seventh state (§7.3).
    What changes is that the operator is told which of the two things
    happened, in the one column that had room for it, rather than being left
    to read the integration branch's git log for the difference (#93).
    """
    if merge_cause == scheduler_types.MergeCause.OPERATOR_ACCEPTED.value:
        return "operator-accepted "
    if merge_cause == scheduler_types.MERGE_CAUSE_UNRECORDED:
        return "unrecorded merge, output "
    return "output "


def _pending_cause_detail(pending_cause: Optional[str]) -> str:
    """What `run status` puts in DETAIL for a PENDING node that was reopened.

    Seeded PENDING keeps an empty DETAIL: the node never left the frontier,
    so there is no writer to name. A reopened PENDING used to look the same,
    which is the defect (#103). The state stays PENDING; the column says who
    wrote it.
    """
    if pending_cause == scheduler_types.PendingCause.OPERATOR_RETRY.value:
        return "operator retry"
    if pending_cause == scheduler_types.PendingCause.OPERATOR_RESUME.value:
        return "operator resume"
    if pending_cause == scheduler_types.PendingCause.SCHEDULER.value:
        return "scheduler retry"
    return ""


def _merge_evidence_lines(node: Mapping[str, Any]) -> List[str]:
    """The evidence-chain reading for a node an operator accepted.

    Printed beside `BLOCKED:` and in the same shape, because it answers the
    same question — why does this node read the way it does — and because
    §1.1 item 4's chain being absent is exactly as worth an operator's
    attention as the reason a node stopped. Says what the ledger counted and
    stops there: no line here asserts that the work is wrong, only that the
    run did not establish it.
    """
    evidence = node.get("merge_evidence")
    if not isinstance(evidence, Mapping):
        return []
    if evidence.get("verified_ever"):
        chain = "node reached VERIFIED {} time(s) before it was accepted".format(
            evidence.get("verified_transitions")
        )
    else:
        chain = (
            "node never reached VERIFIED: no post-node gate pass and no "
            "reviewer verdict is recorded for it"
        )
    return [
        "    OPERATOR-ACCEPTED: {} — evidence chain not established by this run".format(
            node["output_sha"][:12] if node["output_sha"] else "?"
        ),
        "      {}".format(chain),
        "      {} review rejection(s) over {} attempt(s); blocked as {}".format(
            evidence.get("review_rejections"),
            evidence.get("attempts_recorded"),
            evidence.get("block_reason") or "no stored reason",
        ),
    ]


def _merge_evidence_by_node(
    transitions: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """The evidence record `skip` wrote, per node it accepted (§1.1 item 4).

    `node_lifecycle.merge_cause` says *that* an operator accepted the node,
    which is the fact a reader must key on and the fact §1.2 requires to be
    typed and stored. What the evidence chain held at that moment is the
    audit half of the same write, and it lives on the transition — the shape
    §11.3 settled for `retry --grant`'s magnitude, where the column carries
    what a guard reads and the transition carries what an operator reads.

    Read directly rather than through `_attempt_history`, which files a
    transition under the attempt that was open when it happened. A skip is
    not about an attempt: it is about the node, and the operator asking why a
    node reads operator-accepted should not have to know which attempt was
    open when they typed the command.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for row in transitions:
        node_id = row.get("node_id")
        if node_id is None or row.get("reason") != scheduler_types.Escape.SKIP.value:
            continue
        evidence = (row.get("detail") or {}).get(lc.MERGE_EVIDENCE_KEY)
        if isinstance(evidence, dict):
            found[node_id] = evidence
    return found


def _live_state(record: "lc.RunRecord", nodes: Sequence["lc.NodeRow"]) -> str:
    """What the run is doing now, which is not what it last *declared*.

    `runs.latest_outcome` is the last quiescence a scheduler declared, and it
    survives a resume — so a run that blocked, was rescued, and is now working
    still reads BLOCKED there. An operator watching a run needs the live
    shape, so the declared outcome is reported beside this rather than as it.

    The derivation itself lives in `lifecycle` beside the rows it reads, so
    `run status`, `run list`, and any later reader answer from one function
    rather than from three re-derivations of the same rule (§10.6). Two of the
    facts it turns on were previously not derivable at all: whether a
    cancellation had *finished*, and whether any scheduler process is still
    behind the run.
    """
    return lc.derive_run_state(record, nodes)


def _test_strength_projection(
    node: Any,
    evidence: Sequence[Any],
    accepted_test_sha: Mapping[str, str],
    pairings: Sequence[Any],
) -> Dict[str, Any]:
    """The test-strength view of one node, for `run status` and the console.

    Every value is read from the ledger or derived from it in one place, so
    the CLI, the dashboard and the console cannot come to describe the same
    node differently — which is how `tester MERGED` came to mean two things.

    `test_bytes` is the field the old vocabulary could not express: a tests
    node's candidate can be *private* (committed, not yet accepted), *staged*
    (accepted, not yet on the integration branch) or *integrated*. Reporting
    `MERGED` alone said only the last of the three and implied all of them.
    """
    kind = node.kind
    try:
        node_kind = scheduler_types.NodeKind(kind)
    except ValueError:
        return {"test_strength": None}
    accepted = accepted_test_sha.get(node.node_id)
    latest = evidence[-1] if evidence else None
    if node_kind is scheduler_types.NodeKind.TESTS:
        if accepted is None:
            location = scheduler_types.TestBytesLocation.PRIVATE
        elif node.state is scheduler_types.NodeState.MERGED:
            location = scheduler_types.TestBytesLocation.INTEGRATED
        else:
            location = scheduler_types.TestBytesLocation.STAGED
    else:
        location = None
    paired = [
        {
            "tests_node_id": row.tests_node_id,
            "accepted_test_sha": row.accepted_test_sha,
            "implementation_sha": row.implementation_sha,
            "verifier_command": row.verifier_command,
            "selector": row.selector,
            "executed_cases": row.executed_cases,
        }
        for row in pairings
        if node.output_sha is None or row.implementation_sha == node.output_sha
    ]
    phase = scheduler_types.test_strength_phase(
        node_kind,
        node.state,
        node.lane_phase,
        accepted=accepted is not None,
        paired=bool(paired),
    )
    if phase is None:
        return {"test_strength": None}
    return {
        "test_strength": {
            "phase": phase.value,
            "test_candidate_sha": accepted or (
                latest.candidate_sha if latest is not None else None),
            "accepted_test_sha": accepted,
            "test_bytes": location.value if location is not None else None,
            "gate_evidence": (
                {
                    "candidate_sha": latest.candidate_sha,
                    "runner": latest.runner,
                    "selector": latest.selector,
                    "strong": latest.strong,
                    "refusal": latest.refusal,
                    "coverage": (latest.evidence or {}).get("coverage"),
                    "falsifiability": (latest.evidence or {}).get(
                        "falsifiability"),
                }
                if latest is not None
                else None
            ),
            "pairings": paired,
        }
    }


def _run_progress(
    reader: "lc.LifecycleReader", record: "lc.RunRecord", args: argparse.Namespace
) -> Dict[str, Any]:
    """Everything `run status` knows about one run, as one document."""
    now = time.time()
    nodes = reader.nodes(record.run_id)
    attempts = reader.attempts(record.run_id)
    candidate_reviews = reader.candidate_reviews(record.run_id, limit=10_000)
    repair_handoffs = reader.repair_handoffs(record.run_id, limit=10_000)
    transitions = reader.transitions(record.run_id)
    history = _attempt_history(transitions)
    merge_evidence = _merge_evidence_by_node(transitions)
    results = reader.results(record.run_id)
    names = {
        digest: name
        for name, digest in (getattr(args, "plan_digests", None) or {}).items()
    }

    by_node: Dict[str, List[Dict[str, Any]]] = {}
    in_flight: List[Dict[str, Any]] = []
    for attempt in attempts:
        entries = history.get((attempt.node_id, attempt.attempt_no), [])
        started = attempt.launched_at or attempt.started_at or None
        running = attempt.state is scheduler_types.NodeState.RUNNING
        identity = attempt_identity.identity_from_record(attempt)
        projected = {
            "attempt_no": attempt.attempt_no,
            "state": attempt.state.value,
            "base_sha": attempt.base_sha,
            "turn_count": attempt.turn_count,
            "retry_class": (attempt.retry_class.value if attempt.retry_class else None),
            "pid": attempt.pid,
            "started_at": attempt.started_at or None,
            "launched_at": attempt.launched_at,
            "running": running,
            "elapsed_s": (max(0.0, now - started) if running and started else None),
            "session_path": attempt.extra.get(watchdog.SESSION_PATH_KEY),
            "vendor": identity.vendor,
            "model": identity.model,
            "route": identity.route,
            "verdict": _attempt_verdict(entries),
            "transitions": entries,
            "legacy_review_findings": [
                finding.as_dict()
                for finding in review_findings.legacy_findings_from_extra(attempt.extra)
            ],
        }
        by_node.setdefault(attempt.node_id, []).append(projected)
        if running:
            in_flight.append(
                {
                    "node_id": attempt.node_id,
                    "attempt_no": attempt.attempt_no,
                    "turn_count": attempt.turn_count,
                    "elapsed_s": projected["elapsed_s"],
                }
            )

    # The test-strength ledger, read once and joined onto the nodes below.
    # Read here rather than per node so `run status` takes one pass over each
    # table, and so a ledger written before these tables existed answers
    # "nothing recorded" uniformly instead of once per node.
    test_evidence = reader.test_gate_evidence(record.run_id, limit=10_000)
    test_pairings = reader.test_pairings(record.run_id, limit=10_000)
    test_strength_blocks = reader.legacy_test_strength_blocks(
        record.run_id, limit=10_000
    )
    accepted_reviews = {
        (review.review_node_id, review.candidate_sha)
        for review in candidate_reviews
        if review.state is scheduler_types.CandidateReviewState.COMPLETED
        and review.verdict is scheduler_types.ReviewVerdict.PASS
    }
    accepted_test_sha: Dict[str, str] = {}
    evidence_by_node: Dict[str, List[Any]] = {}
    for item in test_evidence:
        evidence_by_node.setdefault(item.tests_node_id, []).append(item)
        if item.strong and (
            "{0}::review".format(item.tests_node_id), item.candidate_sha
        ) in accepted_reviews:
            accepted_test_sha[item.tests_node_id] = item.candidate_sha
    pairings_by_build: Dict[str, List[Any]] = {}
    for pairing in test_pairings:
        pairings_by_build.setdefault(pairing.build_node_id, []).append(pairing)

    projected_nodes = []
    for node in nodes:
        node_attempts = by_node.get(node.node_id, [])
        projected_nodes.append(
            {
                "node_id": node.node_id,
                "kind": node.kind,
                "depth": node.depth,
                "needs": list(node.needs),
                "state": node.state.value,
                "block_reason": (
                    node.block_reason.value if node.block_reason else None
                ),
                "attempt_no": node.attempt_no,
                "attempts_recorded": len(node_attempts),
                "granted_extra_attempts": node.granted_extra_attempts,
                # How the node reached MERGED, as one key with three values and a
                # null: `SCHEDULER`, `OPERATOR_ACCEPTED`, `UNRECORDED` for a row
                # written before the column, and `null` where the node is not
                # MERGED and the question does not arise. Both `MERGED` shapes
                # used to render identically here, which is what left the git log
                # as the only place the difference was visible (#93).
                "merge_cause": node.merge_provenance,
                # How the node reached PENDING after leaving it: `SCHEDULER`,
                # `OPERATOR_RETRY`, `OPERATOR_RESUME`, or `null` where the node
                # never left the frontier or the column was never recorded.
                # The three PENDING writers used to render identically here (#103).
                "pending_cause": node.pending_provenance,
                # What the ledger could show about the evidence chain when the
                # operator accepted it — present only on a node `skip` wrote.
                "merge_evidence": merge_evidence.get(node.node_id),
                "output_sha": node.output_sha,
                "updated_at": node.updated_at,
                "idle_s": _since(node.updated_at, now),
                "attempts": node_attempts,
                # The test-strength lifecycle, projected rather than stored.
                # `null` on a kind the lifecycle does not describe, and on a
                # ledger that recorded nothing, so a surface renders what is
                # known instead of a phase nobody measured.
                **_test_strength_projection(
                    node,
                    evidence_by_node.get(node.node_id, ()),
                    accepted_test_sha,
                    pairings_by_build.get(node.node_id, ()),
                ),
            }
        )

    integration = None
    state_root = getattr(args, "repository_state", None)
    if state_root:
        integration = _integration_head(
            Path(state_root) / "runs" / record.run_id / "integration"
        )
    return {
        "run_id": record.run_id,
        "plan_name": names.get(record.plan_digest),
        "plan_digest": record.plan_digest,
        "state": _live_state(record, nodes),
        "declared_outcome": (
            record.latest_outcome.value if record.latest_outcome else None
        ),
        "declared_outcome_at": record.latest_outcome_at,
        "cancel_requested": record.cancel_requested,
        # Which of §7.3's two cancellation shapes the declared CANCELLED was,
        # and therefore whether `run resume` will take this run back. Reported
        # rather than left to be inferred from the node states: the inference
        # is exactly the heuristic the stored cause exists to replace.
        "cancel_cause": (record.cancel_cause.value if record.cancel_cause else None),
        # The three facts the state above was derived from, reported so an
        # ABANDONED verdict can be checked rather than believed.
        "scheduler_pid": record.scheduler_pid,
        "scheduler_host": record.scheduler_host,
        "scheduler_alive": lc.scheduler_liveness(record),
        "created_at": record.created_at,
        "last_transition_at": record.last_transition_at,
        "elapsed_s": _since(record.created_at, now),
        "idle_s": _since(record.last_transition_at, now),
        "integration": integration,
        "in_flight": in_flight,
        "nodes": projected_nodes,
        "review_findings": [
            review.as_dict()
            for review in review_findings.run_findings(
                record.run_id,
                candidate_reviews,
                declared_outcome=(
                    record.latest_outcome.value if record.latest_outcome else None
                ),
            ).reviews
        ],
        "repair_handoffs": [
            {
                "build_node_id": handoff.build_node_id,
                "rejected_candidate_sha": handoff.rejected_candidate_sha,
                "findings": [dict(finding) for finding in handoff.findings],
                "state": handoff.state.value,
                "builder_generation": handoff.builder_generation,
                "submitted_at": handoff.submitted_at,
                "acknowledged_at": handoff.acknowledged_at,
            }
            for handoff in repair_handoffs
        ],
        # Which test-acceptance contract this run was created under, and what
        # its tests nodes are classified as against the current one. Both are
        # reported because they answer different operator questions: the pin
        # says which rules decided this run, the audit says what would be
        # unproven if it were decided under today's.
        "test_strength_contract": reader.test_strength_contract(
            record.run_id
        ).value,
        "legacy_test_strength_blocks": [
            {
                "tests_node_id": block.tests_node_id,
                "reason": block.classification,
                "detail": dict(block.detail),
            }
            for block in test_strength_blocks
        ],
        "results": [
            {
                "node_id": row.get("node_id"),
                "attempt_no": row.get("attempt_no"),
                "adjudication": row.get("adjudication"),
                "subject_sha": row.get("subject_sha"),
                "at": row.get("created_at"),
                "payload": row.get("payload"),
            }
            for row in results
        ],
    }


def _render_progress(progress: Dict[str, Any]) -> str:
    """The default human view. One run, top to bottom, newest fact last."""
    lines = [
        "{}  {}".format(
            progress["run_id"], progress["plan_name"] or "(plan not installed)"
        )
    ]
    declared = progress["declared_outcome"]
    lines.append(
        "  state        {}{}".format(
            progress["state"],
            ""
            if declared is None
            else "   (last declared outcome {} at {})".format(
                declared, progress["declared_outcome_at"]
            ),
        )
    )
    findings = progress.get("review_findings") or []
    if findings:
        lines.append(
            "  findings     {} rejected candidate review{}".format(
                len(findings), "" if len(findings) == 1 else "s"
            )
        )
    lines.append("  plan digest  {}".format(progress["plan_digest"]))
    lines.append(
        "  started      {}   ({} ago)".format(
            progress["created_at"], _duration(progress["elapsed_s"])
        )
    )
    lines.append(
        "  last change  {}   ({} ago)".format(
            progress["last_transition_at"], _duration(progress["idle_s"])
        )
    )
    if progress["cancel_requested"]:
        lines.append("  cancel       requested")
    if progress["cancel_cause"]:
        lines.append(
            "  cancel cause {}{}".format(
                progress["cancel_cause"],
                "   (resumable)"
                if progress["cancel_cause"]
                == scheduler_types.CancelCause.RUN_CANCEL.value
                else "   (not reopenable)",
            )
        )
    if progress["scheduler_pid"]:
        alive = progress["scheduler_alive"]
        lines.append(
            "  scheduler    pid {} on {} — {}".format(
                progress["scheduler_pid"],
                progress["scheduler_host"] or "?",
                "alive" if alive else "gone" if alive is False else "unknown host",
            )
        )
    integration = progress["integration"]
    if integration is None:
        lines.append("  integration  (no worktree found)")
    else:
        lines.append(
            "  integration  {} @ {}".format(
                integration.get("branch") or "?", (integration.get("head") or "?")[:12]
            )
        )
        lines.append("               {}".format(integration["path"]))
        if integration.get("subject"):
            lines.append("               head: {}".format(integration["subject"]))

    lines.append("")
    lines.append(
        "  {:<44} {:<10} {:>8}  {}".format("NODE", "STATE", "ATTEMPT", "DETAIL")
    )
    for node in progress["nodes"]:
        detail = node["block_reason"] or ""
        live = [item for item in node["attempts"] if item["running"]]
        if live:
            detail = "in flight {}, {} turns".format(
                _duration(live[0]["elapsed_s"]), live[0]["turn_count"]
            )
        elif node.get("pending_cause"):
            detail = _pending_cause_detail(node["pending_cause"])
        elif node["output_sha"]:
            detail = "{}{}".format(
                _merge_cause_prefix(node["merge_cause"]), node["output_sha"][:12]
            )
        lines.append(
            "  {:<44} {:<10} {:>8}  {}".format(
                node["node_id"][:44], node["state"], node["attempt_no"], detail
            )
        )
        strength = node.get("test_strength")
        if strength:
            # Rendered under the node rather than in place of its state,
            # because they answer different questions and collapsing them is
            # what made a private acceptance read as `tester MERGED`.
            phase = strength["phase"]
            bytes_where = strength.get("test_bytes")
            lines.append(
                "  {:<44} {:<10} {:>8}  {}".format(
                    "", "", "",
                    "{0}{1}{2}".format(
                        phase,
                        "" if bytes_where is None
                        else "  test bytes {0}".format(bytes_where),
                        "" if not strength.get("accepted_test_sha")
                        else "  @{0}".format(
                            strength["accepted_test_sha"][:12]),
                    ),
                )
            )
            evidence = strength.get("gate_evidence") or {}
            control = (evidence.get("falsifiability") or {})
            if evidence:
                lines.append(
                    "  {:<44} {:<10} {:>8}  {}".format(
                        "", "", "",
                        "gate strength {0}{1}".format(
                            "proven" if evidence.get("strong")
                            else evidence.get("refusal") or "unmeasured",
                            "" if not control.get("strategy")
                            else "  control {0}".format(control["strategy"]),
                        ),
                    )
                )
            for pairing in strength.get("pairings") or ():
                lines.append(
                    "  {:<44} {:<10} {:>8}  {}".format(
                        "", "", "",
                        "paired with {0} @{1}  {2} case(s) green".format(
                            pairing["tests_node_id"],
                            pairing["accepted_test_sha"][:12],
                            pairing["executed_cases"],
                        ),
                    )
                )
    contract = progress.get("test_strength_contract")
    if contract:
        lines.append("")
        lines.append("  test contract  {}".format(contract))
    for block in progress.get("legacy_test_strength_blocks") or ():
        lines.append(
            "  {:<44} {}".format(block["tests_node_id"][:44], block["reason"])
        )
    if findings:
        lines.append("")
        lines.append("  rejected candidate reviews")
        for review in findings:
            lines.append(
                "  {}  {}  {} blocking".format(
                    review["review_node_id"],
                    review["candidate_sha"][:12],
                    len(review.get("findings") or ()),
                )
            )
            for finding in review.get("findings") or ():
                lines.append(
                    "    {}  {}".format(
                        finding.get("check_id") or "", finding.get("object_id") or ""
                    )
                )
                if finding.get("message"):
                    lines.append("      {}".format(finding["message"]))

    for node in progress["nodes"]:
        if not node["attempts"]:
            continue
        lines.append("")
        lines.append("  {} — attempts".format(node["node_id"]))
        for attempt in node["attempts"]:
            outcome = attempt["retry_class"] or attempt["state"]
            lines.append(
                "    a{:<3} {:<10} {:<22} {:>4} turns  {}".format(
                    attempt["attempt_no"],
                    attempt["state"],
                    outcome,
                    attempt["turn_count"],
                    _duration(attempt["elapsed_s"]) if attempt["running"] else "",
                )
            )
            if attempt["verdict"]:
                lines.append("         why: {}".format(attempt["verdict"]))
            if attempt["session_path"]:
                lines.append("         session: {}".format(attempt["session_path"]))
            lines.append(
                "         vendor: {}".format(
                    attempt_identity.display(attempt.get("vendor"))
                )
            )
            lines.append(
                "         model: {}".format(
                    attempt_identity.display(attempt.get("model"))
                )
            )
            lines.append(
                "         route: {}".format(
                    attempt_identity.display(attempt.get("route"))
                )
            )
            for finding in attempt.get("review_findings") or ():
                lines.append(
                    "         finding: {}  {}".format(
                        finding.get("check_id") or "", finding.get("object_id") or ""
                    )
                )
        if node["block_reason"]:
            lines.append("    BLOCKED: {}".format(node["block_reason"]))
        for line in _merge_evidence_lines(node):
            lines.append(line)

    for row in progress["results"]:
        lines.append("")
        lines.append(
            "  result {}#{} {}".format(
                row["node_id"], row["attempt_no"], row["adjudication"] or ""
            )
        )
        lines.append("    {}".format(json.dumps(row["payload"], sort_keys=True)))
    return "\n".join(lines)


def _run_status(args: argparse.Namespace) -> int:
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    reader = _open_reader(args.db)
    try:
        progress = _run_progress(reader, _select_run(reader, args), args)
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(progress, sort_keys=True))
    else:
        print(_render_progress(progress))
    return 0


def _run_test_strength(args: argparse.Namespace) -> int:
    """Audit — and only on demand, migrate — one run's test-strength evidence.

    The default is a **read**. `maestro run test-strength <run>` opens the
    ledger read-only, classifies every tests node against the current
    contract, and prints what it found. Nothing is written, no terminal row
    moves, and no dependency decision changes. That is the whole of what an
    existing run gets by default, and it is deliberate: a run created under
    the old rules stays reproducible under them, and reclassifying its history
    because a newer binary ran would make every completed run's meaning depend
    on when someone last looked at it.

    `--migrate` is the explicit operator command the rollout requires. It
    takes a SQLite backup first and names it in the output, applies its writes
    in one transaction, and restores the backup if any of them fails. Without
    `--apply` it is still a dry run: the same code path produces the report,
    so what an operator reads is what would happen rather than a second
    description of it.

    `--policy block-unadmitted` is the only policy that fences anything, and
    it fences only dependants that **have never been admitted** — PENDING,
    with no attempt row. A dependant that ever ran was admitted under the
    pinned contract and is left exactly as it is.
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    migrating = bool(getattr(args, "migrate", False))
    policy = str(getattr(args, "policy", "classify") or "classify").replace(
        "-", "_")
    if not migrating:
        reader = _open_reader(args.db)
        try:
            record = _select_run(reader, args)
            payload = {
                "run_id": record.run_id,
                "test_strength_contract": reader.test_strength_contract(
                    record.run_id
                ).value,
                "applied": False,
                "backup_path": None,
                "findings": [
                    {
                        "tests_node_id": item.tests_node_id,
                        "state": item.state,
                        "candidate_sha": item.candidate_sha,
                        "classification": item.classification,
                        "blocking": item.blocking,
                        "detail": dict(item.detail),
                    }
                    for item in _reader_test_strength_audit(reader, record.run_id)
                ],
            }
        finally:
            reader.close()
    else:
        reader = _open_reader(args.db)
        try:
            record = _select_run(reader, args)
            run_id = record.run_id
        finally:
            reader.close()
        store = lc.LifecycleStore(args.db)
        try:
            report = store.migrate_test_strength(
                run_id,
                apply=bool(getattr(args, "apply", False)),
                policy=policy,
                backup=not bool(getattr(args, "no_backup", False)),
            )
        except lc.LifecycleError as exc:
            return _refusal("RUN_TEST_STRENGTH_MIGRATION_FAILED", str(exc))
        finally:
            store.conn.close()
        payload = {
            "run_id": report.run_id,
            "test_strength_contract": report.contract,
            "applied": report.applied,
            "backup_path": report.backup_path,
            "policy": policy,
            "reason": report.reason,
            "blocked_nodes": list(report.blocked_nodes),
            "migrated_nodes": list(report.migrated_nodes),
            "findings": [
                {
                    "tests_node_id": item.tests_node_id,
                    "state": item.state,
                    "candidate_sha": item.candidate_sha,
                    "classification": item.classification,
                    "blocking": item.blocking,
                    "detail": dict(item.detail),
                }
                for item in report.findings
            ],
        }
    if getattr(args, "as_json", False):
        print(json.dumps(payload, sort_keys=True))
        return 0
    print("{}  contract {}".format(
        payload["run_id"], payload["test_strength_contract"]))
    if payload.get("backup_path"):
        print("  backup       {}".format(payload["backup_path"]))
    if payload.get("reason"):
        print("  {}".format(payload["reason"]))
    print("")
    print("  {:<44} {:<10} {:<32} {}".format(
        "TESTS NODE", "STATE", "CLASSIFICATION", "CANDIDATE"))
    for item in payload["findings"]:
        print("  {:<44} {:<10} {:<32} {}".format(
            item["tests_node_id"][:44],
            item["state"],
            item["classification"] + ("  (fenced)" if item["blocking"] else ""),
            (item["candidate_sha"] or "-")[:12],
        ))
    for node_id in payload.get("blocked_nodes") or ():
        print("  unadmitted dependant fenced: {}".format(node_id))
    return 0


def _reader_test_strength_audit(reader, run_id: str):
    """The audit, computed from a read-only handle.

    `LifecycleStore.legacy_test_strength_audit` is the definition; this
    reproduces it over `LifecycleReader` because opening the writable store
    for a read verb would create the ledger it was asked about and take a
    write lock on a database a live scheduler is transacting against. The two
    read the same rows and answer with the same classification names; keeping
    them in step is what `tests/test_test_gate_strength.py` asserts.
    """
    evidence = {
        (item.tests_node_id, item.candidate_sha): item
        for item in reader.test_gate_evidence(run_id, limit=10_000)
    }
    reviews = {
        (review.review_node_id, review.candidate_sha): review
        for review in reader.candidate_reviews(run_id, limit=10_000)
    }
    fenced = {
        block.tests_node_id
        for block in reader.legacy_test_strength_blocks(run_id, limit=10_000)
    }
    findings = []
    for node in reader.nodes(run_id):
        if node.kind != scheduler_types.NodeKind.TESTS.value:
            continue
        sha = node.output_sha
        item = evidence.get((node.node_id, sha)) if sha else None
        review = reviews.get(
            ("{0}::review".format(node.node_id), sha)) if sha else None
        reviewed = (
            review is not None
            and review.state is scheduler_types.CandidateReviewState.COMPLETED
            and review.verdict is scheduler_types.ReviewVerdict.PASS
        )
        strong = item is not None and item.strong
        if strong and reviewed:
            classification = "TEST_ACCEPTED"
        elif node.state in (
            scheduler_types.NodeState.MERGED,
            scheduler_types.NodeState.ACCEPTED,
        ):
            classification = lc.LEGACY_TEST_STRENGTH_UNPROVEN
        elif node.state in (
            scheduler_types.NodeState.BLOCKED,
            scheduler_types.NodeState.CANCELLED,
        ):
            classification = "TEST_TERMINAL_WITHOUT_MERGE"
        else:
            classification = "TEST_STRENGTH_PENDING"
        findings.append(
            lc.LegacyTestStrengthFinding(
                tests_node_id=node.node_id,
                state=node.state.value,
                candidate_sha=sha,
                classification=classification,
                blocking=node.node_id in fenced,
                detail={
                    "state": node.state.value,
                    "candidate_sha": sha,
                    "has_gate_evidence": item is not None,
                    "gate_evidence_strong": strong,
                    "independently_reviewed": reviewed,
                },
            )
        )
    return tuple(findings)


def _run_list(args: argparse.Namespace) -> int:
    """Every run in the ledger, newest first — the index into `--run-id`."""
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    digests = getattr(args, "plan_digests", None) or {}
    names = {digest: name for name, digest in digests.items()}
    selector = getattr(args, "selector", None)
    wanted = None
    if selector:
        wanted = digests.get(selector)
        if wanted is None:
            raise _RunSelectionError(selector + " is not an installed plan name")
    reader = _open_reader(args.db)
    try:
        records = reader.runs(wanted)
        rows = [
            {
                "run_id": record.run_id,
                "plan_name": names.get(record.plan_digest),
                "plan_digest": record.plan_digest,
                "created_at": record.created_at,
                "last_transition_at": record.last_transition_at,
                "declared_outcome": (
                    record.latest_outcome.value if record.latest_outcome else None
                ),
                "cancel_requested": record.cancel_requested,
                "scheduler_pid": record.scheduler_pid,
                "scheduler_alive": lc.scheduler_liveness(record),
                "state": _live_state(record, reader.nodes(record.run_id)),
            }
            for record in records
        ]
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(rows, sort_keys=True))
        return 0
    if not rows:
        print("no runs")
        return 0
    print(
        "{:<40} {:<28} {:<11} {:<11} {}".format(
            "RUN", "PLAN", "STATE", "DECLARED", "STARTED"
        )
    )
    for row in rows:
        print(
            "{:<40} {:<28} {:<11} {:<11} {}".format(
                row["run_id"],
                (row["plan_name"] or row["plan_digest"][:12])[:28],
                row["state"],
                row["declared_outcome"] or "-",
                row["created_at"],
            )
        )
    return 0


def _run_convergence(args: argparse.Namespace) -> int:
    """Findings per immutable candidate, from the durable review ledger.

    The scheduler report also carries this series for the process that ended a
    run. This command rebuilds it from ``lane_candidates`` and
    ``candidate_reviews``, so resume, process exit, and stale attempt extras
    cannot change the answer.
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    reader = _open_reader(args.db)
    try:
        record = _select_run(reader, args)
        # Read once and reused: the liveness derivation and the lane profile
        # must be two views of one observation, not two reads of a ledger a
        # scheduler is still writing to.
        nodes = reader.nodes(record.run_id)
        profile = review_convergence.run_convergence(
            record.run_id,
            nodes,
            reader.lane_candidates(record.run_id, limit=10_000),
            reader.candidate_reviews(record.run_id, limit=10_000),
            review_ceiling=getattr(args, "review_ceiling", None),
            in_flight=lc.run_in_flight(record, nodes),
        )
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(profile.as_dict(), sort_keys=True))
    else:
        print(review_convergence.render(profile))
    return 0


def _run_findings(args: argparse.Namespace) -> int:
    """Rejected candidate findings, read from the durable review ledger.

    Each entry is keyed by derived review node and immutable candidate SHA.
    Earlier rejections remain visible after a descendant passes; only the
    matching PASS candidate can authorize merge. Legacy attempt guidance is
    audit-only and is not consulted by this command.
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    reader = _open_reader(args.db)
    try:
        record = _select_run(reader, args)
        profile = review_findings.run_findings(
            record.run_id,
            reader.candidate_reviews(record.run_id, limit=10_000),
            declared_outcome=(
                record.latest_outcome.value if record.latest_outcome else None
            ),
        )
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(profile.as_dict(), sort_keys=True))
    else:
        print(review_findings.render(profile))
    return 0


def _run_pause(args: argparse.Namespace) -> int:
    """Stop the scheduler without making the run terminal (§7.3).

    SIGINT the claiming process. `KeyboardInterrupt` — or, once the scheduler
    has installed its handler, the latched pause — unwinds `_execute_run`'s
    `finally`, so the integration checkout is released. Nodes stay where they
    are, no outcome is declared, `latest_outcome` stays NULL, and `run resume`
    is legal against the run afterwards. A pause is therefore not a lifecycle
    transition at all, which is why it may be triggered by a signal (§1.2).

    The pid is signalled only when `scheduler_signal_pid` proves it is still
    the process that claimed the run — a float comparison of the recorded
    start epoch against the live process's. `scheduler_liveness` is a weak
    witness: the kernel reuses pids, so "a process with this number exists"
    is not authority to interrupt it, and a run whose identity cannot be
    proven is refused rather than signalled (#37).
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    reader = _open_reader(args.db)
    try:
        record = _select_run(reader, args)
        run_id = record.run_id
        pid = record.scheduler_pid
        alive = lc.scheduler_liveness(record)
        target = lc.scheduler_signal_pid(record)
    finally:
        reader.close()
    if not pid or alive is not True:
        print(
            json.dumps(
                {
                    "outcome": "ALREADY_STOPPED",
                    "run_id": run_id,
                    "scheduler_pid": pid,
                    "scheduler_alive": alive,
                },
                sort_keys=True,
            )
        )
        return 0
    if target is None:
        return _refusal(
            "PAUSE_PID_UNPROVEN",
            "recorded scheduler pid {0} exists but is not proven to be "
            "the process that claimed this run".format(pid),
        )
    try:
        os.kill(target, signal.SIGINT)
    except ProcessLookupError:
        print(
            json.dumps(
                {
                    "outcome": "ALREADY_STOPPED",
                    "run_id": run_id,
                    "scheduler_pid": pid,
                    "scheduler_alive": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except OSError as exc:
        return _refusal("PAUSE_SIGNAL_FAILED", str(exc))
    print(
        json.dumps(
            {
                "outcome": "PAUSE_REQUESTED",
                "run_id": run_id,
                "scheduler_pid": target,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_cancel(args: argparse.Namespace) -> int:
    """Two verbs behind one word, and the safe one is the default.

    Without `--discard` this pauses. `run cancel` was the operator's only
    stop control, so it was reached for both to stop a run for good and to
    stop one that was about to be resumed; making the destructive reading the
    default meant the second intent silently took the first. `--discard` is
    what now says "end it", and it is terminal: the nodes it takes are
    stamped `DISCARDED`, the declared outcome carries that cause, and
    `resume_run` refuses it.
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    if not getattr(args, "discard", False):
        return _run_pause(args)
    reader = _open_reader(args.db)
    try:
        run_id = _select_run(reader, args).run_id
    finally:
        reader.close()
    store = lc.LifecycleStore(args.db)
    try:
        outcome = store.latest_outcome(run_id)
        if outcome in (
            scheduler_types.RunOutcome.ACCEPTED,
            scheduler_types.RunOutcome.CANCELLED,
        ):
            return _refusal(
                "RUN_ALREADY_TERMINAL",
                "{0} already declared {1}".format(run_id, outcome.value),
            )
        # Named before the discard, not after: this is work that reached a
        # measured predicate and is about to stop being reachable, and the
        # operator is the only one who can decide that is acceptable.
        adoptable = store.adoptable_attempts(run_id)
        if adoptable:
            print(
                "cancel --discard will make these completed attempts "
                "unreachable through Maestro (§7.3): "
                + ", ".join(
                    "{0} ({1})".format(row["node_id"], row["why"]) for row in adoptable
                ),
                file=sys.stderr,
            )
        store.cancel_run(run_id, cause=scheduler_types.CancelCause.DISCARDED)
        store.declare_outcome(
            run_id, cancel_cause=scheduler_types.CancelCause.DISCARDED
        )
        print(
            json.dumps(
                {
                    "outcome": "CANCELLED",
                    "run_id": run_id,
                    "unreachable": list(adoptable),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        store.close()


def _run_amend(args: argparse.Namespace) -> int:
    """Adopt corrected plan bytes on a run in flight, keeping its merged work.

    The verb exists because a plan defect found mid-run used to cost every
    merged node: the run's plan was identified by the digest of a *file*, so
    `plan ship` correcting it made the run unresumable, and the only escape was
    abandoning it. `run_plan_versions` retains the bytes; this decides whether
    the new ones may be adopted over them.

    Amendment is its own verb rather than a flag on `resume`, and that is what
    keeps `resume` a bare single command. An operator amending a plan is making
    a decision about already-accepted work and should have to say so; an
    operator resuming is not, and should not have to acknowledge anything. The
    typed `plan-amended` transition is the record of the first, so §1.2 holds
    without a re-approval dance.
    """
    amended = _load_runnable_plan(args)
    reader = _open_reader(args.db)
    try:
        record = _select_run(reader, args)
        retained = reader.current_plan(record.run_id)
        states = {row.node_id: row.state for row in reader.nodes(record.run_id)}
    finally:
        reader.close()

    if retained is None:
        return _refusal(
            "RUN_PLAN_NOT_RETAINED",
            "{0} was created before its plan bytes were retained, so there is "
            "nothing to amend from. Resume it once against the plan file it "
            "still matches and the bytes are captured, or start a fresh run."
            .format(record.run_id),
        )
    current_digest, current_bytes = retained
    if current_digest == args.digest:
        return _refusal(
            "RUN_PLAN_UNCHANGED",
            "{0} already executes {1}".format(record.run_id, args.digest[:12]),
        )

    current = plan_model.parse_bytes(current_bytes)
    code, payload = _adjudicate_amendment(
        args.db,
        record.run_id,
        states,
        current.to_plan_nodes(),
        amended.to_plan_nodes(),
        args.digest,
        args.plan_bytes,
        current_merge_policy=current.merge_policy,
        amended_merge_policy=amended.merge_policy,
        current_schema=current.schema_version,
        amended_schema=amended.schema_version,
    )
    print(json.dumps(payload, sort_keys=True))
    return code


def _adjudicate_amendment(
    db_path,
    run_id: str,
    states,
    current_nodes,
    amended_nodes,
    digest: str,
    plan_bytes: bytes,
    *,
    current_merge_policy=None,
    amended_merge_policy=None,
    current_schema=None,
    amended_schema=None,
):
    """Decide and apply an amendment, returning `(exit_code, payload)`.

    Split from `_run_amend` so the decision is reachable without plan files,
    receipts and a configured installation — the shell above it binds those and
    does nothing else. That split is not tidiness: the first version of this
    verb read `args.database`, a name nothing binds, and would have raised
    `AttributeError` on its first real invocation. Nothing exercised it,
    because exercising it meant standing up the whole plan apparatus. A verb
    whose body cannot be reached by a test is a verb nobody has run.
    """
    verdict = plan_amendment.classify(
        current_nodes,
        amended_nodes,
        states,
        current_merge_policy=current_merge_policy,
        amended_merge_policy=amended_merge_policy,
        current_schema=current_schema,
        amended_schema=amended_schema,
    )
    if not verdict.amendable:
        return 3, {
            "outcome": "RUN_PLAN_AMENDMENT_REFUSED",
            "run_id": run_id,
            **verdict.as_mapping(),
        }

    amended_by_id = {node.node_id: node for node in amended_nodes}
    updates = {node_id: amended_by_id[node_id] for node_id in verdict.changed}
    additions = [amended_by_id[node_id] for node_id in verdict.added]
    store = lc.LifecycleStore(Path(db_path))
    try:
        seq = store.amend_run_plan(
            run_id, digest, plan_bytes, updates, additions,
            transfers=verdict.transfers,
        )
    finally:
        store.close()
    return 0, {
        "outcome": "RUN_PLAN_AMENDED",
        "run_id": run_id,
        "plan_digest": digest,
        "version": seq,
        **verdict.as_mapping(),
    }


def _run_resume(args: argparse.Namespace) -> int:
    # A manual, unconfigured resume still names its run positionally; the
    # configured path has already resolved one into `run_id` by here.
    if not getattr(args, "run_id", None) and getattr(args, "selector", None):
        args.run_id = args.selector
    try:
        return _execute_run(args, resuming=True)
    except _RunRefused as exc:
        return exc.emit()
    except launcher.HarnessQuiescenceError as exc:
        return _quiescence_refusal(exc)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RUN_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
    ) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except Exception as exc:
        return _refusal(
            "RUN_EXECUTION_FAILED", "{0}: {1}".format(type(exc).__name__, exc)
        )


def _escape(args: argparse.Namespace) -> int:
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        if args.command == "retry":
            row = store.retry(
                args.run_id,
                args.node_id,
                force=args.force,
                grant=getattr(args, "grant", 0) or 0,
            )
        elif args.command == "skip":
            row = store.skip(
                args.run_id,
                args.node_id,
                accept_sha=args.accept_sha,
                repo_path=args.repo,
            )
        else:
            row = store.abandon(args.run_id, args.node_id)
        print(json.dumps({"node_id": row.node_id, "state": row.state.value}))
        return 0
    finally:
        store.close()


def _parse_salvage_seed(value: object) -> bytes:
    if not value:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED", "--signing-seed is required"
        )
    try:
        seed = bytes.fromhex(str(value))
    except (TypeError, ValueError) as exc:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED", "signing seed must be hexadecimal"
        ) from exc
    if len(seed) != receipt_crypto.SEED_SIZE:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED", "signing seed must be a 32-byte Ed25519 seed"
        )
    return seed


def _attempt_salvage(args: argparse.Namespace) -> int:
    missing = [
        flag
        for flag, value in (
            ("--worktrees-root", getattr(args, "worktrees_root", None)),
            ("--scratch-root", getattr(args, "scratch_root", None)),
        )
        if not value
    ]
    if missing:
        return _refusal(
            "RUN_CONFIGURATION_REQUIRED",
            " and ".join(missing) + " is required outside a configured repository",
        )
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        result = salvage.salvage_attempt(
            store,
            run_id=args.run_id,
            node_id=args.node_id,
            attempt_no=args.attempt_no,
            repo=Path(args.repo),
            worktrees_root=Path(args.worktrees_root),
            scratch_root=Path(args.scratch_root),
            invoked_by=args.invoked_by,
            reason=args.reason,
            signing_seed=_parse_salvage_seed(args.signing_seed),
            record_dir=Path(args.record_dir),
        )
        print(
            json.dumps(
                {
                    "outcome": "SALVAGED",
                    "run_id": result.run_id,
                    "node_id": result.node_id,
                    "attempt_no": result.attempt_no,
                    "base_sha": result.base_sha,
                    "output_sha": result.output_sha,
                    "record": str(result.record_path),
                    "files": list(result.files),
                    # Printed on every salvage, including the empty and the unknown
                    # case, because an operator reading only the happy path is what
                    # this field exists to prevent: `[]` says no declared output was
                    # left behind, `null` says nobody could tell (#67).
                    "uncommittable_outputs": (
                        None
                        if result.uncommittable_outputs is None
                        else list(result.uncommittable_outputs)
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    except salvage.SalvageRefused as exc:
        payload = {"outcome": exc.outcome}
        payload.update(exc.fields)
        return _typed_refusal(payload, exc.detail)
    except lc.UnknownNode as exc:
        return _refusal("SALVAGE_ATTEMPT_ABSENT", str(exc))
    finally:
        store.close()


def _plan_contract_layout() -> Dict[str, Any]:
    """The repository layout the plan-contract pipeline derives every path from."""
    config_path = _installed_config_path()
    if not config_path.is_file():
        raise _MaestroConfigurationError(
            "the plan pipeline requires an installed " + str(_MAESTRO_CONFIG_FILE)
        )
    return _load_maestro_layout(config_path.parent.parent.resolve(), config_path)


def _plan_contract_path(layout: Dict[str, Any], name: str, suffix: str) -> Path:
    """<plans_dir>/<name><suffix>. An operator names the plan and nothing else."""
    path = (layout["plans_dir"] / (_named_plan_name(name) + suffix)).resolve()
    if not _path_is_within(path, layout["plans_dir"]):
        raise _MaestroConfigurationError(
            "plan contract artifact resolves outside plans_dir"
        )
    return path


def _plan_contract_artifacts(layout: Dict[str, Any], name: str) -> Dict[str, Path]:
    return {
        "plan_ir": _plan_contract_path(layout, name, _PLAN_CONTRACT_IR_SUFFIX),
        "rendered": _plan_contract_path(layout, name, _PLAN_CONTRACT_RENDERED_SUFFIX),
        "receipt": _plan_contract_path(layout, name, _PLAN_CONTRACT_RECEIPT_SUFFIX),
    }


def _planctl_script(candidate: Path) -> Path:
    """Accept either the planctl script itself or the skill directory holding it."""
    return candidate / _PLAN_CONTRACT_SCRIPT if candidate.is_dir() else candidate


def _resolve_planctl(layout: Dict[str, Any]) -> Path:
    """planctl's location, in search order. None of it is ever typed."""
    searched = []
    configured = layout.get("plan_contract")
    if configured is not None:
        searched.append(Path(configured))
    searched.append(layout["repo"] / _PLAN_CONTRACT_REPOSITORY_SKILL)
    environment = os.environ.get(_PLAN_CONTRACT_SKILL_ENV)
    if environment:
        searched.append(Path(environment))
    for candidate in searched:
        script = _planctl_script(candidate)
        if script.is_file():
            return script.resolve()
    raise _MaestroConfigurationError(
        "planctl is unavailable: set plan_contract in "
        + str(_MAESTRO_CONFIG_FILE)
        + ", install the plan-contract skill at "
        + str(_PLAN_CONTRACT_REPOSITORY_SKILL)
        + ", or export "
        + _PLAN_CONTRACT_SKILL_ENV
        + "; searched "
        + ", ".join(str(_planctl_script(item)) for item in searched)
    )


def _planctl_supports_repo_root(script: Path) -> bool:
    """Ask the installed planctl: the flag is pending upstream, so never assume."""
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "validate", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--repo-root" in (completed.stdout or "") + (completed.stderr or "")


def _planctl_repo_root(layout: Dict[str, Any], script: Path) -> Optional[Path]:
    """The --repo-root argument, or None when the installed planctl lacks it."""
    repo = layout["repo"]
    if _planctl_supports_repo_root(script):
        return repo
    if layout["plans_dir"] != repo:
        raise _MaestroConfigurationError(
            "the installed planctl has no --repo-root, so a Plan IR must sit at "
            "the repository root, but plans_dir is "
            + str(layout["plans_dir"])
            + "; install a planctl that supports --repo-root or set plans_dir "
            "to the repository root"
        )
    return None


def _reviewer_key_environment_names(layout: Dict[str, Any]) -> Tuple[str, ...]:
    """Every variable that could carry the reviewer key into this process."""
    configured = layout["key_env"].get("reviewer_hmac_key_env")
    names = [_REVIEWER_HMAC_KEY_ENV]
    if configured and configured not in names:
        names.append(configured)
    return tuple(names)


def _reviewer_keys_in_environment(layout: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(
        name for name in _reviewer_key_environment_names(layout) if os.environ.get(name)
    )


def _reviewer_hmac_key_file(layout: Dict[str, Any]) -> Path:
    return Path(layout["repository_state"]) / "keys" / _REVIEWER_HMAC_KEY_FILE


def _existing_reviewer_hmac_key(path: Path) -> str:
    try:
        existing = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise _MaestroConfigurationError(
            "the reviewer key at " + str(path) + " is unreadable; it is not "
            "replaced, because that would invalidate every receipt already "
            "signed with it"
        ) from exc
    if len(existing.encode("utf-8")) < _REVIEWER_HMAC_KEY_MINIMUM_BYTES:
        raise _MaestroConfigurationError(
            "the reviewer key at "
            + str(path)
            + " is shorter than "
            + str(_REVIEWER_HMAC_KEY_MINIMUM_BYTES)
            + " bytes; it is not "
            "regenerated, because that would invalidate every receipt already "
            "signed with it"
        )
    return existing


def _minted_reviewer_hmac_key(layout: Dict[str, Any]) -> str:
    """Reuse the key `maestro bootstrap` wrote; mint it here only if absent.

    Bootstrap is the primary minter, because the key is needed before these
    verbs are -- `/arch-review` drives `planctl review` through the skill
    directly, sourcing the env file bootstrap writes. Both paths produce the
    same 64-character hex in the same 0600 file, so whichever runs first wins
    and the other reuses it. Regenerating would silently invalidate every
    receipt already signed with the old key, so a present-but-unusable file is
    a refusal, never a fresh mint.
    """
    path = _reviewer_hmac_key_file(layout)
    if _path_is_within(path, layout["repo"]):
        raise _MaestroConfigurationError(
            "the reviewer key would be stored inside the repository at "
            + str(path)
            + "; state_root must resolve outside the repository"
        )
    for ancestor in (path.parent, *path.parent.parents):
        if (ancestor / ".git").exists():
            raise _MaestroConfigurationError(
                "the reviewer key would be stored inside the git work tree at "
                + str(ancestor)
                + "; point state_root outside every repository"
            )
    if path.exists():
        return _existing_reviewer_hmac_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path.parent), stat.S_IRWXU)
    minted = secrets.token_hex(_REVIEWER_HMAC_KEY_MINTED_BYTES)
    try:
        descriptor = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
        )
    except FileExistsError:
        return _existing_reviewer_hmac_key(path)
    except OSError as exc:
        raise _MaestroConfigurationError(
            "cannot create the reviewer key at " + str(path)
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(minted + "\n")
    except OSError as exc:
        raise _MaestroConfigurationError(
            "cannot write the reviewer key at " + str(path)
        ) from exc
    return minted


def _reviewer_hmac_key(layout: Dict[str, Any]) -> str:
    """Operator-supplied environment wins; the key Maestro minted is the default."""
    for name in _reviewer_key_environment_names(layout):
        supplied = os.environ.get(name)
        if supplied:
            if len(supplied.encode("utf-8")) < _REVIEWER_HMAC_KEY_MINIMUM_BYTES:
                raise _MaestroEnvironmentError(
                    name
                    + " must carry at least "
                    + str(_REVIEWER_HMAC_KEY_MINIMUM_BYTES)
                    + " bytes"
                )
            return supplied
    return _minted_reviewer_hmac_key(layout)


def _redacted(text: Optional[str], secret: Optional[str]) -> str:
    """No emitted byte may carry the reviewer key, whatever planctl printed."""
    value = text or ""
    if secret:
        value = value.replace(secret, "[redacted]")
    return value


class _PlanPaneUnavailable(RuntimeError):
    """No visible Herdr pane, so the verb refuses rather than working unseen."""


class _PlanPane:
    """A visible Herdr pane tailing one step log, so no work happens unseen.

    planctl is deterministic local computation, not an agent turn, so this uses
    Herdr's pane path only. No route receipt is minted, no admission is claimed,
    and no agent readiness is polled -- a signed receipt here would attest to
    nothing. The command bound to the pane only ever tails a log file, so the
    reviewer key can never reach the pane's argv, text, or scrollback; it is
    handed to planctl through the subprocess environment alone.

    There is no inline fallback. A pane that cannot be opened is a refusal
    raised before any step runs, because an escape hatch is how invisible
    execution comes back.
    """

    def __init__(self, layout: Dict[str, Any], log: Path) -> None:
        self._herdr = layout["executables"]["herdr"]
        self._cwd = layout["repo"]
        self._log = log
        self.log = log
        self.pane_id: Optional[str] = None
        self._reported_pane: Optional[str] = None

    def _call(self, *args: str) -> Dict[str, Any]:
        result = subprocess.run(
            [self._herdr, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "herdr failed").strip()[-200:]
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def open(self) -> None:
        """Prove the pane exists before a single artifact is written."""
        try:
            payload = self._call(
                "pane",
                "split",
                "--current",
                "--direction",
                "right",
                "--cwd",
                str(self._cwd),
                "--no-focus",
            )
            container = payload.get("result", payload)
            pane = container.get("pane") if isinstance(container, dict) else None
            if not isinstance(pane, dict) or not pane.get("pane_id"):
                raise RuntimeError("herdr opened no pane")
            pane_id = str(pane["pane_id"])
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise _PlanPaneUnavailable(
                "no visible Herdr pane ("
                + (str(exc) or type(exc).__name__)
                + "); start Herdr and rerun, or fix executables.herdr in "
                + str(_MAESTRO_CONFIG_FILE)
                + ". Nothing was rendered, reviewed, or authored."
            ) from exc
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            self._log.write_text("", encoding="utf-8")
            self._call("pane", "run", pane_id, "tail", "-n", "+1", "-f", str(self._log))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            self.pane_id = pane_id
            self.close()
            raise _PlanPaneUnavailable(
                "the Herdr pane could not stream this step ("
                + (str(exc) or type(exc).__name__)
                + "). Nothing was rendered, reviewed, or authored."
            ) from exc
        self.pane_id = pane_id
        self._reported_pane = pane_id

    def close(self) -> None:
        if self.pane_id is None:
            return
        try:
            self._call("pane", "close", self.pane_id)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        self.pane_id = None

    def note(self, text: str) -> None:
        """Append a line an operator watching the pane needs to see."""
        try:
            with self._log.open("a", encoding="utf-8") as handle:
                handle.write(text.rstrip("\n") + "\n")
        except OSError:
            pass

    def report(self) -> Dict[str, Any]:
        return {"pane": self._reported_pane, "log": str(self._log)}


def _plan_step_log(layout: Dict[str, Any], name: str, verb: str) -> Path:
    return Path(layout["repository_state"]) / "plan-contract" / name / (verb + ".log")


def _planctl_run(
    script: Path,
    layout: Dict[str, Any],
    repo_root: Optional[Path],
    verb: str,
    plan_ir: Path,
    arguments: Sequence[str],
    *,
    log: Path,
    reviewer_key: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """One planctl step, streamed into the pane's log, key in the environment only."""
    command = [sys.executable, str(script), verb, str(plan_ir)]
    command.extend(arguments)
    if repo_root is not None:
        command.extend(["--repo-root", str(repo_root)])
    command.append("--json")
    # The key reaches planctl through this mapping and nowhere else. An empty
    # value clears anything inherited, so a gate step can never sign anything.
    environment = {
        _PLAN_STEP_LOG_ENV: str(log),
        _REVIEWER_HMAC_KEY_ENV: reviewer_key if reviewer_key is not None else "",
    }
    argv = list(_PLAN_STEP_SHELL) + command
    before = log.stat().st_size if log.is_file() else 0
    try:
        completed = launcher.run_harness_process(
            argv, cwd=layout["repo"], env=environment
        )
    except (
        launcher.HarnessCancelled,
        launcher.HarnessQuiescenceError,
        TimeoutError,
        OSError,
    ) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))
    return subprocess.CompletedProcess(
        argv, completed.returncode, _log_tail(log, before), completed.stderr or ""
    )


def _log_tail(log: Path, offset: int) -> str:
    try:
        with log.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return ""


def _plan_contract_step_failure(
    outcome: str,
    step: str,
    completed: subprocess.CompletedProcess,
    pane: Dict[str, Any],
    secret: Optional[str] = None,
) -> int:
    """Name the step that failed and hand back planctl's diagnostics verbatim."""
    payload = {
        "outcome": outcome,
        "step": step,
        "status": completed.returncode,
        "stdout": _redacted(completed.stdout, secret),
        "stderr": _redacted(completed.stderr, secret),
    }
    payload.update(pane)
    print(json.dumps(payload, sort_keys=True))
    return 2


def _run_plan_contract(
    args: argparse.Namespace,
    verb: str,
    steps: Sequence[Tuple[str, Sequence[str]]],
    *,
    outcome: str,
    failure: str,
    plan_ir: Path,
    layout: Dict[str, Any],
    reviewer_key: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    """Run planctl steps in order in a visible pane, stopping at the first failure."""
    # Fail closed before any step runs: no pane, no work, no artifacts left over.
    pane = _PlanPane(layout, _plan_step_log(layout, args.plan_name, verb))
    pane.open()
    try:
        script = _resolve_planctl(layout)
        repo_root = _planctl_repo_root(layout, script)
        for step, arguments in steps:
            pane.note("$ planctl " + step + " " + plan_ir.name)
            completed = _planctl_run(
                script,
                layout,
                repo_root,
                step,
                plan_ir,
                arguments,
                log=pane.log,
                reviewer_key=reviewer_key,
            )
            if completed.returncode != 0:
                return _plan_contract_step_failure(
                    failure, step, completed, pane.report(), reviewer_key
                )
    finally:
        pane.close()
    payload = {
        "outcome": outcome,
        "plan": args.plan_name,
        "plan_ir": str(plan_ir),
        "planctl": str(script),
        "steps": [step for step, _arguments in steps],
    }
    payload.update(extra or {})
    payload.update(pane.report())
    print(json.dumps(payload, sort_keys=True))
    return 0


def _plan_gate(args: argparse.Namespace) -> int:
    """render + validate + mutate, on the author side, without the reviewer key."""
    layout = _plan_contract_layout()
    held = _reviewer_keys_in_environment(layout)
    if held:
        return _refusal(
            "REVIEWER_KEY_PRESENT",
            "plan gate refuses to run while the reviewer key is in its "
            "environment (" + ", ".join(held) + " is set); the author side must "
            "not hold the key that authorizes its own plan",
        )
    artifacts = _plan_contract_artifacts(layout, args.plan_name)
    plan_ir = artifacts["plan_ir"]
    if not plan_ir.is_file():
        return _refusal("PLAN_CONTRACT_IR_MISSING", "no Plan IR at " + str(plan_ir))
    rendered = artifacts["rendered"]
    return _run_plan_contract(
        args,
        "gate",
        (
            ("render", ("--out", str(rendered))),
            ("validate", ("--rendered", str(rendered))),
            ("mutate", ("--rendered", str(rendered))),
        ),
        outcome="PLAN_GATED",
        failure="PLAN_GATE_FAILED",
        plan_ir=plan_ir,
        layout=layout,
        extra={"rendered": str(rendered)},
    )


def _plan_review(args: argparse.Namespace) -> int:
    """review + validate --require-approved, holding the key Maestro owns."""
    layout = _plan_contract_layout()
    artifacts = _plan_contract_artifacts(layout, args.plan_name)
    plan_ir = artifacts["plan_ir"]
    if not plan_ir.is_file():
        return _refusal("PLAN_CONTRACT_IR_MISSING", "no Plan IR at " + str(plan_ir))
    rendered = artifacts["rendered"]
    if not rendered.is_file():
        return _refusal(
            "PLAN_CONTRACT_RENDER_MISSING",
            "no rendered plan at "
            + str(rendered)
            + "; run: maestro plan gate "
            + args.plan_name,
        )
    reviewer = layout["reviewer"]
    missing = [key for key in ("id", "vendor") if not reviewer.get(key)]
    if missing:
        return _refusal(
            "REVIEWER_IDENTITY_UNCONFIGURED",
            "reviewer."
            + " and reviewer.".join(missing)
            + " must be set in "
            + str(_MAESTRO_CONFIG_FILE),
        )
    receipt = artifacts["receipt"]
    return _run_plan_contract(
        args,
        "review",
        (
            (
                "review",
                (
                    "--rendered",
                    str(rendered),
                    "--receipt-out",
                    str(receipt),
                    "--reviewer",
                    reviewer["id"],
                    "--reviewer-vendor",
                    reviewer["vendor"],
                ),
            ),
            (
                "validate",
                (
                    "--rendered",
                    str(rendered),
                    "--receipt",
                    str(receipt),
                    "--require-approved",
                ),
            ),
        ),
        outcome="PLAN_REVIEWED",
        failure="PLAN_REVIEW_FAILED",
        plan_ir=plan_ir,
        layout=layout,
        reviewer_key=_reviewer_hmac_key(layout),
        extra={
            "rendered": str(rendered),
            "receipt": str(receipt),
            "reviewer": reviewer["id"],
            "reviewer_vendor": reviewer["vendor"],
        },
    )


def _plan_author_options() -> Dict[str, str]:
    """The author verb's options, so a pending flag is detected and never assumed."""
    parser = build_parser()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        plan_parser = action.choices.get("plan")
        if plan_parser is None:
            continue
        for plan_action in plan_parser._actions:
            if not isinstance(plan_action, argparse._SubParsersAction):
                continue
            author = plan_action.choices.get("author")
            if author is None:
                continue
            return {
                option: item.dest
                for item in author._actions
                for option in item.option_strings
            }
    return {}


def _configured_plan_step(plan_command: str, name: str) -> argparse.Namespace:
    """Exactly what `maestro plan <command> <name>` binds, resolved in process."""
    argv = ("plan", plan_command, name)
    args = build_parser().parse_args(list(argv))
    _apply_repository_config(args, argv)
    return args


class _PaneTee:
    """Stream a step's stdout to the operator's terminal and the pane log at once."""

    def __init__(self, stream, log: Path) -> None:
        self._stream = stream
        self._log = log

    def write(self, text: str) -> int:
        try:
            with self._log.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()


def _plan_ship_approved(args: argparse.Namespace) -> Callable[[str], bool]:
    """Whether a digest carries a signed receipt whose verdict is PASS.

    Read from the receipt store rather than from any file beside the plan: the
    receipt is the only thing that binds a reviewer to a set of bytes, and it
    is what a run consults.

    Fails closed. Only a receipt that is absent proves a plan is unapproved;
    a receipt that cannot be read, verified, or even located because the store
    is misconfigured proves nothing, and the caller uses this answer to decide
    whether to replace a plan's bytes. "I could not tell" must therefore read
    as approved, so an unreadable receipt costs a refusal the operator can act
    on rather than a plan silently replaced out from under a run.
    """

    def approved(digest: str) -> bool:
        try:
            store = _plan_receipt_store(
                _configured_plan_step("finalize", args.plan_name),
                missing_detail="receipt configuration is required to ship",
            )
        except (
            _PlanReceiptConfigurationError,
            _MaestroConfigurationError,
            finalization.ReceiptStoreLocationError,
            OSError,
            ValueError,
        ):
            # No store means no answer, and no answer means do not replace.
            return True
        try:
            return store.load(digest).verdict is finalization.Verdict.PASS
        except FileNotFoundError:
            return False
        except (
            finalization.ReceiptInvalid,
            finalization.SignatureMissing,
            finalization.SignatureInvalid,
            UnicodeError,
            ValueError,
            KeyError,
            OSError,
        ):
            return True

    return approved


class _ShipSupersedeRefused(RuntimeError):
    """The plan on disk differs from the projection and is already approved."""


def _superseded_plan_path(destination: Path, digest: str) -> Path:
    """Where a replaced plan's bytes are kept, keyed by their own digest."""
    return destination.parent / "superseded" / digest / destination.name


def _plan_ship_authoring(
    destination: Path, projected: bytes, approved: Callable[[str], bool]
) -> Optional[str]:
    """Decide what the ship's author step must do, and make room for it.

    Returns the digest of a plan that was superseded, or None when nothing was
    moved. Raises `_ShipSupersedeRefused` when the existing plan must not be
    replaced.

    `plan ship` is one command by design, and a command the operator cannot
    re-run is not one command -- it is one command plus a manual `rm`. Its
    author step is create-once (`PLAN_EXISTS`), which is right for a plan's
    bytes and wrong for a pipeline that has to be resumable: a ship whose
    finalize step failed refused on the file its own author step had just
    written. Three cases, decided from bytes and receipts rather than from
    intent:

    * nothing on disk -- author writes, as before;
    * identical bytes -- the author step already ran and produced exactly this
      plan, so it is skipped and the ship resumes at validate. Re-running is
      then free and changes nothing;
    * different bytes -- the IR moved. The existing plan is kept under its own
      digest and the new one takes its place, unless that plan is approved.

    An approved plan is never replaced. `Verdict.PASS` means a receipt exists
    over those exact bytes, and a run keys on the digest, so replacing the file
    could pull the plan out from under work that refers to it. `FAIL` carries
    no such risk -- it is terminal for those bytes, so nothing may ever run
    them -- and neither does a plan with no receipt at all.

    This guard bites harder than it used to, and deliberately. While a plan
    could FAIL review, a finalized plan was sometimes replaceable; now that
    `plan finalize` is deterministic, every finalized plan is PASS, so any
    re-ship whose IR moved is refused. The route back is to remove the plan
    directory once no live run holds its bytes -- which is exactly what
    `maestro deliver` does on a re-ship, and what the refusal below names.
    `plan set-aside` is NOT that route: it reopens a FAIL and refuses a PASS.
    """
    if not destination.exists():
        return None
    existing = destination.read_bytes()
    if existing == projected:
        return None
    superseded = plan_digest.digest_of(existing)
    if approved(superseded):
        raise _ShipSupersedeRefused(
            "the plan on disk ({}) carries a PASS receipt and differs from "
            "the projected plan; every finalized plan is PASS now that "
            "`plan finalize` is deterministic, so re-shipping moved bytes "
            "means removing this plan's directory once no live run holds it "
            "(`maestro deliver` does that on a re-ship). `plan set-aside` "
            "cannot help here: it reopens a FAIL and refuses a PASS.".format(superseded)
        )
    archive = _superseded_plan_path(destination, superseded)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(existing)
    destination.unlink()
    return superseded


def _plan_ship_step_failure(step: str, status: int, pane: Dict[str, Any]) -> int:
    payload = {"outcome": "PLAN_SHIP_FAILED", "step": step, "status": status}
    payload.update(pane)
    print(json.dumps(payload, sort_keys=True))
    return status


def _plan_ship(args: argparse.Namespace) -> int:
    """plan author --from-plan-contract + plan validate + plan finalize.

    The finalize step is the only agent work here: it drives the independent
    reviewer through the existing Herdr launcher and admitted route receipts,
    untouched. Author and validate are local computation, shown in the pane.
    """
    layout = _plan_contract_layout()
    artifacts = _plan_contract_artifacts(layout, args.plan_name)
    for label, outcome, remedy in (
        ("plan_ir", "PLAN_CONTRACT_IR_MISSING", ""),
        (
            "rendered",
            "PLAN_CONTRACT_RENDER_MISSING",
            "; run: maestro plan gate " + args.plan_name,
        ),
        (
            "receipt",
            "PLAN_CONTRACT_RECEIPT_MISSING",
            "; run: maestro plan review " + args.plan_name,
        ),
    ):
        if not artifacts[label].is_file():
            return _refusal(
                outcome,
                "no "
                + label.replace("_", " ")
                + " at "
                + str(artifacts[label])
                + remedy,
            )
    options = _plan_author_options()
    required = (
        _PLAN_CONTRACT_AUTHOR_OPTION,
        _PLAN_CONTRACT_RECEIPT_OPTION,
        _PLAN_CONTRACT_RENDERED_OPTION,
    )
    if any(option not in options for option in required):
        return _refusal(
            "PLAN_CONTRACT_INGRESS_UNAVAILABLE",
            "the installed plan author verb has no "
            + _PLAN_CONTRACT_AUTHOR_OPTION
            + ", so an approved Plan IR cannot be projected onto a Maestro plan",
        )
    author_args = _configured_plan_step("author", args.plan_name)
    setattr(
        author_args, options[_PLAN_CONTRACT_AUTHOR_OPTION], str(artifacts["plan_ir"])
    )
    setattr(
        author_args, options[_PLAN_CONTRACT_RECEIPT_OPTION], str(artifacts["receipt"])
    )
    setattr(
        author_args, options[_PLAN_CONTRACT_RENDERED_OPTION], str(artifacts["rendered"])
    )

    destination = Path(author_args.plan_file)

    # Fail closed before authoring anything: no pane, no work, no leftovers.
    # The projection and the supersede both live *inside* the pane, not before
    # it. `_plan_ship_authoring` archives and unlinks the plan on disk, and a
    # command that moves a plan's bytes while no pane exists is precisely the
    # invisible work `VisiblePaneTest` forbids -- it was hoisted above the pane
    # when ship was made resumable and has to come back down.
    pane = _PlanPane(layout, _plan_step_log(layout, args.plan_name, "ship"))
    pane.open()
    try:
        # What the author step would write, computed before anything is
        # written, so a re-run can tell "already authored" from "the IR moved"
        # -- see `_plan_ship_authoring`. Projection failures are the author
        # step's own refusals and are reported as that step failing, not as a
        # ship that never started.
        try:
            projected, _draft, _ir = plan_contract_ingress.project_canonical_plan(
                artifacts["plan_ir"],
                artifacts["receipt"],
                Path(author_args.repo),
                artifacts["rendered"],
            )
        except (plan_author.AuthoringError, plan_contract_ingress.IngressError) as exc:
            return _refusal("PLAN_AUTHORING_FAILED", str(exc))
        try:
            superseded = _plan_ship_authoring(
                destination, projected, _plan_ship_approved(args)
            )
        except _ShipSupersedeRefused as exc:
            return _refusal("PLAN_SUPERSEDE_REFUSED", str(exc))
        except OSError as exc:
            return _refusal("PLAN_SUPERSEDE_FAILED", str(exc))
        authored_already = destination.exists()
        steps = (("validate", _plan_validate, None), ("finalize", _plan_finalize, None))
        if not authored_already:
            steps = (("author", _plan_author, author_args),) + steps
        for step, handler, prepared in steps:
            pane.note("$ maestro plan " + step + " " + args.plan_name)
            with _redirect_stdout(_PaneTee(sys.stdout, pane.log)):
                status = int(
                    handler(
                        prepared
                        if prepared is not None
                        else _configured_plan_step(step, args.plan_name)
                    )
                )
            if status != 0:
                return _plan_ship_step_failure(step, status, pane.report())
    finally:
        pane.close()
    payload = {
        "outcome": "PLAN_SHIPPED",
        "plan": args.plan_name,
        "plan_ir": str(artifacts["plan_ir"]),
        "receipt": str(artifacts["receipt"]),
        "steps": [step for step, _handler, _prepared in steps],
        "authored": not authored_already,
        "superseded": superseded,
    }
    payload.update(pane.report())
    print(json.dumps(payload, sort_keys=True))
    return 0


# ── `maestro deliver` ───────────────────────────────────────────────────────
# One verb for the sequence `docs/plan-authoring.md` documents: a source
# document becomes an architecture anchor, the anchor becomes N brownfield work
# packages, each package is gated, reviewed, and shipped, and each shipped
# package is then RUN -- sequentially, in dependency order, halting at the
# first run that is not ACCEPTED. `--no-run` stops after shipping and reports
# the commands instead.


def _deliver_config() -> Dict[str, Any]:
    """The plan-contract layout with route keys bound, which the lane needs."""
    config_path = _installed_config_path()
    if not config_path.is_file():
        raise _MaestroConfigurationError(
            "maestro deliver requires an installed " + str(_MAESTRO_CONFIG_FILE)
        )
    return _load_maestro_config(config_path.parent.parent.resolve(), config_path)


def _deliver_author_lane(config: Dict[str, Any]) -> deliver_module.AuthorLane:
    configured = config.get("author")
    if not configured:
        raise _MaestroConfigurationError(
            "maestro deliver requires an author: block in "
            + str(_MAESTRO_CONFIG_FILE)
            + " naming the route, model, effort, "
            "and timeouts of the authoring lane"
        )
    return deliver_module.AuthorLane(**configured)


def _deliver_runner(config: Dict[str, Any]) -> launcher.HerdrLauncher:
    """The authoring lane rides the same admitted-route launcher as every
    other agent turn. No new trust material is minted for it."""
    route_keys = (bytes.fromhex(config["route_verify_key"]),)
    admitted = route_receipts.load_admitted_routes(
        dict(config["route_paths"]), verify_keys=route_keys
    )
    return launcher.HerdrLauncher(
        herdr_path=Path(config["executables"]["herdr"]),
        omp_path=Path(config["executables"]["omp"]),
        claude_path=Path(config["executables"]["claude"]),
        admitted_routes=admitted,
    )


def _deliver_close_pane(config: Dict[str, Any], pane_id: Optional[str]) -> None:
    """Close the pane whatever the launcher's proof said.

    `cancel` already closes and proves absence, but a quiescence failure
    raises before the close is retried, and a pane left open is the one
    failure an operator sees every single time. This runs after it,
    unconditionally and best-effort.
    """
    if not pane_id:
        return
    try:
        subprocess.run(
            [config["executables"]["herdr"], "pane", "close", pane_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _deliver_author_turn(
    config: Dict[str, Any],
    lane: deliver_module.AuthorLane,
    runner: launcher.HerdrLauncher,
    session_root: Path,
):
    """One opus authoring turn, ending when the lane writes its envelope."""

    def turn(kind: str, prompt: str, envelope: Path) -> Dict[str, Any]:
        token = "deliver-" + kind + "-" + uuid.uuid4().hex[:8]
        session_dir = session_root / token
        prompt_path = session_root / (token + ".prompt.md")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        envelope.parent.mkdir(parents=True, exist_ok=True)
        # `poll` ends the turn on the envelope's presence, so a stale envelope
        # from a previous attempt would end this one before it began.
        if envelope.exists():
            envelope.unlink()
        # §8.3: the author writes in a pane like any other agent, so its
        # byproducts are redirected to a scratch beside this turn's own session
        # directory under the harness-owned session root -- never into the
        # repository it is authoring against.
        # What this does NOT buy, stated because the other two sites do buy
        # it: the plan-author lane runs outside any scheduler attempt, so
        # there is no containment handler here, no retry class, and no budget
        # to spend correctly. It gains a typed error surface and a detail
        # string, and nothing more. It is wrapped so that every `.launch(` in
        # this module goes through one path, which is what lets the structural
        # guard be a flat rule rather than a rule plus an exception.
        handle = _typed_launch_pane(
            runner,
            launcher.LaunchSpec(
                correlation_token=token,
                worktree=Path(config["repo"]),
                prompt_path=prompt_path,
                envelope_path=envelope,
                route=lane.route,
                model=lane.model,
                effort=lane.effort,
                profile=lane.profile,
                session_dir=session_dir,
                # B13 at the chokepoint, stated here rather than skipped. The
                # author lane runs the `claude` route, which publishes no model
                # catalog -- `opus` does not resolve in omp's -- so this resolves
                # to `None` and `preflight_launch_prompt` makes no comparison.
                # That is the answer for a route with no declared window: refuse
                # to invent one, and refuse nothing. It is a property of the route
                # (`handoff_budget.ROUTES_PUBLISHING_A_WINDOW`), so the day the
                # route publishes a catalog this site is covered with no edit.
                context_window_tokens=_route_context_window(lane.route, lane.model),
                pane_role="author",
                restrict_tools=bool(
                    config.get("execution", {}).get("restrict_actor_tools", False)
                ),
                environment=worktree.launch_env(
                    session_root / (token + ".scratch"),
                    concurrency=config.get("execution", {}).get("concurrency"),
                ),
            ),
        )

        deadline = time.monotonic() + lane.author_timeout_s
        state = None
        try:
            while time.monotonic() < deadline:
                state = runner.poll(handle)
                if state.state in (launcher.PollState.EXITED, launcher.PollState.GONE):
                    break
                time.sleep(lane.poll_interval_s)
        finally:
            try:
                runner.cancel(handle, time.monotonic() + 5.0)
            except (launcher.HarnessQuiescenceError, RuntimeError, OSError):
                pass
            _deliver_close_pane(config, handle.pane_id)
        if not envelope.is_file():
            raise deliver_module.DeliverError(
                "AUTHOR_LANE_NO_ENVELOPE:{}:{}".format(
                    kind, state.detail if state is not None else "TIMEOUT"
                )
            )
        try:
            payload = json.loads(envelope.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise deliver_module.DeliverError(
                "AUTHOR_LANE_ENVELOPE_UNPARSED:{}".format(kind)
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("summary") or "")
            raise deliver_module.DeliverError(
                "AUTHOR_LANE_FAILED:{}:{}".format(kind, detail)
            )
        return payload

    return turn


def _deliver_json_lines(text: str):
    payloads = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def _deliver_plan_step(verb: str, name: str):
    """`maestro plan <verb> <name>` in process, tee'd and parsed.

    The operator sees each step's output exactly as if they had typed it; the
    parsed payloads are what the repair loop reads its findings out of.
    """
    handlers = {"gate": _plan_gate, "review": _plan_review, "ship": _plan_ship}
    stream = io.StringIO()
    with _redirect_stdout(stream):
        status = int(handlers[verb](_configured_plan_step(verb, name)))
    text = stream.getvalue()
    sys.stdout.write(text)
    return status, _deliver_json_lines(text)


def _deliver_run_start(name: str):
    """`maestro run start <name>`, resolved exactly as an operator's would be.

    The run id, worktree roots, and execution bounds all come from
    `_apply_repository_config`, untouched: `deliver` starting a run must be
    indistinguishable from an operator starting the same one.
    """
    argv = ("run", "start", name)
    args = build_parser().parse_args(list(argv))
    _apply_repository_config(args, argv)
    stream = io.StringIO()
    with _redirect_stdout(stream):
        status = int(_run_start(args))
    text = stream.getvalue()
    sys.stdout.write(text)
    return status, _deliver_json_lines(text)


def _deliver_plan_bytes(config: Dict[str, Any], name: str) -> Optional[bytes]:
    plan_file = config["plans_dir"] / _named_plan_name(name) / "maestro-plan.v1"
    if not plan_file.is_file():
        return None
    return plan_file.read_bytes()


def _deliver_shipped(config: Dict[str, Any], name: str) -> bool:
    """True when this package's CURRENT bytes already carry a PASS receipt.

    Keyed on the digest of the bytes on disk, so a re-authored package is
    never mistaken for the approved one it replaced.
    """
    stored = _deliver_plan_bytes(config, name)
    if stored is None:
        return False
    try:
        store = finalization.ReceiptStore(
            str(config["receipt_dir"]),
            repo_paths=(config["repo"],),
            data_dir=str(config["data_dir"]),
            verify_keys=(bytes.fromhex(config["verify_key"]),),
            create=False,
        )
        receipt = store.load(plan_digest.digest_of(stored))
    except (
        finalization.ReceiptInvalid,
        finalization.SignatureMissing,
        finalization.SignatureInvalid,
        finalization.ReceiptStoreLocationError,
        receipt_crypto.KeyMaterialError,
        KeyError,
        OSError,
        ValueError,
    ):
        return False
    return receipt.verdict is finalization.Verdict.PASS


def _deliver_accepted_run(config: Dict[str, Any], name: str) -> Optional[str]:
    """An ACCEPTED run for these exact plan bytes, if one already happened.

    This is what makes a resumed `deliver` cheap: package 3 of 5 halting must
    not re-run packages 1 and 2, and the lifecycle store already records, per
    plan digest, that their runs reached ACCEPTED.
    """
    stored = _deliver_plan_bytes(config, name)
    database = config["database"]
    if stored is None or not Path(database).is_file():
        return None
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(database), uri=True, timeout=5.0
        )
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT run_id FROM runs WHERE plan_digest=? AND latest_outcome=?"
            " ORDER BY latest_outcome_at DESC LIMIT 1",
            (plan_digest.digest_of(stored), scheduler_types.RunOutcome.ACCEPTED.value),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def _deliver_blocked_lanes(config: Dict[str, Any], run_id: str):
    """Every lane that did not merge: which lane, which attempt, and why.

    Attempt and reason both come from the lifecycle store rather than the run
    report, because the report names the node and the reason but not the
    attempt the ceiling was reached on, and "on which attempt" is half of what
    a halted operator needs.
    """
    database = config["database"]
    if not Path(database).is_file():
        return ()
    store = lc.LifecycleStore(str(database))
    try:
        rows = []
        for record in store.node_records(run_id):
            node = store.get_node(run_id, record.node_id)
            if node.state is scheduler_types.NodeState.MERGED:
                continue
            rows.append(
                {
                    "lane": record.node_id,
                    "state": node.state.value,
                    "attempt": node.attempt_no,
                    "reason": (node.block_reason.value if node.block_reason else None),
                }
            )
        return tuple(rows)
    except (lc.LifecycleError, sqlite3.Error, KeyError):
        return ()
    finally:
        store.close()


def _deliver_release_run(config: Dict[str, Any], name: str, discard_live: bool = False):
    """Free the integration branch a previous run's worktree still holds.

    Deliberately narrow. `_execute_run` now releases the checkout it added on
    every path out of the run, so a fresh leak is no longer produced. What
    remains is the backlog: worktrees stranded by runs that predate that
    release, or by a release that failed and said so on stderr. Either one
    refuses the next run with INTEGRATION_BRANCH_CHECKED_OUT until something
    hands the branch back, and this verb is that something.

    Only a worktree inside this repository's own run root is ever removed. An
    operator's checkout of the same branch is left exactly where it is, so
    `run start` still refuses with its own message and explains itself; a verb
    that deleted an operator's worktree to get its own work started would be a
    far worse bug than the one it is working around. That boundary is
    `_reclaim_stranded_integration_worktree`, shared with `run start`, which
    now applies it to the backlog itself before it refuses -- there is one
    rule about whose worktree may be removed and one place it is written.

    "Backlog" is the whole justification for the verb, and until the state
    check below existed nothing held the code to it: a run that sat BLOCKED
    for hours with seven of twelve nodes MERGED, its integration checkout
    holding those merges, was removed on exactly the same terms as a run that
    finished last week. That is the second predicate now -- the run's own
    recorded outcome, read from the lifecycle store (§1.2), never from this
    docstring's description of what the verb is for.

    Refusing is not failing. The verb returns the paths it released, and it
    released none, so it answers `()` and says on stderr which run held the
    branch and in what state. `--discard-live-runs` is how an operator says
    they meant it.
    """
    stored = _deliver_plan_bytes(config, name)
    if stored is None:
        return ()
    try:
        branch = plan_model.parse_bytes(stored).merge_policy.integration_branch
    except (ValueError, KeyError):
        return ()
    try:
        released = _reclaim_stranded_integration_worktree(
            Path(config["repo"]),
            config["repository_state"] / "runs",
            branch,
            config.get("database"),
            discard_live=discard_live,
        )
    except _RunStateStillHeld as held:
        # The escape is named by the verb that offers it. `run start` reaches
        # the same refusal and has no such flag, so the shared message cannot
        # carry one -- an escape an operator cannot type is worse than none.
        print(
            "RELEASE_REFUSED_RUN_NOT_OVER: "
            + str(held)
            + ", or pass --discard-live-runs to discard it deliberately",
            file=sys.stderr,
        )
        return ()
    return () if released is None else (str(released),)


def _plan_runs_not_over(
    config: Dict[str, Any], name: str
) -> Tuple[Tuple[str, str], ...]:
    """Runs against these exact plan bytes that an operator can still resume.

    Keyed by `plan_digest`, the same join `_deliver_accepted_run` uses, because
    that is what binds a run to the bytes rather than to the plan's name: a
    re-authored plan under the same name is different bytes and different runs.

    Every unreadable case answers "there are runs" by refusing to answer at
    all -- an existing ledger that will not open yields a single synthetic row
    rather than an empty tuple, so the caller fails closed. A ledger that does
    not exist yields nothing, because a run cannot be recorded in a store that
    was never written.
    """
    stored = _deliver_plan_bytes(config, name)
    database = config.get("database")
    if stored is None or database is None or not Path(database).is_file():
        return ()
    unreadable = (("(unidentified)", "unreadable lifecycle ledger"),)
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(database), uri=True, timeout=5.0
        )
    except sqlite3.Error:
        return unreadable
    try:
        rows = connection.execute(
            "SELECT run_id FROM runs WHERE plan_digest=?",
            (plan_digest.digest_of(stored),),
        ).fetchall()
    except sqlite3.Error:
        return unreadable
    finally:
        connection.close()
    held = []
    for row in rows:
        run_id = str(row[0])
        over, state = _recorded_run_is_over(database, run_id)
        if not over:
            held.append((run_id, state))
    return tuple(held)


def _deliver_remove_plan(
    config: Dict[str, Any], name: str, discard_live: bool = False
) -> None:
    """`plan author` is create-once, so a re-ship starts from no plan.

    Bounded to `plans_dir` by the same check every other derived path uses: a
    verb that deletes directories may not be talked into deleting one outside
    the tree it owns.

    Bounded in time by the same predicate the release verb applies to a
    worktree, and for the same reason. Containment answers *whose* directory
    this is; it says nothing about whether anything still needs it. A run is
    bound to plan bytes by their digest -- `_deliver_accepted_run` finds an
    already-ACCEPTED run that way, and it is how a resumed `deliver` knows not
    to redo a package -- so deleting the bytes while a resumable run is keyed
    to them destroys the run's resumption evidence and not merely a plan the
    author is about to rewrite.

    Raised as a `DeliverError` because that is the failure `_deliver` already
    knows how to turn into a refusal, and a re-ship that cannot clear the plan
    directory has not "partly" failed -- it must not proceed to author over
    bytes a live run is standing on.
    """
    directory = (config["plans_dir"] / _named_plan_name(name)).resolve()
    if not _path_is_within(directory, config["plans_dir"]):
        raise _MaestroConfigurationError(
            "plan directory resolves outside plans_dir: " + name
        )
    if not discard_live:
        held = _plan_runs_not_over(config, name)
        if held:
            raise deliver_module.DeliverError(
                "PLAN_BYTES_HELD_BY_RUN: " + name + "'s plan bytes are the "
                "bytes of "
                + ", ".join(run_id + " (" + state + ")" for run_id, state in held)
                + ", which is resumable. Removing the plan directory would "
                "destroy the digest that run is found by. Resume or cancel "
                "that run, or pass --discard-live-runs to discard it "
                "deliberately"
            )
    if directory.is_dir():
        shutil.rmtree(directory)


def _deliver(args: argparse.Namespace) -> int:
    config = _deliver_config()
    lane = _deliver_author_lane(config)
    spec = Path(args.spec)
    resolved_spec = (
        spec if spec.is_absolute() else (Path(config["repo"]) / spec)
    ).resolve()
    if not resolved_spec.is_file():
        return _refusal(
            "DELIVER_SPEC_MISSING", "no source document at " + str(resolved_spec)
        )
    if not _path_is_within(resolved_spec, config["repo"]):
        return _refusal(
            "DELIVER_SPEC_OUTSIDE_REPOSITORY",
            "a source document is pinned by repository-relative path, and "
            + str(resolved_spec)
            + " is outside "
            + str(config["repo"]),
        )
    relative = str(resolved_spec.relative_to(Path(config["repo"]).resolve()))

    session_root = config["repository_state"] / "deliver"
    discard_live = bool(getattr(args, "discard_live_runs", False))
    runner = _deliver_runner(config)
    delivery = deliver_module.Delivery(
        spec=relative,
        repo=Path(config["repo"]),
        # The same derivation `plan gate`, `plan review`, and `plan ship` use.
        # An IR written anywhere else is an IR those verbs never see.
        plans_dir=config["plans_dir"],
        envelope_dir=session_root,
        author_turn=_deliver_author_turn(config, lane, runner, session_root),
        plan_step=_deliver_plan_step,
        request=args.request or "",
        max_attempts=args.max_attempts,
        remove_plan_dir=lambda name: _deliver_remove_plan(config, name, discard_live),
        run_start=None if args.no_run else _deliver_run_start,
        accepted_run=lambda name: _deliver_accepted_run(config, name),
        blocked_lanes=lambda run_id: _deliver_blocked_lanes(config, run_id),
        release_run=lambda name: _deliver_release_run(config, name, discard_live),
        shipped=lambda name: _deliver_shipped(config, name),
        ledger_path=session_root / (_deliver_ledger_name(relative)),
    )
    try:
        payload = delivery.run()
    except deliver_module.DeliverError as exc:
        return _refusal("DELIVER_FAILED", str(exc))
    print(json.dumps(payload, sort_keys=True))
    # DELIVERED_NOT_RUN is `--no-run` doing exactly what it was asked to do.
    return 0 if payload["outcome"] in ("DELIVERED", "DELIVERED_NOT_RUN") else 2


def _deliver_ledger_name(spec: str) -> str:
    """One ledger per source document, named from it and never by it."""
    return "packages-" + plan_digest.digest_of(spec.encode("utf-8"))[:16] + ".json"


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
    return [
        {
            "base_commit": spec.base_commit,
            "mode": spec.mode.value,
            "plan_digest": spec.plan_digest,
            "repository_id": spec.repository_id,
            "target_branch": spec.target_branch,
        }
        for spec in plan.repositories
    ]


def _load_workspace_manifest(
    args: argparse.Namespace,
) -> Tuple[Path, workspace_model.WorkspacePlan, str]:
    manifest_file = Path(args.manifest_file).resolve()
    stored = manifest_file.read_bytes()
    plan = workspace_model.parse_bytes(stored)
    if stored != workspace_canonical.canonicalize_workspace(plan):
        raise _WorkspaceNotCanonical(
            "workspace manifest must contain canonical WorkspacePlan bytes"
        )
    return manifest_file.parent, plan, workspace_digest.digest_of(stored)


def _hex_material(value: str, label: str) -> bytes:
    try:
        material = bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise _WorkspaceConfigurationError(
            "{0} must be hexadecimal".format(label)
        ) from exc
    if len(material) != receipt_crypto.PUBLIC_KEY_SIZE:
        raise _WorkspaceConfigurationError(
            "{0} must contain exactly {1} bytes".format(
                label, receipt_crypto.PUBLIC_KEY_SIZE
            )
        )
    return material


def _verify_keys(args: argparse.Namespace) -> Tuple[bytes, ...]:
    keys = tuple(_hex_material(value, "verify key") for value in args.verify_key)
    if not keys:
        raise _WorkspaceConfigurationError("at least one verify key is required")
    return keys


def _participant_boundaries(
    manifest_dir: Path, plan: workspace_model.WorkspacePlan
) -> Tuple[Path, ...]:
    return tuple((manifest_dir / spec.path).resolve() for spec in plan.repositories)


def _workspace_receipt_store(
    args: argparse.Namespace,
    manifest_dir: Path,
    plan: workspace_model.WorkspacePlan,
    verify_keys: Tuple[bytes, ...],
    *,
    signing_seed: Optional[bytes] = None,
) -> workspace_receipt.WorkspaceReceiptStore:
    return workspace_receipt.WorkspaceReceiptStore(
        args.workspace_receipt_dir,
        participant_repos=_participant_boundaries(manifest_dir, plan),
        data_dir=args.data_dir,
        verify_keys=verify_keys,
        signing_seed=signing_seed,
        create=signing_seed is not None,
    )


def _plan_receipt_loader(
    args: argparse.Namespace,
    manifest_dir: Path,
    plan: workspace_model.WorkspacePlan,
    verify_keys: Tuple[bytes, ...],
):
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
                "no participant receipt store for {0}".format(plan_digest)
            )
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
        "targets": []
        if intent is None
        else [
            {
                "accepted_sha": target.accepted_sha,
                "candidate_branch": target.candidate_branch,
                "expected_base_sha": target.expected_base_sha,
                "remote_repository": target.remote_repository,
                "remote_url": target.remote_url,
                "repository_id": target.repository_id,
            }
            for target in intent.targets
        ],
        "steps": [
            {
                "detail": dict(step.detail),
                "from_state": step.from_state.value,
                "repository_id": step.repository_id,
                "step_id": step.step_id,
                "to_state": step.to_state.value,
            }
            for step in steps
        ],
    }


def _workspace_author(args: argparse.Namespace) -> int:
    try:
        stored = workspace_author.author_from_draft(
            Path(args.from_file), Path(args.out), Path(args.root)
        )
    except workspace_author.WorkspaceAuthoringError as exc:
        return _workspace_refusal("WORKSPACE_AUTHORING_FAILED", str(exc))
    _workspace_emit(
        {
            "digest": workspace_digest.digest_of(stored),
            "outcome": "WORKSPACE_AUTHORED",
            "workspace": str(Path(args.out)),
        }
    )
    return 0


def _workspace_validate(args: argparse.Namespace) -> int:
    _manifest_dir, plan, digest = _load_workspace_manifest(args)
    _workspace_emit(
        {
            "digest": digest,
            "outcome": "VALID",
            "participants": _workspace_participants(plan),
        }
    )
    return 0


def _workspace_finalize(args: argparse.Namespace) -> int:
    manifest_dir, plan, digest = _load_workspace_manifest(args)
    verify_keys = _verify_keys(args)
    signing_seed = _hex_material(args.signing_seed, "signing seed")
    store = _workspace_receipt_store(
        args, manifest_dir, plan, verify_keys, signing_seed=signing_seed
    )
    receipt = workspace_receipt.finalize(
        digest, plan, _plan_receipt_loader(args, manifest_dir, plan, verify_keys), store
    )
    _workspace_emit(
        {
            "digest": receipt.workspace_digest,
            "outcome": "FINALIZED",
            "participants": [
                participant.to_mapping() for participant in receipt.participants
            ],
        }
    )
    return 0


def _workspace_execute(args: argparse.Namespace, *, resuming: bool) -> int:
    manifest_dir, plan, digest = _load_workspace_manifest(args)
    verify_keys = _verify_keys(args)
    receipt = _workspace_receipt_store(args, manifest_dir, plan, verify_keys).load(
        digest
    )
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        if resuming:
            persisted = store.get_run(args.run_id)
            if persisted.workspace_digest != digest or persisted.workspace != plan:
                raise coordinator.CoordinatorError(
                    "stored run does not match the exact signed workspace manifest"
                )
        else:
            try:
                store.get_run(args.run_id)
            except coordinator_store.UnknownRun:
                pass
            else:
                raise coordinator_store.RunAlreadyExists(
                    "workspace run {0} already exists; use workspace resume".format(
                        args.run_id
                    )
                )
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
        _workspace_emit(
            {
                "digest": digest,
                "outcome": outcome.value,
                "participants": _workspace_participants(plan),
                "run_id": args.run_id,
            }
        )
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
        _workspace_emit(
            {
                "outcome": "STATUS",
                "publication": _publication_projection(intent, steps),
                "repositories": [
                    {
                        "accepted_sha": record.accepted_sha,
                        "block_reason": record.block_reason,
                        "candidate_branch": record.candidate_branch,
                        "child_run_id": record.child_run_id,
                        "repository_id": record.repository_id,
                        "resolved_path": record.resolved_path,
                        "state": record.state.value,
                    }
                    for record in repositories
                ],
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
                "gates": [
                    {
                        "detail": dict(gate.detail),
                        "gate_index": gate.gate_index,
                        "passed": gate.passed,
                    }
                    for gate in gates
                ],
            }
        )
        return 0
    finally:
        store.close()


def _workspace_cancel(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.request_cancellation(args.run_id, actor=args.actor)
        _workspace_emit(
            {
                "cancel_requested": run.cancel_requested,
                "outcome": "CANCELLATION_REQUESTED",
                "run_id": run.run_id,
            }
        )
        return 0
    finally:
        store.close()


def _workspace_publish(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.get_run(args.run_id)
        paths = workspace_runtime.resolve_repository_paths(
            Path(args.manifest_dir), run.workspace
        )
        result = WorkspacePublisher(
            store=store, repository_paths=paths, actor=args.actor
        ).publish(args.run_id)
        _workspace_emit(
            {
                "outcome": result.outcome.value,
                "publication": _publication_projection(result.intent, result.steps),
                "reason": result.reason,
                "run_id": result.run_id,
            }
        )
        return 0 if result.outcome is workspace_model.WorkspaceOutcome.PUBLISHED else 2
    finally:
        store.close()


def _workspace_rollback(args: argparse.Namespace) -> int:
    store = coordinator_store.CoordinatorStore(args.db)
    try:
        run = store.get_run(args.run_id)
        paths = workspace_runtime.resolve_repository_paths(
            Path(args.manifest_dir), run.workspace
        )
        result = WorkspacePublisher(
            store=store, repository_paths=paths, actor=args.actor
        ).rollback(args.run_id)
        _workspace_emit(
            {
                "outcome": result.outcome.value,
                "publication": _publication_projection(result.intent, result.steps),
                "reason": result.reason,
                "run_id": result.run_id,
            }
        )
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
    (
        coordinator_store.CoordinatorDatabaseUnavailable,
        "COORDINATOR_DATABASE_UNAVAILABLE",
    ),
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


def _internal_error(exc: BaseException) -> int:
    """`main`'s last-chance net, for every verb that is not `workspace`.

    The workspace branch has said `INTERNAL_ERROR` here since it was written;
    the branch beside it printed `type(exc).__name__.upper()`, so the same
    condition reached an operator as `SQLITE3.DATABASEERROR`, `OSERROR` or
    `VALUEERROR` depending on which library raised. That is the same defect the
    run path carried (`_RunRefused`): a class name is an implementation detail
    and an `outcome` is a vocabulary. The class name keeps its diagnostic value
    in `detail`.

    Exit 2 rather than 3 is preserved: 3 is a refusal Maestro decided on, 2 is
    an error it did not, and collapsing the two would lose the distinction.
    """
    print(
        json.dumps(
            {
                "detail": "{0}: {1}".format(type(exc).__name__, exc),
                "outcome": "MAESTRO_INTERNAL_ERROR",
            },
            sort_keys=True,
        )
    )
    return 2


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
    parser.add_argument("--review-ceiling", type=int)
    parser.add_argument("--test-review-ceiling", type=int)
    # §3.6 B10's escape from `NODE_BUDGET_EXHAUSTED_ACROSS_RUNS`, repeatable
    # so a run with two spent nodes needs one invocation rather than two. One
    # node id per flag, for the reason `--provision` takes one argv word per
    # flag: a comma-separated list is a parser nobody wrote.
    parser.add_argument(
        "--allow-exhausted-node",
        action="append",
        dest="allow_exhausted_nodes",
        metavar="NODE_ID",
    )
    parser.add_argument("--environmental-retries", type=int)
    parser.add_argument("--launcher-retries", type=int)
    parser.add_argument("--credential-retries", type=int)
    # One argv word per flag, in order: `--provision npm --provision ci`. The
    # same reason a gate takes real argv rather than a shell string.
    parser.add_argument("--provision", action="append", dest="provision_argv")
    parser.add_argument("--herdr")
    parser.add_argument("--omp")
    parser.add_argument("--claude")
    parser.add_argument("--agent-route")
    parser.add_argument("--agent-model")
    parser.add_argument("--agent-effort")
    parser.add_argument("--agent-profile")
    parser.add_argument("--review-start-deadline-s", type=float)
    parser.add_argument("--review-quiescence-confirm-s", type=float)
    parser.add_argument("--route-receipt", action="append")
    parser.add_argument("--route-verify-key", action="append")


def _add_run_selection(parser: argparse.ArgumentParser) -> None:
    """Which run to read, and how to print it — the only two choices a read
    verb offers, because everything else it needs is configured."""
    parser.add_argument("--run-id")
    parser.add_argument("--json", action="store_true", dest="as_json")


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

    bootstrap = root.add_parser("bootstrap")
    bootstrap.set_defaults(handler=_bootstrap)

    plan = root.add_parser("plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    author = plan_sub.add_parser("author")
    author.add_argument("plan_name")
    author.add_argument("--from", dest="from_file")
    author.add_argument("--from-plan-contract")
    author.add_argument("--plan-contract-receipt")
    author.add_argument("--plan-contract-rendered")
    author.add_argument("--repo", default=".")
    author.set_defaults(handler=_plan_author)
    validate = plan_sub.add_parser("validate")
    validate.add_argument("plan_name")
    _add_plan_receipt_access(validate)
    validate.set_defaults(handler=_plan_validate)
    finalize = plan_sub.add_parser("finalize")
    finalize.add_argument("plan_name")
    finalize.add_argument("--repo", default=".")
    # The receipt is the whole surface. Twelve reviewer flags stood here --
    # route, model, effort, profile, session dir, report file, the three
    # executables, the two route-receipt flags and three window clocks -- and
    # every one of them existed to launch and bound a pane. There is no pane.
    finalize.add_argument("--receipt-dir")
    finalize.add_argument("--data-dir")
    finalize.add_argument("--verify-key", action="append")
    finalize.add_argument("--signing-seed")
    finalize.set_defaults(handler=_plan_finalize)
    gate = plan_sub.add_parser("gate")
    gate.add_argument("plan_name")
    gate.set_defaults(handler=_plan_gate)
    review = plan_sub.add_parser("review")
    review.add_argument("plan_name")
    review.set_defaults(handler=_plan_review)
    ship = plan_sub.add_parser("ship")
    ship.add_argument("plan_name")
    ship.set_defaults(handler=_plan_ship)
    show = plan_sub.add_parser("show")
    show.add_argument("digest")
    _add_plan_receipt_access(show)
    show.set_defaults(handler=_plan_show)
    listing = plan_sub.add_parser("list")
    _add_plan_receipt_access(listing)
    listing.set_defaults(handler=_plan_list)
    set_aside = plan_sub.add_parser("set-aside")
    set_aside.add_argument("digest")
    set_aside.add_argument("--invoked-by", required=True)
    set_aside.add_argument("--reason", required=True)
    _add_plan_receipt_access(set_aside)
    set_aside.add_argument("--signing-seed")
    set_aside.set_defaults(handler=_plan_set_aside)
    set_aside_log = plan_sub.add_parser("set-aside-log")
    set_aside_log.add_argument("digest")
    _add_plan_receipt_access(set_aside_log)
    set_aside_log.set_defaults(handler=_plan_set_aside_log)

    # One verb from a source document to shipped, runnable plans. It prints the
    # `run start` commands and stops; it never starts one.
    deliver = root.add_parser("deliver")
    deliver.add_argument("spec")
    deliver.add_argument("--request", default="")
    deliver.add_argument(
        "--max-attempts", type=int, default=deliver_module.MAX_ATTEMPTS
    )
    deliver.add_argument("--no-run", action="store_true")
    # The operator escape for the two destructive steps inside `deliver`:
    # reclaiming a previous run's integration checkout, and clearing a plan
    # directory before a re-ship. Both refuse while the run they would take
    # state from is resumable; this is how an operator says the discard is
    # what they meant. One flag, because it is one intention (§11.3).
    deliver.add_argument(
        "--discard-live-runs", action="store_true", dest="discard_live_runs"
    )
    deliver.set_defaults(handler=_deliver)

    run = root.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    start = run_sub.add_parser("start")
    _add_run_execution_options(start)
    start.add_argument("plan_name")
    start.set_defaults(handler=_run_start)
    status = run_sub.add_parser("status")
    status.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    _add_run_selection(status)
    _add_db(status)
    status.set_defaults(handler=_run_status)
    run_listing = run_sub.add_parser("list")
    run_listing.add_argument("selector", nargs="?", metavar="PLAN")
    run_listing.add_argument("--json", action="store_true", dest="as_json")
    _add_db(run_listing)
    run_listing.set_defaults(handler=_run_list)
    pause = run_sub.add_parser("pause")
    pause.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    pause.add_argument("--run-id")
    _add_db(pause)
    pause.set_defaults(handler=_run_pause)
    cancel = run_sub.add_parser("cancel")
    cancel.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    cancel.add_argument("--run-id")
    cancel.add_argument(
        "--discard",
        action="store_true",
        help="make the run terminal; without this flag, cancel pauses",
    )
    _add_db(cancel)
    cancel.set_defaults(handler=_run_cancel)
    # Amendment takes the *amended* plan by name in the positional, so the
    # ordinary plan-resolution path binds it, and names the run it applies to
    # with a flag. Resume is the mirror image and deliberately stays bare.
    amend = run_sub.add_parser("amend")
    _add_run_execution_options(amend)
    amend.add_argument("plan_name")
    amend.add_argument("--run", dest="selector", metavar="PLAN_OR_RUN_ID",
                       required=True)
    amend.set_defaults(handler=_run_amend)
    resume = run_sub.add_parser("resume")
    resume.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    resume.add_argument("--digest")
    _add_run_execution_options(resume)
    resume.set_defaults(handler=_run_resume)
    # A read verb, so it takes exactly what `run status` takes: which run, and
    # how to print it (#30).
    convergence = run_sub.add_parser("convergence")
    convergence.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    _add_run_selection(convergence)
    _add_db(convergence)
    convergence.set_defaults(handler=_run_convergence)
    findings_cmd = run_sub.add_parser("findings")
    findings_cmd.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    _add_run_selection(findings_cmd)
    _add_db(findings_cmd)
    findings_cmd.set_defaults(handler=_run_findings)
    test_strength = run_sub.add_parser("test-strength")
    test_strength.add_argument("selector", metavar="PLAN_OR_RUN_ID")
    _add_run_selection(test_strength)
    _add_db(test_strength)
    test_strength.add_argument(
        "--migrate",
        action="store_true",
        help="plan a migration onto the test-strength contract (dry run)",
    )
    test_strength.add_argument(
        "--apply",
        action="store_true",
        help="with --migrate, perform the writes the dry run described",
    )
    test_strength.add_argument(
        "--policy",
        choices=("classify", "block-unadmitted"),
        default="classify",
        help=(
            "classify records the audit and fences nothing; block-unadmitted "
            "additionally fences dependants that have never been admitted"
        ),
    )
    test_strength.add_argument(
        "--no-backup",
        action="store_true",
        help="with --migrate --apply, skip the SQLite backup (not advised)",
    )
    test_strength.set_defaults(handler=_run_test_strength)

    def _positive_grant(value: str) -> int:
        """`--grant 0` and `--grant -2` are refused at parse time rather than
        accepted as a no-op escape: an operator who typed a grant asked for
        one, and a silently ignored magnitude would leave the node blocked
        with a transition claiming it had been retried."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"grant must be an integer, got {value!r}")
        if parsed < 1:
            raise argparse.ArgumentTypeError(f"grant must be at least 1, got {parsed}")
        return parsed

    retry = root.add_parser("retry")
    retry.add_argument("run_id")
    retry.add_argument("node_id")
    # One escape, two magnitudes. `--force` is a grant of one and keeps that
    # meaning exactly; `--grant N` is the same grant sized to a node that is
    # already N past its ceiling, which repeated `--force` cannot express —
    # the first call moves the node to PENDING and the store's `require_state`
    # then refuses the second (#81). Mutually exclusive so the total is never
    # ambiguous between N and N+1.
    grant_group = retry.add_mutually_exclusive_group()
    grant_group.add_argument(
        "--force",
        action="store_true",
        help="grant one extra attempt beyond the ceiling",
    )
    grant_group.add_argument(
        "--grant",
        type=_positive_grant,
        default=0,
        metavar="N",
        help="grant N extra attempts beyond the ceiling; a node blocked "
        "SEMANTIC_BUDGET_EXHAUSTED reports the N it needs as "
        "semantic_grant_required",
    )
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

    attempt = root.add_parser("attempt")
    attempt_sub = attempt.add_subparsers(dest="attempt_command", required=True)
    salvage_cmd = attempt_sub.add_parser("salvage")
    salvage_cmd.add_argument("run_id")
    salvage_cmd.add_argument("node_id")
    salvage_cmd.add_argument("attempt_no", type=int)
    salvage_cmd.add_argument("--invoked-by", required=True)
    salvage_cmd.add_argument("--reason", required=True)
    salvage_cmd.add_argument("--repo", default=".")
    # Not `required`: both are derived from the installed configuration by
    # `_bind_salvage_configuration`, and an explicit flag still wins (#83).
    salvage_cmd.add_argument("--worktrees-root")
    salvage_cmd.add_argument("--scratch-root")
    salvage_cmd.add_argument("--record-dir", required=True)
    salvage_cmd.add_argument("--signing-seed", required=True)
    _add_db(salvage_cmd)
    salvage_cmd.set_defaults(handler=_attempt_salvage)

    workspace = root.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)

    workspace_author_cmd = workspace_sub.add_parser("author")
    workspace_author_cmd.add_argument("--from", dest="from_file", required=True)
    workspace_author_cmd.add_argument("--out", required=True)
    workspace_author_cmd.add_argument("--root", default=".")
    workspace_author_cmd.set_defaults(handler=_workspace_author)

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
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    try:
        _apply_repository_config(args, raw_argv)
        return int(args.handler(args))
    except _PlanPaneUnavailable as exc:
        return _refusal("HERDR_PANE_UNAVAILABLE", str(exc))
    except _MaestroEnvironmentError as exc:
        return _refusal("MAESTRO_ENVIRONMENT_REQUIRED", str(exc))
    except _MaestroConfigurationError as exc:
        return _refusal("MAESTRO_CONFIGURATION_INVALID", str(exc))
    except _RunSelectionError as exc:
        return _refusal("RUN_NOT_FOUND", str(exc))
    except route_admission.AdmissionError as exc:
        return _refusal("ROUTE_ADMISSION_FAILED", str(exc))
    except deliver_module.DeliverError as exc:
        return _refusal("DELIVER_FAILED", str(exc))
    except plan_author.AuthoringError as exc:
        return _refusal("PLAN_AUTHORING_FAILED", str(exc))
    except _WORKSPACE_TYPED_ERRORS as exc:
        if args.command == "workspace":
            return _workspace_refusal(_workspace_error_code(exc), str(exc))
        if isinstance(exc, (lc.LifecycleError, OSError, ValueError)):
            return _internal_error(exc)
        raise
    except (lc.LifecycleError, OSError, ValueError, sqlite3.Error) as exc:
        if args.command == "workspace":
            return _workspace_internal_error(exc)
        return _internal_error(exc)
    except Exception as exc:
        if args.command == "workspace":
            return _workspace_internal_error(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
