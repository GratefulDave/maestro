"""External contract tests for signed Maestro workspace receipts."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ADWS))

from adw_modules import finalization as finalization  # noqa: E402
from adw_modules import receipt_crypto as crypto  # noqa: E402
from adw_modules import workspace_canonical as workspace_canonical  # noqa: E402
from adw_modules import workspace_digest as workspace_digest  # noqa: E402
from adw_modules import workspace_model as workspace_model  # noqa: E402
from adw_modules import workspace_receipt as receipts  # noqa: E402


WORKSPACE_DIGEST = "a" * 64
PLAN_DIGEST = "b" * 64
BASE_COMMIT = "c" * 40


def authorization(mode="write", plan_digest=PLAN_DIGEST, target_branch="main"):
    return receipts.ParticipantAuthorization(
        repository_id="api",
        mode=mode,
        base_commit=BASE_COMMIT,
        plan_digest=plan_digest,
        target_branch=target_branch,
    )

def workspace_plan():
    return workspace_model.parse_mapping({
        "schema_version": "maestro-workspace.v1",
        "workspace_id": "release",
        "repositories": [{
            "repository_id": "api",
            "mode": "write",
            "path": "participant",
            "base_commit": BASE_COMMIT,
            "plan_path": "plans/api.json",
            "plan_digest": PLAN_DIGEST,
            "target_branch": "main",
            "run_argv": ["maestro"],
        }],
    })

def canonical_workspace_digest():
    return workspace_digest.digest_of(
        workspace_canonical.canonicalize_workspace(workspace_plan()))


def plan_receipt(verdict=finalization.Verdict.PASS, digest=PLAN_DIGEST):
    return finalization.Receipt(
        plan_digest=digest,
        rubric_version="test",
        verdict=verdict,
        cells=(),
        reviewer=finalization.ReviewerIdentity("route", "model", "session"),
        created_at_epoch=0,
    )


class WorkspaceReceiptContract(unittest.TestCase):
    def make_store(self, root, repo, data, seed):
        return receipts.WorkspaceReceiptStore(
            root,
            participant_repos=(repo,),
            data_dir=data,
            verify_keys=(crypto.seed_to_public_key(seed),),
            signing_seed=seed,
        )

    def make_finalization_store(self, directory):
        root = Path(directory)
        repo = root / "participant"
        data = root / "sssf-data"
        repo.mkdir()
        data.mkdir()
        return self.make_store(root / "receipts", repo, data, bytes(range(32)))

    def test_signed_receipt_binds_exact_ordered_participant_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            seed = bytes(range(32))
            store = self.make_store(root / "receipts", repo, data, seed)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST,
                participants=(authorization(),),
            )

            store.write(receipt)

            loaded = store.load(WORKSPACE_DIGEST)
            self.assertTrue(loaded.authorizes(
                WORKSPACE_DIGEST, (authorization(),)))
            self.assertFalse(loaded.authorizes(
                WORKSPACE_DIGEST, (authorization(target_branch="release"),)))

    def test_receipt_store_refuses_participant_or_sssf_data_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            seed = bytes(range(32))

            with self.assertRaises(receipts.ReceiptStoreLocationError):
                self.make_store(repo / "receipts", repo, data, seed)
            with self.assertRaises(receipts.ReceiptStoreLocationError):
                self.make_store(data / "receipts", repo, data, seed)

    def test_a_workspace_signing_key_must_be_among_verification_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            signer = bytes(range(32))
            stranger = bytes(reversed(range(32)))
            with self.assertRaises(receipts.SigningKeyUnavailable):
                receipts.WorkspaceReceiptStore(
                    root / "receipts", participant_repos=(repo,), data_dir=data,
                    verify_keys=(crypto.seed_to_public_key(stranger),),
                    signing_seed=signer)

    def test_workspace_timestamp_overflow_is_a_typed_receipt_failure(self):
        with self.assertRaises(receipts.ReceiptInvalid):
            receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST, participants=(authorization(),),
                created_at_epoch=10 ** 400)

    def test_workspace_keyless_store_replays_existing_receipts_but_cannot_mint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            seed = bytes(range(32))
            store = self.make_store(root / "receipts", repo, data, seed)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST, participants=(authorization(),))
            store.write(receipt)
            keyless = receipts.WorkspaceReceiptStore(
                store.root, participant_repos=(repo,), data_dir=data,
                verify_keys=(crypto.seed_to_public_key(seed),), create=False)

            self.assertEqual(keyless.load(WORKSPACE_DIGEST), receipt)
            with self.assertRaises(receipts.SigningKeyUnavailable):
                keyless.write(receipt)

    def test_receipt_is_create_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            seed = bytes(range(32))
            store = self.make_store(root / "receipts", repo, data, seed)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST,
                participants=(authorization(),),
            )
            store.write(receipt)

            with self.assertRaises(receipts.ReceiptExists):
                store.write(receipt)

    def test_read_only_store_does_not_create_a_missing_receipt_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            missing = root / "missing-receipts"
            repo.mkdir()
            data.mkdir()
            store = receipts.WorkspaceReceiptStore(
                missing, participant_repos=(repo,), data_dir=data,
                verify_keys=(crypto.seed_to_public_key(bytes(range(32))),),
                create=False)

            self.assertFalse(store.has(WORKSPACE_DIGEST))
            with self.assertRaises(FileNotFoundError):
                store.load(WORKSPACE_DIGEST)
            self.assertFalse(missing.exists())

    def test_non_utf8_workspace_signature_is_a_typed_verification_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_finalization_store(directory)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST, participants=(authorization(),))
            path = store.write(receipt)
            Path(str(path) + ".sig").write_bytes(b"\xff")

            with self.assertRaises(receipts.SignatureInvalid):
                store.load(WORKSPACE_DIGEST)



    def test_finalize_refuses_failed_participant_plan_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(receipts.AuthorizationError):
                receipts.finalize(
                    canonical_workspace_digest(), workspace_plan(),
                    lambda _digest: plan_receipt(finalization.Verdict.FAIL),
                    self.make_finalization_store(directory))

    def test_finalize_refuses_participant_receipt_for_other_plan_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(receipts.AuthorizationError):
                receipts.finalize(
                    canonical_workspace_digest(), workspace_plan(),
                    lambda _digest: plan_receipt(digest="d" * 64),
                    self.make_finalization_store(directory))

    def test_finalize_refuses_missing_participant_plan_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(receipts.AuthorizationError):
                receipts.finalize(
                    canonical_workspace_digest(), workspace_plan(), lambda _digest: None,
                    self.make_finalization_store(directory))

    def test_finalize_refuses_participant_receipt_loader_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            def unavailable(_digest):
                raise FileNotFoundError("plan receipt is absent")

            with self.assertRaises(receipts.AuthorizationError):
                receipts.finalize(
                    canonical_workspace_digest(), workspace_plan(), unavailable,
                    self.make_finalization_store(directory))


    def test_finalize_recomputes_canonical_workspace_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(receipts.AuthorizationError):
                receipts.finalize(
                    WORKSPACE_DIGEST, workspace_plan(), lambda _digest: plan_receipt(),
                    self.make_finalization_store(directory))

    def test_finalize_replays_before_loading_child_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_finalization_store(directory)
            digest = canonical_workspace_digest()
            receipts.finalize(digest, workspace_plan(), lambda _digest: plan_receipt(), store)

            replayed = receipts.finalize(
                digest, workspace_plan(),
                lambda _digest: self.fail("replay must not load child receipts"), store)

            self.assertEqual(replayed.workspace_digest, digest)

    def test_signature_stage_crash_is_repaired_only_for_matching_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_finalization_store(directory)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST, participants=(authorization(),))
            original_replace = store._replace
            store._replace = lambda _source, _destination: (_ for _ in ()).throw(OSError("crash"))
            with self.assertRaises(OSError):
                store.write(receipt)
            store._replace = original_replace

            store.write(receipt)
            self.assertTrue(store.has(WORKSPACE_DIGEST))

    def test_receipt_stage_crash_is_repaired_only_for_matching_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_finalization_store(directory)
            receipt = receipts.WorkspaceReceipt(
                workspace_digest=WORKSPACE_DIGEST, participants=(authorization(),))
            original_replace = store._replace
            calls = []

            def crash_receipt_commit(source, destination):
                calls.append(destination)
                if len(calls) == 2:
                    raise OSError("crash")
                original_replace(source, destination)

            store._replace = crash_receipt_commit
            with self.assertRaises(OSError):
                store.write(receipt)
            store._replace = original_replace

            store.write(receipt)
            self.assertTrue(store.has(WORKSPACE_DIGEST))

    def test_concurrent_finalizers_return_the_exact_same_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "participant"
            data = root / "sssf-data"
            repo.mkdir()
            data.mkdir()
            seed = bytes(range(32))
            stores = (
                self.make_store(root / "receipts", repo, data, seed),
                self.make_store(root / "receipts", repo, data, seed),
            )
            digest = canonical_workspace_digest()
            results = []
            errors = []
            barrier = threading.Barrier(2)

            def finalize_with(store):
                try:
                    barrier.wait()
                    results.append(receipts.finalize(
                        digest, workspace_plan(), lambda _digest: plan_receipt(), store))
                except Exception as error:
                    errors.append(error)

            workers = tuple(threading.Thread(target=finalize_with, args=(store,))
                            for store in stores)
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(errors, [])
            self.assertEqual([item.workspace_digest for item in results], [digest, digest])

if __name__ == "__main__":
    unittest.main()
