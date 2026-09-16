"""The environment a command gets when the factory runs it inside a tree.

**A command the factory runs inside a provisioned tree runs in that tree's
environment, never the operator's ambient one.**

Every measurement the factory makes is made by starting a child in a tree that
`provisioning.provision_tree` has just installed dependencies into. Until this
module existed each of those children was started with a wholesale copy of
`os.environ`, or with no `env=` at all, which is the same thing. So the tree
decided which *files* were present and the operator's shell decided which
*interpreter and module set* resolved them, and the second half of that
sentence is not something a candidate, a tester, or a reviewer can act on.

Measured on FDAdb run `d246ae95`: the sealed suite failed
`ModuleNotFoundError: No module named 'bcrypt'` in three consecutive review
rounds. `bcrypt` was installed in the review tree's own `.venv`. The scheduler
had been started with `uv run`, which exports `VIRTUAL_ENV` pointing at the
*scheduler's* venv, that variable was copied into the child, and the runner
resolved `$VIRTUAL_ENV/bin/python`. Unsetting `VIRTUAL_ENV` in the same tree,
with no other change, removed the failure. The builder was told its code failed
a test; the code was fine and the environment was the harness's own — the same
shape as the `vitest/config` rounds the one-provisioner change closed, arriving
by a different door.

Two rules, and no configuration key.

**Ambient toolchain selection is dropped, not merged.** The variables in
`AMBIENT_TOOLCHAIN_KEYS` all answer the question "which interpreter, toolchain,
or module set does this child resolve", so any of them surviving from the
operator's shell overrides what the tree was provisioned with. Nothing else is
touched: `PATH`, `HOME`, `LANG`, the SSH and Git identity, the provider
credentials an agent needs, and every variable a deployment's own tooling reads
are the operator's and are passed through unchanged.

**The tree's own bin directories win the PATH they are on.** If the tree has a
`.venv/bin` or a `node_modules/.bin`, they are prepended and `VIRTUAL_ENV` is
re-pointed at the tree's venv, so a child that resolves a bare `python`,
`pytest`, or `vitest` gets the tree's. The rest of `PATH` is kept in order
behind them, because a project that legitimately uses the system toolchain must
still work — the same reason `runner_resolution._rank_candidates` keeps `PATH`
as its last rank rather than dropping it.

Deterministic and pure: two `is_dir()` checks and dictionary arithmetic. It
never installs anything, never writes, and never reads a config file. A tree
with neither bin directory gets the same environment minus the ambient
toolchain variables, which is the correct answer for a language whose tooling
this function does not know about.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

#: Variables whose only job is to select an interpreter, a toolchain, or a
#: module set. Each one, carried in from the operator's shell, can make a child
#: in a provisioned tree resolve something the tree does not contain.
#:
#: * ``VIRTUAL_ENV`` -- names the active Python venv. ``uv run``, ``poetry
#:   run``, and every venv activation set it, and ``uv`` re-uses it rather than
#:   the tree's. This is the variable that cost FDAdb run ``d246ae95``.
#: * ``PYTHONHOME`` -- relocates the standard library itself.
#: * ``PYTHONPATH`` -- prepends directories to ``sys.path``, so an operator's
#:   checkout shadows the tree's installed package of the same name.
#: * ``CONDA_PREFIX`` / ``CONDA_DEFAULT_ENV`` -- the conda equivalents of
#:   ``VIRTUAL_ENV``; conda's own shims read them to pick an environment.
#: * ``__PYVENV_LAUNCHER__`` -- macOS framework-Python launcher hint; a stale
#:   one sends a child to a different interpreter than its argv names.
#: * ``PYTHONSTARTUP`` -- executes an operator's file inside the child.
#: * ``NODE_PATH`` -- prepends module resolution roots for Node, the
#:   ``PYTHONPATH`` of the JS side.
#: * ``NODE_OPTIONS`` -- injects flags and ``--require`` hooks into every Node
#:   process, including loaders that change which modules resolve.
#: * ``npm_config_prefix`` -- relocates the global install root, so a bare
#:   binary resolves out of the operator's global tree instead of the tree's
#:   ``node_modules/.bin``.
#: * ``RUSTUP_TOOLCHAIN`` -- pins which Rust toolchain every ``cargo``/``rustc``
#:   shim dispatches to.
#: * ``CARGO_TARGET_DIR`` -- redirects build output out of the tree, so one
#:   tree's artifacts are read as another's.
#: * ``GOFLAGS`` -- injects flags into every ``go`` invocation, ``-mod=`` among
#:   them, which changes module resolution.
#: * ``GOWORK`` -- points Go at an operator's workspace file, which replaces the
#:   tree's module set outright.
#:
#: Not here, deliberately: ``PATH``, ``HOME``, ``LANG``, ``SSH_*``, ``GIT_*``
#: identity, provider credentials, and the §8.3 scratch redirects. Those are the
#: operator's environment doing its job, and the factory depends on them.
AMBIENT_TOOLCHAIN_KEYS: Tuple[str, ...] = (
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "__PYVENV_LAUNCHER__",
    "PYTHONSTARTUP",
    "NODE_PATH",
    "NODE_OPTIONS",
    "npm_config_prefix",
    "RUSTUP_TOOLCHAIN",
    "CARGO_TARGET_DIR",
    "GOFLAGS",
    "GOWORK",
)

#: Variables that name an environment root whose `bin` directory the shell
#: prepended to `PATH` when it activated them. Dropping the variable without
#: dropping that `PATH` entry leaves a bare `python` resolving to exactly the
#: interpreter the drop was meant to avoid, which is how the FDAdb failure
#: would have survived a fix that only popped `VIRTUAL_ENV`.
_ACTIVATED_ROOTS: Tuple[str, ...] = ("VIRTUAL_ENV", "CONDA_PREFIX")


def tree_environment(
    tree: Path, base: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """The environment for a child whose cwd is inside `tree`.

    `base` defaults to `os.environ`. The result is a new dict; neither `base`
    nor the process environment is modified.
    """
    source = os.environ if base is None else base
    env: Dict[str, str] = dict(source)

    #: Resolve the activated roots' bin directories before the keys are popped.
    stale_bins = set()
    for key in _ACTIVATED_ROOTS:
        root = env.get(key, "").strip()
        if root:
            stale_bins.add(str(Path(root) / "bin"))

    for key in AMBIENT_TOOLCHAIN_KEYS:
        env.pop(key, None)

    root = Path(tree)
    prepend: List[str] = []

    venv = root / ".venv"
    if (venv / "bin").is_dir():
        # Re-point rather than merely unset: `uv run` and `poetry run` both
        # read this, and a tool that finds it absent may create a fresh venv
        # instead of using the one provisioning just filled.
        env["VIRTUAL_ENV"] = str(venv)
        prepend.append(str(venv / "bin"))

    node_bin = root / "node_modules" / ".bin"
    if node_bin.is_dir():
        prepend.append(str(node_bin))

    kept = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and part not in stale_bins and part not in prepend
    ]
    if prepend or "PATH" in env:
        # Assigning even an empty result matters: a `PATH` that held nothing
        # but the stale venv's bin must come out empty rather than unchanged.
        env["PATH"] = os.pathsep.join(prepend + kept)
    return env
