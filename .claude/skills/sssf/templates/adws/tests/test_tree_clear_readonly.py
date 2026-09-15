"""Emptying a private tree removes read-only leftovers inside it.

FDAdb run be064e58, `lane-wp3-reader-tests`: a test reviewer's pytest `tmp_path`
lived inside its tree (`.maestro-agent/scratch/tmp`) and a test left `0555`
directories holding files. The next refresh of that tree raised
`PermissionError: 'manifest.json'` out of a bare `shutil.rmtree`, ending
`run resume`. These cases fail against that code.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import hidden_vault as hv
from adw_modules import launcher as lch


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(path: Path, name: str) -> str:
    (path / name).write_text(name + "\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", name)
    return _git(path, "rev-parse", "HEAD")


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    return _commit(path, "seed.txt")


def _read_only_leftover(tree: Path) -> Path:
    """Leave what the be064e58 test left: a file inside `0555` directories."""
    releases = tree / lch.ROLE_AGENT_DIR / "scratch" / "tmp" / "releases"
    page = releases / "PAGE-SYN-1"
    page.mkdir(parents=True)
    (page / "manifest.json").write_text("{}", encoding="utf-8")
    page.chmod(0o555)
    releases.chmod(0o555)
    return releases


class _ReadOnlyTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for dirpath, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                path = os.path.join(dirpath, name)
                if not os.path.islink(path):
                    os.chmod(path, os.lstat(path).st_mode | stat.S_IRWXU)
        self._tmp.cleanup()


class RefreshMaterializedCommitReadOnlyTest(_ReadOnlyTreeCase):
    def test_refresh_removes_read_only_leftovers_and_keeps_root_inode(self) -> None:
        repo = self.root / "repo"
        first = _init_repo(repo)
        _git(repo, "rm", "-q", "seed.txt")
        second = _commit(repo, "next.txt")
        tree = self.root / "tree"
        hv.materialize_commit(repo, first, tree)
        _read_only_leftover(tree)
        inode = tree.stat().st_ino

        hv.refresh_materialized_commit(repo, second, tree)

        self.assertEqual(tree.stat().st_ino, inode)
        self.assertTrue((tree / "next.txt").is_file())
        self.assertFalse((tree / "seed.txt").exists())
        self.assertFalse((tree / lch.ROLE_AGENT_DIR).exists())


class ClearPrecreatedRoleCwdReadOnlyTest(_ReadOnlyTreeCase):
    def test_clear_removes_read_only_leftovers(self) -> None:
        tree = self.root / "tree"
        tree.mkdir()
        _read_only_leftover(tree)
        inode = tree.stat().st_ino

        self.assertTrue(maestro._clear_precreated_role_cwd(tree))

        self.assertEqual(tree.stat().st_ino, inode)
        self.assertEqual(list(tree.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
