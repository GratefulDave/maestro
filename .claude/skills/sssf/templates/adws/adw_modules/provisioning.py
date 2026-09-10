"""The one provisioner every factory tree crosses.

Every tree the factory hands to an actor or a measurement -- an agent
worktree, the review tree the sealed suite runs in, the draft-collect and
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

from .launcher import PROVISION_TIMEOUT_S, run_harness_process
from .private_review import SealedEnvironmentError
from .tree_env import tree_environment


class ReviewProvisioningError(SealedEnvironmentError):
    """A tree could not be provisioned, so nothing measured in it is a verdict.

    Typed and fail-closed on purpose. A provisioning failure is a failure of the
    harness's environment, never of the candidate or the draft: an
    unprovisioned tree collects zero cases, and `_RUNNER_REVISE` would then
    report that as "sealed private tests failed" and send the builder to fix
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

    Ordering is a containment property, not a convenience: on the review path
    this runs after the commit is materialized and before any sealed blob is
    copied in, so nothing provisioning writes, reads, or reports back in an
    error can carry private test bytes.

    The invariant every caller owes this function: **provision a tree after its
    final materialization, never before one.** Materializing is destructive --
    `hv.refresh_materialized_commit` unlinks every child of the tree, which is
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
        # Install into this tree with this tree's environment. `uv sync` and
        # `poetry install` both write into whatever `VIRTUAL_ENV` names, so an
        # ambient one inherited from the operator's shell would have this
        # function install the tree's dependencies somewhere else entirely and
        # report success.
        result = run_harness_process(
            argv,
            cwd=Path(dest),
            env=tree_environment(Path(dest)),
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
