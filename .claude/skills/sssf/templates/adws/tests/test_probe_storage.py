"""Probe sources are pinned under an immutable, output-addressed ref.

Real git. `git gc --prune=now` is the test of "durable": a loose blob written
with `hash-object -w` and nothing pointing at it does not survive it.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from probe_support import git

from adw_modules import probes
from adw_modules import review_contract as rc
from adw_modules.git_helper import BoundGit

SOURCES = {"tests/probes/test_a.py": "def test_a():\n    assert False\n"}
OTHER = {"tests/probes/test_a.py": "def test_a():\n    assert 1 == 2\n"}


class Pinning(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "h@example.invalid")
        git(self.repo, "config", "user.name", "H")
        (self.repo / "README").write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.git = BoundGit(self.repo)

    def test_the_ref_name_carries_the_commit_sha(self) -> None:
        commit, ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        self.assertEqual(ref, "refs/maestro/probes/run1/lane-a/" + commit)
        self.assertEqual(self.git.read_ref(ref), commit)

    def test_pinning_the_same_sources_again_is_the_same_commit_and_ref(self) -> None:
        first = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        again = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        self.assertEqual(first, again)

    def test_the_commit_holds_exactly_the_sources_and_no_parent(self) -> None:
        commit, _ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        self.assertEqual(self.git.commit_parents(commit), ())
        self.assertEqual(
            probes.read_pinned_sources(self.git, commit),
            {path: body.encode() for path, body in SOURCES.items()},
        )

    def test_a_ref_that_already_names_another_commit_is_refused_and_not_moved(self) -> None:
        commit, ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        git(self.repo, "update-ref", "-d", ref)
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", ref, base)
        with self.assertRaises(rc.ReviewContractError) as caught:
            probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        self.assertIn("PROBE_REF_MISMATCH", str(caught.exception))
        self.assertEqual(self.git.read_ref(ref), base)

    def test_a_second_attempt_with_divergent_sources_makes_its_own_ref(self) -> None:
        first_commit, first_ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        second_commit, second_ref = probes.pin_sources(self.git, "run1", "lane-a", OTHER)
        self.assertNotEqual(first_commit, second_commit)
        self.assertNotEqual(first_ref, second_ref)
        self.assertEqual(self.git.read_ref(first_ref), first_commit)
        self.assertEqual(self.git.read_ref(second_ref), second_commit)

    def test_the_sources_survive_gc_and_reconstruct_from_the_commit_alone(self) -> None:
        commit, _ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        loose = self.git.hash_object(b"unreferenced", write=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "reflog", "expire", "--expire=now", "--all"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "gc", "--prune=now", "-q"],
            check=True, capture_output=True,
        )
        self.assertEqual(
            probes.read_pinned_sources(self.git, commit),
            {path: body.encode() for path, body in SOURCES.items()},
        )
        gone = self.git.run("cat-file", "-e", loose, check=False)
        self.assertNotEqual(gone.returncode, 0, "gc kept an unreferenced blob: the test proves nothing")

    def test_the_blob_oid_of_a_record_is_the_blob_in_the_pinned_commit(self) -> None:
        commit, _ref = probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        (path, body), = SOURCES.items()
        oid = self.git.hash_object(body.encode(), write=False)
        self.assertEqual(self.git.tree_blob(commit, path), oid)

    def test_no_pin_touches_the_working_tree_or_a_branch(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        probes.pin_sources(self.git, "run1", "lane-a", SOURCES)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "for-each-ref", "refs/heads").count("\n"), 0)


if __name__ == "__main__":
    unittest.main()
