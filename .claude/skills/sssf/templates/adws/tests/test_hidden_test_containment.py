"""Executable proof that a builder cannot read a hidden tests node's bytes.

This is regression 7 of `docs/hidden-tests-design.md`, and it is the gate the
whole hidden-tests feature stands on. The design's own verdict is that if this
property does not hold, nothing else in the feature is worth building: a linked
git worktree shares its parent repository's object database, so a hidden test
committed into the run repository is one `git cat-file` away from the builder
that is supposed not to have it.

Measured 2026-08-27 against the live EPA run, before any of this existed:

    git -C <lane-routing-chemical-a4 worktree> cat-file -p 5ff0616c...
    -> printed the assertions at lines 104-112

`--git-common-dir` in that worktree resolved to the run repository's `.git`,
which is why. That command is what these tests exist to break.

Nothing here is mocked. Every test builds real git repositories and runs real
git, because the property under test is a property of git's object database and
a mock would assert it back at us (the reasoning `test_worktree_merge.py` opens
with, for the same reason).

The tests come in a discriminating pair, deliberately:

  - `merged` visibility must LEAK. That is today's shipped behaviour and it is
    what every plan written before this feature keeps. Asserting it here is not
    an endorsement; it is the negative control that proves these tests can tell
    the two apart. A containment test that passes without one is compatible
    with a seal that silently did nothing.
  - `hidden` visibility must NOT leak, and the vault must still hold the bytes.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# This file ships inside adws/tests/, so the package root is its parent's parent.
ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

HIDDEN_TEST_SOURCE = '''\
"""A hidden tests node's output. A builder must never read this."""


def test_refund_rejects_a_negative_amount() -> None:
    assert refund(-1) is None, "a negative refund must be refused"
'''

INTEGRATION_BRANCH = "main"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git in a throwaway repository and return its stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} -> {result.returncode}: {result.stderr}"
        )
    return result.stdout.strip()


def _make_run_repo(root: Path) -> Path:
    """A run repository with an integration branch and no tests yet."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", INTEGRATION_BRANCH)
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    # The developer's own global hooks must never run inside a test repository.
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "refund.py").write_text("def refund(amount):\n    return amount\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


class _Sealed:
    """Where a tests node's candidate ended up, and what it holds."""

    def __init__(self, attempt_repo: Path, commit: str, blob: str) -> None:
        self.attempt_repo = attempt_repo
        self.commit = commit
        self.blob = blob


def _seal_tests_candidate(
    repo: Path, root: Path, visibility: hv.Visibility
) -> _Sealed:
    """Run a tests node's attempt to a committed candidate, as production does.

    The seam under test is `hidden_vault.attempt_repository`, which decides
    which repository the attempt's worktree is created from. Everything after
    it is the ordinary attempt path: branch from the integration head, write the
    declared output, commit.
    """
    attempt_repo = hv.attempt_repository(
        repo=repo,
        state_root=root / "state",
        run_id="run1",
        visibility=visibility,
        integration_branch=INTEGRATION_BRANCH,
    )
    attempt = wt.create_attempt_worktree(
        repo=attempt_repo,
        run_id="run1",
        node_id=f"tests-{visibility}",
        attempt_no=1,
        integration_head=_git(attempt_repo, "rev-parse", INTEGRATION_BRANCH),
        worktrees_root=root / f"worktrees-{visibility}",
        scratch_root=root / f"scratch-{visibility}",
    )
    target = attempt.path / "tests" / "test_hidden.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HIDDEN_TEST_SOURCE)
    _git(attempt.path, "config", "user.email", "harness@example.invalid")
    _git(attempt.path, "config", "user.name", "Harness")
    _git(attempt.path, "add", "-A")
    _git(attempt.path, "commit", "-qm", "tests candidate")
    commit = _git(attempt.path, "rev-parse", "HEAD")
    blob = hv.blob_id_in(attempt_repo, commit, "tests/test_hidden.py")
    assert blob is not None, "the fixture failed to commit the tests file"
    return _Sealed(attempt_repo, commit, blob)


def _builder_worktree(repo: Path, root: Path) -> Path:
    """An implementation builder's worktree, made the way production makes it."""
    attempt = wt.create_attempt_worktree(
        repo=repo,
        run_id="run1",
        node_id="build",
        attempt_no=1,
        integration_head=_git(repo, "rev-parse", INTEGRATION_BRANCH),
        worktrees_root=root / "worktrees-build",
        scratch_root=root / "scratch-build",
    )
    return attempt.path


class SharedObjectDatabaseIsTheLeak(unittest.TestCase):
    """The negative control: `merged` visibility leaks, and that is the defect."""

    def test_a_merged_tests_candidate_is_readable_from_a_builder_worktree(self):
        """Today's shipped behaviour, executed rather than asserted.

        This test passing is what makes the containment test below meaningful.
        It is also the exact defect measured on the live run: the builder's
        worktree shares the run repository's object database, so one plumbing
        command prints the assertions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.MERGED)
            builder = _builder_worktree(repo, root)

            self.assertEqual(
                sealed.attempt_repo,
                repo.resolve(),
                "merged visibility must keep using the run repository",
            )
            printed = _git(builder, "cat-file", "-p", sealed.blob)
            self.assertIn(
                "a negative refund must be refused",
                printed,
                "the control must demonstrate the leak it is controlling for",
            )

    def test_the_builder_worktree_shares_the_run_repositorys_object_database(self):
        """Why the leak exists, named at its cause rather than its symptom."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            builder = _builder_worktree(repo, root)
            common = _git(
                builder, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            self.assertEqual(
                Path(common).resolve(),
                (repo / ".git").resolve(),
                "a linked worktree resolves to the run repository's object store",
            )


class HiddenTestsAreUnreachableFromABuilder(unittest.TestCase):
    """Regression 7. Each of these fails against a run-repository seal."""

    def test_a_hidden_tests_blob_is_unreachable_from_a_builder_worktree(self):
        """The invariant the whole feature stands on."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)
            builder = _builder_worktree(repo, root)

            probe = subprocess.run(
                ["git", "cat-file", "-p", sealed.blob],
                cwd=str(builder),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(
                probe.returncode,
                0,
                "a builder worktree read the hidden test blob:\n" + probe.stdout,
            )
            self.assertNotIn("a negative refund must be refused", probe.stdout)
            self.assertTrue(hv.unreachable_from(builder, [sealed.blob, sealed.commit]))

    def test_the_run_repository_itself_never_receives_the_hidden_objects(self):
        """Containment is a property of the object graph, not of the worktree.

        Asserted against the repository rather than a worktree so a future
        change that merely hides the *path* — without moving the bytes — cannot
        pass this suite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)

            self.assertTrue(hv.object_is_absent(repo, sealed.blob))
            self.assertTrue(hv.object_is_absent(repo, sealed.commit))

    def test_no_run_repository_ref_advertises_the_hidden_candidate(self):
        """A ref would make the commit fetchable even from a shallow clone.

        The containment story is only as good as what the run repository will
        serve, and `git fetch` serves what refs advertise.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)

            advertised = _git(repo, "for-each-ref", "--format=%(objectname)")
            self.assertNotIn(sealed.commit, advertised.split())

    def test_the_vault_holds_what_the_run_repository_does_not(self):
        """The seal must actually store the bytes somewhere.

        Without this, every assertion above is satisfied by a seal that wrote
        nothing at all, which is the vacuous-pass shape §7.4 spends a paragraph
        refusing in another context.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)

            vault = hv.vault_path(root / "state", "run1")
            self.assertEqual(sealed.attempt_repo, vault)
            self.assertFalse(hv.object_is_absent(vault, sealed.blob))
            printed = _git(vault, "cat-file", "-p", sealed.blob)
            self.assertIn("a negative refund must be refused", printed)

    def test_objects_flow_only_from_the_run_repository_into_the_vault(self):
        """The seed direction, asserted as a direction rather than assumed.

        The vault must carry the integration head it branched from — otherwise
        the tests node could not have run — and the run repository must not
        carry anything the vault produced.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            base = _git(repo, "rev-parse", INTEGRATION_BRANCH)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)
            vault = hv.vault_path(root / "state", "run1")

            self.assertFalse(hv.object_is_absent(vault, base), "vault needs the base")
            self.assertTrue(hv.object_is_absent(repo, sealed.commit))


class AdversarialProbesFromTheBuilder(unittest.TestCase):
    """Containment asked the hard way: enumerate, traverse, and fetch.

    `cat-file -p <blob>` is the attack that was measured, so it is the one
    regression 7 leads with. It is also the *weakest* probe: it only proves the
    builder cannot read an object whose id it already knows. These ask whether
    the object database contains the bytes at all, which is the property the
    design actually claims.
    """

    def _sealed_and_builder(self, root: Path):
        repo = _make_run_repo(root)
        sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)
        return repo, sealed, _builder_worktree(repo, root)

    def test_enumerating_every_object_does_not_reach_the_hidden_blob(self):
        """The definitive probe: the whole object database, listed.

        `--batch-all-objects` walks loose and packed objects alike, so this
        does not depend on knowing an id, on a ref existing, or on the blob
        being reachable from any commit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo, sealed, builder = self._sealed_and_builder(root)
            every = _git(
                builder,
                "cat-file",
                "--batch-all-objects",
                "--batch-check=%(objectname)",
            )
            self.assertNotIn(sealed.blob, every.split())
            self.assertNotIn(sealed.commit, every.split())

    def test_traversing_every_ref_does_not_reach_the_hidden_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo, sealed, builder = self._sealed_and_builder(root)
            reachable = _git(builder, "rev-list", "--all", "--objects")
            self.assertNotIn(sealed.blob, reachable)
            self.assertNotIn(sealed.commit, reachable)

    def test_fetching_the_hidden_commit_from_the_run_repository_fails(self):
        """Even a builder that learns the sha cannot pull it from its origin.

        This is the residual a shallow-clone containment scheme would have left
        open, and it is why the bytes are kept out of the run repository rather
        than merely off its branches.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sealed, builder = self._sealed_and_builder(root)
            probe = subprocess.run(
                ["git", "fetch", str(repo), sealed.commit],
                cwd=str(builder),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(probe.returncode, 0)
            self.assertTrue(hv.unreachable_from(builder, [sealed.blob]))

    def test_the_vault_is_not_a_sibling_of_the_builders_worktree(self):
        """Placement, asserted so a later refactor cannot quietly undo it.

        No filesystem sandbox exists (§16.3 item 15), so this is hygiene rather
        than a boundary — but a vault one `..` from the builder's cwd would be
        an invitation, and this test is what stops it becoming one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _repo, _sealed, builder = self._sealed_and_builder(root)
            vault = hv.vault_path(root / "state", "run1")
            self.assertNotIn(vault.resolve(), [p.resolve() for p in builder.parents])
            self.assertFalse(
                str(vault.resolve()).startswith(str(builder.resolve()) + "/")
            )


class VaultCreationIsIdempotent(unittest.TestCase):
    """A resumed run re-opens its vault rather than refusing or clobbering it."""

    def test_ensure_vault_twice_returns_the_same_repository_and_keeps_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_run_repo(root)
            sealed = _seal_tests_candidate(repo, root, hv.HIDDEN)

            again = hv.ensure_vault(root / "state", "run1")
            self.assertEqual(again, hv.vault_path(root / "state", "run1"))
            self.assertFalse(hv.object_is_absent(again, sealed.blob))


if __name__ == "__main__":
    unittest.main()
