"""Executable proof of finalization (§6.5, §6.6, §11.1).

What §6.5 actually requires, and what each block below settles:

  ordering    -- every deterministic obligation runs BEFORE any reviewer
                 is launched. The failure being prevented is a reviewer
                 passing thirty nodes and publication then dying on a pure
                 function of the authored bytes.
  the matrix  -- code computes the (check x object) applicability matrix,
                 so two reviewers receive identical coverage.
  the report  -- the reviewer emits per-cell `clear|finding` plus a
                 message, and **cannot** emit a verdict or a severity.
                 Not "must not": the schema has no such fields and
                 `extra=forbid` rejects them, so they are unrepresentable.
                 This is the same construction `FailureSignal` uses to
                 make reading process output impossible rather than
                 merely forbidden (§7.5), and it gets the same treatment:
                 a detector, plus a planted violation proving the detector
                 convicts (§13.4).
  the canary  -- a pair, because §13.4's rule is a pair. One synthetic
                 cell known-bad by construction and one known-good, both
                 excluded from verdict derivation. A reviewer answering
                 `clear` to everything is caught by the first; one
                 answering `finding` to everything by the second.
  the receipt -- the FULL per-cell matrix on PASS and FAIL, plus the
                 reviewer's (route, model, session id), signed with a
                 detached Ed25519 signature, in a store that refuses to
                 sit inside the repository or the SSSF data directory.
  replay      -- keyed on the digest alone, short-circuiting with zero
                 reviewer calls.
  the stall   -- `FINALIZATION_STALLED` is not a receipt: no receipt
                 exists for the digest afterwards, and a rerun is legal.

Real resources only: a real `git init` repository and real temporary
directories for the store-location invariant, and the real
`FinalizationWindow` (driven by an injected clock) wherever a window is
needed — never a mock of either.

Run with:  uv run adws/adw_test.py -k finaliz
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import pydantic  # noqa: E402

from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402

DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
OTHER_DIGEST = "a" * 64

OBJECTS = (
    fin.ReviewObject(object_id="plan", kind=fin.ObjectKind.PLAN),
    fin.ReviewObject(object_id="node:build", kind=fin.ObjectKind.NODE),
    fin.ReviewObject(object_id="node:build#gate", kind=fin.ObjectKind.GATE),
    fin.ReviewObject(object_id="evidence:0", kind=fin.ObjectKind.EVIDENCE),
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def matrix_for(objects=OBJECTS, digest=DIGEST) -> fin.ApplicabilityMatrix:
    return fin.compute_matrix(fin.DEFAULT_RUBRIC, digest, objects)


def clean_report(matrix, *, digest=None, pair_count=None,
                 findings=(), drop=(), extra_cells=()) -> dict:
    """A report answering every cell: `clear` everywhere except the
    known-bad canary and whatever `findings` names."""
    cells = []
    for cell in matrix.cells:
        if cell.check_id in drop or cell.object_id in drop:
            continue
        finding = ((cell.check_id, cell.object_id) in findings
                   or cell.canary is fin.CanaryKind.KNOWN_BAD)
        cells.append({
            "check_id": cell.check_id,
            "object_id": cell.object_id,
            "status": "finding" if finding else "clear",
            "message": "read it" if finding else "",
        })
    cells.extend(extra_cells)
    return {
        "plan_digest": matrix.plan_digest if digest is None else digest,
        "pair_count": matrix.pair_count if pair_count is None else pair_count,
        "cells": cells,
    }


class WindowFactory:
    """Builds the real `FinalizationWindow` over injected collaborators.

    `stalled=True` never produces a report and lets the injected sleep
    advance the clock past the span bound, which is how the stall path is
    exercised without sleeping.
    """

    def __init__(self, report=None, *, stalled=False, route="omp",
                 model="opus", session_id="sess-1"):
        self.report = report
        self.stalled = stalled
        self.route = route
        self.model = model
        self.session_id = session_id
        self.launches = 0
        self.calls = []
        self.stalls = []
        self.clock = FakeClock()

    def __call__(self, matrix) -> fw.FinalizationWindow:
        self.matrix = matrix
        return fw.FinalizationWindow(
            config=fw.FinalizationConfig(finalization_timeout_s=10.0,
                                         turn_timeout_s=5.0,
                                         poll_interval_s=1.0),
            launch=self._launch,
            poll_report=self._poll_report,
            record_reviewer_session=lambda session: self.calls.append("record"),
            record_stall=lambda session, signal, elapsed: self.stalls.append(
                (session, signal, elapsed)),
            kill=lambda session: self.calls.append("kill"),
            time_source=self.clock,
            wall_clock=lambda: 1_760_000_000.0,
        )

    def _launch(self):
        self.launches += 1
        self.calls.append("launch")
        return fw.ReviewerSession(route=self.route, model=self.model,
                                  session_id=self.session_id,
                                  harness_owned_group=True)

    def _poll_report(self):
        if self.stalled:
            return None
        return self.report

    def sleep(self, seconds):
        self.clock.advance(4.0)


def make_store(tmp: Path, *, seed=None, verify_keys=None, root=None):
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    data_dir = tmp / "sssf-data"
    data_dir.mkdir(exist_ok=True)
    seed = rc.generate_seed() if seed is None else seed
    keys = [rc.seed_to_public_key(seed)] if verify_keys is None else verify_keys
    store_root = (tmp / "receipts") if root is None else root
    store = fin.ReceiptStore(store_root, repo_paths=(repo,), data_dir=data_dir,
                             verify_keys=keys, signing_seed=seed)
    return store, repo, data_dir, seed


class TheMatrixIsComputedByCode(unittest.TestCase):

    def test_every_applicable_pair_is_present_exactly_once(self):
        matrix = matrix_for()
        pairs = [(c.check_id, c.object_id) for c in matrix.cells]
        self.assertEqual(len(pairs), len(set(pairs)))
        for obj in OBJECTS:
            for check in fin.DEFAULT_RUBRIC.applicable(obj):
                self.assertIn((check.check_id, obj.object_id), pairs)

    def test_a_check_is_not_applied_to_an_inapplicable_object_kind(self):
        matrix = matrix_for()
        for cell in matrix.cells:
            if cell.is_canary:
                continue
            obj = next(o for o in OBJECTS if o.object_id == cell.object_id)
            check = fin.DEFAULT_RUBRIC.check(cell.check_id)
            self.assertIn(obj.kind, check.applies_to)

    def test_two_computations_of_the_same_plan_are_identical(self):
        """Identical coverage for two reviewers is the whole reason the
        matrix is code-computed rather than reviewer-chosen (§6.5)."""
        self.assertEqual(matrix_for().cells, matrix_for().cells)

    def test_the_matrix_carries_exactly_two_canary_cells(self):
        matrix = matrix_for()
        kinds = sorted(c.canary.value for c in matrix.cells if c.is_canary)
        self.assertEqual(kinds, ["KNOWN_BAD", "KNOWN_GOOD"])

    def test_pair_count_includes_the_canaries_the_reviewer_must_answer(self):
        matrix = matrix_for()
        self.assertEqual(matrix.pair_count, len(matrix.cells))
        self.assertEqual(len(matrix.graded_cells), matrix.pair_count - 2)


class VerdictAndSeverityAreUnrepresentable(unittest.TestCase):
    """§6.5: the report schema has no such fields, so under
    `extra=forbid` they are unrepresentable rather than ignored."""

    def test_a_cell_carrying_a_verdict_is_rejected_at_parse(self):
        with self.assertRaises(pydantic.ValidationError):
            fin.ReportCell(check_id="c", object_id="o", status="clear",
                           message="", verdict="PASS")

    def test_a_cell_carrying_a_severity_is_rejected_at_parse(self):
        with self.assertRaises(pydantic.ValidationError):
            fin.ReportCell(check_id="c", object_id="o", status="clear",
                           message="", severity="BLOCKING")

    def test_a_report_carrying_a_verdict_is_rejected_at_parse(self):
        matrix = matrix_for()
        payload = clean_report(matrix)
        payload["verdict"] = "PASS"
        with self.assertRaises(pydantic.ValidationError):
            fin.ReviewerReport.model_validate(payload)

    def test_a_status_outside_clear_or_finding_is_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            fin.ReportCell(check_id="c", object_id="o", status="pass",
                           message="")

    def test_the_detector_finds_no_forbidden_field_in_the_real_schema(self):
        self.assertEqual(fin.find_forbidden_report_fields(fin.ReportCell), [])
        self.assertEqual(fin.find_forbidden_report_fields(fin.ReviewerReport), [])

    def test_the_detector_convicts_a_planted_violation(self):
        """§13.4: a detector returning zero proves nothing until it has
        been shown to return non-zero on a planted violation."""

        class PlantedCell(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            check_id: str
            object_id: str
            status: fin.CellStatus
            message: str
            verdict: str
            severity: str

        found = fin.find_forbidden_report_fields(PlantedCell)
        self.assertEqual(sorted(found), ["severity", "verdict"])

    def test_severity_comes_from_the_rubric_not_the_report(self):
        matrix = matrix_for()
        report = fin.ReviewerReport.model_validate(clean_report(matrix))
        derived = fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC)
        for cell in derived.cells:
            if cell.canary is not None:
                continue
            self.assertEqual(
                cell.severity,
                fin.DEFAULT_RUBRIC.check(cell.check_id).severity)


class TheReportIsVerifiedAgainstTheMatrix(unittest.TestCase):

    def setUp(self):
        self.matrix = matrix_for()

    def _reject(self, payload):
        report = fin.ReviewerReport.model_validate(payload)
        with self.assertRaises(fin.ReportRejected) as caught:
            fin.verify_report(self.matrix, report)
        return caught.exception

    def test_a_clean_report_is_accepted(self):
        report = fin.ReviewerReport.model_validate(clean_report(self.matrix))
        fin.verify_report(self.matrix, report)

    def test_a_digest_echo_mismatch_is_rejected(self):
        rejected = self._reject(clean_report(self.matrix, digest=OTHER_DIGEST))
        self.assertEqual(rejected.reason, fin.RejectionReason.DIGEST_ECHO)

    def test_a_pair_count_mismatch_is_rejected(self):
        rejected = self._reject(clean_report(self.matrix, pair_count=3))
        self.assertEqual(rejected.reason, fin.RejectionReason.PAIR_COUNT)

    def test_a_missing_cell_is_rejected(self):
        payload = clean_report(self.matrix)
        payload["cells"].pop()
        payload["pair_count"] = self.matrix.pair_count
        rejected = self._reject(payload)
        self.assertEqual(rejected.reason, fin.RejectionReason.CELL_SET)

    def test_an_invented_cell_is_rejected(self):
        payload = clean_report(self.matrix)
        payload["cells"].append({"check_id": "invented", "object_id": "plan",
                                 "status": "clear", "message": ""})
        rejected = self._reject(payload)
        self.assertEqual(rejected.reason, fin.RejectionReason.CELL_SET)

    def test_a_duplicated_cell_is_rejected(self):
        payload = clean_report(self.matrix)
        payload["cells"].append(dict(payload["cells"][0]))
        rejected = self._reject(payload)
        self.assertEqual(rejected.reason, fin.RejectionReason.CELL_SET)


class TheCanaryIsAPair(unittest.TestCase):
    """§6.5: an always-clear reviewer passes every plan and an
    always-finding reviewer permanently poisons the plan it was asked
    about. Both are detected before any receipt exists."""

    def setUp(self):
        self.matrix = matrix_for()

    def test_an_always_clear_reviewer_is_convicted(self):
        payload = clean_report(self.matrix)
        for cell in payload["cells"]:
            cell["status"] = "clear"
        report = fin.ReviewerReport.model_validate(payload)
        with self.assertRaises(fin.ReportRejected) as caught:
            fin.verify_report(self.matrix, report)
        self.assertEqual(caught.exception.reason,
                         fin.RejectionReason.CANARY_KNOWN_BAD_CLEARED)

    def test_an_always_finding_reviewer_is_convicted(self):
        payload = clean_report(self.matrix)
        for cell in payload["cells"]:
            cell["status"] = "finding"
        report = fin.ReviewerReport.model_validate(payload)
        with self.assertRaises(fin.ReportRejected) as caught:
            fin.verify_report(self.matrix, report)
        self.assertEqual(caught.exception.reason,
                         fin.RejectionReason.CANARY_KNOWN_GOOD_FLAGGED)

    def test_the_known_bad_canarys_finding_never_reaches_the_verdict(self):
        """Excluded from verdict derivation -- otherwise every plan
        FAILs, since the known-bad cell is always answered `finding`."""
        report = fin.ReviewerReport.model_validate(clean_report(self.matrix))
        derived = fin.derive_verdict(self.matrix, report, fin.DEFAULT_RUBRIC)
        self.assertEqual(derived.verdict, fin.Verdict.PASS)
        canaries = [c for c in derived.cells if c.canary is not None]
        self.assertEqual(len(canaries), 2)


class TheOccupancyGate(unittest.TestCase):
    """§6.5: convicts above threshold, and convicts on a NULL row, so it
    cannot rot back into unread telemetry."""

    def test_a_null_occupancy_convicts(self):
        with self.assertRaises(fin.ReportRejected) as caught:
            fin.check_occupancy(None, threshold=0.8)
        self.assertEqual(caught.exception.reason, fin.RejectionReason.OCCUPANCY)

    def test_an_occupancy_above_threshold_convicts(self):
        with self.assertRaises(fin.ReportRejected):
            fin.check_occupancy(0.95, threshold=0.8)

    def test_an_occupancy_below_threshold_passes(self):
        fin.check_occupancy(0.4, threshold=0.8)


class VerdictDerivation(unittest.TestCase):

    def test_all_clear_is_a_pass(self):
        matrix = matrix_for()
        report = fin.ReviewerReport.model_validate(clean_report(matrix))
        self.assertEqual(
            fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC).verdict,
            fin.Verdict.PASS)

    def test_a_blocking_finding_is_a_fail(self):
        matrix = matrix_for()
        blocking = next(c for c in matrix.graded_cells
                        if fin.DEFAULT_RUBRIC.check(c.check_id).severity
                        is fin.Severity.BLOCKING)
        payload = clean_report(
            matrix, findings=((blocking.check_id, blocking.object_id),))
        report = fin.ReviewerReport.model_validate(payload)
        self.assertEqual(
            fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC).verdict,
            fin.Verdict.FAIL)

    def test_an_advisory_finding_alone_is_still_a_pass(self):
        matrix = matrix_for()
        advisory = next(c for c in matrix.graded_cells
                        if fin.DEFAULT_RUBRIC.check(c.check_id).severity
                        is fin.Severity.ADVISORY)
        payload = clean_report(
            matrix, findings=((advisory.check_id, advisory.object_id),))
        report = fin.ReviewerReport.model_validate(payload)
        derived = fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC)
        self.assertEqual(derived.verdict, fin.Verdict.PASS)


class TheStoreLocationInvariant(unittest.TestCase):
    """§6.6: receipts live outside both the repository and the SSSF data
    directory, with a startup invariant that refuses to run otherwise."""

    def test_a_store_beside_the_repository_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            self.assertTrue(Path(store.root).is_dir())

    def test_a_store_inside_the_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fin.ReceiptStoreLocationError):
                make_store(Path(tmp), root=Path(tmp) / "repo" / "receipts")

    def test_a_store_that_is_the_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fin.ReceiptStoreLocationError):
                make_store(Path(tmp), root=Path(tmp) / "repo")

    def test_a_store_inside_the_data_directory_is_refused(self):
        """Placing them there would grant every agent unconditional write
        authority over receipts (§6.6)."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fin.ReceiptStoreLocationError):
                make_store(Path(tmp), root=Path(tmp) / "sssf-data" / "receipts")


class ReceiptIntegrity(unittest.TestCase):
    """§6.6: a detached Ed25519 signature over the receipt's stored bytes,
    verified before trusting; an invalid or missing signature is a hard
    error, never treated as absent."""

    def _receipt(self, verdict=fin.Verdict.PASS, digest=DIGEST):
        matrix = matrix_for(digest=digest)
        report = fin.ReviewerReport.model_validate(clean_report(matrix))
        derived = fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC)
        return fin.Receipt(
            plan_digest=digest,
            rubric_version=fin.DEFAULT_RUBRIC.version,
            verdict=verdict,
            cells=derived.cells,
            reviewer=fin.ReviewerIdentity(route="omp", model="opus",
                                          session_id="sess-1"),
            created_at_epoch=1_760_000_000.0)

    def test_a_written_receipt_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            written = self._receipt()
            store.write(written)
            self.assertEqual(store.load(DIGEST), written)

    def test_the_signature_is_detached_and_beside_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            path = store.write(self._receipt())
            signature_path = Path(str(path) + ".sig")
            self.assertTrue(signature_path.is_file())
            self.assertTrue(rc.verify(rc.seed_to_public_key(seed),
                                      path.read_bytes(),
                                      bytes.fromhex(
                                          signature_path.read_text().strip())))

    def test_tampered_receipt_bytes_are_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            path = store.write(self._receipt(verdict=fin.Verdict.FAIL))
            payload = json.loads(path.read_text())
            payload["verdict"] = "PASS"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(fin.SignatureInvalid):
                store.load(DIGEST)

    def test_a_missing_signature_is_a_hard_error_not_an_absent_receipt(self):
        """Otherwise a forger could delete signatures to force re-review
        roulette (§6.6)."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            path = store.write(self._receipt())
            Path(str(path) + ".sig").unlink()
            self.assertTrue(store.has(DIGEST))
            with self.assertRaises(fin.SignatureMissing):
                store.load(DIGEST)

    def test_a_receipt_is_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            store.write(self._receipt())
            with self.assertRaises(fin.ReceiptExists):
                store.write(self._receipt())

    def test_rotation_appends_a_key_and_old_receipts_still_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store, repo, data, old_seed = make_store(tmp_path)
            store.write(self._receipt())
            new_seed = rc.generate_seed()
            rotated = fin.ReceiptStore(
                store.root, repo_paths=(repo,), data_dir=data,
                verify_keys=[rc.seed_to_public_key(new_seed),
                             rc.seed_to_public_key(old_seed)],
                signing_seed=new_seed)
            self.assertEqual(rotated.load(DIGEST).plan_digest, DIGEST)

    def test_a_key_that_never_signed_it_does_not_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store, repo, data, _old = make_store(tmp_path)
            store.write(self._receipt())
            stranger = rc.generate_seed()
            other = fin.ReceiptStore(
                store.root, repo_paths=(repo,), data_dir=data,
                verify_keys=[rc.seed_to_public_key(stranger)])
            with self.assertRaises(fin.SignatureInvalid):
                other.load(DIGEST)

    def test_private_key_loss_leaves_every_receipt_verifiable(self):
        """§6.6: the failure where a lost secret made a reviewed package
        unfinalizable has no analogue here."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store, repo, data, seed = make_store(tmp_path)
            store.write(self._receipt())
            keyless = fin.ReceiptStore(
                store.root, repo_paths=(repo,), data_dir=data,
                verify_keys=[rc.seed_to_public_key(seed)])
            self.assertEqual(keyless.load(DIGEST).verdict, fin.Verdict.PASS)
            with self.assertRaises(fin.SigningKeyUnavailable):
                keyless.write(self._receipt(digest=OTHER_DIGEST))

    def test_a_signing_key_must_be_among_the_store_verification_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            data = root / "data"
            repo.mkdir()
            data.mkdir()
            signer = rc.generate_seed()
            stranger = rc.generate_seed()
            with self.assertRaises(fin.SigningKeyUnavailable):
                fin.ReceiptStore(
                    root / "receipts", repo_paths=(repo,), data_dir=data,
                    verify_keys=(rc.seed_to_public_key(stranger),),
                    signing_seed=signer)

    def test_unsafe_receipt_timestamps_never_serialize_or_publish(self):
        for timestamp in (True, "1", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(timestamp=repr(timestamp)[:40]):
                receipt = self._receipt()
                object.__setattr__(receipt, "created_at_epoch", timestamp)
                with self.assertRaises(fin.ReceiptInvalid):
                    receipt.to_bytes()
                with tempfile.TemporaryDirectory() as tmp:
                    store, _repo, _data, _seed = make_store(Path(tmp))
                    with self.assertRaises(fin.ReceiptInvalid):
                        store.write(receipt)
                    self.assertFalse(store.has(DIGEST))

    def test_receipt_parsing_maps_timestamp_overflow_to_receipt_invalid(self):
        payload = json.loads(self._receipt().to_bytes())
        payload["created_at_epoch"] = 10 ** 400
        with self.assertRaises(fin.ReceiptInvalid):
            fin.Receipt.from_bytes(json.dumps(payload).encode("utf-8"))

    def test_the_receipt_persists_the_full_matrix_on_pass_and_on_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            matrix = matrix_for()
            for verdict, digest in ((fin.Verdict.PASS, DIGEST),
                                    (fin.Verdict.FAIL, OTHER_DIGEST)):
                store.write(self._receipt(verdict=verdict, digest=digest))
                loaded = store.load(digest)
                self.assertEqual(len(loaded.cells), matrix.pair_count)
                self.assertEqual(loaded.verdict, verdict)

    def test_the_receipt_records_the_reviewers_route_model_and_session(self):
        """§6.5: independence is an auditable fact after the run rather
        than a promise before it."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            store.write(self._receipt())
            reviewer = store.load(DIGEST).reviewer
            self.assertEqual((reviewer.route, reviewer.model,
                              reviewer.session_id), ("omp", "opus", "sess-1"))

    def test_read_only_store_does_not_create_a_missing_receipt_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            data = root / "data"
            repo.mkdir()
            data.mkdir()
            missing_store = root / "missing-receipts"
            store = fin.ReceiptStore(
                missing_store, repo_paths=(repo,), data_dir=data,
                verify_keys=(rc.seed_to_public_key(rc.generate_seed()),),
                create=False)
            self.assertFalse(missing_store.exists())
            self.assertFalse(store.has(DIGEST))
            with self.assertRaises(FileNotFoundError):
                store.load(DIGEST)
            self.assertFalse(missing_store.exists())

    def test_load_refuses_a_verified_receipt_renamed_for_another_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            source = store.write(self._receipt())
            source.rename(store.path_for(OTHER_DIGEST))
            Path(str(source) + ".sig").rename(
                store.signature_path_for(OTHER_DIGEST))
            with self.assertRaises(fin.ReceiptInvalid):
                store.load(OTHER_DIGEST)

    def test_load_maps_malformed_signature_text_to_signature_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            path = store.write(self._receipt())
            signature = Path(str(path) + ".sig")
            for malformed in (b"\xff", b"not hexadecimal\n"):
                with self.subTest(malformed=malformed):
                    signature.write_bytes(malformed)
                    with self.assertRaises(fin.SignatureInvalid):
                        store.load(DIGEST)

    def test_load_maps_signed_schema_failure_to_receipt_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            data = b'{"not":"a receipt"}'
            store.path_for(DIGEST).write_bytes(data)
            store.signature_path_for(DIGEST).write_text(
                rc.sign(seed, data).hex() + "\n", encoding="ascii")
            with self.assertRaises(fin.ReceiptInvalid):
                store.load(DIGEST)

    def test_matching_orphaned_receipt_is_repaired_by_create_once_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            receipt = self._receipt()
            path = store.path_for(DIGEST)
            data = receipt.to_bytes()
            path.write_bytes(data)
            self.assertFalse(store.signature_path_for(DIGEST).exists())

            self.assertEqual(store.write(receipt), path)
            self.assertTrue(rc.verify(
                rc.seed_to_public_key(seed), path.read_bytes(),
                bytes.fromhex(store.signature_path_for(DIGEST).read_text())))
            self.assertEqual(store.load(DIGEST), receipt)

    def test_concurrent_create_once_write_has_one_authoritative_creator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            receipt = self._receipt()
            start = threading.Barrier(3)
            outcomes = []

            def write_once():
                start.wait()
                try:
                    outcomes.append(store.write(receipt))
                except fin.ReceiptExists:
                    outcomes.append(None)

            first = threading.Thread(target=write_once)
            second = threading.Thread(target=write_once)
            first.start()
            second.start()
            start.wait()
            first.join()
            second.join()

            self.assertEqual(sum(item is not None for item in outcomes), 1)
            self.assertEqual(store.load(DIGEST), receipt)


class FinalizeOrchestration(unittest.TestCase):

    def _finalize(self, store, factory, *, blockers=(), occupancy=0.4,
                  digest=DIGEST, calls=None, clock=lambda: 1_760_000_000.0):
        def validate():
            if calls is not None:
                calls.append("validate")
            return list(blockers)

        def window_factory(matrix):
            if calls is not None:
                calls.append("window")
            return factory(matrix)

        return fin.finalize(
            plan_digest=digest,
            objects=OBJECTS,
            rubric=fin.DEFAULT_RUBRIC,
            store=store,
            validate=validate,
            window_factory=window_factory,
            occupancy_reader=lambda session: occupancy,
            sleep=factory.sleep,
            clock=clock)

    def test_obligations_run_before_any_reviewer_is_launched(self):
        """§6.5's asserted, tested ordering invariant."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            calls = []
            factory = WindowFactory(report=clean_report(matrix_for()))
            # One shared list, so the ordering asserted is a real ordering
            # across validation, window construction, and the launch.
            factory.calls = calls
            self._finalize(store, factory, calls=calls)
            self.assertEqual(calls, ["validate", "window", "launch", "record"])

    def test_an_unsafe_finalization_clock_never_publishes_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            factory = WindowFactory(report=clean_report(matrix_for()))
            with self.assertRaises(fin.ReceiptInvalid):
                self._finalize(store, factory, clock=lambda: float("nan"))
            self.assertFalse(store.has(DIGEST))

    def test_blockers_launch_no_reviewer_and_publish_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            factory = WindowFactory(report=clean_report(matrix_for()))
            blocker = fin.Blocker(pointer="/nodes/0/gate",
                                  code="GATE_NOT_EXECUTABLE",
                                  message="selector collects nothing")
            with self.assertRaises(fin.AuthoringBlocked) as caught:
                self._finalize(store, factory, blockers=[blocker])
            self.assertEqual(caught.exception.blockers, (blocker,))
            self.assertEqual(factory.launches, 0)
            self.assertFalse(store.has(DIGEST))

    def test_a_pass_writes_a_signed_receipt_with_the_full_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            factory = WindowFactory(report=clean_report(matrix_for()))
            outcome = self._finalize(store, factory)
            self.assertEqual(outcome.verdict, fin.Verdict.PASS)
            self.assertFalse(outcome.replayed)
            self.assertTrue(store.has(DIGEST))
            self.assertEqual(len(store.load(DIGEST).cells),
                             matrix_for().pair_count)

    def test_a_blocking_finding_publishes_a_fail_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            matrix = matrix_for()
            blocking = next(c for c in matrix.graded_cells
                            if fin.DEFAULT_RUBRIC.check(c.check_id).severity
                            is fin.Severity.BLOCKING)
            factory = WindowFactory(report=clean_report(
                matrix, findings=((blocking.check_id, blocking.object_id),)))
            outcome = self._finalize(store, factory)
            self.assertEqual(outcome.verdict, fin.Verdict.FAIL)
            self.assertEqual(store.load(DIGEST).verdict, fin.Verdict.FAIL)

    def test_a_replay_short_circuits_with_zero_reviewer_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            first = WindowFactory(report=clean_report(matrix_for()))
            self._finalize(store, first)
            second = WindowFactory(report=clean_report(matrix_for()))
            outcome = self._finalize(store, second)
            self.assertTrue(outcome.replayed)
            self.assertEqual(second.launches, 0)
            self.assertEqual(outcome.verdict, fin.Verdict.PASS)

    def test_interrupted_detached_publication_recovers_as_a_verified_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            original_stage = store._stage

            def interrupt_after_receipt_commit(destination, data):
                original_stage(destination, data)
                if destination == store.path_for(DIGEST):
                    raise OSError("simulated crash after receipt commit")

            store._stage = interrupt_after_receipt_commit
            with self.assertRaises(OSError):
                self._finalize(store, WindowFactory(report=clean_report(matrix_for())))
            store._stage = original_stage

            replay = WindowFactory(report=clean_report(matrix_for()))
            outcome = self._finalize(store, replay)

            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(store.load(DIGEST).plan_digest, DIGEST)

    def test_concurrent_finalizers_replay_the_one_verified_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            start = threading.Barrier(3)
            outcomes = []
            errors = []

            def finalize_once():
                try:
                    start.wait()
                    outcomes.append(self._finalize(
                        store, WindowFactory(report=clean_report(matrix_for()))))
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=finalize_once)
            second = threading.Thread(target=finalize_once)
            first.start()
            second.start()
            start.wait()
            first.join()
            second.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(outcomes), 2)
            self.assertEqual({outcome.receipt for outcome in outcomes},
                             {store.load(DIGEST)})
            self.assertEqual(sum(not outcome.replayed for outcome in outcomes), 1)

    def test_replay_is_keyed_on_the_digest_alone(self):
        """§6.5: keying on routes, policy, or delivery target would mean
        changing a route re-reviews unchanged bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            self._finalize(store, WindowFactory(report=clean_report(matrix_for())))
            rerouted = WindowFactory(report=clean_report(matrix_for()),
                                     route="claude", model="sonnet",
                                     session_id="sess-99")
            outcome = self._finalize(store, rerouted)
            self.assertTrue(outcome.replayed)
            self.assertEqual(rerouted.launches, 0)
            self.assertEqual(outcome.receipt.reviewer.route, "omp")

    def test_a_fail_is_terminal_for_those_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            matrix = matrix_for()
            blocking = next(c for c in matrix.graded_cells
                            if fin.DEFAULT_RUBRIC.check(c.check_id).severity
                            is fin.Severity.BLOCKING)
            failing = clean_report(
                matrix, findings=((blocking.check_id, blocking.object_id),))
            self._finalize(store, WindowFactory(report=failing))
            retry = WindowFactory(report=clean_report(matrix))
            outcome = self._finalize(store, retry)
            self.assertTrue(outcome.replayed)
            self.assertEqual(outcome.verdict, fin.Verdict.FAIL)
            self.assertEqual(retry.launches, 0)

    def test_a_stall_writes_no_receipt_and_leaves_the_digest_reviewable(self):
        """§6.5: `FINALIZATION_STALLED` is deliberately not a receipt --
        FAIL is terminal for the bytes, and a stall is a fact about the
        machine or the route."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            stalling = WindowFactory(stalled=True)
            with self.assertRaises(fin.FinalizationStalled) as caught:
                self._finalize(store, stalling)
            stalled = caught.exception
            self.assertEqual(stalled.route, "omp")
            self.assertEqual(stalled.model, "opus")
            self.assertEqual(stalled.session_id, "sess-1")
            self.assertEqual(stalled.signal,
                             fw.FinalizationSignal.WINDOW_TIMEOUT)
            self.assertFalse(store.has(DIGEST))
            self.assertEqual(len(stalling.stalls), 1)

            rerun = WindowFactory(report=clean_report(matrix_for()))
            outcome = self._finalize(store, rerun)
            self.assertEqual(outcome.verdict, fin.Verdict.PASS)
            self.assertEqual(rerun.launches, 1)

    def test_a_rejected_report_writes_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            fabricated = clean_report(matrix_for(), digest=OTHER_DIGEST)
            factory = WindowFactory(report=fabricated)
            with self.assertRaises(fin.ReportRejected):
                self._finalize(store, factory)
            self.assertFalse(store.has(DIGEST))

    def test_a_null_occupancy_row_rejects_before_the_receipt_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            factory = WindowFactory(report=clean_report(matrix_for()))
            with self.assertRaises(fin.ReportRejected) as caught:
                self._finalize(store, factory, occupancy=None)
            self.assertEqual(caught.exception.reason,
                             fin.RejectionReason.OCCUPANCY)
            self.assertFalse(store.has(DIGEST))

    def test_a_report_with_an_unknown_field_never_reaches_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            payload = clean_report(matrix_for())
            payload["cells"][0]["severity"] = "BLOCKING"
            factory = WindowFactory(report=payload)
            with self.assertRaises(pydantic.ValidationError):
                self._finalize(store, factory)
            self.assertFalse(store.has(DIGEST))



class ALegacyV1ReceiptStillReplays(unittest.TestCase):
    """`reject_at` is required for v2, not for receipts written before it.

    §6.5 keys replay on the digest alone. A signed `maestro-rubric.v1`
    receipt omits `reject_at`; requiring the field for every version
    makes previously finalized identities unusable after the upgrade.
    """

    def _install_signed_v1(self, store, seed, receipt_bytes):
        payload = json.loads(receipt_bytes.decode("utf-8"))
        payload.pop("reject_at", None)
        payload["rubric_version"] = "maestro-rubric.v1"
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        store.path_for(DIGEST).write_bytes(data)
        store.signature_path_for(DIGEST).write_text(
            rc.sign(seed, data).hex() + "\n", encoding="ascii")
        return data

    def test_a_signed_v1_receipt_parses_and_replays_after_the_v2_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            first = WindowFactory(report=clean_report(matrix_for()))
            written = fin.finalize(
                plan_digest=DIGEST, objects=OBJECTS,
                rubric=fin.DEFAULT_RUBRIC, store=store,
                validate=lambda: (), window_factory=first,
                occupancy_reader=lambda session: 0.4, sleep=first.sleep,
                clock=lambda: 1_760_000_000.0)
            self.assertFalse(written.replayed)
            historical = self._install_signed_v1(
                store, seed, store.path_for(DIGEST).read_bytes())

            parsed = fin.Receipt.from_bytes(historical)
            self.assertEqual(parsed.rubric_version, "maestro-rubric.v1")
            self.assertIsNone(parsed.reject_at)
            self.assertEqual(parsed.verdict, fin.Verdict.PASS)
            self.assertEqual(store.load(DIGEST), parsed)

            replay = WindowFactory(report=clean_report(matrix_for()))
            outcome = fin.finalize(
                plan_digest=DIGEST, objects=OBJECTS,
                rubric=fin.DEFAULT_RUBRIC, store=store,
                validate=lambda: (), window_factory=replay,
                occupancy_reader=lambda session: 0.4, sleep=replay.sleep,
                clock=lambda: 1_760_000_000.0)
            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(outcome.receipt.rubric_version,
                             "maestro-rubric.v1")
            self.assertIsNone(outcome.receipt.reject_at)



class APreGradingV2ReceiptStillReplays(unittest.TestCase):
    """`maestro-rubric.v2` names two receipt schemas.

    Graded findings added `reject_at` and `DerivedCell.grade` without
    moving the rubric version. Receipts finalized before that change
    have neither key. Requiring the keys for every v2 receipt makes
    those identities unusable — §6.5 digest-only replay, the same
    breakage as v1, one version later.
    """

    def _install_signed_pre_grading_v2(self, store, seed, receipt_bytes):
        payload = json.loads(receipt_bytes.decode("utf-8"))
        payload.pop("reject_at", None)
        payload["rubric_version"] = "maestro-rubric.v2"
        for cell in payload["cells"]:
            cell.pop("grade", None)
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        store.path_for(DIGEST).write_bytes(data)
        store.signature_path_for(DIGEST).write_text(
            rc.sign(seed, data).hex() + "\n", encoding="ascii")
        return data

    def test_a_signed_pre_grading_v2_receipt_parses_verifies_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            first = WindowFactory(report=clean_report(matrix_for()))
            written = fin.finalize(
                plan_digest=DIGEST, objects=OBJECTS,
                rubric=fin.DEFAULT_RUBRIC, store=store,
                validate=lambda: (), window_factory=first,
                occupancy_reader=lambda session: 0.4, sleep=first.sleep,
                clock=lambda: 1_760_000_000.0)
            self.assertFalse(written.replayed)
            historical = self._install_signed_pre_grading_v2(
                store, seed, store.path_for(DIGEST).read_bytes())

            parsed = fin.Receipt.from_bytes(historical)
            self.assertEqual(parsed.rubric_version, "maestro-rubric.v2")
            self.assertIsNone(parsed.reject_at)
            self.assertTrue(parsed.cells)
            for cell in parsed.cells:
                self.assertIsNone(cell.grade)
            self.assertEqual(parsed.verdict, fin.Verdict.PASS)
            self.assertEqual(store.load(DIGEST), parsed)

            replay = WindowFactory(report=clean_report(matrix_for()))
            outcome = fin.finalize(
                plan_digest=DIGEST, objects=OBJECTS,
                rubric=fin.DEFAULT_RUBRIC, store=store,
                validate=lambda: (), window_factory=replay,
                occupancy_reader=lambda session: 0.4, sleep=replay.sleep,
                clock=lambda: 1_760_000_000.0)
            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(outcome.receipt.rubric_version,
                             "maestro-rubric.v2")
            self.assertIsNone(outcome.receipt.reject_at)
            for cell in outcome.receipt.cells:
                self.assertIsNone(cell.grade)

    def test_a_receipt_written_today_still_requires_reject_at_and_grade(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            factory = WindowFactory(report=clean_report(matrix_for()))
            fin.finalize(
                plan_digest=DIGEST, objects=OBJECTS,
                rubric=fin.DEFAULT_RUBRIC, store=store,
                validate=lambda: (), window_factory=factory,
                occupancy_reader=lambda session: 0.4, sleep=factory.sleep,
                clock=lambda: 1_760_000_000.0)
            payload = json.loads(store.path_for(DIGEST).read_bytes())
            self.assertIn("reject_at", payload)
            self.assertTrue(payload["cells"])
            for cell in payload["cells"]:
                self.assertIn("grade", cell)
            missing_threshold = dict(payload)
            del missing_threshold["reject_at"]
            with self.assertRaises(fin.ReceiptInvalid):
                fin.Receipt.from_bytes(json.dumps(
                    missing_threshold, sort_keys=True,
                    separators=(",", ":")).encode())
            missing_grade = json.loads(json.dumps(payload))
            del missing_grade["cells"][0]["grade"]
            with self.assertRaises(fin.ReceiptInvalid):
                fin.Receipt.from_bytes(json.dumps(
                    missing_grade, sort_keys=True,
                    separators=(",", ":")).encode())



if __name__ == "__main__":
    unittest.main()
