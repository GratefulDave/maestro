"""Focused contract tests for git-publication Wave A.

Real Git repositories only. No mocks. Tests are not executed by this agent.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import git_publication as gp  # noqa: E402
from adw_modules import workspace_receipt as wr  # noqa: E402
from adw_modules.git_helper import BoundGit, zero_oid  # noqa: E402

from adw_modules.scheduler_types import canonical_bytes, digest_canonical  # noqa: E402


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(
    cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "commit.gpgsign=false", *args],
        env=GIT_ENV,
        capture_output=True,
        check=check,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_repo(root: Path, files: dict[str, str] | None = None) -> str:
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        env=GIT_ENV,
        capture_output=True,
        check=True,
    )
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "user.email", "fixture@localhost.invalid")
    payload = files or {"seed.txt": "seed\n"}
    for rel, text in payload.items():
        _write(root / rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return _git(root, "rev-parse", "HEAD").stdout.decode().strip()


def _commit_paths(root: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        _write(root / rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.decode().strip()


def _tree_commit(root: Path, parent: str, files: dict[str, str]) -> str:
    git = BoundGit(root)
    git_dir = str(git.git_dir())
    index = Path(git_dir) / "maestro-test.index"
    if index.exists():
        index.unlink()
    git.read_tree_to_index(parent, str(index), git_dir=git_dir)
    for rel, text in files.items():
        oid = git.hash_object(text.encode("utf-8"), write=True)
        git.run(
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{oid},{rel}",
            index_file=str(index),
            git_dir=git_dir,
        )
    tree = git.text("write-tree", index_file=str(index), git_dir=git_dir)
    return git.commit_tree(tree, (parent,), b"candidate\n", epoch_seconds=1_700_000_000)


class GitPublicationContract(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "product"
        self.root.mkdir()
        self.base = _init_repo(self.root)
        self.binding = gp.bind_target_worktree(self.root, "refs/heads/main")
        self.run_id = "run1"
        self.lane_id = "laneA"
        self.digest = "a" * 64
        gp.ensure_integration_ref(
            self.binding, self.run_id, self.binding.integration_initial_sha
        )

    def _pub(
        self,
        reviewed: str,
        fingerprint: str,
        *,
        expected_before: str | None = None,
        artifact: str | None = None,
    ) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "review_input_fingerprint": fingerprint,
            "final_review_artifact_id": artifact or "5" * 64,
            "expected_before_sha": expected_before or self.base,
            "reviewed_integration_sha": reviewed,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bind_records_fingerprint_and_equal_shas(self) -> None:
        self.assertEqual(
            self.binding.integration_initial_sha, self.binding.target_initial_main_sha
        )
        self.assertEqual(self.binding.target_initial_main_sha, self.base)
        self.assertEqual(self.binding.target_main_ref, "refs/heads/main")
        gp.revalidate_binding(self.binding)
        payload = gp.target_repository_fingerprint_payload(
            self.binding.worktree_root,
            self.binding.worktree_git_dir,
            self.binding.git_common_dir,
            self.binding.target_object_format,
        )
        self.assertEqual(
            digest_canonical(payload), self.binding.target_repository_fingerprint
        )

    def test_bind_refuses_detached_bare_wrong_head(self) -> None:
        _git(self.root, "checkout", "--detach", "HEAD")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.bind_target_worktree(self.root, "refs/heads/main")
        self.assertEqual(raised.exception.code, "TARGET_DETACHED")
        _git(self.root, "checkout", "main")
        _git(self.root, "checkout", "-b", "other")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.bind_target_worktree(self.root, "refs/heads/main")
        self.assertEqual(raised.exception.code, "TARGET_HEAD_MISMATCH")

    def test_ensure_journal_fingerprint(self) -> None:
        self.assertEqual(len(self.binding.target_sync_journal_fingerprint), 64)
        git_fd, journal_fd, digest, _path = wr.ensure_journal_root(
            self.binding.target_worktree_git_dir,
            expected_fingerprint=self.binding.target_sync_journal_fingerprint,
        )
        os.close(journal_fd)
        os.close(git_fd)
        self.assertEqual(digest, self.binding.target_sync_journal_fingerprint)

    def test_journal_refuses_cross_device(self) -> None:
        with self.assertRaises(wr.JournalError) as raised:
            wr.ensure_journal_root(
                self.binding.target_worktree_git_dir,
                expected_fingerprint=self.binding.target_sync_journal_fingerprint,
                manifest_parent_devices=(self.binding.worktree_root.device + 1,),
            )
        self.assertEqual(raised.exception.code, "PUBLICATION_CROSS_DEVICE")

    def test_receipt_collision_refused(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "ee" * 32
        git = BoundGit(self.root)
        other = git.hash_object(b"not-the-receipt\n", write=True)
        git.update_ref(
            gp.publication_ref_name(self.run_id, fingerprint),
            other,
            zero_oid(self.binding.target_object_format),
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.publish_or_reconcile(self.binding, **self._pub(reviewed, fingerprint))
        self.assertEqual(raised.exception.code, "PUBLICATION_RECEIPT_COLLISION")
        self.assertEqual(git.rev_parse("refs/heads/main"), self.base)

    def test_integration_ref_create_match_collision(self) -> None:
        first = gp.ensure_integration_ref(self.binding, self.run_id, self.base)
        second = gp.ensure_integration_ref(self.binding, self.run_id, self.base)
        self.assertEqual(first["sha"], second["sha"])
        self.assertEqual(first["sha"], self.base)
        ref = gp.integration_ref_name(self.run_id)
        other = _commit_paths(self.root, {"seed.txt": "moved\n"}, "move")
        BoundGit(self.root).update_ref(ref, other, first["sha"])
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.ensure_integration_ref(self.binding, self.run_id, self.base)
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")

    def test_integration_ref_missing_initial_and_durable_tip(self) -> None:
        missing_run = "run-missing"
        ref = gp.integration_ref_name(missing_run)
        git = BoundGit(self.root)
        self.assertIsNone(git.read_ref(ref))
        created = gp.ensure_integration_ref(self.binding, missing_run, self.base)
        self.assertEqual(created["sha"], self.base)
        self.assertEqual(git.read_ref(ref), self.base)
        durable = _tree_commit(self.root, self.base, {"tip.txt": "t\n"})
        git.update_ref(ref, durable, self.base)
        matched = gp.ensure_integration_ref(self.binding, missing_run, durable)
        self.assertEqual(matched["sha"], durable)
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.ensure_integration_ref(self.binding, missing_run, self.base)
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")
        absent = "run-absent-durable"
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.ensure_integration_ref(self.binding, absent, durable)
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_MISSING")
        self.assertIsNone(git.read_ref(gp.integration_ref_name(absent)))

    def test_candidate_ref_pin_immutable(self) -> None:
        sha = _commit_paths(self.root, {"owned.txt": "one\n"}, "cand")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=sha,
            changed=True,
            declared_outputs=("owned.txt",),
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.pin_candidate_ref(
                self.binding,
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=self.digest,
                candidate_sha=self.base,
            )
        self.assertEqual(raised.exception.code, "CANDIDATE_REF_COLLISION")

    def test_tree_delta_ownership_full_matrix(self) -> None:
        _commit_paths(
            self.root, {"keep.txt": "k\n", "old.txt": "o\n", "mode.txt": "m\n"}, "setup"
        )
        base = _git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
        _write(self.root / "keep.txt", "k2\n")
        _write(self.root / "new.txt", "n\n")
        (self.root / "old.txt").unlink()
        _write(self.root / "renamed.txt", "o\n")
        os.chmod(self.root / "mode.txt", 0o755)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "delta")
        cand = _git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
        delta = gp.measure_tree_delta(self.binding, base, cand)
        statuses = {item.status for item in delta}
        self.assertTrue({"A", "M", "D"}.issubset(statuses) or "R" in statuses)
        declared = tuple({path for item in delta for path in item.represented_paths()})
        gp.validate_declared_ownership(delta, declared, changed=True)

    def test_undeclared_path_refuses_before_ref(self) -> None:
        sha = _commit_paths(self.root, {"secret.txt": "no\n"}, "secret")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.admit_candidate(
                self.binding,
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest="b" * 64,
                builder_base_sha=self.base,
                candidate_sha=sha,
                changed=True,
                declared_outputs=("owned.txt",),
            )
        self.assertEqual(raised.exception.code, "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED")
        self.assertIsNone(
            BoundGit(self.root).read_ref(
                gp.candidate_ref_name(self.run_id, self.lane_id, "b" * 64)
            )
        )

    def test_changed_true_empty_delta_refused(self) -> None:
        git = BoundGit(self.root)
        tree = git.resolve_tree(self.base)
        empty = git.commit_tree(
            tree, (self.base,), b"empty\n", epoch_seconds=1_700_000_000
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.admit_candidate(
                self.binding,
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest="c" * 64,
                builder_base_sha=self.base,
                candidate_sha=empty,
                changed=True,
                declared_outputs=("seed.txt",),
            )
        self.assertEqual(raised.exception.code, "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED")

    def test_candidate_reconcile_changed_and_zero(self) -> None:
        sha = _commit_paths(self.root, {"owned.txt": "x\n"}, "c")
        digest = "d" * 64
        first = gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=digest,
            builder_base_sha=self.base,
            candidate_sha=sha,
            changed=True,
            declared_outputs=("owned.txt",),
        )
        replay = gp.reconcile_candidate_ref(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=digest,
            builder_base_sha=self.base,
            changed=True,
            declared_outputs=("owned.txt",),
        )
        self.assertEqual(first, replay)
        zero_digest = "e" * 64
        zero = gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id="laneZ",
            input_digest=zero_digest,
            builder_base_sha=self.base,
            candidate_sha=self.base,
            changed=False,
            declared_outputs=("seed.txt",),
        )
        self.assertFalse(zero["changed"])
        self.assertEqual(zero["candidate_sha"], self.base)

    def test_exact_sha_merge_cas(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        payload = gp.execute_exact_sha_merge(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            stage_input_digest=self.digest,
            builder_artifact_id="f" * 64,
            code_review_artifact_id="1" * 64,
            builder_base_sha=self.base,
            candidate_ref=gp.candidate_ref_name(self.run_id, self.lane_id, self.digest),
            candidate_sha=cand,
            before_sha=self.base,
            epoch_seconds=1_700_000_000,
            input_digest=self.digest,
        )
        self.assertEqual(payload["before_sha"], self.base)
        self.assertNotEqual(payload["after_sha"], self.base)
        self.assertFalse(payload["revalidated"])
        ref = gp.integration_ref_name(self.run_id)
        self.assertEqual(BoundGit(self.root).read_ref(ref), payload["after_sha"])

    def test_merge_tree_conflict_does_not_move_ref(self) -> None:
        _commit_paths(self.root, {"clash.txt": "left\n"}, "left")
        left = _git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
        _git(self.root, "reset", "--hard", self.base)
        cand = _commit_paths(self.root, {"clash.txt": "right\n"}, "right")
        gp.ensure_integration_ref(self.binding, "run-conflict", self.base)

        BoundGit(self.root).update_ref(
            gp.integration_ref_name("run-conflict"),
            left,
            self.base,
        )
        gp.admit_candidate(
            self.binding,
            run_id="run-conflict",
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("clash.txt",),
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.execute_exact_sha_merge(
                self.binding,
                run_id="run-conflict",
                lane_id=self.lane_id,
                stage_input_digest=self.digest,
                builder_artifact_id="f" * 64,
                code_review_artifact_id="1" * 64,
                builder_base_sha=self.base,
                candidate_ref=gp.candidate_ref_name(
                    "run-conflict", self.lane_id, self.digest
                ),
                candidate_sha=cand,
                before_sha=left,
                epoch_seconds=1,
                input_digest=self.digest,
            )
        self.assertEqual(raised.exception.code, "MERGE_TREE_REFUSED")
        self.assertEqual(
            BoundGit(self.root).read_ref(gp.integration_ref_name("run-conflict")), left
        )

    def test_commit_tree_stable_across_identity_and_encoding(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        git = BoundGit(self.root)
        tree = git.merge_tree_write_tree(self.base, cand)
        message = gp.canonical_merge_message(
            run_id=self.run_id,
            lane_id=self.lane_id,
            stage_input_digest=self.digest,
            builder_artifact_id="f" * 64,
            before_sha=self.base,
            candidate_sha=cand,
            expected_tree_sha=tree,
        )
        first = git.commit_tree(tree, (self.base, cand), message, epoch_seconds=42)
        extra = {**GIT_ENV, "GIT_AUTHOR_NAME": "other", "LC_ALL": "C"}
        second = git.commit_tree(tree, (self.base, cand), message, epoch_seconds=42)
        self.assertEqual(first, second)
        self.assertIsNotNone(extra)

    def test_zero_delta_revalidation_no_commit(self) -> None:
        payload = gp.revalidate_zero_delta(
            self.binding,
            builder_artifact_id="f" * 64,
            code_review_artifact_id="1" * 64,
            builder_base_sha=self.base,
            candidate_ref="refs/maestro/candidates/run1/laneA/" + self.digest,
            candidate_sha=self.base,
            input_digest=self.digest,
        )
        self.assertTrue(payload["revalidated"])
        self.assertEqual(payload["before_sha"], payload["after_sha"])
        self.assertEqual(payload["after_sha"], self.base)
        self.assertEqual(
            BoundGit(self.root).read_ref(gp.integration_ref_name(self.run_id)),
            self.base,
        )

    def test_stale_zero_delta_emits_base_invalidation(self) -> None:
        advanced = _commit_paths(self.root, {"other.txt": "x\n"}, "other")
        decision = gp.decide_merge_action(
            changed=False,
            builder_base_sha=self.base,
            candidate_sha=self.base,
            integration_head=advanced,
        )
        self.assertEqual(decision.action, "BASE_INVALIDATION")
        payload = gp.base_invalidation_payload(
            stale_builder_output_artifact_id="f" * 64,
            stale_code_review_artifact_id="1" * 64,
            stale_builder_base_sha=self.base,
            stale_candidate_sha=self.base,
            observed_integration_head=advanced,
            input_digest=self.digest,
        )
        self.assertEqual(payload["kind"], "BASE_INVALIDATION")
        self.assertEqual(payload["observed_integration_head"], advanced)

    def _merge_kwargs(
        self, cand: str, *, builder: str = "f" * 64, epoch: int = 9
    ) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "lane_id": self.lane_id,
            "stage_input_digest": self.digest,
            "builder_artifact_id": builder,
            "code_review_artifact_id": "1" * 64,
            "builder_base_sha": self.base,
            "candidate_ref": gp.candidate_ref_name(
                self.run_id, self.lane_id, self.digest
            ),
            "candidate_sha": cand,
            "before_sha": self.base,
            "epoch_seconds": epoch,
            "input_digest": self.digest,
        }

    def test_merge_reconcile_accepts_matching_commit(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        kwargs = self._merge_kwargs(cand)
        first = gp.execute_exact_sha_merge(self.binding, **kwargs)
        replay = gp.merge_or_reconcile(self.binding, **kwargs)
        self.assertEqual(first, replay)
        self.assertEqual(
            BoundGit(self.root).read_ref(gp.integration_ref_name(self.run_id)),
            first["after_sha"],
        )

    def test_merge_cas_then_death_before_ledger_replays(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        kwargs = self._merge_kwargs(cand)
        first = gp.execute_exact_sha_merge(self.binding, **kwargs)
        ref = gp.integration_ref_name(self.run_id)
        git = BoundGit(self.root)
        self.assertEqual(git.read_ref(ref), first["after_sha"])
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.execute_exact_sha_merge(self.binding, **kwargs)
        self.assertEqual(raised.exception.code, "INTEGRATION_HEAD_MISMATCH")
        replay = gp.merge_or_reconcile(self.binding, **kwargs)
        self.assertEqual(replay["kind"], "INTEGRATION_MERGE")
        self.assertEqual(replay["after_sha"], first["after_sha"])
        self.assertEqual(replay["before_sha"], self.base)
        self.assertFalse(replay["revalidated"])
        self.assertEqual(git.read_ref(ref), first["after_sha"])

    def test_merge_or_reconcile_arbitrary_ahead_is_collision(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        kwargs = self._merge_kwargs(cand)
        first = gp.execute_exact_sha_merge(self.binding, **kwargs)
        other = _tree_commit(self.root, self.base, {"other.txt": "x\n"})
        BoundGit(self.root).update_ref(
            gp.integration_ref_name(self.run_id), other, first["after_sha"]
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.merge_or_reconcile(self.binding, **kwargs)
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")

    def test_merge_reconcile_refuses_mismatch(self) -> None:
        cand = _commit_paths(self.root, {"lane.txt": "L\n"}, "lane")
        gp.admit_candidate(
            self.binding,
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=self.digest,
            builder_base_sha=self.base,
            candidate_sha=cand,
            changed=True,
            declared_outputs=("lane.txt",),
        )
        kwargs = self._merge_kwargs(cand)
        gp.execute_exact_sha_merge(self.binding, **kwargs)
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.merge_or_reconcile(
                self.binding, **self._merge_kwargs(cand, builder="0" * 64)
            )
        self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")

    def test_final_review_fingerprint_changes_without_sha_change(self) -> None:
        lanes = (
            {
                "lane_id": "a",
                "spec_digest": "1" * 64,
                "public_contract_artifact_id": "2" * 64,
                "sealed_test_bundle_artifact_id": "3" * 64,
            },
        )
        first = gp.final_review_input_fingerprint(
            integration_sha=self.base,
            plan_revision=1,
            plan_digest="4" * 64,
            lanes=lanes,
        )
        second = gp.final_review_input_fingerprint(
            integration_sha=self.base,
            plan_revision=2,
            plan_digest="4" * 64,
            lanes=lanes,
        )
        third = gp.final_review_input_fingerprint(
            integration_sha=self.base,
            plan_revision=1,
            plan_digest="4" * 64,
            lanes=(
                {
                    "lane_id": "a",
                    "spec_digest": "1" * 64,
                    "public_contract_artifact_id": "2" * 64,
                    "sealed_test_bundle_artifact_id": "9" * 64,
                },
            ),
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_preflight_refusals_do_not_mutate_refs(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "aa" * 32
        _write(self.root / "pub.txt", "x")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.publish_or_reconcile(self.binding, **self._pub(reviewed, fingerprint))
        self.assertEqual(raised.exception.code, "PUBLICATION_PREFLIGHT_REFUSED")
        git = BoundGit(self.root)
        self.assertEqual(git.rev_parse("refs/heads/main"), self.base)
        self.assertIsNone(
            git.read_ref(gp.publication_ref_name(self.run_id, fingerprint))
        )
        (self.root / "pub.txt").unlink()

    def test_untouched_untracked_and_ignored_files_do_not_refuse(self) -> None:
        """A dependency tree is not a reason to refuse publication.

        The synchronizer only opens paths the delta names, so the presence of
        `node_modules/` or the deployment's own `adws/` says nothing about
        whether the publication can be applied. Refusing on repo-wide presence
        made publication unreachable in every checkout that had ever installed
        anything -- which is why no run had ever published.
        """
        _write(self.root / ".gitignore", "node_modules/\n")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-m", "ignore deps")
        self.base = BoundGit(self.root).rev_parse("refs/heads/main")
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = gp.final_review_input_fingerprint(
            integration_sha=reviewed,
            plan_revision=1,
            plan_digest="4" * 64,
            lanes=(),
        )
        _write(self.root / "node_modules" / "left-pad" / "index.js", "module\n")
        _write(self.root / "adws" / "maestro.py", "runtime\n")
        payload = gp.publish_or_reconcile(
            self.binding, **self._pub(reviewed, fingerprint)
        )
        self.assertEqual(payload["published_sha"], reviewed)
        self.assertEqual(BoundGit(self.root).rev_parse("refs/heads/main"), reviewed)

    def test_untracked_file_on_a_published_path_still_refuses(self) -> None:
        """The narrowing keeps the collision it was built for."""

        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "bb" * 32
        _write(self.root / "pub.txt", "squatter\n")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.publish_or_reconcile(self.binding, **self._pub(reviewed, fingerprint))
        self.assertEqual(raised.exception.code, "PUBLICATION_PREFLIGHT_REFUSED")
        self.assertEqual(raised.exception.detail, "untracked:pub.txt")
        self.assertEqual(BoundGit(self.root).rev_parse("refs/heads/main"), self.base)

    def test_atomic_receipt_and_main(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = gp.final_review_input_fingerprint(
            integration_sha=reviewed,
            plan_revision=1,
            plan_digest="4" * 64,
            lanes=(),
        )
        payload = gp.publish_or_reconcile(
            self.binding, **self._pub(reviewed, fingerprint)
        )
        self.assertEqual(payload["kind"], "MAIN_PUBLICATION")
        self.assertEqual(payload["published_sha"], reviewed)
        git = BoundGit(self.root)
        self.assertEqual(git.rev_parse("refs/heads/main"), reviewed)
        self.assertEqual(
            git.read_ref(gp.publication_ref_name(self.run_id, fingerprint)),
            payload["receipt_object"],
        )
        blob = git.cat_file("blob", payload["receipt_object"])
        expected = gp.publication_receipt_payload(
            run_id=self.run_id,
            target_repository_fingerprint=self.binding.target_repository_fingerprint,
            target_main_ref=self.binding.target_main_ref,
            review_input_fingerprint=fingerprint,
            final_review_artifact_id="5" * 64,
            expected_before_sha=self.base,
            reviewed_integration_sha=reviewed,
        )
        self.assertEqual(blob, canonical_bytes(expected))

    def test_multi_ref_cas_is_atomic(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "77" * 32
        kwargs = self._pub(reviewed, fingerprint)
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        wr.publish_journal(journal)
        wrong = _tree_commit(self.root, self.base, {"other.txt": "o\n"})
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.cas_receipt_and_main(
                self.binding,
                journal,
                run_id=self.run_id,
                review_input_fingerprint=fingerprint,
                expected_before_sha=wrong,
                reviewed_integration_sha=reviewed,
            )
        self.assertEqual(raised.exception.code, "PUBLICATION_REF_CAS_REFUSED")
        git = BoundGit(self.root)
        self.assertIsNone(
            git.read_ref(gp.publication_ref_name(self.run_id, fingerprint))
        )
        self.assertEqual(git.rev_parse("refs/heads/main"), self.base)
        wr.close_journal(journal)

    def test_repeat_publish_replays(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "ab" * 32
        kwargs = self._pub(reviewed, fingerprint)
        first = gp.publish_or_reconcile(self.binding, **kwargs)
        second = gp.publish_or_reconcile(self.binding, **kwargs)
        self.assertEqual(first, second)
        again = gp.reconcile_publication_if_present(self.binding, **kwargs)
        self.assertEqual(first, again)

    def test_same_main_sha_without_receipt_refused(self) -> None:
        reviewed = _commit_paths(self.root, {"pub.txt": "p\n"}, "pub")
        fingerprint = "cd" * 32
        kwargs = self._pub(reviewed, fingerprint)
        self.assertIsNone(
            gp.reconcile_publication_if_present(
                self.binding,
                **self._pub(self.base, fingerprint),
            )
        )
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.reconcile_publication_if_present(self.binding, **kwargs)
        self.assertEqual(raised.exception.code, "PUBLICATION_EXTERNAL_SAME_SHA")
        with self.assertRaises(gp.GitPublicationRefused) as raised:
            gp.publish_or_reconcile(self.binding, **kwargs)
        self.assertEqual(raised.exception.code, "PUBLICATION_EXTERNAL_SAME_SHA")

    def test_crash_before_journal_publish_prepares_again(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "11" * 32
        kwargs = self._pub(reviewed, fingerprint)
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        self.assertFalse(wr.journal_is_published(journal))
        wr.close_journal(journal)
        self.assertIsNone(gp.reconcile_publication_if_present(self.binding, **kwargs))
        payload = gp.publish_or_reconcile(self.binding, **kwargs)
        self.assertEqual(payload["kind"], "MAIN_PUBLICATION")

    def test_crash_after_journal_before_refs_replays(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "22" * 32
        kwargs = self._pub(reviewed, fingerprint)
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        wr.publish_journal(journal)
        wr.close_journal(journal)
        payload = gp.reconcile_publication_if_present(self.binding, **kwargs)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["published_sha"], reviewed)
        self.assertEqual((self.root / "pub.txt").read_text(), "p\n")

    def test_crash_after_refs_completes_sync(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "33" * 32
        kwargs = self._pub(reviewed, fingerprint)
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        wr.publish_journal(journal)
        gp.cas_receipt_and_main(
            self.binding,
            journal,
            run_id=self.run_id,
            review_input_fingerprint=fingerprint,
            expected_before_sha=self.base,
            reviewed_integration_sha=reviewed,
        )
        wr.close_journal(journal)
        self.assertFalse((self.root / "pub.txt").exists())
        payload = gp.reconcile_publication_if_present(self.binding, **kwargs)
        self.assertIsNotNone(payload)
        self.assertEqual((self.root / "pub.txt").read_text(), "p\n")

    def test_sync_installs_reviewed_tree(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"dir/file.txt": "new\n"})
        fingerprint = "44" * 32
        gp.publish_or_reconcile(self.binding, **self._pub(reviewed, fingerprint))
        self.assertEqual((self.root / "dir" / "file.txt").read_text(), "new\n")

    def test_parent_symlink_race_refuses(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"race/child.txt": "c\n"})
        fingerprint = "55" * 32
        kwargs = self._pub(reviewed, fingerprint)
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        wr.publish_journal(journal)
        gp.cas_receipt_and_main(
            self.binding,
            journal,
            run_id=self.run_id,
            review_input_fingerprint=fingerprint,
            expected_before_sha=self.base,
            reviewed_integration_sha=reviewed,
        )
        wr.create_index_lock(self.binding.target_worktree_git_dir, journal)
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        raced = self.root / "race"
        if raced.exists():
            for child in raced.iterdir():
                child.unlink()
            raced.rmdir()
        raced.symlink_to(outside)
        with self.assertRaises(wr.JournalError) as raised:
            wr.synchronize_publication_worktree(
                target_root=self.binding.target_repository_root,
                worktree_git_dir=self.binding.target_worktree_git_dir,
                journal=journal,
                object_format=self.binding.target_object_format,
                git=self.binding.git(),
            )
        self.assertTrue(
            str(raised.exception).startswith("PUBLICATION_WORKTREE_SYNC_REFUSED")
        )
        wr.close_journal(journal)

    def test_reconcile_none_until_started_then_payload(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        fingerprint = "66" * 32
        kwargs = self._pub(reviewed, fingerprint)
        self.assertIsNone(gp.reconcile_publication_if_present(self.binding, **kwargs))
        journal = gp.prepare_publication_journal(self.binding, **kwargs)
        wr.publish_journal(journal)
        wr.close_journal(journal)
        started = gp.reconcile_publication_if_present(self.binding, **kwargs)
        self.assertIsNotNone(started)
        assert started is not None
        self.assertEqual(started["kind"], "MAIN_PUBLICATION")
        replay = gp.publish_or_reconcile(self.binding, **kwargs)
        self.assertEqual(started, replay)

    def test_lock_unavailable(self) -> None:
        with gp.target_worktree_lock(self.binding.target_worktree_git_dir):
            with self.assertRaises(gp.GitPublicationRefused) as raised:
                with gp.target_worktree_lock(self.binding.target_worktree_git_dir):
                    pass
        self.assertEqual(raised.exception.code, "PUBLICATION_WORKTREE_LOCK_REFUSED")

    def test_locked_apis_under_held_worktree_lock(self) -> None:
        reviewed = _tree_commit(self.root, self.base, {"pub.txt": "p\n"})
        kwargs = self._pub(reviewed, "88" * 32)
        with gp.target_worktree_lock(self.binding.target_worktree_git_dir):
            with self.assertRaises(gp.GitPublicationRefused) as nested:
                gp.publish_or_reconcile(self.binding, **kwargs)
            self.assertEqual(nested.exception.code, "PUBLICATION_WORKTREE_LOCK_REFUSED")
            with self.assertRaises(gp.GitPublicationRefused) as nested:
                gp.reconcile_publication_if_present(self.binding, **kwargs)
            self.assertEqual(nested.exception.code, "PUBLICATION_WORKTREE_LOCK_REFUSED")
            self.assertIsNone(
                gp.reconcile_publication_if_present_locked(self.binding, **kwargs)
            )
            payload = gp.publish_or_reconcile_locked(self.binding, **kwargs)
            self.assertEqual(payload["kind"], "MAIN_PUBLICATION")
            self.assertEqual(payload["published_sha"], reviewed)
            replay = gp.reconcile_publication_if_present_locked(self.binding, **kwargs)
            self.assertEqual(payload, replay)

    def test_module_does_not_import_store_or_stage(self) -> None:
        owned = [
            ADWS / "adw_modules" / "publication.py",
            ADWS / "adw_modules" / "git_publication.py",
            ADWS / "adw_modules" / "git_helper.py",
            ADWS / "adw_modules" / "workspace_receipt.py",
            ADWS / "adw_modules" / "receipt_crypto.py",
        ]
        forbidden = {
            "lifecycle",
            "scheduler",
            "coordinator_store",
            "coordinator",
            "hidden_vault",
            "plan_model",
        }
        for path in owned:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    name = node.module.rsplit(".", 1)[-1]
                    self.assertNotIn(name, forbidden, path.name)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(
                            alias.name.rsplit(".", 1)[-1], forbidden, path.name
                        )


if __name__ == "__main__":
    unittest.main()
