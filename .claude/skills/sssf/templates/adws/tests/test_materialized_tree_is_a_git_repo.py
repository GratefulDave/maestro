"""A materialized tree is a git repository of exactly its own files.

FDAdb run be064e58, `lane-wp3-reader-tests`: the plan's case eight runs
`node --test deploy/tests/runtime-wiring.test.mjs`, which calls
`git grep -n <needle>` from the repository root. Every tree the factory runs
agents and sealed suites in came from `git archive`, and `_extract_commit`
refuses a tree that carries `.git`, so `git grep` answered "not a git
repository" in every round and the lane parked NO_PROGRESS.

The tree now holds one commit of exactly what was extracted. These cases run
the real git binary, and they also pin what that must never cost: a
destination outside the runtime-state root, or inside someone's repository,
is refused before anything is written, and a caller's `GIT_DIR` cannot point
the init at another repository.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import hidden_vault as hv


def _clean_env() -> dict[str, str]:
    # The fixture's own git, isolated from the user's hooks and config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=check,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _source_repo(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "user@example.test")
    _git(path, "config", "user.name", "user")
    (path / "deploy" / "tests").mkdir(parents=True)
    (path / "deploy" / "tests" / "wiring.test.mjs").write_text(
        'const needle = "RUNTIME_WIRING_NEEDLE";\n', encoding="utf-8"
    )
    (path / "app.py").write_text("VALUE = 'py-literal'\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    _git(path, "update-ref", "refs/maestro/candidates/secret", "HEAD")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _snapshot(repo: Path) -> dict[str, str]:
    """sha256 of every file under `.git`, plus HEAD, refs and status."""
    found: dict[str, str] = {}
    git_dir = repo / ".git"
    for dirpath, _dirnames, filenames in os.walk(git_dir):
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(git_dir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    found["<HEAD>"] = _git(repo, "rev-parse", "HEAD").stdout
    found["<refs>"] = _git(repo, "for-each-ref").stdout
    found["<status>"] = _git(repo, "status", "--porcelain", "--ignored").stdout
    return found


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.repo = self.root / "repo"
        self.sha = _source_repo(self.repo)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)


class GitGrepRunsInTheTreeTest(_Case):
    def test_git_grep_finds_a_tracked_literal_and_misses_an_absent_one(self) -> None:
        tree = hv.materialize_commit(
            self.repo, self.sha, self.state / "worktrees" / "t", state_root=self.state
        )
        hit = _git(tree, "grep", "-n", "RUNTIME_WIRING_NEEDLE", check=False)
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertIn("deploy/tests/wiring.test.mjs:1:", hit.stdout)
        py = _git(tree, "grep", "-n", "py-literal", check=False)
        self.assertEqual(py.returncode, 0, py.stderr)
        miss = _git(tree, "grep", "-n", "NEEDLE_THAT_IS_NOWHERE", check=False)
        self.assertEqual(miss.returncode, 1, miss.stderr)

    def test_refresh_makes_the_new_commit_the_only_one(self) -> None:
        tree = hv.materialize_commit(
            self.repo, self.sha, self.state / "worktrees" / "t", state_root=self.state
        )
        (self.repo / "later.txt").write_text("LATER_LITERAL\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "later")
        second = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        hv.refresh_materialized_commit(self.repo, second, tree, state_root=self.state)
        self.assertEqual(_git(tree, "grep", "-n", "LATER_LITERAL", check=False).returncode, 0)
        self.assertEqual(_git(tree, "rev-list", "--all", "--count").stdout.strip(), "1")


class TheTreeRepositoryHoldsOneCommitTest(_Case):
    def test_one_commit_one_branch_no_foreign_refs_clean_status(self) -> None:
        tree = hv.materialize_commit(
            self.repo, self.sha, self.state / "worktrees" / "t", state_root=self.state
        )
        self.assertTrue((tree / ".git").is_dir(), "a gitfile would link elsewhere")
        self.assertEqual(_git(tree, "rev-list", "--all", "--count").stdout.strip(), "1")
        self.assertEqual(
            _git(tree, "for-each-ref", "--format=%(refname)").stdout.split(),
            ["refs/heads/materialized"],
        )
        self.assertEqual(_git(tree, "remote").stdout.strip(), "")
        self.assertEqual(_git(tree, "status", "--porcelain").stdout, "")
        self.assertEqual(
            sorted(_git(tree, "ls-files").stdout.split()),
            sorted(_git(self.repo, "ls-tree", "-r", "--name-only", self.sha).stdout.split()),
        )
        self.assertEqual(list((tree / ".git" / "hooks").glob("*")), [])


class DestinationInsideAUserRepoIsRefusedTest(_Case):
    def test_refused_and_the_user_repo_is_byte_identical(self) -> None:
        before = _snapshot(self.repo)
        dest = self.repo / "nested-tree"
        with self.assertRaises(hv.TreeContainmentRefused):
            # A state root that wrongly contains the repository.
            hv.materialize_commit(self.repo, self.sha, dest, state_root=self.root)
        with self.assertRaises(hv.TreeContainmentRefused):
            hv.materialize_commit(self.repo, self.sha, dest, state_root=self.state)
        self.assertFalse(dest.exists())
        self.assertEqual(_snapshot(self.repo), before)

    def test_refresh_into_a_user_repo_deletes_nothing(self) -> None:
        before = _snapshot(self.repo)
        with self.assertRaises(hv.TreeContainmentRefused):
            hv.refresh_materialized_commit(
                self.repo, self.sha, self.repo / "deploy", state_root=self.root
            )
        self.assertTrue((self.repo / "deploy" / "tests" / "wiring.test.mjs").is_file())
        self.assertEqual(_snapshot(self.repo), before)

    def test_explicit_forbidden_path_is_refused(self) -> None:
        other = self.state / "worktrees"
        with self.assertRaises(hv.TreeContainmentRefused):
            hv.materialize_commit(
                self.repo,
                self.sha,
                other / "t",
                state_root=self.state,
                forbidden=(other,),
            )
        self.assertFalse((other / "t").exists())


class SourceRepositoryIsUntouchedTest(_Case):
    def test_source_git_dir_is_byte_identical_after_materialization(self) -> None:
        # Compared: sha256 of every file under the source `.git`, HEAD, every
        # ref, and `status --porcelain --ignored`. Nothing is excluded --
        # `git archive` only reads objects, so no file there may change.
        before = _snapshot(self.repo)
        hv.materialize_commit(
            self.repo, self.sha, self.state / "worktrees" / "t", state_root=self.state
        )
        self.assertEqual(_snapshot(self.repo), before)


class CallerGitDirDoesNotRedirectTheInitTest(_Case):
    def test_inherited_git_dir_and_index_are_ignored(self) -> None:
        before = _snapshot(self.repo)
        hostile = {
            "GIT_DIR": str(self.repo / ".git"),
            "GIT_WORK_TREE": str(self.repo),
            "GIT_INDEX_FILE": str(self.repo / ".git" / "index"),
        }
        with mock.patch.dict(os.environ, hostile):
            tree = hv.materialize_commit(
                self.repo,
                self.sha,
                self.state / "worktrees" / "t",
                state_root=self.state,
            )
        self.assertEqual(_snapshot(self.repo), before)
        self.assertEqual(_git(tree, "rev-list", "--all", "--count").stdout.strip(), "1")
        self.assertEqual(
            _git(tree, "rev-parse", "--absolute-git-dir").stdout.strip(),
            str(tree / ".git"),
        )


if __name__ == "__main__":
    unittest.main()
