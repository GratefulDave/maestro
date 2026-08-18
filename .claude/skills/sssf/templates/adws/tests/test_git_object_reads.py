"""Reading a git object has three answers, not two (§7.5).

`plan_validate.blob_at` was `return out if code == 0 else None`. Every caller
reads `None` as "the object is absent at that commit" — a durable fact about
the repository, which every eligibility obligation depends on (§6.4). But
`git cat-file blob <commit>:<path>` exits 128 for a missing path, for a
directory, for an invalid revision, for a path outside the repository, and for
a git that simply failed. All five arrived as absence.

§7.5 is explicit: "Only git's documented not-found exit code means 'the object
is absent.' Every other nonzero exit is ENVIRONMENTAL, never a fact about the
repository." `cat-file blob` has no such distinguishing code — 128 for all of
it — so the read now goes through `ls-tree`, which exits **zero** and answers
by what it prints. Absence is derived from a successful command's empty
output; a nonzero exit can therefore only be environmental, by construction.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import plan_author
from adw_modules import plan_validate as pv

from test_step2_plan_validation import (
    Collector, Receipts, make_repo, plan_mapping, sha256_text,
)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True,
                          check=True).stdout.strip()


class GitObjectReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _head(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_file_reads_as_its_bytes(self):
        self.assertEqual(pv.blob_at(self.repo, self.base, "README.md"),
                         b"fixture repository\n")

    def test_a_missing_path_is_absence_and_absence_alone(self):
        self.assertIsNone(pv.blob_at(self.repo, self.base, "nope.md"))

    def test_a_directory_is_not_absence(self):
        """`tests/` exists at base. Reported as a missing file, an operator
        goes looking for a deleted file that was never there."""
        with self.assertRaises(pv.GitPathNotAFile) as caught:
            pv.blob_at(self.repo, self.base, "tests")
        self.assertIn("is a tree", str(caught.exception))

    def test_an_unreadable_revision_is_a_failure_not_absence(self):
        with self.assertRaises(pv.GitReadFailed):
            pv.blob_at(self.repo, "deadbeef" * 5, "README.md")

    def test_a_directory_that_is_not_a_repository_is_a_failure_not_absence(self):
        """The case §7.5 names: a git that fails must not resolve to a claim
        about the repository's contents."""
        outside = self.root / "not-a-repository"
        outside.mkdir()
        with self.assertRaises(pv.GitReadFailed):
            pv.blob_at(outside, self.base, "README.md")


class AuthoringRefusesANonFilePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _head(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_observed_evidence_citing_a_directory_refuses_by_name(self):
        draft = plan_mapping(self.base)
        draft["evidence"][0]["path"] = "tests"
        draft["evidence"][0].pop("sha256")
        with self.assertRaises(plan_author.AuthoringError) as caught:
            plan_author.author_plan(draft, self.repo)
        message = str(caught.exception)
        self.assertIn("OBSERVED_PATH_NOT_A_FILE", message)
        self.assertNotIn("OBSERVED_PATH_ABSENT", message)

    def test_validation_blocks_a_directory_citation_as_not_a_file(self):
        """A fact about the repository proven by a zero-exit command, so it is
        a blocker like any other rather than an escaping exception."""
        draft = plan_mapping(self.base)
        stored = plan_author.author_plan(draft, self.repo)
        tampered = stored.replace(b'"README.md"', b'"tests"')
        result = pv.validate_plan(tampered, self.repo, receipts=Receipts(),
                                  collector=Collector())
        self.assertFalse(result.eligible)
        self.assertTrue(
            any("is not a file" in blocker.message
                for blocker in result.blockers),
            repr([blocker.message for blocker in result.blockers]))


class ProducedBaseDigestRefusesAtAuthoringTest(unittest.TestCase):
    """The `produced` branch, made symmetric with `observed`.

    A declared `base_sha256` that disagreed with the object was left for
    `plan validate` to convict. `write_canonical_plan` is create-once
    (`PLAN_EXISTS`), so a plan authored invalid has to be deleted by hand
    before the corrected draft can be authored at all.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _head(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _draft_producing_an_existing_path(self):
        draft = plan_mapping(self.base)
        produced = next(item for item in draft["evidence"]
                        if item["kind"] == "produced")
        produced["path"] = "tests/test_existing.py"
        return draft, produced

    def test_a_wrong_declared_base_digest_refuses(self):
        draft, produced = self._draft_producing_an_existing_path()
        produced["base_sha256"] = "0" * 64
        with self.assertRaises(plan_author.AuthoringError) as caught:
            plan_author.author_plan(draft, self.repo)
        self.assertIn("PRODUCED_BASE_DIGEST_MISMATCH", str(caught.exception))

    def test_an_undeclared_base_digest_is_still_filled(self):
        draft, produced = self._draft_producing_an_existing_path()
        produced["base_sha256"] = None
        filled = plan_author.fill_git_facts(draft, self.repo)
        carried = next(item for item in filled["evidence"]
                       if item["kind"] == "produced")
        self.assertEqual(
            carried["base_sha256"],
            sha256_text((self.repo / "tests" / "test_existing.py")
                        .read_text(encoding="utf-8")))


class WorktreeLookupDoesNotInventAnAnswerTest(unittest.TestCase):
    """`_worktree_holding_branch` returning `None` reads as "no worktree holds
    this branch", which `run start` uses to decide whether to refuse. A git
    failure resolving to that answer authorised the run to take a branch that
    may well be checked out."""

    def test_a_git_failure_raises_rather_than_reporting_no_occupant(self):
        import maestro
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "empty"
            not_a_repo.mkdir()
            with self.assertRaises(RuntimeError) as caught:
                maestro._worktree_holding_branch(not_a_repo, "main")
        self.assertIn("GIT_READ_FAILED", str(caught.exception))

    def test_a_real_repository_with_no_occupant_still_answers_none(self):
        import maestro
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            self.assertIsNone(
                maestro._worktree_holding_branch(repo, "no-such-branch"))


if __name__ == "__main__":
    unittest.main()
