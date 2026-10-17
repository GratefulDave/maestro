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


class LaunchActivatesTheTreeVenvBeforeAgentStart(unittest.TestCase):
    """The real `HerdrLauncher.launch`, against the Herdr fake."""

    def _launch_calls(self, *, with_venv: bool) -> tuple[list, Path]:
        from tests.herdr_fake import FakeHerdr
        from tests.test_herdr_workspace_topology import (
            PROJECT,
            RUN_HASH,
            _checkout,
            _launch,
            _launcher,
            _spec,
        )

        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        root = Path(tempfile.mkdtemp(prefix="launch venv "))
        self.addCleanup(shutil.rmtree, root, True)
        herdr.add_workspace(PROJECT, root)
        checkout = _checkout(root, "tester")
        if with_venv:
            (checkout / ".venv" / "bin").mkdir(parents=True)
        _launch(launcher, herdr, _spec(checkout, lane="lane-x", role="tester"))
        return list(herdr.calls), checkout

    def test_export_is_submitted_in_the_pane_before_agent_start(self) -> None:
        calls, checkout = self._launch_calls(with_venv=True)
        verbs = [call[:2] for call in calls]
        start = verbs.index(("agent", "start"))
        line = lch.tree_venv_activation(checkout.resolve())
        sent = [
            index
            for index, call in enumerate(calls)
            if call[:2] == ("pane", "send-text") and line in call
        ]
        self.assertEqual(len(sent), 1)
        self.assertLess(sent[0], start)
        self.assertEqual(calls[sent[0] + 1][:2], ("pane", "send-keys"))

    def test_an_unprovisioned_tree_gets_no_export(self) -> None:
        calls, _ = self._launch_calls(with_venv=False)
        start = [call[:2] for call in calls].index(("agent", "start"))
        self.assertFalse(
            any(call[:2] == ("pane", "send-text") for call in calls[:start])
        )


if __name__ == "__main__":
    unittest.main()
