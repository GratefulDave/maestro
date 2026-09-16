"""A command run inside a provisioned tree runs in that tree's environment.

These cases start real children. A stubbed `subprocess.run` records the env it
was handed and replays scripted stdout; it cannot observe which interpreter a
bare `python` resolves to, which is the entire question here. Case 1 builds two
real virtual environments and asks a real child to import a module that exists
in only one of them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adw_modules import provisioning  # noqa: E402
from adw_modules.tree_env import (  # noqa: E402
    AMBIENT_TOOLCHAIN_KEYS,
    tree_environment,
)

MARKER = "maestro_tree_env_marker"


def _make_venv(root: Path) -> Path | None:
    """A real virtual environment at `root`, or None when none can be built."""
    for argv in (
        [sys.executable, "-m", "venv", "--without-pip", str(root)],
        ["uv", "venv", "--python", sys.executable, str(root)],
    ):
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=180
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and (root / "bin" / "python").exists():
            return root
    return None


def _purelib(venv: Path) -> Path:
    out = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


class AmbientVenvDoesNotReachTheTree(unittest.TestCase):
    """The proven FDAdb `d246ae95` failure, reproduced and closed.

    Ambient venv A has no marker module; the tree's own venv B does. A child
    started with the operator's environment resolves A and fails to import it.
    The same child started through `tree_environment` resolves B and imports.
    """

    def test_the_tree_venv_wins_over_an_ambient_virtual_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ambient = _make_venv(base / "ambient")
            if ambient is None:
                self.skipTest("no venv builder available")
            tree = base / "tree"
            tree.mkdir()
            inner = _make_venv(tree / ".venv")
            if inner is None:
                self.skipTest("no venv builder available")
            (_purelib(inner) / (MARKER + ".py")).write_text("value = 1\n")

            operator = dict(os.environ)
            operator["VIRTUAL_ENV"] = str(ambient)
            operator["PATH"] = os.pathsep.join(
                [str(ambient / "bin"), operator.get("PATH", "")]
            )

            argv = ["python", "-c", "import {0}".format(MARKER)]
            without = subprocess.run(
                argv, cwd=str(tree), env=operator, capture_output=True, text=True
            )
            self.assertNotEqual(
                without.returncode,
                0,
                "the ambient venv was expected to lack the marker module",
            )
            self.assertIn("ModuleNotFoundError", without.stderr)

            with_helper = subprocess.run(
                argv,
                cwd=str(tree),
                env=tree_environment(tree, operator),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                with_helper.returncode,
                0,
                "tree_environment did not reach the tree's own venv: {0}".format(
                    with_helper.stderr
                ),
            )

    def test_the_activated_bin_is_removed_from_path_not_only_the_variable(
        self,
    ) -> None:
        """Popping `VIRTUAL_ENV` alone leaves its `bin` first on `PATH`."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tree = base / "tree"
            tree.mkdir()
            ambient = base / "ambient"
            (ambient / "bin").mkdir(parents=True)
            operator = {
                "VIRTUAL_ENV": str(ambient),
                "PATH": os.pathsep.join([str(ambient / "bin"), "/usr/bin"]),
            }
            env = tree_environment(tree, operator)
            self.assertEqual(env["PATH"], "/usr/bin")


class TheDroppedSet(unittest.TestCase):
    def test_every_ambient_toolchain_variable_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = {key: "/ambient/" + key for key in AMBIENT_TOOLCHAIN_KEYS}
            operator.update({"PATH": "/usr/bin", "HOME": "/home/engineer"})
            env = tree_environment(Path(tmp), operator)
            for key in AMBIENT_TOOLCHAIN_KEYS:
                self.assertNotIn(key, env, key)

    def test_the_operators_own_environment_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = {
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/engineer",
                "LANG": "en_US.UTF-8",
                "SSH_AUTH_SOCK": "/tmp/ssh",
                "ANTHROPIC_API_KEY": "secret",
                "TMPDIR": "/scratch/tmp",
            }
            env = tree_environment(Path(tmp), operator)
            self.assertEqual(env["PATH"], "/usr/bin:/bin")
            for key in ("HOME", "LANG", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "TMPDIR"):
                self.assertEqual(env[key], operator[key], key)

    def test_it_is_pure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operator = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/ambient"}
            before = dict(operator)
            tree_environment(Path(tmp), operator)
            self.assertEqual(operator, before)

    def test_a_tree_venv_is_pointed_at_rather_than_only_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / ".venv" / "bin").mkdir(parents=True)
            env = tree_environment(tree, {"PATH": "/usr/bin"})
            self.assertEqual(env["VIRTUAL_ENV"], str(tree / ".venv"))
            self.assertEqual(
                env["PATH"], os.pathsep.join([str(tree / ".venv" / "bin"), "/usr/bin"])
            )


class NodeModulesBin(unittest.TestCase):
    def test_a_tree_shim_is_found_before_an_ambient_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tree = base / "tree"
            shim = tree / "node_modules" / ".bin"
            shim.mkdir(parents=True)
            ambient = base / "ambient-bin"
            ambient.mkdir()
            for directory, word in ((ambient, "ambient"), (shim, "tree")):
                script = directory / "maestro-marker-tool"
                script.write_text("#!/bin/sh\necho {0}\n".format(word))
                script.chmod(0o755)

            operator = dict(os.environ)
            operator["PATH"] = os.pathsep.join(
                [str(ambient), operator.get("PATH", "")]
            )

            without = subprocess.run(
                ["maestro-marker-tool"],
                cwd=str(tree),
                env=operator,
                capture_output=True,
                text=True,
            )
            self.assertEqual(without.stdout.strip(), "ambient")

            with_helper = subprocess.run(
                ["maestro-marker-tool"],
                cwd=str(tree),
                env=tree_environment(tree, operator),
                capture_output=True,
                text=True,
            )
            self.assertEqual(with_helper.stdout.strip(), "tree")


class ProvisioningRunsInTheTree(unittest.TestCase):
    """`provision_tree` installs into the tree, so it gets the tree's env.

    Real child, real argv: the provisioning command reports the `VIRTUAL_ENV`
    it was handed. `uv sync` and `poetry install` write into whatever that
    names, so an inherited one installs the tree's dependencies elsewhere and
    reports success.
    """

    def test_the_provisioning_command_does_not_see_an_ambient_virtual_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            tree.mkdir()
            report = Path(tmp) / "seen"
            previous = os.environ.get("VIRTUAL_ENV")
            os.environ["VIRTUAL_ENV"] = "/ambient/venv"
            try:
                provisioning.provision_tree(
                    tree,
                    [
                        "sh",
                        "-c",
                        'printf "%s" "${{VIRTUAL_ENV-unset}}" > {0}'.format(report),
                    ],
                    60.0,
                )
            finally:
                if previous is None:
                    os.environ.pop("VIRTUAL_ENV", None)
                else:
                    os.environ["VIRTUAL_ENV"] = previous
            self.assertEqual(report.read_text(), "unset")


if __name__ == "__main__":
    unittest.main()
