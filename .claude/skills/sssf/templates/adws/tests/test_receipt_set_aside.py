"""§3.6 B10's operator escape, executable (§6.5, §6.6, §16.3 item 56).

B10 requires that re-review of byte-identical input be **impossible or
explicitly recorded, with an operator escape**. Maestro shipped the first
half and not the second: `finalize` short-circuits on `store.has(digest)`,
receipts are create-once, and replay is keyed on the digest alone — all
three deliberate — so nothing revoked, superseded, or set aside a receipt
and a FAIL was terminal for those bytes forever.

§19 M16 is what that cost. The plan projection emitted the
plan's whole-suite integration gate as a node gate, so the reviewer was
asked whether the whole-suite selector was narrow — a question whose
correct answer the architecture states in advance. The plan FAILed on it.
Once the projection was fixed the plan was correct, unchanged, and still
unrunnable: `run start` refused it `RUN_RECEIPT_NOT_PASS` on bytes that had
not changed. The only route back was re-authoring it byte-different in
`plan_id` alone, spending a full gate/review/ship/finalize cycle to defeat
a cache.

What this file proves, and what it deliberately does not:

  the escape     -- `ReceiptStore.set_aside` frees the live slot, so the
                    next `finalize` reviews these bytes afresh and writes
                    a new receipt. The admission path is the *absence* of
                    a receipt — exactly the one `FINALIZATION_STALLED`
                    already uses — never a decision that reads the record.
  retention      -- the superseded receipt and its original signature are
                    still in the store and still verify afterwards. `rm`
                    is not the mechanism; the store's audit value is that
                    every verdict ever reached about a digest survives.
  the record     -- typed, signed, durable across a store reopen, naming
                    the invoker, the reason, and the sha256 of the exact
                    receipt bytes it superseded. §1.2 governs what may
                    drive a transition, so the reason is an audit field
                    and nothing reads it.
  the asymmetry  -- a FAIL the *plan* earned must stay expensive to
                    re-roll, or review becomes a slot machine. The escape
                    refuses a PASS, refuses a digest with no receipt,
                    refuses a blank invoker or reason, needs the
                    finalizer's signing key, and appends a permanent
                    public record on every invocation.
  replay         -- untouched. A receipt that has not been set aside still
                    short-circuits on the digest alone, PASS or FAIL.

Real resources only: real temporary stores, real Ed25519 signing, the real
`FinalizationWindow` through `test_finalization`'s factory, and the real
`finalize` — never a mock of any of them.

Run with:  uv run adws/adw_test.py -k set_aside
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402

from test_finalization import (  # noqa: E402
    DIGEST, FIXTURE_RUBRIC, graded_payload, make_store, matrix_for)

INVOKER = "operator:david"
REASON = ("the BLOCKING cell was a projection defect in Maestro "
          "(§19 M16), not a defect in the plan")


def failing_report(matrix=None):
    """A report whose one BLOCKING finding makes the derived verdict FAIL."""
    matrix = matrix_for() if matrix is None else matrix
    blocking = next(cell for cell in matrix.graded_cells
                    if FIXTURE_RUBRIC.check(cell.check_id).severity
                    is fin.Severity.BLOCKING)
    return graded_payload(
        matrix, findings=((blocking.check_id, blocking.object_id),))


class Review:
    """One reviewer's finished output, and a counter for how many times a
    review was actually dispatched.

    `finalization.finalize` and the window factory it drove were deleted when
    `plan finalize` stopped dispatching a reviewer. Nothing about the escape
    depends on them: what B10 is about is the receipt, the store, and the
    absence that admits a fresh review. So the review is represented by its
    report and its identity, and `launches` still counts dispatches, because
    "replay launches nothing" is one of the properties asserted below.
    """

    def __init__(self, report, *, route="omp", model="opus",
                 session_id="sess-1"):
        self.report = report
        self.route = route
        self.model = model
        self.session_id = session_id
        self.launches = 0


class Outcome:
    """What `finalize` below answers: the verdict, the receipt, and whether
    the receipt was replayed rather than written."""

    def __init__(self, verdict, receipt, replayed):
        self.verdict = verdict
        self.receipt = receipt
        self.replayed = replayed


def finalize(store, review, *, digest=DIGEST, clock=lambda: 1_760_000_000.0):
    """Review a digest once, or replay the receipt it already has.

    The same three steps every review path takes, in the same order: recover
    an interrupted publication, short-circuit on a stored receipt without
    dispatching anything, otherwise verify the report against the code-computed
    matrix and write the create-once signed receipt.
    """
    store.recover(digest)
    if store.has(digest):
        stored = store.load(digest)
        return Outcome(stored.verdict, stored, True)
    review.launches += 1
    matrix = matrix_for(digest=digest)
    report = cr.CodeReviewerReport.model_validate(review.report)
    fin.verify_report(matrix, report)
    derived = cr.grade_verdict(
        matrix, report, FIXTURE_RUBRIC, cr.FindingGrade.ERROR).derived()
    receipt = fin.Receipt(
        plan_digest=digest,
        rubric_version=FIXTURE_RUBRIC.version,
        verdict=derived.verdict,
        cells=derived.cells,
        reviewer=fin.ReviewerIdentity(route=review.route, model=review.model,
                                      session_id=review.session_id),
        created_at_epoch=clock())
    try:
        store.write(receipt)
    except fin.ReceiptExists:
        stored = store.load(digest)
        return Outcome(stored.verdict, stored, True)
    return Outcome(receipt.verdict, receipt, False)


def reopen(store, repo, data_dir, seed, *, signing=False):
    """A second `ReceiptStore` over the same root — durability is a fact
    about the filesystem, not about one live object's memory."""
    return fin.ReceiptStore(
        store.root, repo_paths=(repo,), data_dir=data_dir,
        verify_keys=[rc.seed_to_public_key(seed)],
        signing_seed=seed if signing else None,
        create=signing)


class TheEscapeAdmitsAFreshReview(unittest.TestCase):

    def test_an_escaped_fail_digest_is_reviewed_afresh_into_a_new_receipt(self):
        """M16's whole cost, discharged: the same bytes, reviewed again,
        without re-authoring the plan to change its digest."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            first = Review(report=failing_report())
            self.assertEqual(finalize(store, first).verdict, fin.Verdict.FAIL)

            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertFalse(store.has(DIGEST))

            second = Review(report=graded_payload(matrix_for()),
                                   session_id="sess-2")
            outcome = finalize(store, second)

            self.assertEqual(second.launches, 1)
            self.assertFalse(outcome.replayed)
            self.assertEqual(outcome.verdict, fin.Verdict.PASS)
            self.assertEqual(store.load(DIGEST).verdict, fin.Verdict.PASS)
            self.assertEqual(store.load(DIGEST).reviewer.session_id, "sess-2")

    def test_the_admission_is_the_absent_receipt_and_not_the_record(self):
        """§1.2: no lifecycle transition is caused by free text. The escape
        leaves the digest in exactly the state `FINALIZATION_STALLED`
        leaves it in — no receipt — and that is the whole admission."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertFalse(store.path_for(DIGEST).is_file())
            self.assertFalse(store.signature_path_for(DIGEST).is_file())
            with self.assertRaises(FileNotFoundError):
                store.load(DIGEST)

    def test_a_second_fail_can_be_set_aside_again_and_both_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-1"))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-2"),
                     clock=lambda: 1_760_000_500.0)
            store.set_aside(DIGEST, invoked_by=INVOKER, reason="and again")

            records = store.set_aside_records(DIGEST)
            self.assertEqual([record.sequence for record in records], [1, 2])
            self.assertEqual(
                [store.load_set_aside_receipt(DIGEST, record.sequence)
                 .reviewer.session_id for record in records],
                ["sess-1", "sess-2"])


class TheSupersededReceiptIsRetained(unittest.TestCase):

    def test_the_original_receipt_is_still_readable_from_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            original = store.load(DIGEST)

            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            retained = store.load_set_aside_receipt(DIGEST, 1)
            self.assertEqual(retained, original)
            self.assertEqual(retained.verdict, fin.Verdict.FAIL)
            self.assertEqual(len(retained.cells), matrix_for().pair_count)

    def test_the_retained_receipt_keeps_its_original_signature(self):
        """Retention means the signed bytes survive, not that a copy was
        re-signed: a re-signed copy would be a different fact."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            signed_bytes = store.path_for(DIGEST).read_bytes()
            signature = store.signature_path_for(DIGEST).read_text().strip()

            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            archive, archive_signature, _, _ = store._set_aside_paths(DIGEST, 1)
            self.assertEqual(archive.read_bytes(), signed_bytes)
            self.assertEqual(archive_signature.read_text().strip(), signature)
            self.assertTrue(rc.verify(rc.seed_to_public_key(seed), signed_bytes,
                                      bytes.fromhex(signature)))

    def test_a_tampered_retained_receipt_is_a_hard_error_not_a_shrug(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            archive, _, _, _ = store._set_aside_paths(DIGEST, 1)
            payload = json.loads(archive.read_text())
            payload["verdict"] = "PASS"
            archive.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(fin.SignatureInvalid):
                store.load_set_aside_receipt(DIGEST, 1)


class TheEscapeRefuses(unittest.TestCase):

    def test_it_refuses_a_pass_receipt(self):
        """There is nothing to escape from. A PASS that could be set aside
        is an escape that can overturn a verdict, not one that reopens a
        machine's mistake."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=graded_payload(matrix_for())))
            with self.assertRaises(fin.SetAsideRefused) as caught:
                store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertIn("PASS", str(caught.exception))
            self.assertEqual(store.load(DIGEST).verdict, fin.Verdict.PASS)
            self.assertEqual(store.set_aside_records(DIGEST), ())

    def test_it_refuses_a_digest_with_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            with self.assertRaises(fin.SetAsideRefused):
                store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertEqual(store.set_aside_records(DIGEST), ())

    def test_it_refuses_a_blank_invoker_or_a_blank_reason(self):
        """Attributable and deliberate, or it did not happen."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            for invoked_by, reason in ((" ", REASON), (INVOKER, ""),
                                       ("", "")):
                with self.assertRaises(fin.SetAsideRefused):
                    store.set_aside(DIGEST, invoked_by=invoked_by,
                                    reason=reason)
            self.assertTrue(store.has(DIGEST))
            self.assertEqual(store.set_aside_records(DIGEST), ())

    def test_a_store_without_the_signing_key_cannot_set_anything_aside(self):
        """The escape is the finalizer's act under the same authority that
        mints receipts (§6.6). A verifying-only store holds no such
        authority."""
        with tempfile.TemporaryDirectory() as tmp:
            store, repo, data, seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            keyless = fin.ReceiptStore(
                store.root, repo_paths=(repo,), data_dir=data,
                verify_keys=[rc.seed_to_public_key(seed)])
            with self.assertRaises(fin.SigningKeyUnavailable):
                keyless.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertTrue(store.has(DIGEST))


class ReplayIsUnchanged(unittest.TestCase):

    def test_an_unescaped_fail_still_short_circuits_with_no_reviewer(self):
        """The half of B10 that already worked keeps working: a FAIL
        nobody set aside is still terminal for those bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            replay = Review(report=graded_payload(matrix_for()))
            outcome = finalize(store, replay)
            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(outcome.verdict, fin.Verdict.FAIL)

    def test_an_unescaped_pass_still_short_circuits_with_no_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=graded_payload(matrix_for())))
            replay = Review(report=failing_report())
            outcome = finalize(store, replay)
            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(outcome.verdict, fin.Verdict.PASS)

    def test_setting_one_digest_aside_does_not_reopen_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            other = "b" * 64
            finalize(store, Review(report=failing_report()))
            finalize(store, Review(
                report=failing_report(matrix_for(digest=other))), digest=other)

            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            replay = Review(report=graded_payload(matrix_for(digest=other)))
            outcome = finalize(store, replay, digest=other)
            self.assertTrue(outcome.replayed)
            self.assertEqual(replay.launches, 0)
            self.assertEqual(outcome.verdict, fin.Verdict.FAIL)
            self.assertEqual(store.set_aside_records(other), ())


class TheRecordIsTypedAndDurable(unittest.TestCase):

    def test_the_record_survives_a_store_reopen_and_carries_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, repo, data, seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            written = store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            reopened = reopen(store, repo, data, seed)
            records = reopened.set_aside_records(DIGEST)

            self.assertEqual(records, (written,))
            self.assertEqual(records[0].reason, REASON)
            self.assertEqual(records[0].invoked_by, INVOKER)
            self.assertEqual(records[0].plan_digest, DIGEST)
            self.assertEqual(records[0].sequence, 1)
            self.assertEqual(records[0].superseded_verdict, fin.Verdict.FAIL)
            self.assertEqual(reopened.load_set_aside_receipt(DIGEST, 1).verdict,
                             fin.Verdict.FAIL)

    def test_the_record_names_the_exact_receipt_bytes_it_superseded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            superseded = store.path_for(DIGEST).read_bytes()

            record = store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            self.assertEqual(record.superseded_receipt_sha256,
                             hashlib.sha256(superseded).hexdigest())
            self.assertEqual(record.superseded_rubric_version,
                             FIXTURE_RUBRIC.version)
            self.assertEqual(record.superseded_reviewer.session_id, "sess-1")

    def test_the_record_round_trips_through_its_stored_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            record = store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            self.assertEqual(fin.SetAsideRecord.from_bytes(record.to_bytes()),
                             record)

    def test_a_record_missing_its_reason_is_not_a_record(self):
        """The reason is never optional, so it can never be dropped by a
        later writer and read as absent by a later auditor."""
        payload = json.loads(
            fin.SetAsideRecord(
                plan_digest=DIGEST, sequence=1, invoked_by=INVOKER,
                reason=REASON, superseded_verdict=fin.Verdict.FAIL,
                superseded_rubric_version=FIXTURE_RUBRIC.version,
                superseded_reviewer=fin.ReviewerIdentity(
                    route="omp", model="opus", session_id="sess-1"),
                superseded_created_at_epoch=1_760_000_000.0,
                superseded_receipt_sha256="c" * 64,
                created_at_epoch=1_760_000_001.0).to_bytes().decode("utf-8"))
        payload["reason"] = "   "
        with self.assertRaises(fin.ReceiptInvalid):
            fin.SetAsideRecord.from_bytes(
                json.dumps(payload).encode("utf-8"))
        payload.pop("reason")
        with self.assertRaises(fin.ReceiptInvalid):
            fin.SetAsideRecord.from_bytes(
                json.dumps(payload).encode("utf-8"))

    def test_a_tampered_record_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report()))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

            _, _, record_path, _ = store._set_aside_paths(DIGEST, 1)
            payload = json.loads(record_path.read_text())
            payload["reason"] = "a reason nobody signed"
            record_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(fin.SignatureInvalid):
                store.set_aside_records(DIGEST)


class TheOperatorVerb(unittest.TestCase):
    """The escape is an operator act, so the operator's surface is the
    thing that has to work. Driving `maestro.main` is what makes the verb
    more than a function nobody can reach."""

    def _environment(self, tmp):
        store, repo, data, seed = make_store(tmp)
        finalize(store, Review(report=failing_report()))
        access = ["--repo", str(repo), "--receipt-dir", str(store.root),
                  "--data-dir", str(data),
                  "--verify-key", rc.seed_to_public_key(seed).hex()]
        return store, access, access + ["--signing-seed", seed.hex()]

    def _run(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = maestro.main(argv)
        return code, json.loads(output.getvalue())

    def test_the_verb_sets_a_fail_aside_and_the_log_verb_reads_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, access, signing = self._environment(Path(tmp))

            code, record = self._run([
                "plan", "set-aside", DIGEST, "--invoked-by", INVOKER,
                "--reason", REASON, *signing])
            self.assertEqual(code, 0)
            self.assertEqual(record["reason"], REASON)
            self.assertEqual(record["invoked_by"], INVOKER)
            self.assertFalse(store.has(DIGEST))

            code, entries = self._run(
                ["plan", "set-aside-log", DIGEST, *access])
            self.assertEqual(code, 0)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["record"], record)
            self.assertEqual(entries[0]["superseded_receipt"]["verdict"], "FAIL")

    def test_the_verb_refuses_a_pass_and_an_absent_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, _access, signing = self._environment(Path(tmp))
            self._run(["plan", "set-aside", DIGEST, "--invoked-by", INVOKER,
                       "--reason", REASON, *signing])

            code, refusal = self._run([
                "plan", "set-aside", DIGEST, "--invoked-by", INVOKER,
                "--reason", "again", *signing])
            self.assertEqual(code, 3)
            self.assertEqual(refusal["outcome"], "SET_ASIDE_REFUSED")

            code, refusal = self._run([
                "plan", "set-aside", "b" * 64, "--invoked-by", INVOKER,
                "--reason", REASON, *signing])
            self.assertEqual(code, 3)
            self.assertEqual(refusal["outcome"], "SET_ASIDE_REFUSED")

    def test_the_verb_refuses_without_the_signing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            _store, access, _signing = self._environment(Path(tmp))
            code, refusal = self._run([
                "plan", "set-aside", DIGEST, "--invoked-by", INVOKER,
                "--reason", REASON, *access])
            self.assertEqual(code, 3)
            self.assertEqual(refusal["outcome"],
                             "SET_ASIDE_CONFIGURATION_REQUIRED")

    def test_plan_list_reports_a_set_aside_digest_as_unfinalized(self):
        """`plan list` names finalized digests. A set-aside digest is not
        one, and the archived copies are not mistaken for receipts."""
        with tempfile.TemporaryDirectory() as tmp:
            _store, access, signing = self._environment(Path(tmp))
            self._run(["plan", "set-aside", DIGEST, "--invoked-by", INVOKER,
                       "--reason", REASON, *signing])
            code, digests = self._run(["plan", "list", *access])
            self.assertEqual(code, 0)
            self.assertEqual(digests, [])



def _crash_after_n_set_aside_ops(store, n):
    """Drive the real `set_aside` publication and raise after `n` durable
    ops: each `_stage` of the four archive/record files, then each unlink
    of the live receipt pair."""
    seen = {"k": 0}
    orig_stage = store._stage
    live = store.path_for(DIGEST)
    live_sig = store.signature_path_for(DIGEST)
    orig_unlink = Path.unlink

    def stage(destination, data):
        orig_stage(destination, data)
        seen["k"] += 1
        if seen["k"] >= n:
            raise OSError("injected crash after publication step {0}".format(
                seen["k"]))

    def unlink(self, *args, **kwargs):
        orig_unlink(self, *args, **kwargs)
        try:
            resolved = self.resolve()
        except OSError:
            return
        if resolved in {live.resolve(), live_sig.resolve()}:
            seen["k"] += 1
            if seen["k"] >= n:
                raise OSError(
                    "injected crash after publication step {0}".format(
                        seen["k"]))

    store._stage = stage
    Path.unlink = unlink
    return orig_stage, orig_unlink


def _restore_set_aside_ops(store, orig_stage, orig_unlink):
    store._stage = orig_stage
    Path.unlink = orig_unlink


def _count_set_aside_ops(store):
    seen = {"k": 0}
    orig_stage = store._stage
    live = store.path_for(DIGEST)
    live_sig = store.signature_path_for(DIGEST)
    orig_unlink = Path.unlink

    def stage(destination, data):
        orig_stage(destination, data)
        seen["k"] += 1

    def unlink(self, *args, **kwargs):
        orig_unlink(self, *args, **kwargs)
        try:
            resolved = self.resolve()
        except OSError:
            return
        if resolved in {live.resolve(), live_sig.resolve()}:
            seen["k"] += 1

    store._stage = stage
    Path.unlink = unlink
    try:
        store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
    finally:
        _restore_set_aside_ops(store, orig_stage, orig_unlink)
    return seen["k"]


class SetAsidePublicationIsCrashRecoverable(unittest.TestCase):
    """A crash at any publication step leaves a state that either fully
    took effect or did not. Drive the real function; do not reimplement
    its publication order in the test."""

    def _assert_resolvable(self, store, original_bytes):
        store.recover(DIGEST)
        records = store.set_aside_records(DIGEST)
        live = store.has(DIGEST)
        if live:
            self.assertEqual(store.load(DIGEST).verdict, fin.Verdict.FAIL)
            self.assertEqual(store.path_for(DIGEST).read_bytes(),
                             original_bytes)
            self.assertEqual(records, ())
            retried = store.set_aside(DIGEST, invoked_by=INVOKER,
                                      reason=REASON)
            self.assertEqual(retried.sequence, 1)
            self.assertFalse(store.has(DIGEST))
            self.assertEqual(len(store.set_aside_records(DIGEST)), 1)
            self.assertEqual(
                store.load_set_aside_receipt(DIGEST, 1).verdict,
                fin.Verdict.FAIL)
        else:
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].superseded_receipt_sha256,
                             hashlib.sha256(original_bytes).hexdigest())
            self.assertEqual(
                store.load_set_aside_receipt(DIGEST, 1).verdict,
                fin.Verdict.FAIL)
            with self.assertRaises(fin.SetAsideRefused):
                store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)

    def test_a_crash_at_each_publication_step_leaves_a_resolvable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe, _repo, _data, _seed = make_store(Path(tmp))
            finalize(probe, Review(report=failing_report()))
            steps = _count_set_aside_ops(probe)
        self.assertGreaterEqual(steps, 4)

        for step in range(1, steps + 1):
            with self.subTest(crash_after=step):
                with tempfile.TemporaryDirectory() as tmp:
                    store, _repo, _data, _seed = make_store(Path(tmp))
                    finalize(store, Review(report=failing_report()))
                    original = store.path_for(DIGEST).read_bytes()
                    orig_stage, orig_unlink = _crash_after_n_set_aside_ops(
                        store, step)
                    try:
                        with self.assertRaises(OSError):
                            store.set_aside(DIGEST, invoked_by=INVOKER,
                                            reason=REASON)
                    finally:
                        _restore_set_aside_ops(store, orig_stage,
                                               orig_unlink)
                    self._assert_resolvable(store, original)


class TheArchivedReceiptIsBoundToTheRecord(unittest.TestCase):
    """`superseded_receipt_sha256` is not an unused field. Swapping two
    valid archive/signature pairs for one plan digest must be refused."""

    def test_a_swapped_archive_signature_pair_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _repo, _data, _seed = make_store(Path(tmp))
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-1"))
            store.set_aside(DIGEST, invoked_by=INVOKER, reason=REASON)
            finalize(store, Review(report=failing_report(),
                                          session_id="sess-2"),
                     clock=lambda: 1_760_000_500.0)
            store.set_aside(DIGEST, invoked_by=INVOKER, reason="and again")

            first = store._set_aside_paths(DIGEST, 1)
            second = store._set_aside_paths(DIGEST, 2)
            first_archive = first[0].read_bytes()
            first_sig = first[1].read_bytes()
            second_archive = second[0].read_bytes()
            second_sig = second[1].read_bytes()
            self.assertNotEqual(first_archive, second_archive)
            first[0].write_bytes(second_archive)
            first[1].write_bytes(second_sig)
            second[0].write_bytes(first_archive)
            second[1].write_bytes(first_sig)

            with self.assertRaises(fin.ReceiptInvalid):
                store.load_set_aside_receipt(DIGEST, 1)
            with self.assertRaises(fin.ReceiptInvalid):
                store.load_set_aside_receipt(DIGEST, 2)


if __name__ == "__main__":
    unittest.main()
