"""Private draft/sealed objects are absent from the product repo and builder.

A linked worktree shares the product repository's object database. Private
tests therefore cannot live there: one `git cat-file` would print them. This
suite seals through `hidden_vault` only and checks the bytes stay in the
external bare vault.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import hidden_vault as hv

HIDDEN_TEST_SOURCE = '''\
"""Private tests. A builder must never read this."""


def test_refund_rejects_a_negative_amount() -> None:
    assert refund(-1) is None, "a negative refund must be refused"
'''
INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_hidden.py"
RUN_ID = "run1"
LANE_ID = "lane-a"
INPUT_DIGEST = hashlib.sha256(b"seal-1").hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {} -> {}: {}".format(" ".join(args), result.returncode, result.stderr)
        )
    return result.stdout.strip()


def _product_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "refund.py").write_text("def refund(amount):\n    return amount\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


class _Sealed:
    def __init__(self, vault: Path, commit: str, blob: str, ref: str) -> None:
        self.vault = vault
        self.commit = commit
        self.blob = blob
        self.ref = ref


def _seal(root: Path, repo: Path) -> _Sealed:
    state = root / "state"
    vault = hv.ensure_vault(state, RUN_ID)
    base = hv.seed(vault, repo, INTEGRATION_REF)
    dest = root / "vault-worktrees" / "draft"
    hv.checkout_vault_worktree(vault, base, dest)
    target = dest / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text(HIDDEN_TEST_SOURCE)
    commit = hv.commit_all(dest, "private draft")
    ref = hv.sealed_ref(RUN_ID, LANE_ID, INPUT_DIGEST)
    hv.update_immutable_ref(vault, hv.draft_ref(RUN_ID, LANE_ID, INPUT_DIGEST), commit)
    hv.update_immutable_ref(vault, ref, commit)
    blob = hv.blob_id_in(vault, commit, TEST_PATH)
    assert blob is not None
    return _Sealed(vault, commit, blob, ref)


def _builder(root: Path, repo: Path) -> Path:
    head = hv.rev_parse(repo, "HEAD")
    return hv.linked_worktree(repo, root / "builder", head)


class HiddenTestsAreUnreachableFromABuilder(unittest.TestCase):
    def test_sealed_blob_is_unreachable_from_a_builder_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            sealed = _seal(root, repo)
            builder = _builder(root, repo)
            common = _git(
                builder, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            self.assertEqual(Path(common).resolve(), (repo / ".git").resolve())
            probe = subprocess.run(
                ["git", "cat-file", "-p", sealed.blob],
                cwd=str(builder),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(probe.returncode, 0)
            self.assertNotIn("a negative refund must be refused", probe.stdout)
            self.assertTrue(hv.unreachable_from(builder, [sealed.blob, sealed.commit]))
            hv.prove_absent((repo, builder), (sealed.blob, sealed.commit))

    def test_the_product_repository_never_receives_the_private_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            sealed = _seal(root, repo)
            self.assertTrue(hv.object_is_absent(repo, sealed.blob))
            self.assertTrue(hv.object_is_absent(repo, sealed.commit))

    def test_no_product_ref_advertises_the_sealed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            sealed = _seal(root, repo)
            self.assertNotIn(sealed.commit, hv.advertised_object_ids(repo))

    def test_the_vault_holds_what_the_product_repository_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            sealed = _seal(root, repo)
            self.assertEqual(sealed.vault, hv.vault_path(root / "state", RUN_ID))
            self.assertFalse(hv.object_is_absent(sealed.vault, sealed.blob))
            printed = hv.cat_blob(sealed.vault, sealed.blob).decode("utf-8")
            self.assertIn("a negative refund must be refused", printed)
            self.assertEqual(hv.rev_parse(sealed.vault, sealed.ref), sealed.commit)

    def test_seed_is_one_way_from_product_into_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            base = hv.rev_parse(repo, "HEAD")
            sealed = _seal(root, repo)
            self.assertFalse(hv.object_is_absent(sealed.vault, base))
            self.assertTrue(hv.object_is_absent(repo, sealed.commit))
            self.assertNotIn(sealed.commit, hv.advertised_object_ids(repo))


class AdversarialProbesFromTheBuilder(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, _Sealed, Path]:
        repo = _product_repo(root)
        sealed = _seal(root, repo)
        return repo, sealed, _builder(root, repo)

    def test_enumerating_every_object_does_not_reach_the_hidden_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, sealed, builder = self._fixture(root)
            every = hv.batch_object_ids(builder)
            self.assertNotIn(sealed.blob, every)
            self.assertNotIn(sealed.commit, every)

    def test_traversing_every_ref_does_not_reach_the_hidden_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, sealed, builder = self._fixture(root)
            reachable = hv.rev_list_objects(builder)
            self.assertNotIn(sealed.blob, reachable)
            self.assertNotIn(sealed.commit, reachable)

    def test_fetching_the_sealed_commit_from_the_product_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sealed, builder = self._fixture(root)
            hv.prove_unfetchable(repo, builder, sealed.commit)
            self.assertTrue(hv.unreachable_from(builder, [sealed.blob]))

    def test_the_vault_is_outside_the_builder_and_the_product_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sealed, builder = self._fixture(root)
            vault = sealed.vault.resolve()
            for host in (builder.resolve(), repo.resolve()):
                self.assertNotIn(vault, list(host.parents) + [host])
                self.assertFalse(str(vault).startswith(str(host) + "/"))
                self.assertNotIn(host, list(vault.parents) + [vault])


class VaultCreationIsIdempotent(unittest.TestCase):
    def test_ensure_vault_twice_keeps_the_same_repository_and_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _product_repo(root)
            sealed = _seal(root, repo)
            again = hv.ensure_vault(root / "state", RUN_ID)
            self.assertEqual(again, hv.vault_path(root / "state", RUN_ID))
            self.assertFalse(hv.object_is_absent(again, sealed.blob))


if __name__ == "__main__":
    unittest.main()
