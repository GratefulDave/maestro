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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

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
from adw_modules import plan_finalization
from adw_modules import plan_digest
from adw_modules import plan_model
from adw_modules import plan_validate as pv
from adw_modules import publication
from adw_modules import receipt_crypto
from adw_modules import retry_policy
from adw_modules import review_convergence
from adw_modules import route_admission
from adw_modules import route_receipts
from adw_modules import scheduler
from adw_modules import scheduler_types
from adw_modules import watchdog
from adw_modules import worktree
from adw_modules import salvage

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
            return _typed_refusal({"outcome": self.outcome, **self.fields},
                                  self.detail)
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
    Path(".claude") / "skills" / "plan-contract" / _PLAN_CONTRACT_SCRIPT)
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
    "/bin/sh", "-c",
    'exec >>"$' + _PLAN_STEP_LOG_ENV + '" 2>&1; exec "$0" "$@"')


class _StrictYamlLoader(yaml.SafeLoader):
    """Safe YAML plus duplicate-key refusal for operator configuration."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None, None, "configuration mapping keys must be strings",
                key_node.start_mark)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate configuration key: " + key,
                key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


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
        raise _MaestroConfigurationError(
            label + " has " + "; ".join(detail) + " keys")
    return value


def _config_string(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\x00" in value):
        raise _MaestroConfigurationError(label + " must be a non-empty string")
    return value


def _config_positive_number(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value <= 0):
        raise _MaestroConfigurationError(label + " must be positive")
    return float(value)


def _config_positive_integer(value, label):
    if (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
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
        raise _MaestroConfigurationError(
            label + " must be a non-negative integer")
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
            "execution.review_reject_grade: " + str(exc)) from exc


def _config_argv(value, label) -> Tuple[str, ...]:
    """A command as a real argv list, never a shell string.

    The same rule the plan's gates already live under (`docs/plan-authoring.md`,
    "Pass the real argv, never a script alias"): a string would have to be
    split by something, and whatever split it would be a shell this process
    does not run.
    """
    if not isinstance(value, list) or not value:
        raise _MaestroConfigurationError(
            label + " must be a non-empty list of argv strings")
    return tuple(
        _config_string(item, label + "[{}]".format(index))
        for index, item in enumerate(value))


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
                    loaded.get("installations"), list):
                installations = [
                    item for item in loaded["installations"]
                    if isinstance(item, dict)
                    and item.get("database") != entry["database"]
                ]
        installations.insert(0, entry)
        # Written beside the destination and renamed, so a dashboard reading
        # concurrently sees either the old file or the new one, never a
        # half-written one.
        scratch = path.with_name(path.name + ".tmp")
        scratch.write_text(
            json.dumps({"installations": installations}, indent=2,
                       sort_keys=True) + "\n",
            encoding="utf-8")
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
        name, label, expected_size: int, *, fallback: Optional[Path] = None,
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
                "key material file is unreadable: " + str(fallback)) from exc
    if not value:
        raise _MaestroEnvironmentError(
            "required environment variable is unset: " + environment_name)
    try:
        material = bytes.fromhex(value)
    except ValueError as exc:
        raise _MaestroEnvironmentError(
            "environment variable is not hexadecimal: " + environment_name) from exc
    if len(material) != expected_size:
        raise _MaestroEnvironmentError(
            "environment variable has invalid key length: " + environment_name)
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


def _validate_review_clocks(reviewer: Mapping[str, Any],
                            execution: Mapping[str, Any]) -> None:
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
                reviewer_turn_s, finalization_s))
    if finalization_s >= backstop_s:
        raise _MaestroConfigurationError(
            "LIVENESS_BOUND_UNSATISFIED: reviewer.finalization_timeout_s "
            "must be less than execution.backstop_t_s, or the run-level "
            "backstop fires inside a healthy review. "
            "finalization={0}, backstop={1}".format(
                finalization_s, backstop_s))
    sequential_s = node_timeout_s + finalization_s
    if sequential_s >= backstop_s:
        raise _MaestroConfigurationError(
            "LIVENESS_BOUND_UNSATISFIED: execution.node_timeout_s plus "
            "reviewer.finalization_timeout_s must be less than "
            "execution.backstop_t_s, or the run-level backstop fires on a "
            "healthy sequential node-and-review path. "
            "node_timeout={0}, finalization={1}, sequential={2}, "
            "backstop={3}".format(
                node_timeout_s, finalization_s, sequential_s, backstop_s))


def _load_maestro_layout(repo: Path, config_path: Path) -> Dict[str, Any]:
    """Repository layout, binaries, and route destinations. No key material."""
    try:
        with config_path.open(encoding="utf-8") as handle:
            raw = yaml.load(handle, Loader=_StrictYamlLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _MaestroConfigurationError(
            "cannot load " + str(_MAESTRO_CONFIG_FILE)) from exc
    root = _config_mapping(
        raw, "maestro configuration",
        ("schema", "plans_dir", "state_root", "keys", "executables",
         "route_receipts", "reviewer", "execution"),
        ("plan_contract", "author", "runners"))
    if root["schema"] != _MAESTRO_SCHEMA:
        raise _MaestroConfigurationError(
            "schema must be " + _MAESTRO_SCHEMA)

    plans_dir = _repository_path(repo, root["plans_dir"], "plans_dir", inside=True)
    if not plans_dir.is_dir():
        raise _MaestroConfigurationError("plans_dir is not a directory")
    # Anchored to the repository, not the checkout: a worktree shares the
    # ledger, receipts, and admitted routes of the repository it belongs to.
    # `plans_dir` stays bound to `repo` above, because the plan being run is
    # whatever this checkout has on disk.
    identity = _repository_identity_root(repo)
    state_root = _repository_path(
        identity, root["state_root"], "state_root", inside=False)
    if _path_is_within(state_root, repo):
        raise _MaestroConfigurationError(
            "state_root must resolve outside the repository")
    repository_state = (state_root / identity.name).resolve()
    if not _path_is_within(repository_state, state_root):
        raise _MaestroConfigurationError(
            "repository state must remain below state_root")

    keys = _config_mapping(
        root["keys"], "keys",
        ("verify_key_env", "signing_seed_env", "route_verify_key_env"),
        ("reviewer_hmac_key_env",))
    if "reviewer_hmac_key_env" in keys:
        reviewer_key_env = _config_string(
            keys["reviewer_hmac_key_env"], "keys.reviewer_hmac_key_env")
        if _ENVIRONMENT_NAME.fullmatch(reviewer_key_env) is None:
            raise _MaestroConfigurationError(
                "keys.reviewer_hmac_key_env must name an environment variable")

    executable_values = _config_mapping(
        root["executables"], "executables", ("herdr", "omp", "claude"))
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
            root["runners"], "runners", (), tuple(plan_model.RUNNERS))
        for name in plan_model.RUNNERS:
            if name in runner_values:
                runners[name] = _config_string(
                    runner_values[name], "runners." + name)

    receipts = root["route_receipts"]
    if not isinstance(receipts, dict) or not receipts:
        raise _MaestroConfigurationError("route_receipts must be a non-empty mapping")
    route_paths = {}
    runs_root = repository_state / "runs"
    for route, value in receipts.items():
        route_name = _config_string(route, "route_receipts route")
        if "=" in route_name:
            raise _MaestroConfigurationError(
                "route_receipts route must not contain '='")
        relative = _config_relative_path(
            value, "route_receipts." + route_name)
        path = (repository_state / relative).resolve()
        if not _path_is_within(path, repository_state):
            raise _MaestroConfigurationError(
                "route receipt must remain below repository state")
        if _path_is_within(path, runs_root):
            raise _MaestroConfigurationError(
                "route receipt must not be in a participant run boundary")
        route_paths[route_name] = path

    reviewer_raw = _config_mapping(
        root["reviewer"], "reviewer",
        ("route", "model", "effort", "finalization_timeout_s",
         "turn_timeout_s", "poll_interval_s"), ("profile", "id", "vendor"))
    execution_raw = _config_mapping(
        root["execution"], "execution",
        ("route", "model", "effort", "concurrency", "node_timeout_s",
         "turn_timeout_s", "final_acceptance_timeout_s", "backstop_t_s",
         "semantic_ceiling"),
        ("profile", "vendor", "review_ceiling", "review_reject_grade",
         "provision", "environmental_retries", "launcher_retries",
         "credential_retries"))

    reviewer = {
        "route": _config_string(reviewer_raw["route"], "reviewer.route"),
        "model": _config_string(reviewer_raw["model"], "reviewer.model"),
        "effort": _config_string(reviewer_raw["effort"], "reviewer.effort"),
        "profile": (_config_string(reviewer_raw["profile"], "reviewer.profile")
                    if "profile" in reviewer_raw else None),
        "id": (_config_string(reviewer_raw["id"], "reviewer.id")
               if "id" in reviewer_raw else None),
        "vendor": (_config_string(reviewer_raw["vendor"], "reviewer.vendor")
                   if "vendor" in reviewer_raw else None),
        "finalization_timeout_s": _config_positive_number(
            reviewer_raw["finalization_timeout_s"],
            "reviewer.finalization_timeout_s"),
        "turn_timeout_s": _config_positive_number(
            reviewer_raw["turn_timeout_s"], "reviewer.turn_timeout_s"),
        "poll_interval_s": _config_positive_number(
            reviewer_raw["poll_interval_s"], "reviewer.poll_interval_s"),
    }
    execution = {
        "route": _config_string(execution_raw["route"], "execution.route"),
        "model": _config_string(execution_raw["model"], "execution.model"),
        "effort": _config_string(execution_raw["effort"], "execution.effort"),
        "profile": (_config_string(execution_raw["profile"], "execution.profile")
                    if "profile" in execution_raw else None),
        "concurrency": _config_positive_integer(
            execution_raw["concurrency"], "execution.concurrency"),
        "node_timeout_s": _config_positive_number(
            execution_raw["node_timeout_s"], "execution.node_timeout_s"),
        "turn_timeout_s": _config_positive_number(
            execution_raw["turn_timeout_s"], "execution.turn_timeout_s"),
        "final_acceptance_timeout_s": _config_positive_number(
            execution_raw["final_acceptance_timeout_s"],
            "execution.final_acceptance_timeout_s"),
        "backstop_t_s": _config_positive_number(
            execution_raw["backstop_t_s"], "execution.backstop_t_s"),
        "semantic_ceiling": _config_positive_integer(
            execution_raw["semantic_ceiling"], "execution.semantic_ceiling"),
        "vendor": (_config_string(execution_raw["vendor"], "execution.vendor")
                   if "vendor" in execution_raw else None),
        # Separate from the semantic ceiling by construction. Defaulted rather
        # than required so an existing config keeps working, but the default is
        # a real bound, not "unlimited".
        "review_ceiling": (
            _config_positive_integer(execution_raw["review_ceiling"],
                                     "execution.review_ceiling")
            if "review_ceiling" in execution_raw else 3),
        # §3.6 A9's grading threshold, beside the ceiling because it answers
        # the half of A9 the ceiling cannot: the ceiling decides how many
        # rejections a node may collect, this decides what counts as one. A
        # property of the installation, never of the plan (§6.2), so a plan
        # cannot raise or lower its own bar.
        "review_reject_grade": (
            _config_reject_grade(execution_raw["review_reject_grade"])
            if "review_reject_grade" in execution_raw
            else code_review.DEFAULT_REJECT_GRADE),
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
        "provision": (_config_argv(execution_raw["provision"],
                                   "execution.provision")
                      if "provision" in execution_raw else ()),
        # §7.5's two non-semantic budgets and CREDENTIAL's zero. The defaults
        # are read off `SchedulerConfig` rather than restated here: a literal
        # in this file would be a second representation of a number the
        # dataclass already declares, which is RC1's shape and the reason
        # these three keys were unreachable in the first place.
        "environmental_retries": (
            _config_nonnegative_integer(
                execution_raw["environmental_retries"],
                "execution.environmental_retries")
            if "environmental_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["environmental_retries"]),
        "launcher_retries": (
            _config_nonnegative_integer(
                execution_raw["launcher_retries"],
                "execution.launcher_retries")
            if "launcher_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["launcher_retries"]),
        "credential_retries": (
            _config_nonnegative_integer(
                execution_raw["credential_retries"],
                "execution.credential_retries")
            if "credential_retries" in execution_raw
            else _SCHEDULER_CONFIG_DEFAULTS["credential_retries"]),
    }
    # `author` is optional so an installation that never runs `maestro deliver`
    # keeps working unchanged; `deliver` refuses when it is absent rather than
    # inventing a lane nobody configured.
    author = None
    if "author" in root:
        author_raw = _config_mapping(
            root["author"], "author",
            ("route", "model", "effort", "author_timeout_s", "turn_timeout_s",
             "poll_interval_s"), ("profile",))
        author = {
            "route": _config_string(author_raw["route"], "author.route"),
            "model": _config_string(author_raw["model"], "author.model"),
            "effort": _config_string(author_raw["effort"], "author.effort"),
            "profile": (_config_string(author_raw["profile"], "author.profile")
                        if "profile" in author_raw else None),
            "author_timeout_s": _config_positive_number(
                author_raw["author_timeout_s"], "author.author_timeout_s"),
            "turn_timeout_s": _config_positive_number(
                author_raw["turn_timeout_s"], "author.turn_timeout_s"),
            "poll_interval_s": _config_positive_number(
                author_raw["poll_interval_s"], "author.poll_interval_s"),
        }

    sections = [("reviewer", reviewer), ("execution", execution)]
    if author is not None:
        sections.append(("author", author))
    for label, section in sections:
        if section["route"] not in route_paths:
            raise _MaestroConfigurationError(
                label + ".route has no route receipt")

    # B12's *equality* half applies to every verb that loads a config: two
    # blocks naming the same vendor is a misconfiguration whenever it is
    # written, not only when a run reads it. The *absence* half is enforced at
    # the run branch instead, because a config with no vendors is legal for
    # `plan validate`, `bootstrap`, and the workspace verbs — none of them
    # launches a reviewer, so none of them can be self-judging.
    if (execution.get("vendor") and reviewer.get("vendor")):
        try:
            code_review.require_distinct_vendor(
                execution["vendor"], reviewer["vendor"])
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
    for label, path in (("data directory", data_dir),
                        ("receipt store", receipt_dir),
                        ("lifecycle database", database)):
        if _path_is_within(path, repo) or _path_is_within(path, runs_root):
            raise _MaestroConfigurationError(
                label + " is inside a repository or participant boundary")
    if _path_is_within(receipt_dir, data_dir) or _path_is_within(data_dir, receipt_dir):
        raise _MaestroConfigurationError(
            "receipt store and data directory must be separate")
    plan_contract = None
    if "plan_contract" in root:
        raw_contract = Path(_config_string(root["plan_contract"], "plan_contract"))
        plan_contract = (raw_contract if raw_contract.is_absolute()
                         else repo / raw_contract).resolve()
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
    }


def _bind_maestro_keys(layout: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve signing and route keys from env, then bootstrapped state files."""
    keys = layout["key_env"]
    key_dir = layout["repository_state"] / "keys"
    verify_key = _resolve_key_environment(
        keys["verify_key_env"], "keys.verify_key_env",
        receipt_crypto.PUBLIC_KEY_SIZE,
        fallback=key_dir / "signing.pub")
    signing_seed = _resolve_key_environment(
        keys["signing_seed_env"], "keys.signing_seed_env",
        receipt_crypto.SEED_SIZE,
        fallback=key_dir / "signing.seed")
    route_verify_key = _resolve_key_environment(
        keys["route_verify_key_env"], "keys.route_verify_key_env",
        receipt_crypto.PUBLIC_KEY_SIZE,
        fallback=key_dir / "route.pub")
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
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and "/" not in name and "\\" not in name
            and Path(name).name == name)


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
_RUN_LEDGER_COMMANDS = ("status", "list", "pause", "cancel", "convergence")

#: Flags that select *which* run to read and how to print it. They are the only
#: flags a configured run verb accepts, because they override no derived path;
#: any other flag means the operator is driving every path by hand. `--discard`
#: belongs here for the same reason: it selects which of `run cancel`'s two
#: behaviours the operator meant, and derives no path at all.
_RUN_SELECTION_OPTIONS = frozenset({"--run-id", "--json", "--discard"})

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
    return (args.command == "plan"
            and args.plan_command in _PLAN_FILE_VERBS
            and not _is_plan_name(getattr(args, "plan_name", None)))


def _configured_command(args: argparse.Namespace) -> bool:
    return (args.command == "bootstrap"
            or (args.command == "plan"
                and args.plan_command in ("validate", "finalize", "author"))
            or args.command == "run")


def _bind_layout_executables(args: argparse.Namespace, layout: Dict[str, Any]) -> None:
    args.repo = str(layout["repo"])
    args.herdr = layout["executables"]["herdr"]
    args.omp = layout["executables"]["omp"]
    args.claude = layout["executables"]["claude"]
    args.route_receipt = [
        route + "=" + str(path)
        for route, path in sorted(layout["route_paths"].items())
    ]
    args.repository_state = str(layout["repository_state"])
    args.runners = dict(layout.get("runners") or {})
    args.layout = layout


def _apply_repository_config(
        args: argparse.Namespace, argv: Sequence[str]) -> None:
    """Bind named-plan and bootstrap entrypoints to installed repository state."""
    if (args.command == "attempt"
            and getattr(args, "attempt_command", None) == "salvage"):
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
            str(_MAESTRO_CONFIG_FILE) + " is not a regular file")
    options = tuple(item.split("=", 1)[0] for item in argv
                    if item.startswith("-"))
    if args.command == "run" and args.run_command != "start":
        if any(option not in _RUN_SELECTION_OPTIONS for option in options):
            # A fully manual invocation. Every path it works on came from a
            # flag, and binding the other half from configuration is exactly
            # how the two halves come to disagree about which run this is.
            return
    elif options:
        raise _MaestroConfigurationError(
            "configured named-plan commands do not accept runtime flags")

    repo = config_path.parent.parent.resolve()
    if args.command == "bootstrap" or (
            args.command == "plan" and args.plan_command == "author"):
        layout = _load_maestro_layout(repo, config_path)
        _bind_layout_executables(args, layout)
        _register_installation(layout)
        if args.command == "plan":
            args.plan_file = str(_named_plan_output(layout, args.plan_name))
        return

    if args.command == "run" and args.run_command in _RUN_LEDGER_COMMANDS:
        _bind_run_ledger_configuration(
            args, _load_maestro_layout(repo, config_path))
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
        route + "=" + str(path)
        for route, path in sorted(config["route_paths"].items())
    ]
    args.herdr = config["executables"]["herdr"]
    args.omp = config["executables"]["omp"]
    args.claude = config["executables"]["claude"]

    if args.command == "plan" and args.plan_command == "finalize":
        digest = plan_digest.digest_of(plan_file.read_bytes())
        reviewer = config["reviewer"]
        finalization_root = (
            config["repository_state"] / "finalization" / digest)
        args.reviewer_route = reviewer["route"]
        args.reviewer_model = reviewer["model"]
        args.reviewer_effort = reviewer["effort"]
        args.reviewer_profile = reviewer["profile"]
        args.reviewer_session_dir = str(finalization_root / "session")
        args.reviewer_report_file = str(finalization_root / "report.json")
        args.finalization_timeout_s = reviewer["finalization_timeout_s"]
        args.reviewer_turn_timeout_s = reviewer["turn_timeout_s"]
        args.reviewer_poll_interval_s = reviewer["poll_interval_s"]
    elif args.command == "run":
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
        args.run_id = run_id
        args.integration_path = str(run_root / "integration")
        args.worktrees_root = str(run_root / "worktrees")
        args.scratch_root = str(run_root / "scratch")
        args.concurrency = execution["concurrency"]
        args.node_timeout_s = execution["node_timeout_s"]
        args.turn_timeout_s = execution["turn_timeout_s"]
        args.final_acceptance_timeout_s = execution[
            "final_acceptance_timeout_s"]
        args.backstop_t_s = execution["backstop_t_s"]
        args.semantic_ceiling = execution["semantic_ceiling"]
        args.review_ceiling = execution["review_ceiling"]
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
        # Per-attempt subdirectories are minted under this root by the runner;
        # each review gets a fresh session directory, because §6.5's structural
        # half of "independent review is recorded" refuses a reused one.
        args.review_root = str(review_root)
        args.review_receipt_dir = str(review_root / "receipts")
        args.review_timeout_s = reviewer["finalization_timeout_s"]
        args.reviewer_turn_timeout_s = reviewer["turn_timeout_s"]
        args.reviewer_poll_interval_s = reviewer["poll_interval_s"]


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


def _bind_salvage_configuration(
        args: argparse.Namespace, argv: Sequence[str]) -> None:
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
            str(_MAESTRO_CONFIG_FILE) + " is not a regular file")
    layout = _load_maestro_layout(
        config_path.parent.parent.resolve(), config_path)
    runs_root = (layout["repository_state"] / "runs").resolve()
    run_root = (runs_root / str(args.run_id)).resolve()
    if not _path_is_within(run_root, runs_root):
        raise _MaestroConfigurationError(
            "run id does not name a directory inside the run boundary")
    if "--worktrees-root" not in supplied:
        args.worktrees_root = str(run_root / "worktrees")
    if "--scratch-root" not in supplied:
        args.scratch_root = str(run_root / "scratch")
    if "--db" not in supplied:
        args.db = str(layout["database"])


def _bind_run_ledger_configuration(
        args: argparse.Namespace, layout: Dict[str, Any]) -> None:
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


def _select_run(reader: "lc.LifecycleReader",
                args: argparse.Namespace) -> "lc.RunRecord":
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
        raise _RunSelectionError(
            "name a plan or a run id, or pass --run-id")
    record = reader.run(selector)
    if record is not None:
        return record
    digest = (getattr(args, "plan_digests", None) or {}).get(selector)
    if digest is None:
        raise _RunSelectionError(
            selector + " is neither a run id in the ledger nor an installed "
            "plan name")
    records = reader.runs(digest)
    if not records:
        raise _RunSelectionError(
            "no run has been started for plan " + selector
            + " at its current contents (digest " + digest[:12] + ")")
    return records[0]


def _resolve_resume_target(
        args: argparse.Namespace,
        config: Dict[str, Any]) -> Tuple[str, Path]:
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
    finally:
        reader.close()
    for name, digest in args.plan_digests.items():
        if digest == record.plan_digest:
            return record.run_id, _named_plan_file(config, name)
    raise _RunSelectionError(
        "run " + record.run_id + " ran plan digest " + record.plan_digest[:12]
        + ", which no installed plan currently matches; the plan file has "
        "changed since the run started")


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
        {"outcome": "RUN_QUIESCENCE_UNPROVEN", "quiescence_code": code}, text)


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
            "bootstrap requires an installed " + str(_MAESTRO_CONFIG_FILE))
    keys_dir = Path(layout["repository_state"]) / "keys"
    try:
        keys = route_admission.provision_keys(keys_dir)
        env_file = route_admission.write_env_file(
            keys,
            verify_key_env=layout["key_env"]["verify_key_env"],
            signing_seed_env=layout["key_env"]["signing_seed_env"],
            route_verify_key_env=layout["key_env"]["route_verify_key_env"],
            reviewer_hmac_key_env=layout["key_env"].get(
                "reviewer_hmac_key_env", _REVIEWER_HMAC_KEY_ENV),
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
            section = next(
                (lane for lane in lanes if lane["route"] == route), None)
            if section is None:
                raise route_admission.AdmissionError(
                    "ROUTE_MODEL_UNCONFIGURED:{}".format(route))
            timeout = section.get("turn_timeout_s") or 180.0
            specs.append(route_admission.RouteCaptureSpec(
                route=route,
                cwd=Path(layout["repo"]),
                herdr=Path(layout["executables"]["herdr"]),
                binary=Path(layout["executables"][route]),
                model=section["model"],
                effort=section["effort"],
                profile=section.get("profile"),
                session_dir=(Path(layout["repository_state"])
                             / "admission" / route),
                timeout_s=float(timeout),
            ))
        written = route_admission.admit_routes(
            specs, layout["route_paths"], route_seed=keys.route_seed)
    except route_admission.AdmissionError as exc:
        return _refusal("ROUTE_ADMISSION_FAILED", str(exc))
    payload = {
        "outcome": "ROUTES_ADMITTED",
        "env_file": str(env_file),
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
                Path(contract), Path(receipt), destination, repo,
                Path(rendered) if rendered else None)
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
        resolver=runner_resolution.resolve)


def _plan_validate(args: argparse.Namespace) -> int:
    try:
        result = pv.validate_plan(
            Path(args.plan_file).read_bytes(), args.repo,
            receipts=_VerifiedReceipts(args),
            collector=_plan_collector(args))
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
            "are required to write to the receipt store")
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
    # §8.3 again: this reviewer's pane needs the redirection exactly as the
    # node reviewer's does. Its scratch is a sibling of the session directory
    # the operator named, so it lands wherever that reviewer's own state does
    # and never in the repository being reviewed.
    scratch_dir = session_dir.with_name(session_dir.name + ".scratch")

    def factory(matrix: finalization.ApplicabilityMatrix):
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_text = json.dumps({
            "matrix": [
                {"check_id": cell.check_id, "object_id": cell.object_id,
                 "canary": cell.canary.value if cell.canary else None}
                for cell in matrix.cells
            ],
            "plan_digest": matrix.plan_digest,
            "report_path": str(report_path.resolve()),
        }, sort_keys=True)
        # B13 for the finalization reviewer. The matrix grows with the plan, so
        # this prompt is runtime-sized too.
        _preflight_prompt(prompt_text, args.reviewer_route, args.reviewer_model)
        prompt_path.write_text(prompt_text, encoding="utf-8")
        _clear_stale_reviewer_report(report_path)
        handle = None

        def launch_reviewer():
            nonlocal handle
            handle = _typed_launch_pane(runner, launcher.LaunchSpec(
                correlation_token="finalize-" + matrix.plan_digest,
                worktree=Path(args.repo), prompt_path=prompt_path,
                envelope_path=report_path, route=args.reviewer_route,
                model=args.reviewer_model, effort=args.reviewer_effort,
                profile=args.reviewer_profile, session_dir=session_dir,
                context_window_tokens=_route_context_window(
                    args.reviewer_route, args.reviewer_model),
                environment=worktree.launch_env(scratch_dir)))
            return finalization_window.ReviewerSession(
                route=args.reviewer_route, model=args.reviewer_model,
                session_id=handle.pane_id, session_dir=str(session_dir),
                # `harness_owned_group` stays keyed on `process_group` alone
                # and must not learn about the fallback: it is what decides
                # whether the stall path SIGKILLs, and §8.3 conditions that on
                # a receipt §9.8 records as only partly executed. `pid` may
                # take the fallback, because its only consumer is the window's
                # `process_alive` read (#20).
                harness_owned_group=handle.process_group is not None,
                pid=handle.process_group or handle.liveness_pid)

        def poll_report():
            return _poll_reviewer_report(report_path)

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
            kill=kill_reviewer)

    return factory


def _code_review_runner(args: argparse.Namespace, runner: "launcher.HerdrLauncher"):
    """Build the scheduler's review stage: one reviewer per verified attempt.

    Shares the launcher with the node runner deliberately — a second
    `HerdrLauncher` would own a second pane registry, and the pane accounting
    that `cancel` depends on would then be split across two objects that cannot
    see each other's handles.
    """
    # B12's absence half, enforced where a reviewer is actually about to run.
    # Fail closed: a vendor nobody declared cannot be shown to differ from the
    # builder's, and "probably a different model" is not a property.
    try:
        code_review.require_distinct_vendor(
            getattr(args, "execution_vendor", "") or "",
            getattr(args, "reviewer_vendor", "") or "")
    except code_review.SelfJudgeRefused as exc:
        raise _PlanReceiptConfigurationError(str(exc)) from exc

    review_root = Path(args.review_root)
    store = finalization.ReceiptStore(
        Path(args.review_receipt_dir), repo_paths=(args.repo,),
        data_dir=args.data_dir,
        verify_keys=tuple(bytes.fromhex(k) for k in args.verify_key),
        signing_seed=bytes.fromhex(args.signing_seed))

    def review(attempt, node, record, base_sha: str, output_sha: str):
        digest = code_review.review_digest(
            run_id=args.run_id, node_id=node.node_id, base_sha=base_sha,
            output_sha=output_sha, rubric_version=code_review.CODE_RUBRIC.version)
        # Keyed by the subject digest, not the attempt number, so a rebuilt but
        # byte-identical output lands on the same directory and the same
        # receipt — B10's replay rather than a second opinion.
        subject_root = review_root / digest
        report_path = subject_root / "report.json"
        # Every finding this review produced, rejecting and sub-threshold
        # alike, beside the reviewer's own report and under the same subject
        # digest as the receipt that admits the merge. This is where a merged
        # node's advisories live: an operator reads
        #   <state>/runs/<run_id>/review/<digest>/findings.json
        # or `code_review.read_finding_ledger` on that path.
        ledger_path = subject_root / code_review.FINDING_LEDGER_FILENAME
        prompt_path = subject_root / "prompt.md"
        session_dir = subject_root / "session"
        # §8.3 applies to the reviewer's pane exactly as it does to a node's.
        # The reviewer runs at the repository, not inside an attempt worktree,
        # so it owns a scratch beside its own session directory under the run's
        # review root -- a location Maestro owns -- rather than borrowing some
        # attempt's. Omitting it entirely left `LaunchSpec.environment` at its
        # empty default, and `pane_env_flags` then refused the launch with
        # SCRATCH_REDIRECT_MISSING for all seven variables, discarding a
        # verified attempt's work at the review stage.
        scratch_dir = subject_root / "scratch"

        diff, changed = code_review.read_diff(
            Path(args.repo), base_sha, output_sha)
        objects = code_review.review_objects(changed, output_sha)
        matrix = finalization.compute_matrix(
            code_review.CODE_RUBRIC, digest, objects)
        handoff = code_review.build_handoff(
            subject_digest=digest, run_id=args.run_id, node=node,
            base_sha=base_sha, output_sha=output_sha, diff=diff,
            matrix=matrix, rubric=code_review.CODE_RUBRIC,
            report_path=report_path)

        # B13 — measured against the reviewer's real window before a pane is
        # allocated, so an oversized handoff is a refusal rather than a
        # confident verdict about something else.
        text = handoff.render()
        _preflight_prompt(text, args.reviewer_route, args.reviewer_model)

        handle_box: Dict[str, Any] = {"handle": None}

        def window_factory(_matrix):
            subject_root.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(text, encoding="utf-8")
            _clear_stale_reviewer_report(report_path)

            def launch_reviewer():
                # The site that actually refused in
                # run-f31686ea41b44c33b117f64e3b319317: its agent node had
                # launched and done 61 turns before this reviewer's launch was
                # refused, and the refusal spent an ENVIRONMENTAL retry.
                handle = _typed_launch_pane(runner, launcher.LaunchSpec(
                    correlation_token="review-" + digest[:16],
                    worktree=Path(args.repo), prompt_path=prompt_path,
                    envelope_path=report_path, route=args.reviewer_route,
                    model=args.reviewer_model, effort=args.reviewer_effort,
                    profile=args.reviewer_profile, session_dir=session_dir,
                    context_window_tokens=_route_context_window(
                        args.reviewer_route, args.reviewer_model),
                    # Same tab as the builder whose output it is judging --
                    # `_code_review_runner` is handed the run's own launcher,
                    # so the group key resolves to the tab that node already
                    # opened.
                    pane_group=node.node_id, pane_role="reviewer",
                    pane_group_size=2,
                    environment=worktree.launch_env(scratch_dir)))
                handle_box["handle"] = handle
                return finalization_window.ReviewerSession(
                    route=args.reviewer_route, model=args.reviewer_model,
                    session_id=handle.pane_id, session_dir=str(session_dir),
                    # As above: the kill stays gated on `process_group`, the
                    # liveness read may use the fallback (#20).
                    harness_owned_group=handle.process_group is not None,
                    pid=handle.process_group or handle.liveness_pid)

            def poll_report():
                return _poll_reviewer_report(report_path)

            def read_status(_session):
                handle = handle_box["handle"]
                return runner.agent_status(handle) if handle is not None else None

            def kill_reviewer(_session):
                _close_reviewer_pane(runner, handle_box)

            return finalization_window.FinalizationWindow(
                config=finalization_window.FinalizationConfig(
                    finalization_timeout_s=args.review_timeout_s,
                    turn_timeout_s=args.reviewer_turn_timeout_s,
                    poll_interval_s=args.reviewer_poll_interval_s),
                launch=launch_reviewer, poll_report=poll_report,
                record_reviewer_session=lambda _s: None,
                kill=kill_reviewer,
                actor_status=read_status)

        try:
            return code_review.review_attempt(
                subject_digest=digest, handoff=handoff, objects=objects,
                rubric=code_review.CODE_RUBRIC, store=store,
                window_factory=window_factory,
                occupancy_reader=_reviewer_occupancy,
                reject_at=args.review_reject_grade,
                ledger_path=ledger_path)
        finally:
            # Unconditional. The pane is closed on the success path too, not
            # only when the window stalls and calls `kill` — a completed review
            # that leaves its pane alive is the leak that accumulates one
            # orphaned pane per node for the length of a run.
            _close_reviewer_pane(runner, handle_box)

    return review


def _close_reviewer_pane(runner: "launcher.HerdrLauncher",
                         handle_box: Dict[str, Any]) -> None:
    """Close the reviewer's pane and forget the handle, at most once.

    `cancel` proves quiescence and raises when it cannot. Here that exception
    is swallowed on purpose: this runs in a `finally` around a review whose
    verdict is already decided, and letting a pane-close failure replace a real
    PASS or FAIL with a quiescence error would turn a cosmetic leak into a lost
    review. The pane id stays in the log either way.
    """
    handle = handle_box.get("handle")
    if handle is None:
        return
    handle_box["handle"] = None
    try:
        runner.cancel(handle, finalization_window.time.monotonic() + 5.0)
    except BaseException as exc:  # noqa: BLE001 — see docstring
        print(json.dumps({
            "event": "reviewer_pane_close_failed",
            "pane_id": handle.pane_id,
            "detail": str(exc)[:200],
        }, sort_keys=True), file=sys.stderr)


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
    "this happen\", and grep only for an exact literal in a known path. It is "
    "faster than reading files whole, and it leaves you the context to finish."
)


def _agent_node_prompt(node: Any, envelope: Path,
                       retry_prompt: Optional[str]) -> str:
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
            "to anything else fails the attempt:")
        lines.extend("  " + path for path in outputs)
        lines.append("")
    gate = getattr(node, "gate", None)
    if gate is not None:
        lines.append(
            "Your work is judged by this command, run from {0!r}. It fails now "
            "and must pass, collecting at least {1} case(s), when you are "
            "done:".format(gate.cwd, gate.min_cases))
        lines.append("  " + " ".join([gate.runner, *gate.argv]))
        lines.append("")
    if retry_prompt:
        lines.extend(["Retry guidance:", retry_prompt, ""])
    lines.extend([
        "When you have finished, write this file and then stop:",
        "  " + str(envelope),
        '  {"success": true, "summary": "<what you did>"}',
        "",
        "Use \"success\": false if you could not finish, with the reason in "
        "the summary. The attempt is not verified without this file, and "
        "nothing else ends it.",
        "",
        AGENT_DISCOVERY_ROUTING,
    ])
    return "\n".join(lines)


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
            capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(
            "GIT_READ_FAILED:worktree list in {}".format(repo)) from exc
    if listed.returncode != 0:
        raise RuntimeError(
            "GIT_READ_FAILED:worktree list in {} exited {}".format(
                repo, listed.returncode))
    wanted = "refs/heads/" + branch
    path: Optional[Path] = None
    for line in (listed.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):].strip())
        elif line.startswith("branch ") and line[len("branch "):].strip() == wanted:
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


def _reclaim_stranded_integration_worktree(
        repo: Path, runs_root: Optional[Path], branch: str) -> Optional[Path]:
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

    The predicate is path containment against the configured run root, read
    from `git worktree list`. It is deliberately not a claim, a message, or a
    naming convention (§1.2): a worktree *under this repository's own runs
    directory* was created by this system and is a leftover of a previous run;
    anything else may be the operator's own checkout and is left exactly where
    it is, so the caller still refuses and explains itself.

    Only the checkout holding the integration branch is ever named here.
    Attempt worktrees -- including the blocked ones §8.8 retains for
    post-mortem -- hold their own attempt branches, never this one, so they are
    outside what this function can even select. `worktree prune` drops
    administrative records of directories that are already gone and never
    removes a worktree that still exists.

    Returns the path released, or `None` when nothing was.
    """
    occupant = _worktree_holding_branch(repo, branch)
    released: Optional[Path] = None
    if (occupant is not None and runs_root is not None
            and _path_is_within(occupant.resolve(), runs_root.resolve())):
        result = subprocess.run(
            ("git", "-C", str(repo), "worktree", "remove", "--force",
             str(occupant)), capture_output=True, text=True, check=False)
        if result.returncode == 0:
            released = occupant
    subprocess.run(("git", "-C", str(repo), "worktree", "prune"),
                   capture_output=True, text=True, check=False)
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
            ("git", "-C", str(repo), "worktree", "remove", "--force",
             str(path)),
            capture_output=True, text=True, check=False)
    except OSError as exc:
        detail = str(exc)
    else:
        if released.returncode == 0:
            return
        detail = (released.stderr or released.stdout or "").strip()
    # stdout carries the run's report, so a failed release is reported beside
    # it rather than inside it: the operator learns there is a worktree left to
    # clean up, and the run still says exactly what it did.
    print("integration worktree release failed: {}: {}".format(path, detail),
          file=sys.stderr)


def _reviewer_occupancy(
        session: finalization_window.ReviewerSession) -> Optional[float]:
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
                encoding="utf-8", errors="replace").splitlines()
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


def _plan_finalize(args: argparse.Namespace) -> int:
    try:
        stored = Path(args.plan_file).read_bytes()
        validation = pv.validate_plan(
            stored, args.repo, receipts=_VerifiedReceipts(args),
            collector=_plan_collector(args))
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
            occupancy_reader=_reviewer_occupancy)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("FINALIZATION_CONFIGURATION_REQUIRED", str(exc))
    except (_PlanReceiptVerificationError, finalization.SignatureMissing,
            finalization.SignatureInvalid, finalization.ReceiptInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except finalization.ReportRejected as exc:
        # S6.5's terminal outcome for the bytes: no receipt exists, and the
        # rejection reason is the operator's whole diagnosis. It is a verb
        # outcome, not a crash -- `plan ship` reads the JSON `outcome`, so a
        # traceback here is invisible to every caller that reads it.
        return _refusal("REPORT_REJECTED", str(exc))
    except finalization.FinalizationStalled as exc:
        # A stall is a fact about the machine or the route, never a verdict
        # about the plan, and it writes no receipt -- so rerunning
        # `plan finalize` is legal and reviews afresh.
        return _refusal("FINALIZATION_STALLED", str(exc))
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
        record = store.set_aside(args.digest, invoked_by=args.invoked_by,
                                 reason=args.reason)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("SET_ASIDE_CONFIGURATION_REQUIRED", str(exc))
    except finalization.SetAsideRefused as exc:
        return _refusal("SET_ASIDE_REFUSED", str(exc))
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid, UnicodeError) as exc:
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
                "are required for receipt access"))
        entries = [
            {
                "record": json.loads(record.to_bytes().decode("utf-8")),
                "superseded_receipt": json.loads(
                    store.load_set_aside_receipt(
                        args.digest, record.sequence
                    ).to_bytes().decode("utf-8")),
            }
            for record in store.set_aside_records(args.digest)
        ]
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RECEIPT_VERIFICATION_CONFIGURATION_REQUIRED", str(exc))
    except (FileNotFoundError, finalization.ReceiptInvalid,
            finalization.SignatureMissing, finalization.SignatureInvalid,
            UnicodeError, ValueError, KeyError) as exc:
        return _plan_receipt_verification_refusal(exc)
    print(json.dumps(entries, sort_keys=True))
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
    except _RunRefused as exc:
        return exc.emit()
    except launcher.HarnessQuiescenceError as exc:
        return _quiescence_refusal(exc)
    except _PlanReceiptConfigurationError as exc:
        return _refusal("RUN_CONFIGURATION_REQUIRED", str(exc))
    except _PlanReceiptVerificationError as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except Exception as exc:
        # Everything with no better name, under the one it already had. The
        # arm that used to sit above this printed `type(exc).__name__.upper()`
        # and differed from it in nothing else, so the class name was the whole
        # of what it added — and a class name is not a refusal vocabulary. It
        # keeps its diagnostic value in `detail`, which is where prose belongs.
        return _refusal("RUN_EXECUTION_FAILED",
                        "{0}: {1}".format(type(exc).__name__, exc))


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
    # Every field of `SchedulerConfig` is named here, and
    # `test_every_scheduler_config_field_is_projected` fails if one is not.
    # This is the projection §7.4 describes: the one that copied a gate's
    # runner, argv and selector and dropped its threshold, because a
    # field-by-field copy has no way to notice the field it did not copy.
    return scheduler_types.SchedulerConfig(
        concurrency=args.concurrency, node_timeout_s=args.node_timeout_s,
        turn_timeout_s=args.turn_timeout_s,
        final_acceptance_timeout_s=args.final_acceptance_timeout_s,
        backstop_t_s=args.backstop_t_s, semantic_ceiling=args.semantic_ceiling,
        review_ceiling=_scheduler_setting(args, "review_ceiling"),
        environmental_retries=_scheduler_setting(
            args, "environmental_retries"),
        launcher_retries=_scheduler_setting(args, "launcher_retries"),
        credential_retries=_scheduler_setting(args, "credential_retries"))


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


def _refuse_base_commit_divergence(args: argparse.Namespace,
                                   plan: plan_model.Plan) -> Optional[int]:
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
            "resolved, so recorded-base identity cannot be verified: {2}"
            .format(base, branch, exc))
    if head.lower() != base_sha.lower():
        return _refusal(
            "BASE_COMMIT_DIVERGED",
            "integration branch {0} is at {1}, plan.base_commit {2} "
            "resolves to {3}. The single-repo path used to create attempt "
            "worktrees against whatever {0} pointed at and never compared "
            "the two.".format(branch, head, base, base_sha))
    return None


def _refuse_uncommittable_outputs(args: argparse.Namespace,
                                  plan: plan_model.Plan) -> Optional[int]:
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
        for nid, path in ignored)
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
        stored, args.repo, receipts=receipts, collector=_plan_collector(args))
    if not validation.eligible or validation.digest != args.digest:
        raise _RunRefused(
            "RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE",
            "the plan bytes are not canonical at {0}, or the plan is not "
            "eligible to run".format(args.digest))
    store = receipts._receipt_store()
    try:
        receipt = store.load(args.digest)
    except FileNotFoundError as exc:
        raise _receipt_absent(store, args.digest) from exc
    if receipt.verdict is not finalization.Verdict.PASS:
        raise _RunRefused(
            "RUN_RECEIPT_NOT_PASS",
            "the finalization receipt for {0} records {1}".format(
                args.digest, receipt.verdict.value))
    return plan_model.parse_bytes(stored)


def _receipt_absent(store: "finalization.ReceiptStore",
                    plan_digest: str) -> _RunRefused:
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
    except (OSError, ValueError, finalization.ReceiptInvalid,
            finalization.SignatureMissing, finalization.SignatureInvalid):
        records = ()
    if records:
        return _RunRefused(
            "RUN_RECEIPT_ABSENT",
            "the finalization receipt for {0} was set aside {1} time(s); "
            "run `maestro plan finalize` to review those bytes afresh".format(
                plan_digest, len(records)),
            cause="SET_ASIDE", set_aside_count=len(records))
    return _RunRefused(
        "RUN_RECEIPT_ABSENT",
        "no finalization receipt exists for {0}".format(plan_digest),
        cause="NEVER_FINALIZED", set_aside_count=0)


def _herdr_workspace_label(args: argparse.Namespace) -> str:
    """The name of the herdr workspace this run's panes land in.

    The plan's own name where there is one, because that is what the operator
    calls the work and what the sidebar has room for; the run id otherwise, so
    the workspace is still named after this run and not after whatever was in
    front of it.
    """
    name = str(getattr(args, "plan_name", "") or "")
    return name or str(getattr(args, "run_id", "") or "maestro")


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
        claude_path=Path(args.claude), admitted_routes=admitted,
        # §9.3's sixth operation. The adapter has implemented it since it was
        # written; nothing had ever handed it the argv to run, so it returned
        # immediately for every attempt of every run.
        provision_argv=tuple(getattr(args, "provision_argv", None) or ()),
        # One workspace per run, created by the launcher and named for the
        # plan. Every pane this run opens -- builders and the reviewers of the
        # same `route_runner` alike -- lands in it, so an operator has one
        # place to watch and an unrelated run's pane can never appear there.
        workspace_label=_herdr_workspace_label(args))


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
    launcher.HarnessCancelled, launcher.HarnessQuiescenceError,
    scheduler.AttemptCancelled, scheduler.AttemptOwnershipLost,
    scheduler.QuiescenceFailure, scheduler.LaunchFailed,
)


def _launcher_failure_for(adapter: Any,
                          exc: BaseException) -> retry_policy.LauncherFailure:
    """The typed launcher class for one failed launch or poll.

    STARTUP is the fall-through rather than a raise: an `ErrorClass` this
    mapping has not met is still a launch that failed, and refusing to name it
    would drop the attempt back to the ENVIRONMENTAL default — the exact
    misclassification this function exists to end.
    """
    return _LAUNCHER_FAILURE_BY_ERROR_CLASS.get(
        adapter.classify(exc), retry_policy.LauncherFailure.STARTUP)


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
            "{0}: {1}".format(type(exc).__name__, exc)) from exc


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
        "SESSION_PATH_MISSING:{0}#{1}".format(node_id, attempt_no))


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
                model_spec, route, exc)) from exc
    return agent_pi.context_window(provider, model_id)


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


def _poll_agent_execution(adapter: Any, handle: Any, envelope_path: Path,
                          record: Any, cancel_requested: Any,
                          quiesce_attempt: Any,
                          sleep: Any = time.sleep) -> "scheduler.NodeExecution":
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
                envelope_parsed=parsed, exit_code=state.exit_code or 1,
                launched_pid=handle.process_group,
                launch_detail=state.detail,
                envelope_payload=payload)
        if state.state is launcher.PollState.GONE:
            # TRANSPORT rather than STARTUP: the agent launched and then its
            # record vanished, so what was lost is the channel to it, not its
            # start. The budget is identical for all three non-CREDENTIAL
            # members, so the choice between them is diagnostic only — the
            # ledger reads this, never a branch.
            return scheduler.NodeExecution(
                envelope_parsed=False, exit_code=1,
                launched_pid=handle.process_group,
                launcher_failure=retry_policy.LauncherFailure.TRANSPORT,
                launch_detail=state.detail)
        if cancel_requested():
            quiesce_attempt(record, "cancel")
            return scheduler.NodeExecution(
                envelope_parsed=False, exit_code=1,
                launched_pid=handle.process_group)
        sleep(0.05)


def _resolve_run_runners(
        args: argparse.Namespace,
        plan: plan_model.Plan) -> Dict[str, "runner_resolution.ResolvedRunner"]:
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
    env = worktree.launch_env(scratch)
    wanted = {node.gate.runner for node in plan.agent_nodes}
    wanted.add(plan.merge_policy.integration_gate.runner)
    return {
        runner: runner_resolution.resolve(
            runner, Path(args.repo), ".", declared=declared.get(runner),
            env=env)
        for runner in sorted(wanted)
    }


def _execute_run(args: argparse.Namespace, *, resuming: bool) -> int:
    from threading import RLock

    config = _run_configuration(args)
    plan = _load_runnable_plan(args)
    _validate_run_paths(args, plan)
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
    runner_resolution.write_record(
        Path(args.integration_path).parent / "runner-resolution.json",
        resolved_runners.values())
    notice = runner_resolution.adoption_notice(resolved_runners.values())
    if notice:
        # stderr, because this verb's stdout is one JSON document and an
        # operator hint is not part of it.
        print(notice, file=sys.stderr)
    route_runner = _runtime_launcher(args) if plan.agent_nodes else None
    store = lc.LifecycleStore(args.db)
    handles = {}
    proven_absent = set()
    handles_lock = RLock()
    # The integration checkout this invocation added, and nothing else. The
    # refusal below returns while another worktree still holds the branch, and
    # a release that did not distinguish the two would delete that worktree --
    # which may be the operator's own. `None` until `worktree add` succeeds.
    created_integration_path: Optional[Path] = None
    try:
        if resuming:
            store.resume_run(args.run_id)
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
            _reclaim_stranded_integration_worktree(
                Path(args.repo), _configured_runs_root(args), branch)
            occupant = _worktree_holding_branch(Path(args.repo), branch)
            if occupant is not None:
                return _refusal(
                    "INTEGRATION_BRANCH_CHECKED_OUT",
                    "the integration branch " + branch + " is checked out at "
                    + str(occupant) + ", and git gives a branch to one worktree "
                    "at a time. The run's integration worktree must hold it, "
                    "because every attempt is based on that branch's head and a "
                    "detached copy would never advance it. Maestro reclaims the "
                    "stranded integration checkouts inside its own run root "
                    "without asking, and this one is not among them, so it has "
                    "been left exactly as it is. Move that checkout to another "
                    "branch and start the run again")
            subprocess.run(
                ("git", "-C", str(args.repo), "worktree", "add",
                 str(args.integration_path), branch),
                check=True, capture_output=True, text=True)
            created_integration_path = Path(args.integration_path)

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
            prompt_text = _agent_node_prompt(
                plan.node_by_id()[node.node_id], envelope, retry_prompt)
            # B13 for the builder. A retry prompt carries guidance derived from
            # the previous attempt's measured failure, so its size is a runtime
            # quantity and not an authored one; dispatching one that cannot fit
            # produces an attempt about a different task rather than an error.
            _preflight_prompt(prompt_text, args.agent_route, args.agent_model)
            prompt.write_text(prompt_text, encoding="utf-8")
            # Through `_typed_launch_pane` like every other dispatch, so the
            # builder's spec is given its route's window on the same path the
            # reviewers' are. Called directly against `runner.launch` before,
            # which meant one of the four dispatch sites would have reached the
            # launcher's B13 check with nothing measured on its spec.
            handle = _typed_launch_pane(route_runner, launcher.LaunchSpec(
                correlation_token="{}-{}-{}".format(
                    args.run_id, node.node_id, record.attempt_no),
                worktree=attempt.path, prompt_path=prompt,
                envelope_path=envelope, route=args.agent_route,
                model=args.agent_model, effort=args.agent_effort,
                profile=args.agent_profile,
                session_dir=attempt.scratch / "session",
                context_window_tokens=_route_context_window(
                    args.agent_route, args.agent_model),
                # This node's own tab, with room for the reviewer that will
                # judge it: two panes side by side rather than a builder and a
                # reviewer in unrelated corners of a flat tab.
                pane_group=node.node_id, pane_role="builder",
                pane_group_size=2,
                environment=launch_environment))
            with handles_lock:
                handles[key] = handle
                proven_absent.discard(key)
            _require_session_path(handle, node.node_id, record.attempt_no)
            # `process_group` first because a harness-spawned launch owns its
            # group outright; `liveness_pid` is the herdr-spawned fallback that
            # makes §7.6's PROCESS_DEAD signal reachable for an agent node at
            # all (#20). Both land in `attempts.pid`, which has exactly one
            # reader — the watchdog's `process_is_alive` check — while the kill
            # path below reads `handle.process_group` from `handles` and is
            # deliberately not given the fallback: see `LaunchHandle`, §8.3 and
            # §16.3 items 17 and 30.
            liveness_pid = handle.process_group
            if liveness_pid is None:
                liveness_pid = handle.liveness_pid
            store.mark_launched(
                args.run_id, node.node_id, record.attempt_no,
                liveness_pid,
                extra={watchdog.SESSION_PATH_KEY: str(handle.transcript_path)})
            on_launch(liveness_pid)
            return _poll_agent_execution(
                route_runner, handle, envelope, record, cancel_requested,
                quiesce_attempt)

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

        run_gate, run_integration_gate = _scheduler_gate_deps(
            plan, resolved_runners)
        review_attempt = (
            _code_review_runner(args, route_runner)
            if route_runner is not None and getattr(args, "review_root", None)
            else None)
        deps = scheduler.SchedulerDeps(
            store=store, repo=Path(args.repo),
            integration_path=Path(args.integration_path),
            integration_branch=plan.merge_policy.integration_branch,
            worktrees_root=Path(args.worktrees_root),
            scratch_root=Path(args.scratch_root), run_node=run_node,
            run_gate=run_gate, run_integration_gate=run_integration_gate,
            quiesce_attempt=quiesce_attempt,
            # §8.8's single integration gate, adjudicated at the number the
            # plan declared for it. Omitting this is what left final
            # acceptance counting to 1 while the plan asked for 70.
            integration_min_cases=(
                plan.merge_policy.integration_gate.min_cases),
            # §8.3's provision step, which the scheduler has always called and
            # nothing had ever supplied. Omitting it measured every baseline
            # against an unprovisioned tree and left §7.4's pre-gate red for
            # the ecosystem's missing install rather than for the node's
            # missing work.
            provision=_run_provisioner(args, route_runner),
            kill_attempt=lambda record: quiesce_attempt(record, "watchdog-kill"),
            review_attempt=review_attempt)
        try:
            report = scheduler.Scheduler(
                args.run_id, plan.to_plan_nodes(), config, deps,
                plan_digest=args.digest).run()
        except scheduler.RunPaused:
            # Not an outcome, and deliberately not printed as one: nothing was
            # declared, no node moved, and `run resume` is legal from here.
            print(json.dumps({
                "outcome": "PAUSED", "run_id": args.run_id,
            }, sort_keys=True))
            return 0
    finally:
        # Nested so that a failing close still releases the checkout, and a
        # failing release still leaves the store closed. Neither may become the
        # reason the run's own outcome goes unreported.
        try:
            store.close()
        finally:
            _release_run_integration_worktree(
                Path(args.repo), created_integration_path)
    print(json.dumps({"outcome": report.outcome.value,
                      "run_id": args.run_id,
                      "merged": list(report.merged),
                      "blocked": [
                          {"node_id": node, "reason": reason.value}
                          for node, reason in report.blocked],
                      # The findings that exhausted a node's review budget. A
                      # bare REVIEW_BUDGET_EXHAUSTED names the rule that fired
                      # and nothing an operator can act on. Read defensively:
                      # the scheduler is a seam tests substitute, and a run's
                      # exit status must not depend on a stand-in carrying
                      # every field of the real report.
                      "review_findings": dict(
                          getattr(report, "review_findings", {}) or {}),
                      # Findings-per-attempt for every reviewed node, in
                      # order. `review_findings` answers "what did the
                      # reviewer object to"; this answers "was it objecting
                      # less each time", which is the question behind
                      # `review_ceiling` and the one nothing in the run
                      # could answer once the process exited.
                      "review_convergence": {
                          node_id: list(counts)
                          for node_id, counts in dict(
                              getattr(report, "review_convergence", {})
                              or {}).items()}},
                     sort_keys=True))
    return 0


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
            attempt, runners[command[0]], command[1:], node.gate_selector,
            cancel_requested, label="{}-{}".format(node.node_id, phase))

    def run_integration_gate(integration_path, specs, cancel_requested):
        return worktree.run_integration_gate(
            integration_path, runners[integration_gate.runner],
            tuple(integration_gate.argv),
            Path(integration_path).parent / ".maestro",
            cancel_requested, label="integration-gate")

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
    read = {"branch": ("rev-parse", "--abbrev-ref", "HEAD"),
            "head": ("rev-parse", "HEAD"),
            "subject": ("log", "-1", "--format=%s")}
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
            ("git", "-C", str(path)) + command,
            capture_output=True, text=True)
        if completed.returncode == 0:
            found[key] = completed.stdout.strip()
        else:
            unreadable.append(key)
    if unreadable:
        found["unreadable"] = sorted(unreadable)
    return found


def _attempt_history(transitions: Sequence[Dict[str, Any]]
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
        history.setdefault((node_id, attempt_no), []).append({
            "reason": row.get("reason"),
            "from_state": row.get("from_state"),
            "to_state": row.get("to_state"),
            "actor": row.get("actor"),
            "at": row.get("created_at"),
            "detail": row.get("detail", {}),
        })
    return history


def _attempt_verdict(entries: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The sentence that explains why an attempt ended, when there is one."""
    for entry in entries:
        detail = entry.get("detail") or {}
        verdict = detail.get("verdict")
        if verdict:
            return str(verdict)
    return None


def _live_state(record: "lc.RunRecord",
                nodes: Sequence["lc.NodeRow"]) -> str:
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


def _run_progress(reader: "lc.LifecycleReader", record: "lc.RunRecord",
                  args: argparse.Namespace) -> Dict[str, Any]:
    """Everything `run status` knows about one run, as one document."""
    now = time.time()
    nodes = reader.nodes(record.run_id)
    attempts = reader.attempts(record.run_id)
    history = _attempt_history(reader.transitions(record.run_id))
    results = reader.results(record.run_id)
    names = {digest: name for name, digest
             in (getattr(args, "plan_digests", None) or {}).items()}

    by_node: Dict[str, List[Dict[str, Any]]] = {}
    in_flight: List[Dict[str, Any]] = []
    for attempt in attempts:
        entries = history.get((attempt.node_id, attempt.attempt_no), [])
        started = attempt.launched_at or attempt.started_at or None
        running = attempt.state is scheduler_types.NodeState.RUNNING
        projected = {
            "attempt_no": attempt.attempt_no,
            "state": attempt.state.value,
            "base_sha": attempt.base_sha,
            "turn_count": attempt.turn_count,
            "retry_class": (attempt.retry_class.value
                            if attempt.retry_class else None),
            "pid": attempt.pid,
            "started_at": attempt.started_at or None,
            "launched_at": attempt.launched_at,
            "running": running,
            "elapsed_s": (max(0.0, now - started)
                          if running and started else None),
            "session_path": attempt.extra.get(watchdog.SESSION_PATH_KEY),
            "verdict": _attempt_verdict(entries),
            "transitions": entries,
        }
        by_node.setdefault(attempt.node_id, []).append(projected)
        if running:
            in_flight.append({"node_id": attempt.node_id,
                              "attempt_no": attempt.attempt_no,
                              "turn_count": attempt.turn_count,
                              "elapsed_s": projected["elapsed_s"]})

    projected_nodes = []
    for node in nodes:
        node_attempts = by_node.get(node.node_id, [])
        projected_nodes.append({
            "node_id": node.node_id,
            "kind": node.kind,
            "depth": node.depth,
            "needs": list(node.needs),
            "state": node.state.value,
            "block_reason": (node.block_reason.value
                             if node.block_reason else None),
            "attempt_no": node.attempt_no,
            "attempts_recorded": len(node_attempts),
            "granted_extra_attempts": node.granted_extra_attempts,
            "output_sha": node.output_sha,
            "updated_at": node.updated_at,
            "idle_s": _since(node.updated_at, now),
            "attempts": node_attempts,
        })

    integration = None
    state_root = getattr(args, "repository_state", None)
    if state_root:
        integration = _integration_head(
            Path(state_root) / "runs" / record.run_id / "integration")
    return {
        "run_id": record.run_id,
        "plan_name": names.get(record.plan_digest),
        "plan_digest": record.plan_digest,
        "state": _live_state(record, nodes),
        "declared_outcome": (record.latest_outcome.value
                             if record.latest_outcome else None),
        "declared_outcome_at": record.latest_outcome_at,
        "cancel_requested": record.cancel_requested,
        # Which of §7.3's two cancellation shapes the declared CANCELLED was,
        # and therefore whether `run resume` will take this run back. Reported
        # rather than left to be inferred from the node states: the inference
        # is exactly the heuristic the stored cause exists to replace.
        "cancel_cause": (record.cancel_cause.value
                         if record.cancel_cause else None),
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
        "results": [
            {"node_id": row.get("node_id"), "attempt_no": row.get("attempt_no"),
             "adjudication": row.get("adjudication"),
             "subject_sha": row.get("subject_sha"), "at": row.get("created_at"),
             "payload": row.get("payload")}
            for row in results],
    }


def _render_progress(progress: Dict[str, Any]) -> str:
    """The default human view. One run, top to bottom, newest fact last."""
    lines = ["{}  {}".format(progress["run_id"],
                             progress["plan_name"] or "(plan not installed)")]
    declared = progress["declared_outcome"]
    lines.append("  state        {}{}".format(
        progress["state"],
        "" if declared is None
        else "   (last declared outcome {} at {})".format(
            declared, progress["declared_outcome_at"])))
    lines.append("  plan digest  {}".format(progress["plan_digest"]))
    lines.append("  started      {}   ({} ago)".format(
        progress["created_at"], _duration(progress["elapsed_s"])))
    lines.append("  last change  {}   ({} ago)".format(
        progress["last_transition_at"], _duration(progress["idle_s"])))
    if progress["cancel_requested"]:
        lines.append("  cancel       requested")
    if progress["cancel_cause"]:
        lines.append("  cancel cause {}{}".format(
            progress["cancel_cause"],
            "   (resumable)"
            if progress["cancel_cause"]
            == scheduler_types.CancelCause.RUN_CANCEL.value
            else "   (not reopenable)"))
    if progress["scheduler_pid"]:
        alive = progress["scheduler_alive"]
        lines.append("  scheduler    pid {} on {} — {}".format(
            progress["scheduler_pid"], progress["scheduler_host"] or "?",
            "alive" if alive else "gone" if alive is False else "unknown host"))
    integration = progress["integration"]
    if integration is None:
        lines.append("  integration  (no worktree found)")
    else:
        lines.append("  integration  {} @ {}".format(
            integration.get("branch") or "?",
            (integration.get("head") or "?")[:12]))
        lines.append("               {}".format(integration["path"]))
        if integration.get("subject"):
            lines.append("               head: {}".format(
                integration["subject"]))

    lines.append("")
    lines.append("  {:<44} {:<10} {:>8}  {}".format(
        "NODE", "STATE", "ATTEMPT", "DETAIL"))
    for node in progress["nodes"]:
        detail = node["block_reason"] or ""
        live = [item for item in node["attempts"] if item["running"]]
        if live:
            detail = "in flight {}, {} turns".format(
                _duration(live[0]["elapsed_s"]), live[0]["turn_count"])
        elif node["output_sha"]:
            detail = "output {}".format(node["output_sha"][:12])
        lines.append("  {:<44} {:<10} {:>8}  {}".format(
            node["node_id"][:44], node["state"], node["attempt_no"], detail))

    for node in progress["nodes"]:
        if not node["attempts"]:
            continue
        lines.append("")
        lines.append("  {} — attempts".format(node["node_id"]))
        for attempt in node["attempts"]:
            outcome = attempt["retry_class"] or attempt["state"]
            lines.append("    a{:<3} {:<10} {:<22} {:>4} turns  {}".format(
                attempt["attempt_no"], attempt["state"], outcome,
                attempt["turn_count"],
                _duration(attempt["elapsed_s"]) if attempt["running"] else ""))
            if attempt["verdict"]:
                lines.append("         why: {}".format(attempt["verdict"]))
            if attempt["session_path"]:
                lines.append("         session: {}".format(
                    attempt["session_path"]))
        if node["block_reason"]:
            lines.append("    BLOCKED: {}".format(node["block_reason"]))

    for row in progress["results"]:
        lines.append("")
        lines.append("  result {}#{} {}".format(
            row["node_id"], row["attempt_no"], row["adjudication"] or ""))
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
        rows = [{"run_id": record.run_id,
                 "plan_name": names.get(record.plan_digest),
                 "plan_digest": record.plan_digest,
                 "created_at": record.created_at,
                 "last_transition_at": record.last_transition_at,
                 "declared_outcome": (record.latest_outcome.value
                                      if record.latest_outcome else None),
                 "cancel_requested": record.cancel_requested,
                 "scheduler_pid": record.scheduler_pid,
                 "scheduler_alive": lc.scheduler_liveness(record),
                 "state": _live_state(record, reader.nodes(record.run_id))}
                for record in records]
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(rows, sort_keys=True))
        return 0
    if not rows:
        print("no runs")
        return 0
    print("{:<40} {:<28} {:<11} {:<11} {}".format(
        "RUN", "PLAN", "STATE", "DECLARED", "STARTED"))
    for row in rows:
        print("{:<40} {:<28} {:<11} {:<11} {}".format(
            row["run_id"], (row["plan_name"] or row["plan_digest"][:12])[:28],
            row["state"], row["declared_outcome"] or "-", row["created_at"]))
    return 0


def _run_convergence(args: argparse.Namespace) -> int:
    """Findings-per-attempt per lane, for a run that has already finished (#30).

    The scheduler prints this series when a run ends in the process that ran
    it. That is the only place it has ever existed, so a run that was resumed,
    cancelled, or simply read the next morning could not be asked whether its
    reviewers were converging — and `execution.review_ceiling` was sized by
    hand from three lanes somebody happened to still have on screen.

    Same reader, same tables, same query path as `run status` (§10.6). It
    writes nothing and decides nothing; the ledger it opens is `mode=ro`.
    """
    if not getattr(args, "db", None):
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    reader = _open_reader(args.db)
    try:
        record = _select_run(reader, args)
        profile = review_convergence.run_convergence(
            record.run_id, reader.nodes(record.run_id),
            reader.attempts(record.run_id), reader.transitions(record.run_id),
            review_ceiling=getattr(args, "review_ceiling", None))
    finally:
        reader.close()
    if getattr(args, "as_json", False):
        print(json.dumps(profile.as_dict(), sort_keys=True))
    else:
        print(review_convergence.render(profile))
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
        print(json.dumps({
            "outcome": "ALREADY_STOPPED", "run_id": run_id,
            "scheduler_pid": pid, "scheduler_alive": alive,
        }, sort_keys=True))
        return 0
    if target is None:
        return _refusal(
            "PAUSE_PID_UNPROVEN",
            "recorded scheduler pid {0} exists but is not proven to be "
            "the process that claimed this run".format(pid))
    try:
        os.kill(target, signal.SIGINT)
    except ProcessLookupError:
        print(json.dumps({
            "outcome": "ALREADY_STOPPED", "run_id": run_id,
            "scheduler_pid": pid, "scheduler_alive": False,
        }, sort_keys=True))
        return 0
    except OSError as exc:
        return _refusal("PAUSE_SIGNAL_FAILED", str(exc))
    print(json.dumps({
        "outcome": "PAUSE_REQUESTED", "run_id": run_id,
        "scheduler_pid": target,
    }, sort_keys=True))
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
        if outcome in (scheduler_types.RunOutcome.ACCEPTED,
                       scheduler_types.RunOutcome.CANCELLED):
            return _refusal(
                "RUN_ALREADY_TERMINAL",
                "{0} already declared {1}".format(run_id, outcome.value))
        # Named before the discard, not after: this is work that reached a
        # measured predicate and is about to stop being reachable, and the
        # operator is the only one who can decide that is acceptable.
        adoptable = store.adoptable_attempts(run_id)
        if adoptable:
            print(
                "cancel --discard will make these completed attempts "
                "unreachable through Maestro (§7.3): "
                + ", ".join(
                    "{0} ({1})".format(row["node_id"], row["why"])
                    for row in adoptable),
                file=sys.stderr)
        store.cancel_run(run_id, cause=scheduler_types.CancelCause.DISCARDED)
        store.declare_outcome(
            run_id, cancel_cause=scheduler_types.CancelCause.DISCARDED)
        print(json.dumps({
            "outcome": "CANCELLED", "run_id": run_id,
            "unreachable": list(adoptable),
        }, sort_keys=True))
        return 0
    finally:
        store.close()


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
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid) as exc:
        return _refusal("RECEIPT_VERIFICATION_FAILED", str(exc))
    except Exception as exc:
        return _refusal("RUN_EXECUTION_FAILED",
                        "{0}: {1}".format(type(exc).__name__, exc))


def _escape(args: argparse.Namespace) -> int:
    store = _store(args)
    if store is None:
        return _refusal("RUN_CONFIGURATION_REQUIRED", "--db is required")
    try:
        if args.command == "retry":
            row = store.retry(args.run_id, args.node_id, force=args.force,
                              grant=getattr(args, "grant", 0) or 0)
        elif args.command == "skip":
            row = store.skip(args.run_id, args.node_id,
                             accept_sha=args.accept_sha, repo_path=args.repo)
        else:
            row = store.abandon(args.run_id, args.node_id)
        print(json.dumps({"node_id": row.node_id, "state": row.state.value}))
        return 0
    finally:
        store.close()


def _parse_salvage_seed(value: object) -> bytes:
    if not value:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED",
            "--signing-seed is required")
    try:
        seed = bytes.fromhex(str(value))
    except (TypeError, ValueError) as exc:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED",
            "signing seed must be hexadecimal") from exc
    if len(seed) != receipt_crypto.SEED_SIZE:
        raise salvage.SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED",
            "signing seed must be a 32-byte Ed25519 seed")
    return seed


def _attempt_salvage(args: argparse.Namespace) -> int:
    missing = [flag for flag, value in (
        ("--worktrees-root", getattr(args, "worktrees_root", None)),
        ("--scratch-root", getattr(args, "scratch_root", None)),
    ) if not value]
    if missing:
        return _refusal(
            "RUN_CONFIGURATION_REQUIRED",
            " and ".join(missing) + " is required outside a configured "
            "repository")
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
        print(json.dumps({
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
                None if result.uncommittable_outputs is None
                else list(result.uncommittable_outputs)),
        }, sort_keys=True))
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
            "the plan pipeline requires an installed "
            + str(_MAESTRO_CONFIG_FILE))
    return _load_maestro_layout(config_path.parent.parent.resolve(), config_path)


def _plan_contract_path(layout: Dict[str, Any], name: str, suffix: str) -> Path:
    """<plans_dir>/<name><suffix>. An operator names the plan and nothing else."""
    path = (layout["plans_dir"] / (_named_plan_name(name) + suffix)).resolve()
    if not _path_is_within(path, layout["plans_dir"]):
        raise _MaestroConfigurationError(
            "plan contract artifact resolves outside plans_dir")
    return path


def _plan_contract_artifacts(layout: Dict[str, Any], name: str) -> Dict[str, Path]:
    return {
        "plan_ir": _plan_contract_path(layout, name, _PLAN_CONTRACT_IR_SUFFIX),
        "rendered": _plan_contract_path(
            layout, name, _PLAN_CONTRACT_RENDERED_SUFFIX),
        "receipt": _plan_contract_path(
            layout, name, _PLAN_CONTRACT_RECEIPT_SUFFIX),
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
        + str(_MAESTRO_CONFIG_FILE) + ", install the plan-contract skill at "
        + str(_PLAN_CONTRACT_REPOSITORY_SKILL) + ", or export "
        + _PLAN_CONTRACT_SKILL_ENV + "; searched "
        + ", ".join(str(_planctl_script(item)) for item in searched))


def _planctl_supports_repo_root(script: Path) -> bool:
    """Ask the installed planctl: the flag is pending upstream, so never assume."""
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "validate", "--help"],
            capture_output=True, text=True, check=False, timeout=120)
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
            "the repository root, but plans_dir is " + str(layout["plans_dir"])
            + "; install a planctl that supports --repo-root or set plans_dir "
            "to the repository root")
    return None


def _reviewer_key_environment_names(layout: Dict[str, Any]) -> Tuple[str, ...]:
    """Every variable that could carry the reviewer key into this process."""
    configured = layout["key_env"].get("reviewer_hmac_key_env")
    names = [_REVIEWER_HMAC_KEY_ENV]
    if configured and configured not in names:
        names.append(configured)
    return tuple(names)


def _reviewer_keys_in_environment(layout: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(name for name in _reviewer_key_environment_names(layout)
                 if os.environ.get(name))


def _reviewer_hmac_key_file(layout: Dict[str, Any]) -> Path:
    return Path(layout["repository_state"]) / "keys" / _REVIEWER_HMAC_KEY_FILE


def _existing_reviewer_hmac_key(path: Path) -> str:
    try:
        existing = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise _MaestroConfigurationError(
            "the reviewer key at " + str(path) + " is unreadable; it is not "
            "replaced, because that would invalidate every receipt already "
            "signed with it") from exc
    if len(existing.encode("utf-8")) < _REVIEWER_HMAC_KEY_MINIMUM_BYTES:
        raise _MaestroConfigurationError(
            "the reviewer key at " + str(path) + " is shorter than "
            + str(_REVIEWER_HMAC_KEY_MINIMUM_BYTES) + " bytes; it is not "
            "regenerated, because that would invalidate every receipt already "
            "signed with it")
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
            + str(path) + "; state_root must resolve outside the repository")
    for ancestor in (path.parent, *path.parent.parents):
        if (ancestor / ".git").exists():
            raise _MaestroConfigurationError(
                "the reviewer key would be stored inside the git work tree at "
                + str(ancestor) + "; point state_root outside every repository")
    if path.exists():
        return _existing_reviewer_hmac_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path.parent), stat.S_IRWXU)
    minted = secrets.token_hex(_REVIEWER_HMAC_KEY_MINTED_BYTES)
    try:
        descriptor = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        return _existing_reviewer_hmac_key(path)
    except OSError as exc:
        raise _MaestroConfigurationError(
            "cannot create the reviewer key at " + str(path)) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(minted + "\n")
    except OSError as exc:
        raise _MaestroConfigurationError(
            "cannot write the reviewer key at " + str(path)) from exc
    return minted


def _reviewer_hmac_key(layout: Dict[str, Any]) -> str:
    """Operator-supplied environment wins; the key Maestro minted is the default."""
    for name in _reviewer_key_environment_names(layout):
        supplied = os.environ.get(name)
        if supplied:
            if len(supplied.encode("utf-8")) < _REVIEWER_HMAC_KEY_MINIMUM_BYTES:
                raise _MaestroEnvironmentError(
                    name + " must carry at least "
                    + str(_REVIEWER_HMAC_KEY_MINIMUM_BYTES) + " bytes")
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
            [self._herdr, *args], capture_output=True, text=True,
            check=False, timeout=30.0)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "herdr failed").strip()[-200:])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def open(self) -> None:
        """Prove the pane exists before a single artifact is written."""
        try:
            payload = self._call(
                "pane", "split", "--current", "--direction", "right",
                "--cwd", str(self._cwd), "--no-focus")
            container = payload.get("result", payload)
            pane = container.get("pane") if isinstance(container, dict) else None
            if not isinstance(pane, dict) or not pane.get("pane_id"):
                raise RuntimeError("herdr opened no pane")
            pane_id = str(pane["pane_id"])
        except (OSError, RuntimeError, ValueError,
                subprocess.SubprocessError) as exc:
            raise _PlanPaneUnavailable(
                "no visible Herdr pane (" + (str(exc) or type(exc).__name__)
                + "); start Herdr and rerun, or fix executables.herdr in "
                + str(_MAESTRO_CONFIG_FILE)
                + ". Nothing was rendered, reviewed, or authored.") from exc
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            self._log.write_text("", encoding="utf-8")
            self._call("pane", "run", pane_id, "tail", "-n", "+1", "-f",
                       str(self._log))
        except (OSError, RuntimeError, ValueError,
                subprocess.SubprocessError) as exc:
            self.pane_id = pane_id
            self.close()
            raise _PlanPaneUnavailable(
                "the Herdr pane could not stream this step ("
                + (str(exc) or type(exc).__name__)
                + "). Nothing was rendered, reviewed, or authored.") from exc
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
    return (Path(layout["repository_state"]) / "plan-contract" / name
            / (verb + ".log"))


def _planctl_run(
        script: Path, layout: Dict[str, Any], repo_root: Optional[Path],
        verb: str, plan_ir: Path, arguments: Sequence[str], *,
        log: Path, reviewer_key: Optional[str] = None
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
            argv, cwd=layout["repo"], env=environment)
    except (launcher.HarnessCancelled, launcher.HarnessQuiescenceError,
            TimeoutError, OSError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))
    return subprocess.CompletedProcess(
        argv, completed.returncode, _log_tail(log, before), completed.stderr or "")


def _log_tail(log: Path, offset: int) -> str:
    try:
        with log.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return ""


def _plan_contract_step_failure(
        outcome: str, step: str, completed: subprocess.CompletedProcess,
        pane: Dict[str, Any], secret: Optional[str] = None) -> int:
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
        args: argparse.Namespace, verb: str,
        steps: Sequence[Tuple[str, Sequence[str]]], *, outcome: str,
        failure: str, plan_ir: Path, layout: Dict[str, Any],
        reviewer_key: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None) -> int:
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
                script, layout, repo_root, step, plan_ir, arguments,
                log=pane.log, reviewer_key=reviewer_key)
            if completed.returncode != 0:
                return _plan_contract_step_failure(
                    failure, step, completed, pane.report(), reviewer_key)
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
            "not hold the key that authorizes its own plan")
    artifacts = _plan_contract_artifacts(layout, args.plan_name)
    plan_ir = artifacts["plan_ir"]
    if not plan_ir.is_file():
        return _refusal(
            "PLAN_CONTRACT_IR_MISSING", "no Plan IR at " + str(plan_ir))
    rendered = artifacts["rendered"]
    return _run_plan_contract(
        args, "gate", (
            ("render", ("--out", str(rendered))),
            ("validate", ("--rendered", str(rendered))),
            ("mutate", ("--rendered", str(rendered))),
        ), outcome="PLAN_GATED", failure="PLAN_GATE_FAILED",
        plan_ir=plan_ir, layout=layout, extra={"rendered": str(rendered)})


def _plan_review(args: argparse.Namespace) -> int:
    """review + validate --require-approved, holding the key Maestro owns."""
    layout = _plan_contract_layout()
    artifacts = _plan_contract_artifacts(layout, args.plan_name)
    plan_ir = artifacts["plan_ir"]
    if not plan_ir.is_file():
        return _refusal(
            "PLAN_CONTRACT_IR_MISSING", "no Plan IR at " + str(plan_ir))
    rendered = artifacts["rendered"]
    if not rendered.is_file():
        return _refusal(
            "PLAN_CONTRACT_RENDER_MISSING",
            "no rendered plan at " + str(rendered)
            + "; run: maestro plan gate " + args.plan_name)
    reviewer = layout["reviewer"]
    missing = [key for key in ("id", "vendor") if not reviewer.get(key)]
    if missing:
        return _refusal(
            "REVIEWER_IDENTITY_UNCONFIGURED",
            "reviewer." + " and reviewer.".join(missing) + " must be set in "
            + str(_MAESTRO_CONFIG_FILE))
    receipt = artifacts["receipt"]
    return _run_plan_contract(
        args, "review", (
            ("review", ("--rendered", str(rendered), "--receipt-out",
                        str(receipt), "--reviewer", reviewer["id"],
                        "--reviewer-vendor", reviewer["vendor"])),
            ("validate", ("--rendered", str(rendered), "--receipt",
                          str(receipt), "--require-approved")),
        ), outcome="PLAN_REVIEWED", failure="PLAN_REVIEW_FAILED",
        plan_ir=plan_ir, layout=layout,
        reviewer_key=_reviewer_hmac_key(layout),
        extra={"rendered": str(rendered), "receipt": str(receipt),
               "reviewer": reviewer["id"],
               "reviewer_vendor": reviewer["vendor"]})


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
            return {option: item.dest
                    for item in author._actions
                    for option in item.option_strings}
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
                missing_detail="receipt configuration is required to ship")
        except (_PlanReceiptConfigurationError, _MaestroConfigurationError,
                finalization.ReceiptStoreLocationError, OSError, ValueError):
            # No store means no answer, and no answer means do not replace.
            return True
        try:
            return store.load(digest).verdict is finalization.Verdict.PASS
        except FileNotFoundError:
            return False
        except (finalization.ReceiptInvalid, finalization.SignatureMissing,
                finalization.SignatureInvalid, UnicodeError, ValueError,
                KeyError, OSError):
            return True
    return approved


class _ShipSupersedeRefused(RuntimeError):
    """The plan on disk differs from the projection and is already approved."""


def _superseded_plan_path(destination: Path, digest: str) -> Path:
    """Where a replaced plan's bytes are kept, keyed by their own digest."""
    return destination.parent / "superseded" / digest / destination.name


def _plan_ship_authoring(destination: Path, projected: bytes,
                         approved: Callable[[str], bool]) -> Optional[str]:
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
    that binds a reviewer to those exact bytes, and a run keys on the digest,
    so replacing the file could pull the plan out from under work that refers
    to it. `FAIL` carries no such risk -- it is terminal for those bytes, so
    nothing may ever run them -- and neither does a plan with no receipt at
    all.
    """
    if not destination.exists():
        return None
    existing = destination.read_bytes()
    if existing == projected:
        return None
    superseded = plan_digest.digest_of(existing)
    if approved(superseded):
        raise _ShipSupersedeRefused(
            "the plan on disk ({}) is approved and differs from the projected "
            "plan; finalize it or remove its approval before re-shipping"
            .format(superseded))
    archive = _superseded_plan_path(destination, superseded)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(existing)
    destination.unlink()
    return superseded


def _plan_ship_step_failure(step: str, status: int,
                            pane: Dict[str, Any]) -> int:
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
            ("rendered", "PLAN_CONTRACT_RENDER_MISSING",
             "; run: maestro plan gate " + args.plan_name),
            ("receipt", "PLAN_CONTRACT_RECEIPT_MISSING",
             "; run: maestro plan review " + args.plan_name)):
        if not artifacts[label].is_file():
            return _refusal(
                outcome, "no " + label.replace("_", " ") + " at "
                + str(artifacts[label]) + remedy)
    options = _plan_author_options()
    required = (_PLAN_CONTRACT_AUTHOR_OPTION, _PLAN_CONTRACT_RECEIPT_OPTION,
                _PLAN_CONTRACT_RENDERED_OPTION)
    if any(option not in options for option in required):
        return _refusal(
            "PLAN_CONTRACT_INGRESS_UNAVAILABLE",
            "the installed plan author verb has no "
            + _PLAN_CONTRACT_AUTHOR_OPTION
            + ", so an approved Plan IR cannot be projected onto a Maestro plan")
    author_args = _configured_plan_step("author", args.plan_name)
    setattr(author_args, options[_PLAN_CONTRACT_AUTHOR_OPTION],
            str(artifacts["plan_ir"]))
    setattr(author_args, options[_PLAN_CONTRACT_RECEIPT_OPTION],
            str(artifacts["receipt"]))
    setattr(author_args, options[_PLAN_CONTRACT_RENDERED_OPTION],
            str(artifacts["rendered"]))

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
            projected, _draft, _ir = (
                plan_contract_ingress.project_canonical_plan(
                    artifacts["plan_ir"], artifacts["receipt"],
                    Path(author_args.repo), artifacts["rendered"]))
        except (plan_author.AuthoringError,
                plan_contract_ingress.IngressError) as exc:
            return _refusal("PLAN_AUTHORING_FAILED", str(exc))
        try:
            superseded = _plan_ship_authoring(
                destination, projected, _plan_ship_approved(args))
        except _ShipSupersedeRefused as exc:
            return _refusal("PLAN_SUPERSEDE_REFUSED", str(exc))
        except OSError as exc:
            return _refusal("PLAN_SUPERSEDE_FAILED", str(exc))
        authored_already = destination.exists()
        steps = (("validate", _plan_validate, None),
                 ("finalize", _plan_finalize, None))
        if not authored_already:
            steps = (("author", _plan_author, author_args),) + steps
        for step, handler, prepared in steps:
            pane.note("$ maestro plan " + step + " " + args.plan_name)
            with _redirect_stdout(_PaneTee(sys.stdout, pane.log)):
                status = int(handler(
                    prepared if prepared is not None
                    else _configured_plan_step(step, args.plan_name)))
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
            "maestro deliver requires an installed " + str(_MAESTRO_CONFIG_FILE))
    return _load_maestro_config(config_path.parent.parent.resolve(), config_path)


def _deliver_author_lane(config: Dict[str, Any]) -> deliver_module.AuthorLane:
    configured = config.get("author")
    if not configured:
        raise _MaestroConfigurationError(
            "maestro deliver requires an author: block in "
            + str(_MAESTRO_CONFIG_FILE) + " naming the route, model, effort, "
            "and timeouts of the authoring lane")
    return deliver_module.AuthorLane(**configured)


def _deliver_runner(config: Dict[str, Any]) -> launcher.HerdrLauncher:
    """The authoring lane rides the same admitted-route launcher as every
    other agent turn. No new trust material is minted for it."""
    route_keys = (bytes.fromhex(config["route_verify_key"]),)
    admitted = route_receipts.load_admitted_routes(
        dict(config["route_paths"]), verify_keys=route_keys)
    return launcher.HerdrLauncher(
        herdr_path=Path(config["executables"]["herdr"]),
        omp_path=Path(config["executables"]["omp"]),
        claude_path=Path(config["executables"]["claude"]),
        admitted_routes=admitted)


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
            capture_output=True, text=True, check=False, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        pass


def _deliver_author_turn(config: Dict[str, Any],
                         lane: deliver_module.AuthorLane,
                         runner: launcher.HerdrLauncher,
                         session_root: Path):
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
        handle = _typed_launch_pane(runner, launcher.LaunchSpec(
            correlation_token=token, worktree=Path(config["repo"]),
            prompt_path=prompt_path, envelope_path=envelope,
            route=lane.route, model=lane.model, effort=lane.effort,
            profile=lane.profile, session_dir=session_dir,
            # B13 at the chokepoint, stated here rather than skipped. The
            # author lane runs the `claude` route, which publishes no model
            # catalog -- `opus` does not resolve in omp's -- so this resolves
            # to `None` and `preflight_launch_prompt` makes no comparison.
            # That is the answer for a route with no declared window: refuse
            # to invent one, and refuse nothing. It is a property of the route
            # (`handoff_budget.ROUTES_PUBLISHING_A_WINDOW`), so the day the
            # route publishes a catalog this site is covered with no edit.
            context_window_tokens=_route_context_window(lane.route, lane.model),
            environment=worktree.launch_env(
                session_root / (token + ".scratch"))))
        deadline = time.monotonic() + lane.author_timeout_s
        state = None
        try:
            while time.monotonic() < deadline:
                state = runner.poll(handle)
                if state.state in (launcher.PollState.EXITED,
                                   launcher.PollState.GONE):
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
                    kind, state.detail if state is not None else "TIMEOUT"))
        try:
            payload = json.loads(envelope.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise deliver_module.DeliverError(
                "AUTHOR_LANE_ENVELOPE_UNPARSED:{}".format(kind)) from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("summary") or "")
            raise deliver_module.DeliverError(
                "AUTHOR_LANE_FAILED:{}:{}".format(kind, detail))
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
            str(config["receipt_dir"]), repo_paths=(config["repo"],),
            data_dir=str(config["data_dir"]),
            verify_keys=(bytes.fromhex(config["verify_key"]),),
            create=False)
        receipt = store.load(plan_digest.digest_of(stored))
    except (finalization.ReceiptInvalid, finalization.SignatureMissing,
            finalization.SignatureInvalid,
            finalization.ReceiptStoreLocationError,
            receipt_crypto.KeyMaterialError, KeyError, OSError, ValueError):
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
            "file:{}?mode=ro".format(database), uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT run_id FROM runs WHERE plan_digest=? AND latest_outcome=?"
            " ORDER BY latest_outcome_at DESC LIMIT 1",
            (plan_digest.digest_of(stored),
             scheduler_types.RunOutcome.ACCEPTED.value)).fetchone()
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
            rows.append({
                "lane": record.node_id,
                "state": node.state.value,
                "attempt": node.attempt_no,
                "reason": (node.block_reason.value
                           if node.block_reason else None),
            })
        return tuple(rows)
    except (lc.LifecycleError, sqlite3.Error, KeyError):
        return ()
    finally:
        store.close()


def _deliver_release_run(config: Dict[str, Any], name: str):
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
    """
    stored = _deliver_plan_bytes(config, name)
    if stored is None:
        return ()
    try:
        branch = plan_model.parse_bytes(stored).merge_policy.integration_branch
    except (ValueError, KeyError):
        return ()
    released = _reclaim_stranded_integration_worktree(
        Path(config["repo"]), config["repository_state"] / "runs", branch)
    return () if released is None else (str(released),)


def _deliver_reviewer_report(config: Dict[str, Any], name: str):
    """The report behind a finalization verdict, so a FAIL yields its cells."""
    plan_file = (config["plans_dir"] / name / "maestro-plan.v1")
    if not plan_file.is_file():
        return None
    digest = plan_digest.digest_of(plan_file.read_bytes())
    report = (config["repository_state"] / "finalization" / digest
              / "report.json")
    if not report.is_file():
        return None
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _deliver_remove_plan(config: Dict[str, Any], name: str) -> None:
    """`plan author` is create-once, so a re-ship starts from no plan.

    Bounded to `plans_dir` by the same check every other derived path uses: a
    verb that deletes directories may not be talked into deleting one outside
    the tree it owns.
    """
    directory = (config["plans_dir"] / _named_plan_name(name)).resolve()
    if not _path_is_within(directory, config["plans_dir"]):
        raise _MaestroConfigurationError(
            "plan directory resolves outside plans_dir: " + name)
    if directory.is_dir():
        shutil.rmtree(directory)


def _deliver(args: argparse.Namespace) -> int:
    config = _deliver_config()
    lane = _deliver_author_lane(config)
    spec = Path(args.spec)
    resolved_spec = (spec if spec.is_absolute()
                     else (Path(config["repo"]) / spec)).resolve()
    if not resolved_spec.is_file():
        return _refusal("DELIVER_SPEC_MISSING",
                        "no source document at " + str(resolved_spec))
    if not _path_is_within(resolved_spec, config["repo"]):
        return _refusal(
            "DELIVER_SPEC_OUTSIDE_REPOSITORY",
            "a source document is pinned by repository-relative path, and "
            + str(resolved_spec) + " is outside " + str(config["repo"]))
    relative = str(resolved_spec.relative_to(Path(config["repo"]).resolve()))

    session_root = config["repository_state"] / "deliver"
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
        reviewer_report=lambda name: _deliver_reviewer_report(config, name),
        request=args.request or "",
        max_attempts=args.max_attempts,
        remove_plan_dir=lambda name: _deliver_remove_plan(config, name),
        run_start=None if args.no_run else _deliver_run_start,
        accepted_run=lambda name: _deliver_accepted_run(config, name),
        blocked_lanes=lambda run_id: _deliver_blocked_lanes(config, run_id),
        release_run=lambda name: _deliver_release_run(config, name),
        shipped=lambda name: _deliver_shipped(config, name),
        ledger_path=session_root / (_deliver_ledger_name(relative)))
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
        } for target in intent.targets],
        "steps": [{
            "detail": dict(step.detail),
            "from_state": step.from_state.value,
            "repository_id": step.repository_id,
            "step_id": step.step_id,
            "to_state": step.to_state.value,
        } for step in steps],
    }


def _workspace_author(args: argparse.Namespace) -> int:
    try:
        stored = workspace_author.author_from_draft(
            Path(args.from_file), Path(args.out), Path(args.root))
    except workspace_author.WorkspaceAuthoringError as exc:
        return _workspace_refusal("WORKSPACE_AUTHORING_FAILED", str(exc))
    _workspace_emit({
        "digest": workspace_digest.digest_of(stored),
        "outcome": "WORKSPACE_AUTHORED",
        "workspace": str(Path(args.out)),
    })
    return 0

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
    print(json.dumps({"detail": "{0}: {1}".format(type(exc).__name__, exc),
                      "outcome": "MAESTRO_INTERNAL_ERROR"}, sort_keys=True))
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
    deliver.add_argument("--max-attempts", type=int,
                         default=deliver_module.MAX_ATTEMPTS)
    deliver.add_argument("--no-run", action="store_true")
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
        "--discard", action="store_true",
        help="make the run terminal; without this flag, cancel pauses")
    _add_db(cancel)
    cancel.set_defaults(handler=_run_cancel)
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

    def _positive_grant(value: str) -> int:
        """`--grant 0` and `--grant -2` are refused at parse time rather than
        accepted as a no-op escape: an operator who typed a grant asked for
        one, and a silently ignored magnitude would leave the node blocked
        with a transition claiming it had been retried."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"grant must be an integer, got {value!r}")
        if parsed < 1:
            raise argparse.ArgumentTypeError(
                f"grant must be at least 1, got {parsed}")
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
        "--force", action="store_true",
        help="grant one extra attempt beyond the ceiling")
    grant_group.add_argument(
        "--grant", type=_positive_grant, default=0, metavar="N",
        help="grant N extra attempts beyond the ceiling; a node blocked "
             "REVIEW_BUDGET_EXHAUSTED reports the N it needs as "
             "review_grant_required")
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
    workspace_sub = workspace.add_subparsers(
        dest="workspace_command", required=True)

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
                if any(isinstance(item, argparse._SubParsersAction)
                       for item in child._actions):
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
