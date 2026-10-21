"""The one provisioner every factory tree crosses.

Every tree the factory hands to an actor or a measurement -- an agent
worktree, the review tree the accepted suite runs in, the draft-collect and
runner-preflight trees -- is provisioned here and nowhere else. That is what
makes the round-1 runner preflight (`FactoryScheduler._assert_runners_usable`)
speak for every later tree: it provisions a checkout with the deployment's
`provision_argv`, probes the runner in it, and refuses `RUNNER_PREFLIGHT_REFUSED`
if the runner cannot load. The refusal is only true of the actor trees if they
are provisioned by the same function with the same argv.

Until 2026-09-09 they were not. `HerdrLauncher.provision` carried its own copy
of "run `provision_argv`, raise on non-zero" -- since deleted -- and the
harness's collect trees
additionally symlinked the product repository's `node_modules` into the
checkout (`runner_resolution.prepare_collect_tree`). The bridge made the
preflight and the draft collect succeed in a deployment whose `provision_argv`
installed no JS dependencies, while every tester, reviewer and builder tree --
which had no bridge -- could not resolve `vitest/config`. FDAdb run
d246ae9592be478396ad5146a89f00ae spent three rounds on `lane-faq-producer-tests`
reporting that as a tester finding. The bridge is gone; a provisioning gap now
fails the preflight at round 1, where it is the deployment's to fix.

Lives apart from `code_review` because `launcher` needs it and `code_review`
imports `launcher`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from . import tree_env as te
from .launcher import PROVISION_TIMEOUT_S, run_harness_process
from .review_contract import SuiteEnvironmentError


class ReviewProvisioningError(SuiteEnvironmentError):
    """A tree could not be provisioned, so nothing measured in it is a verdict.

    Typed and fail-closed on purpose. A provisioning failure is a failure of the
    harness's environment, never of the candidate or the draft: an
    unprovisioned tree collects zero cases, and `_RUNNER_REVISE` would then
    report that as "accepted tests failed" and send the builder to fix
    tests that never ran. Raising instead of returning a verdict makes that
    mislabelling impossible -- no CODE_REVIEW artifact is built on this path at
    all, and the launcher restates it as a typed `LaunchRefused` rather than
    dispatching an agent into a tree the preflight would have refused.
    """

    code = "REVIEW_TREE_PROVISION_FAILED"

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int | None,
        detail: str = "",
    ) -> None:
        self.argv = tuple(str(item) for item in argv)
        self.returncode = returncode
        self.detail = detail
        super().__init__(
            "{0}:{1}:{2}:{3}".format(
                self.code, " ".join(self.argv), returncode, detail
            )
        )


def provision_tree(
    dest: Path,
    provision_argv: Sequence[str],
    timeout_s: float | None = None,
) -> None:
    """Install the tree's declared dependencies with the deployment's command.

    Ordering is an environment property, not a convenience: on the review path
    this runs after the commit is materialized and before the suite is
    verified and run, so what provisioning installs is what the suite runs
    against, and a provisioning failure is a fact about the environment and
    never a verdict about the candidate.

    The invariant every caller owes this function: **provision a tree after its
    final materialization, never before one.** Materializing is destructive --
    `tree_materialize.refresh_materialized_commit` unlinks every child of the tree, which is
    every installed dependency and any marker a previous run could have left,
    and a git checkout resets it -- so there is no "already provisioned" state
    inside the tree to detect, and nothing this function installs survives a
    later materialization. The durable cache is the package manager's own,
    outside the tree.

    `LaunchSpec.prepare_adopted_cwd` is a materialization. Until 2026-09-09 the
    actor path provisioned from `HerdrLauncher.launch`, which runs before that
    callback, so every reused role pane and every adopted agent started in a
    tree whose dependencies had just been unlinked. `HerdrStageActor.
    _prepared_cwd` now owns both, and the launcher provisions nothing.
    """
    argv = tuple(str(item) for item in provision_argv if str(item))
    if not argv:
        return
    bound = PROVISION_TIMEOUT_S if timeout_s is None else float(timeout_s)
    try:
        # Installs into this tree, so it runs in this tree's environment. An
        # inherited `VIRTUAL_ENV` sends `uv sync` / `poetry install` at the
        # scheduler's venv instead, and the tree comes back empty with a zero
        # exit code. See `tree_env`.
        result = run_harness_process(
            argv,
            cwd=Path(dest),
            env=te.tree_environment(Path(dest)),
            timeout=bound,
        )
    except OSError as exc:
        # TimeoutError is an OSError; a missing provisioning executable is one
        # too. Both are the harness failing, and both must stay distinguishable
        # from a candidate that failed its tests.
        raise ReviewProvisioningError(argv, None, str(exc)) from exc
    if result.returncode != 0:
        raise ReviewProvisioningError(
            argv, result.returncode, (result.stderr or "")[-400:]
        )
    if _TREE_PACKAGES:
        install_tree_packages(Path(dest), _TREE_PACKAGES, bound)


#: Packages the run preflight found missing from every tree's manifest and
#: installs tree-local. Process-scoped: set by `set_tree_packages` once per
#: preflight, before any lane is dispatched, and read by every tree
#: `provision_tree` provisions afterwards -- an install that only the
#: preflight's own tree received would be unlinked by the next
#: materialization and missing from the tree an actor is dispatched into.
_TREE_PACKAGES: tuple[str, ...] = ()


def set_tree_packages(packages: Sequence[str]) -> None:
    global _TREE_PACKAGES
    _TREE_PACKAGES = tuple(sorted({str(item) for item in packages if str(item)}))


def tree_packages() -> tuple[str, ...]:
    return _TREE_PACKAGES


def install_tree_packages(
    dest: Path, packages: Sequence[str], timeout_s: float | None = None
) -> None:
    """`uv pip install` into the tree's own `.venv`. Never the manifest.

    Refuses `ReviewProvisioningError` when the tree has no venv to install
    into, the same typed failure as a `provision_argv` that fails.
    """
    wanted = tuple(str(item) for item in packages if str(item))
    if not wanted:
        return
    python = Path(dest) / ".venv" / "bin" / "python"
    argv = ("uv", "pip", "install", "--python", str(python), *wanted)
    if not python.exists():
        raise ReviewProvisioningError(argv, None, "tree has no .venv to install into")
    bound = PROVISION_TIMEOUT_S if timeout_s is None else float(timeout_s)
    try:
        result = run_harness_process(
            argv, cwd=Path(dest), env=te.tree_environment(Path(dest)), timeout=bound
        )
    except OSError as exc:
        raise ReviewProvisioningError(argv, None, str(exc)) from exc
    if result.returncode != 0:
        raise ReviewProvisioningError(
            argv, result.returncode, (result.stderr or "")[-400:]
        )


class SuiteRequirementMissing(ReviewProvisioningError):
    """A host tool or device the deployment says its suites reach is absent.

    Raised on the spot runner, after its `install` ran (when it has one) and
    the probe still failed. A `ReviewProvisioningError`, so the run preflight
    refuses `RUNNER_PREFLIGHT_REFUSED` naming it before any lane is dispatched,
    and a suite job that meets it mid-run is the environment failing, never a
    candidate's tests. Unchecked, a missing `ffmpeg` was scored as errored
    cases against the builder and sent back to the tester as findings.
    """

    code = "SUITE_REQUIREMENT_MISSING"

    def __init__(self, name: str, argv: Sequence[str], returncode: int | None, detail: str = "") -> None:
        self.name = name
        self.argv = tuple(str(item) for item in argv)
        self.returncode = returncode
        self.detail = "{0} {1}: {2}".format(self.code, name, detail)
        SuiteEnvironmentError.__init__(
            self, "{0}:{1}:{2}:{3}:{4}".format(self.code, name, " ".join(self.argv), returncode, detail)
        )


#: `maestro.config.yaml`'s `suite_requirements`, set once per process by the
#: config loader and carried in every remote suite job, like `_TREE_PACKAGES`.
_SUITE_REQUIREMENTS: tuple[dict, ...] = ()


def parse_suite_requirements(value: object) -> tuple[dict, ...]:
    """`[{name, probe: argv, install?: argv}]`. Raises `ValueError` naming the bad entry."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("suite_requirements must be a list")
    parsed = []
    for index, entry in enumerate(value):
        label = "suite_requirements[{0}]".format(index)
        if not isinstance(entry, dict) or set(entry) - {"name", "probe", "install"}:
            raise ValueError(label + " must be a mapping of name, probe and optional install")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(label + ".name must be a non-empty string")
        item = {"name": name.strip()}
        for key in ("probe", "install"):
            argv = entry.get(key)
            if argv is None and key == "install":
                continue
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError("{0}.{1} must be a non-empty argv list of strings".format(label, key))
            item[key] = list(argv)
        parsed.append(item)
    return tuple(parsed)


def set_suite_requirements(requirements: Sequence[dict]) -> None:
    global _SUITE_REQUIREMENTS
    _SUITE_REQUIREMENTS = tuple(dict(item) for item in requirements)


def suite_requirements() -> tuple[dict, ...]:
    return _SUITE_REQUIREMENTS


def ensure_suite_requirements(
    dest: Path, requirements: Sequence[dict], timeout_s: float | None = None
) -> None:
    """Probe each requirement in the provisioned tree; install once if it has a way.

    Runs on the spot runner only (`remote_job.run_op`), after `provision_tree`,
    so a device probe can import the tree's own packages. Never on this host:
    an install command here is the deployment's runner image, not the
    operator's machine.
    """
    bound = PROVISION_TIMEOUT_S if timeout_s is None else float(timeout_s)
    env = te.tree_environment(Path(dest))

    def attempt(argv: Sequence[str]) -> tuple[int | None, str]:
        try:
            result = run_harness_process(argv, cwd=Path(dest), env=env, timeout=bound)
        except OSError as exc:
            return None, str(exc)
        return result.returncode, (result.stderr or result.stdout or "")[-400:]

    for item in requirements:
        name, probe = str(item["name"]), tuple(str(a) for a in item["probe"])
        code, detail = attempt(probe)
        if code == 0:
            continue
        install = tuple(str(a) for a in item.get("install") or ())
        if not install:
            raise SuiteRequirementMissing(name, probe, code, "probe failed and no install is declared: " + detail)
        code, detail = attempt(install)
        if code != 0:
            raise SuiteRequirementMissing(name, install, code, "install failed: " + detail)
        code, detail = attempt(probe)
        if code != 0:
            raise SuiteRequirementMissing(name, probe, code, "probe failed after install: " + detail)
