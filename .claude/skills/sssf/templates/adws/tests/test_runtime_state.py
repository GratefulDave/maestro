"""Runtime-state root binding: mode, overlap, symlink, fingerprint, layout."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules.runtime_state import (  # noqa: E402
    LAYOUT_CHILDREN,
    LEDGER_FILENAME,
    RuntimeStateRefused,
    RuntimeStateRoot,
    open_runtime_state_root,
)
from adw_modules.scheduler_types import runtime_state_fingerprint  # noqa: E402


def _mode700(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class RuntimeStateRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_opens_owned_0700_and_binds_fingerprint(self) -> None:
        state = _mode700(self.root / "state")
        with RuntimeStateRoot(state) as opened:
            info = os.stat(state)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
            self.assertEqual(
                opened.fingerprint,
                runtime_state_fingerprint(str(state), info.st_dev, info.st_ino),
            )
            self.assertEqual(opened.ledger_path(), state / LEDGER_FILENAME)
            opened.ensure_layout()
            for name in LAYOUT_CHILDREN:
                child = state / name
                self.assertTrue(child.is_dir())
                self.assertEqual(stat.S_IMODE(os.stat(child).st_mode), 0o700)

    def test_refuses_mode_other_than_0700(self) -> None:
        state = _mode700(self.root / "state")
        os.chmod(state, 0o755)
        with self.assertRaises(RuntimeStateRefused):
            RuntimeStateRoot(state)

    def test_refuses_overlap_with_target_repository(self) -> None:
        state = _mode700(self.root / "product" / "state")
        product = self.root / "product"
        product.mkdir(exist_ok=True)
        with self.assertRaises(RuntimeStateRefused):
            RuntimeStateRoot(state, overlap_paths=(product,))

    def test_refuses_symlink_root(self) -> None:
        real = _mode700(self.root / "real")
        link = self.root / "link"
        os.symlink(real, link)
        with self.assertRaises(RuntimeStateRefused):
            RuntimeStateRoot(link)

    def test_refuses_symlink_component_under_canonical_root(self) -> None:
        real_parent = _mode700(self.root / "real_parent")
        _mode700(real_parent / "state")
        link_parent = self.root / "link_parent"
        os.symlink(real_parent, link_parent)
        with self.assertRaises(RuntimeStateRefused):
            RuntimeStateRoot(link_parent / "state")

    def test_revalidate_detects_replaced_directory(self) -> None:
        state = _mode700(self.root / "state")
        opened = RuntimeStateRoot(state)
        fingerprint = opened.fingerprint
        opened.revalidate(fingerprint)
        opened.close()
        os.rename(state, self.root / "moved")
        other = _mode700(self.root / "state")
        with RuntimeStateRoot(other) as replacement:
            self.assertNotEqual(replacement.fingerprint, fingerprint)

    def test_context_manager_closes(self) -> None:
        state = _mode700(self.root / "state")
        with open_runtime_state_root(state) as opened:
            self.assertTrue(opened.fingerprint)


if __name__ == "__main__":
    unittest.main()
