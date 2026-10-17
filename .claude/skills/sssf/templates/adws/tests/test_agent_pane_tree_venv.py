"""An agent pane resolves bare `pytest` / `python` to its tree's own venv."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adw_modules import launcher as lch


class TreeVenvActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = Path(tempfile.mkdtemp(prefix="tree venv "))
        self.addCleanup(shutil.rmtree, self.tree, True)

    def _make_venv(self) -> Path:
        bin_dir = self.tree / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        pytest = bin_dir / "pytest"
        pytest.write_text("#!/bin/sh\necho tree-pytest\n")
        pytest.chmod(0o755)
        return bin_dir

    def test_no_venv_sends_nothing(self) -> None:
        calls = []
        sent = lch._activate_tree_venv(
            lambda *a, **k: calls.append(a) or {}, "p1", self.tree, sleep=lambda s: None
        )
        self.assertFalse(sent)
        self.assertEqual(calls, [])
        self.assertIsNone(lch.tree_venv_activation(self.tree))

    def test_venv_is_exported_then_submitted(self) -> None:
        self._make_venv()
        calls = []
        sent = lch._activate_tree_venv(
            lambda *a, **k: calls.append(a) or {}, "p1", self.tree, sleep=lambda s: None
        )
        self.assertTrue(sent)
        self.assertEqual(
            calls,
            [
                ("pane", "send-text", "p1", lch.tree_venv_activation(self.tree)),
                ("pane", "send-keys", "p1", "enter"),
            ],
        )

    def test_line_puts_tree_pytest_first_in_a_real_shell(self) -> None:
        bin_dir = self._make_venv()
        line = lch.tree_venv_activation(self.tree)
        assert line is not None
        decoy = Path(tempfile.mkdtemp(prefix="decoy"))
        self.addCleanup(shutil.rmtree, decoy, True)
        (decoy / "pytest").write_text("#!/bin/sh\necho decoy\n")
        (decoy / "pytest").chmod(0o755)
        env = dict(os.environ, PATH="{0}:{1}".format(decoy, os.environ["PATH"]))
        out = subprocess.run(
            ["/bin/sh", "-c", line + '; command -v pytest; echo "$VIRTUAL_ENV"'],
            env=env, capture_output=True, text=True, check=True,
        ).stdout.split("\n")
        self.assertEqual(out[0], str(bin_dir / "pytest"))
        self.assertEqual(out[1], str(self.tree / ".venv"))


if __name__ == "__main__":
    unittest.main()
