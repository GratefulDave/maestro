"""Observable private-review contracts against scheduler_types.LaneArtifact.

Not run by this Wave A slice. Integration owner runs this file later.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
SECRET_LITERAL = "SECRET_EXPECTED_LITERAL_NEGATIVE_REFUND"
SECRET_SELECTOR = "test_refund_rejects_secret_negative"
SECRET_FIXTURE = "SECRET_FIXTURE"
TEST_PATH = "tests/test_refund_secret.py"
PRODUCT = "def refund(amount):\n    return amount\n"
FIXED = (
    "def refund(amount):\n    if amount < 0:\n        return None\n    return amount\n"
)
TEST_SOURCE = """\
from refund import refund

{fixture} = {{"amount": -1}}


def {selector}():
    assert refund({fixture}["amount"]) is None, "{literal}"
""".format(
    fixture=SECRET_FIXTURE,
    selector=SECRET_SELECTOR,
    literal=SECRET_LITERAL,
)
CONTRACT = {
    "acceptance_criteria": ["negative amounts are refused"],
    "declared_outputs": ["refund.py"],
}
CONSTRAINTS = ("change only declared outputs",)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(
                " ".join(args), result.returncode, result.stderr
            )
        )
    return result.stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-hooks"))
    (repo / "refund.py").write_text(PRODUCT)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _request(*, run_id: str, lane_id: str, input_digest: str) -> pr.VaultLaneRequest:
    return pr.VaultLaneRequest(
        run_id=run_id,
        lane_id=lane_id,
        plan_revision=1,
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        input_digest=input_digest,
    )


def _finding(**overrides: str) -> dict[str, str]:
    row = {
        "violated_requirement": "negative amounts are refused",
        "observed_behavior": "the candidate still accepts the invalid amount",
        "required_behavior": "the candidate must refuse the invalid amount",
        "implementation_area": "refund function",
    }
    row.update(overrides)
    return row


class PrivateReviewContract(unittest.TestCase):
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

    def _draft(self, input_digest: str) -> st.LaneArtifact:
        return tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=input_digest,
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=CONTRACT,
            worktrees_root=self.worktrees / input_digest[:8],
        )

    def _review(
        self,
        draft: st.LaneArtifact,
        input_digest: str,
        verdict: st.ReviewerVerdict,
        findings=(),
    ) -> st.LaneArtifact:
        tokens = tc.draft_private_tokens(
            state_root=self.state, run_id=self.run_id, draft=draft
        )
        return tc.review_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=input_digest,
            ),
            verdict=verdict,
            findings=findings,
            test_draft=draft,
            private_tokens=tokens,
        )

    def _builder(self) -> Path:
        head = _git(self.repo, "rev-parse", "HEAD")
        return hv.linked_worktree(self.repo, self.root / "builder", head)

    def _seal(self, draft, review, builder) -> st.LaneArtifact:
        return tc.seal_accepted_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("seal-" + draft.input_digest),
            ),
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=review,
        )

    def _candidate(self, source: str) -> tuple[str, str]:
        (self.repo / "refund.py").write_text(source)
        status = _git(self.repo, "status", "--porcelain")
        if status:
            _git(self.repo, "add", "refund.py")
            _git(self.repo, "commit", "-qm", "candidate")
        sha = _git(self.repo, "rev-parse", "HEAD")
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("build-" + sha))
        _git(self.repo, "update-ref", ref, sha)
        return sha, ref

    def test_author_reviewer_revise_then_pass_uses_distinct_inputs(self):
        first = self._draft(_digest("draft-1"))
        self.assertIs(first.kind, st.ArtifactKind.TEST_DRAFT)
        self.assertEqual(
            set(first.payload),
            {
                "input_digest",
                "private_draft_digest",
                "private_draft_ref",
                "public_contract",
            },
        )
        revise = self._review(
            first,
            _digest("review-1"),
            st.ReviewerVerdict.REVISE,
            (_finding(observed_behavior=SECRET_LITERAL + " still happens"),),
        )
        self.assertIs(revise.kind, st.ArtifactKind.TEST_REVIEW)
        self.assertIs(revise.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(revise.payload["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertNotIn(SECRET_LITERAL, json.dumps(revise.payload))
        self.assertIn(
            "[redacted]",
            revise.payload["findings"][0]["observed_behavior"],
        )
        second = self._draft(_digest("draft-2"))
        self.assertNotEqual(first.input_digest, second.input_digest)
        self.assertNotEqual(first.artifact_ref, second.artifact_ref)
        self.assertNotEqual(first.output_digest, second.output_digest)
        passed = self._review(second, _digest("review-2"), st.ReviewerVerdict.PASS)
        self.assertIs(passed.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(passed.payload["findings"], [])
        blob = (
            json.dumps(first.payload)
            + json.dumps(second.payload)
            + json.dumps(passed.payload)
        )
        self.assertNotIn(SECRET_LITERAL, blob)
        self.assertNotIn(SECRET_SELECTOR, blob)
        self.assertNotIn(TEST_PATH, blob)

    def test_seal_hides_private_objects_from_run_repo_and_builder(self):
        draft = self._draft(_digest("draft-1"))
        passed = self._review(draft, _digest("review-pass"), st.ReviewerVerdict.PASS)
        builder = self._builder()
        sealed = self._seal(draft, passed, builder)
        self.assertIs(sealed.kind, st.ArtifactKind.SEALED_TEST_BUNDLE)
        commit = hv.rev_parse(
            hv.vault_path(self.state, self.run_id), sealed.artifact_ref
        )
        self.assertTrue(hv.object_is_absent(self.repo, commit))
        self.assertTrue(hv.object_is_absent(builder, commit))
        vault = hv.vault_path(self.state, self.run_id)
        files = tc.sealed_private_files(vault, sealed)
        blob_id = next(iter(files.values()))
        printed = hv.cat_blob(vault, blob_id).decode("utf-8")
        self.assertIn(SECRET_LITERAL, printed)
        public = json.dumps(sealed.payload)
        self.assertNotIn(SECRET_LITERAL, public)
        self.assertNotIn(str(vault.resolve()), public)
        self.assertNotIn("vault_path", public)
        view = cr.builder_view(
            public_contract=sealed.payload["public_contract"],
            architecture_constraints=CONSTRAINTS,
            sealed_digest=sealed.payload["sealed_digest"],
            private_tokens=tc.draft_private_tokens(
                state_root=self.state, run_id=self.run_id, draft=draft
            ),
        )
        view_text = json.dumps(view)
        self.assertEqual(view["sealed_digest"], sealed.payload["sealed_digest"])
        self.assertEqual(view["prior_code_review"], st.NO_CODE_REVIEW)
        self.assertNotIn(SECRET_LITERAL, view_text)
        self.assertNotIn(SECRET_SELECTOR, view_text)
        self.assertNotIn(SECRET_FIXTURE, view_text)
        self.assertNotIn(TEST_PATH, view_text)
        self.assertNotIn(str(vault.resolve()), view_text)
        self.assertNotIn("refs/maestro/sealed/", view_text)
        self.assertNotIn("refs/maestro/drafts/", view_text)

    def test_seal_without_pass_is_refused(self):
        draft = self._draft(_digest("draft-1"))
        revise = self._review(
            draft,
            _digest("review-1"),
            st.ReviewerVerdict.REVISE,
            (_finding(),),
        )
        with self.assertRaises(pr.PrivateReviewError):
            self._seal(draft, revise, self._builder())

    def test_code_review_revise_then_pass_with_real_runner(self):
        draft = self._draft(_digest("draft-1"))
        passed = self._review(draft, _digest("review-pass"), st.ReviewerVerdict.PASS)
        builder = self._builder()
        sealed = self._seal(draft, passed, builder)
        base = _git(self.repo, "rev-parse", "HEAD")
        bad_sha, bad_ref = self._candidate(PRODUCT)
        revise = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("code-review-1"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=bad_sha,
            candidate_ref=bad_ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.REVISE,
            findings=(_finding(),),
            scratch_root=self.root / "scratch-1",
            architecture_constraints=CONSTRAINTS,
        )
        self.assertIs(revise.kind, st.ArtifactKind.CODE_REVIEW)
        self.assertIs(revise.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(revise.payload["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertGreater(revise.payload["public_result_summary"]["failed"], 0)
        public = json.dumps(revise.payload)
        self.assertNotIn(SECRET_LITERAL, public)
        self.assertNotIn(SECRET_SELECTOR, public)
        self.assertNotIn(TEST_PATH, public)
        tokens = tc.draft_private_tokens(
            state_root=self.state, run_id=self.run_id, draft=draft
        )
        view = cr.builder_view(
            public_contract=sealed.payload["public_contract"],
            architecture_constraints=CONSTRAINTS,
            sealed_digest=sealed.payload["sealed_digest"],
            prior_code_review=revise,
            private_tokens=tokens,
        )
        self.assertEqual(
            view["prior_code_review"]["verdict"], st.ReviewerVerdict.REVISE.value
        )
        self.assertNotIn(SECRET_LITERAL, json.dumps(view))
        with self.assertRaises(pr.PrivateReviewError):
            cr.review_builder_output(
                request=_request(
                    run_id=self.run_id,
                    lane_id=self.lane_id,
                    input_digest=_digest("code-review-pass-on-red"),
                ),
                state_root=self.state,
                candidate_repo=self.repo,
                candidate_sha=bad_sha,
                candidate_ref=bad_ref,
                builder_base_sha=base,
                sealed_bundle=sealed,
                verdict=st.ReviewerVerdict.PASS,
                scratch_root=self.root / "scratch-pass-red",
                architecture_constraints=CONSTRAINTS,
            )
        good_sha, good_ref = self._candidate(FIXED)
        accepted = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("code-review-2"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=good_sha,
            candidate_ref=good_ref,
            builder_base_sha=bad_sha,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / "scratch-2",
            architecture_constraints=CONSTRAINTS,
        )
        self.assertIs(accepted.verdict, st.ReviewerVerdict.PASS)
        self.assertGreater(accepted.payload["public_result_summary"]["passed"], 0)
        self.assertEqual(accepted.payload["public_result_summary"]["failed"], 0)
        self.assertNotEqual(revise.input_digest, accepted.input_digest)
        self.assertFalse((self.root / "scratch-2" / ".git").exists())

    def test_same_inputs_yield_identical_canonical_bytes(self):
        first = self._draft(_digest("draft-1"))
        replay = st.LaneArtifact(
            kind=first.kind,
            plan_revision=first.plan_revision,
            spec_digest=first.spec_digest,
            lane_projection_digest=first.lane_projection_digest,
            input_digest=first.input_digest,
            output_digest=first.output_digest,
            artifact_ref=first.artifact_ref,
            payload=first.payload,
        )
        self.assertEqual(replay.output_digest, first.output_digest)
        self.assertEqual(replay.payload["input_digest"], first.input_digest)
        self.assertEqual(
            st.digest_bytes(st.canonical_bytes(first.payload)), first.output_digest
        )

    def test_incomplete_findings_and_rejected_verdict_are_refused(self):
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings(())
        with self.assertRaises(pr.PrivateReviewError):
            pr.actionable_findings("REJECTED", (_finding(),))  # type: ignore[arg-type]
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings(({"violated_requirement": "x"},))
        with self.assertRaises(pr.PrivateReviewError):
            pr.actionable_findings(st.ReviewerVerdict.PASS, (_finding(),))

    def test_owned_producers_do_not_write_stage_or_sqlite(self):
        self._draft(_digest("draft-1"))
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])
        self.assertFalse((self.state / "lifecycle.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
