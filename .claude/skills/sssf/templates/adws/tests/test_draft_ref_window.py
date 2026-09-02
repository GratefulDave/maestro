"""A draft commit is anchored from the moment it exists until its ref pins it.

`write_test_draft` commits private test bytes in a scratch worktree of the
vault and pins the draft ref last, after the leak checks. Between those two
points the only thing reaching the commit is the worktree's HEAD, so the
worktree must outlive the pin. These cases run the real vault under the real
git binary: a stubbed `subprocess.run` cannot observe worktree registration.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_refund_secret.py"
TEST_SOURCE = (
    "from refund import refund\n\n\n"
    "def test_refund_rejects_negative():\n"
    "    assert refund(-1) is None\n"
)
CONTRACT = {
    "acceptance_criteria": ["negative amounts are refused"],
    "declared_outputs": [TEST_PATH],
}


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(" ".join(args), result.returncode, result.stderr)
        )
    return result.stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-hooks"))
    (repo / "refund.py").write_text("def refund(amount):\n    return amount\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _registered_worktrees(vault: Path) -> list[str]:
    listing = _git(vault, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]


class DraftRefWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _repo(self.root)
        self.state = self.root / "state"
        self.worktrees = self.root / "worktrees"
        self.run_id = "run1"
        self.lane_id = "lane-a"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self) -> pr.VaultLaneRequest:
        return pr.VaultLaneRequest(
            run_id=self.run_id,
            lane_id=self.lane_id,
            plan_revision=1,
            spec_digest=_digest("spec"),
            lane_projection_digest=_digest("projection"),
            input_digest=_digest("draft-input"),
        )

    def _draft(self):
        return tc.write_test_draft(
            request=self._request(),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=CONTRACT,
            worktrees_root=self.worktrees,
        )

    def test_the_worktree_still_anchors_the_commit_when_the_ref_is_pinned(self):
        """Red when the scratch worktree is removed before the pin.

        Observed through git itself, at the instant the pin is requested: the
        vault must still register a worktree under `worktrees_root`, and that
        worktree's HEAD must be the commit being pinned.
        """
        vault = hv.vault_path(self.state, self.run_id)
        seen: dict[str, object] = {}
        real_pin = hv.update_immutable_ref

        def observing_pin(vault_arg, ref, sha):
            registered = _registered_worktrees(Path(vault_arg))
            scratch = [
                path
                for path in registered
                if Path(path).resolve().is_relative_to(self.worktrees.resolve())
            ]
            seen["scratch"] = scratch
            if scratch:
                seen["head"] = _git(Path(scratch[0]), "rev-parse", "HEAD")
            seen["sha"] = sha
            return real_pin(vault_arg, ref, sha)

        with mock.patch.object(tc.hv, "update_immutable_ref", observing_pin):
            draft = self._draft()

        self.assertEqual(len(seen["scratch"]), 1, seen)
        self.assertEqual(seen["head"], seen["sha"])
        self.assertEqual(hv.rev_parse(vault, draft.artifact_ref), seen["sha"])
        # And the anchor is released once the ref holds the commit.
        self.assertEqual(
            [
                path
                for path in _registered_worktrees(vault)
                if Path(path).resolve().is_relative_to(self.worktrees.resolve())
            ],
            [],
        )

    def test_a_refused_draft_leaves_no_worktree_and_no_ref(self):
        vault = hv.vault_path(self.state, self.run_id)

        def refuse(*_args, **_kwargs):
            raise pr.PrivateReviewError("refused for the test")

        with mock.patch.object(tc.pr, "refuse_private_leak", refuse):
            with self.assertRaises(pr.PrivateReviewError):
                self._draft()

        self.assertEqual(
            [
                path
                for path in _registered_worktrees(vault)
                if Path(path).resolve().is_relative_to(self.worktrees.resolve())
            ],
            [],
        )
        refs = _git(vault, "for-each-ref", "--format=%(refname)", "refs/maestro/drafts/")
        self.assertEqual(refs, "")


class RefHelperRefusalNamesItsOwnDigestTests(unittest.TestCase):
    """Each ref helper is keyed on a different digest; its refusal must say which."""

    def test_draft_ref_names_private_draft_digest(self):
        with self.assertRaises(hv.VaultError) as caught:
            hv.draft_ref("run1", "lane-a", "not-hex")
        self.assertIn("private_draft_digest", str(caught.exception))
        self.assertNotIn("input_digest", str(caught.exception))

    def test_sealed_ref_names_sealed_digest(self):
        with self.assertRaises(hv.VaultError) as caught:
            hv.sealed_ref("run1", "lane-a", "not-hex")
        self.assertIn("sealed_digest", str(caught.exception))
        self.assertNotIn("input_digest", str(caught.exception))

    def test_private_results_ref_names_results_digest(self):
        with self.assertRaises(hv.VaultError) as caught:
            hv.private_results_ref("run1", "lane-a", "not-hex")
        self.assertIn("results_digest", str(caught.exception))
        self.assertNotIn("input_digest", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
