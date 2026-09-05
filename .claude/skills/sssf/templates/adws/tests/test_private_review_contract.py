"""Observable private-review contracts against scheduler_types.LaneArtifact.

Not run by this Wave A slice. Integration owner runs this file later.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
    "declared_outputs": [TEST_PATH],
}
# Boilerplate a tester writes beside its cases and a repository commonly
# already holds somewhere else. Nothing about it is secret; git still gives
# it one blob id wherever it appears, which is what made it read as a leak.
SHIM_PATH = "tests/store_boundary/conftest.py"
BASE_SHIM_PATH = "tests/sections/conftest.py"
SHIM_SOURCE = (
    "import sys\n"
    "from pathlib import Path\n"
    "\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
)
EMPTY_PATH = "tests/store_boundary/__init__.py"
BASE_EMPTY_PATH = "tests/sections/__init__.py"
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
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

    def test_draft_allows_declared_private_test_path_in_public_contract(self):
        contract = {
            "acceptance_criteria": ["negative amounts are refused"],
            "declared_outputs": [TEST_PATH],
        }
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("declared-test-output"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=contract,
            worktrees_root=self.worktrees / "declared-test-output",
        )

        self.assertEqual(draft.payload["public_contract"], contract)
        self.assertNotIn(SECRET_LITERAL, json.dumps(draft.payload))

    def test_draft_allows_public_contract_phrases_in_private_source(self):
        source = """\
def test_public_contract_terms():
    assert "FAQ block"
    assert "indexation threshold"
"""
        contract = {
            "acceptance_criteria": [
                "Nine cases cover the FAQ block and the indexation threshold."
            ],
            "declared_outputs": [TEST_PATH],
        }

        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("public-contract-phrases"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: source},
            public_contract=contract,
            worktrees_root=self.worktrees / "public-contract-phrases",
        )

        self.assertEqual(draft.payload["public_contract"], contract)

    def test_draft_allows_json_key_substring_quoted_in_private_source(self):
        source = '''\
def test_producer_artifact_pin():
    assert contract["producer"]["artifact"] == pinned["path"]
'''
        contract = {
            "acceptance_criteria": ["producer pin stays on the declared path"],
            "declared_outputs": [TEST_PATH],
        }
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("json-key-substring"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: source},
            public_contract=contract,
            worktrees_root=self.worktrees / "json-key-substring",
        )
        self.assertEqual(draft.payload["public_contract"], contract)

    def test_refuse_private_leak_still_catches_secret_in_json_value(self):
        with self.assertRaises(pr.PrivateLeakError):
            pr.refuse_private_leak(
                {"input_artifact_ids": [], "note": SECRET_LITERAL},
                (SECRET_LITERAL,),
            )

    def test_draft_commit_excludes_agent_runtime_files(self):
        original_write_files = pr.write_files

        def write_files_with_runtime_state(dest, files):
            written = original_write_files(dest, files)
            runtime_file = Path(dest) / ".omc" / "state" / "session.json"
            runtime_file.parent.mkdir(parents=True)
            runtime_file.write_text('{"private":"agent state"}', encoding="utf-8")
            return written

        with mock.patch.object(
            pr, "write_files", side_effect=write_files_with_runtime_state
        ):
            draft = self._draft(_digest("runtime-state-exclusion"))

        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        committed = {path for path, _blob in hv.list_commit_blobs(vault, commit)}
        self.assertIn(TEST_PATH, committed)
        self.assertNotIn(".omc/state/session.json", committed)

    def test_commit_all_paths_excludes_prestaged_files(self):
        vault = hv.ensure_vault(self.state, self.run_id)
        base = hv.seed(vault, self.repo, INTEGRATION_REF)
        dest = self.worktrees / "commit-pathspec"
        hv.checkout_vault_worktree(vault, base, dest)
        (dest / "keep.py").write_text("keep\n", encoding="utf-8")
        (dest / "extra.py").write_text("extra\n", encoding="utf-8")
        _git(dest, "add", "extra.py")
        sha = hv.commit_all(dest, "only keep", paths=["keep.py"])
        committed = {path for path, _blob in hv.list_commit_blobs(vault, sha)}
        self.assertIn("keep.py", committed)
        self.assertNotIn("extra.py", committed)

    def test_draft_allows_private_files_independent_of_declared_outputs(self):
        product = {
            "acceptance_criteria": ["negative amounts are refused"],
            "declared_outputs": ["refund.py"],
        }
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("independent-envelope"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE, "tests/meta_hidden.py": "assert True\n"},
            public_contract=product,
            worktrees_root=self.worktrees / "independent-envelope",
        )
        self.assertEqual(
            draft.payload["private_manifest_schema"], tc.PRIVATE_MANIFEST_SCHEMA
        )
        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        selected = {
            path
            for path, _blob in tc._select_private_blobs(vault, commit, draft.payload)
        }
        self.assertEqual(selected, {TEST_PATH, "tests/meta_hidden.py"})
        self.assertNotIn("refund.py", selected)


    def test_draft_refuses_empty_envelope(self):
        with self.assertRaises(pr.PrivateReviewError) as raised:
            tc.write_test_draft(
                request=_request(
                    run_id=self.run_id,
                    lane_id=self.lane_id,
                    input_digest=_digest("empty-envelope"),
                ),
                state_root=self.state,
                run_repo=self.repo,
                integration_ref=INTEGRATION_REF,
                files={},
                public_contract=CONTRACT,
                worktrees_root=self.worktrees / "empty-envelope",
            )
        self.assertIn("empty", str(raised.exception))

    def test_author_reviewer_revise_then_pass_uses_distinct_inputs(self):
        first = self._draft(_digest("draft-1"))
        self.assertIs(first.kind, st.ArtifactKind.TEST_DRAFT)
        self.assertEqual(
            set(first.payload),
            {
                "input_artifact_ids",
                "input_digest",
                "private_draft_digest",
                "private_draft_ref",
                "private_manifest_digest",
                "private_manifest_schema",
                "public_contract",
            },
        )
        self.assertEqual(
            first.payload["private_manifest_schema"], tc.PRIVATE_MANIFEST_SCHEMA
        )
        self.assertEqual(
            first.payload["private_manifest_digest"], first.payload["private_draft_digest"]
        )
        self.assertEqual(first.payload["input_artifact_ids"], [])
        self.assertEqual(first.payload["input_digest"], first.input_digest)
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
        self.assertNotIn(TEST_PATH, json.dumps(revise.payload) + json.dumps(passed.payload))
        self.assertIn(TEST_PATH, first.payload["public_contract"]["declared_outputs"])

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
        self.assertIn(TEST_PATH, view_text)
        self.assertNotIn(str(vault.resolve()), view_text)
        self.assertNotIn("refs/maestro/sealed/", view_text)
        self.assertNotIn("refs/maestro/drafts/", view_text)

    def _draft_with(self, input_digest, files, contract) -> st.LaneArtifact:
        return tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=input_digest,
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files=files,
            public_contract=contract,
            worktrees_root=self.worktrees / input_digest[:8],
        )

    def _seal_on_base(self, draft, review, base: str) -> st.LaneArtifact:
        """Seal the way the scheduler does: no builder worktree, base named."""
        return tc.seal_accepted_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("seal-" + draft.input_digest),
            ),
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=None,
            test_draft=draft,
            test_review=review,
            integration_initial_sha=base,
        )

    def _commit_repo_file(self, path: str, body: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "add {0}".format(path))
        return _git(self.repo, "rev-parse", "HEAD")

    def test_seal_allows_private_bytes_the_base_commit_already_holds(self):
        base = self._commit_repo_file(BASE_SHIM_PATH, SHIM_SOURCE)
        contract = {
            "acceptance_criteria": CONTRACT["acceptance_criteria"],
            "declared_outputs": [TEST_PATH, SHIM_PATH],
        }
        draft = self._draft_with(
            _digest("collision-draft"),
            {TEST_PATH: TEST_SOURCE, SHIM_PATH: SHIM_SOURCE},
            contract,
        )
        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        shared = hv.blob_id_in(vault, commit, SHIM_PATH)
        self.assertEqual(shared, hv.blob_id_in(self.repo, base, BASE_SHIM_PATH))
        self.assertFalse(hv.object_is_absent(self.repo, shared))
        passed = self._review(
            draft, _digest("collision-review"), st.ReviewerVerdict.PASS
        )

        sealed = self._seal_on_base(draft, passed, base)

        self.assertIs(sealed.kind, st.ArtifactKind.SEALED_TEST_BUNDLE)
        cases = tc.sealed_private_files(vault, sealed)
        self.assertIn(TEST_PATH, cases)
        self.assertTrue(hv.object_is_absent(self.repo, cases[TEST_PATH]))

    def test_seal_allows_an_empty_private_file_the_base_already_holds(self):
        base = self._commit_repo_file(BASE_EMPTY_PATH, "")
        contract = {
            "acceptance_criteria": CONTRACT["acceptance_criteria"],
            "declared_outputs": [TEST_PATH, EMPTY_PATH],
        }
        draft = self._draft_with(
            _digest("empty-draft"),
            {TEST_PATH: TEST_SOURCE, EMPTY_PATH: ""},
            contract,
        )
        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        self.assertEqual(hv.blob_id_in(vault, commit, EMPTY_PATH), EMPTY_BLOB)
        self.assertFalse(hv.object_is_absent(self.repo, EMPTY_BLOB))
        passed = self._review(draft, _digest("empty-review"), st.ReviewerVerdict.PASS)

        sealed = self._seal_on_base(draft, passed, base)

        self.assertIs(sealed.kind, st.ArtifactKind.SEALED_TEST_BUNDLE)
        cases = tc.sealed_private_files(vault, sealed)
        self.assertTrue(hv.object_is_absent(self.repo, cases[TEST_PATH]))

    def test_seal_refuses_private_bytes_the_base_commit_does_not_reach(self):
        base = _git(self.repo, "rev-parse", "HEAD")
        draft = self._draft(_digest("escape-draft"))
        passed = self._review(draft, _digest("escape-review"), st.ReviewerVerdict.PASS)
        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        leaked = hv.blob_id_in(vault, commit, TEST_PATH)
        # The accepted case reaches the product repository after the run was
        # created. That is the escape the check exists to catch, and widening
        # the allowance to the base commit must leave it caught.
        self._commit_repo_file(TEST_PATH, TEST_SOURCE)
        self.assertFalse(hv.object_is_absent(self.repo, leaked))

        with self.assertRaises(hv.VaultError) as caught:
            self._seal_on_base(draft, passed, base)

        self.assertIn(leaked, str(caught.exception))

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

    def test_seal_allows_declared_private_test_path_in_public_contract(self):
        draft = self._draft(_digest("seal-declared-path"))
        passed = self._review(
            draft, _digest("seal-declared-review"), st.ReviewerVerdict.PASS
        )
        sealed = self._seal(draft, passed, self._builder())
        public = json.dumps(sealed.payload)
        self.assertEqual(
            sealed.payload["public_contract"]["declared_outputs"], [TEST_PATH]
        )
        self.assertIn(Path(TEST_PATH).name, public)
        self.assertNotIn(SECRET_LITERAL, public)
        self.assertNotIn(SECRET_SELECTOR, public)

    def test_polluted_draft_seals_and_runs_only_declared_outputs(self):
        digest = _digest("polluted-draft")
        request = _request(
            run_id=self.run_id, lane_id=self.lane_id, input_digest=digest
        )
        contract = pr.public_contract(
            acceptance_criteria=CONTRACT["acceptance_criteria"],
            declared_outputs=[TEST_PATH],
        )
        vault = hv.ensure_vault(self.state, self.run_id)
        base = hv.seed(vault, self.repo, INTEGRATION_REF)
        dest = self.worktrees / "polluted"
        hv.checkout_vault_worktree(vault, base, dest)
        pr.write_files(dest, {TEST_PATH: TEST_SOURCE})
        omc = dest / ".omc" / "state.json"
        omc.parent.mkdir(parents=True)
        omc.write_text('{"agent":"state"}', encoding="utf-8")
        (dest / ".cbmignore").write_text("*\n", encoding="utf-8")
        (dest / "bun.lock").write_text("{}\n", encoding="utf-8")
        commit = hv.commit_all(dest, "polluted draft")
        ref = hv.draft_ref(request.run_id, request.lane_id, request.input_digest)
        hv.update_immutable_ref(vault, ref, commit)
        committed = {path for path, _blob in hv.list_commit_blobs(vault, commit)}
        self.assertIn(TEST_PATH, committed)
        self.assertIn(".omc/state.json", committed)
        self.assertIn(".cbmignore", committed)
        self.assertIn("bun.lock", committed)
        extra_blobs = [
            blob
            for path, blob in hv.list_commit_blobs(vault, commit)
            if path != TEST_PATH
            and dict(hv.list_commit_blobs(vault, base)).get(path) != blob
        ]
        draft = pr.make_lane_artifact(
            kind=st.ArtifactKind.TEST_DRAFT,
            request=request,
            payload={
                "input_artifact_ids": [],
                "input_digest": digest,
                "private_draft_digest": "ab" * 32,
                "private_draft_ref": ref,
                "public_contract": contract,
            },
            artifact_ref=ref,
        )
        passed = self._review(
            draft, _digest("polluted-review"), st.ReviewerVerdict.PASS
        )
        sealed = self._seal(draft, passed, self._builder())
        files = tc.sealed_private_files(vault, sealed)
        self.assertEqual(set(files), {TEST_PATH})
        public = json.dumps(sealed.payload)
        for blob in extra_blobs:
            self.assertNotIn(blob, public)
        tree = self.root / "polluted-run"
        tree.mkdir()
        (tree / "refund.py").write_text(FIXED)
        hv.copy_blobs_to_tree(vault, tree, files)
        self.assertTrue((tree / TEST_PATH).is_file())
        self.assertFalse((tree / ".omc").exists())
        self.assertFalse((tree / ".cbmignore").exists())
        self.assertFalse((tree / "bun.lock").exists())
        self.assertEqual(tuple(files), (TEST_PATH,))

    def test_seal_retry_with_existing_same_target_ref_is_idempotent(self):
        draft = self._draft(_digest("seal-retry-draft"))
        passed = self._review(
            draft, _digest("seal-retry-review"), st.ReviewerVerdict.PASS
        )
        builder = self._builder()
        request = _request(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest("seal-" + draft.input_digest),
        )
        vault = hv.vault_path(self.state, self.run_id)
        commit = hv.rev_parse(vault, draft.artifact_ref)
        sealed_name = hv.sealed_ref(
            request.run_id, request.lane_id, draft.payload["private_draft_digest"]
        )
        hv.update_immutable_ref(vault, sealed_name, commit)
        first = tc.seal_accepted_tests(
            request=request,
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=passed,
        )
        second = tc.seal_accepted_tests(
            request=request,
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=passed,
        )
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(first.artifact_ref, sealed_name)
        self.assertEqual(hv.rev_parse(vault, sealed_name), commit)

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
        demoted = cr.review_builder_output(
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
        self.assertIs(demoted.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(demoted.payload["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertGreater(demoted.payload["public_result_summary"]["failed"], 0)
        self.assertEqual(
            set(demoted.payload["findings"][0]),
            set(st.REVISE_FINDING_KEYS),
        )
        self.assertNotIn(SECRET_LITERAL, json.dumps(demoted.payload))
        self.assertNotEqual(demoted.payload["verdict"], st.ReviewerVerdict.PASS.value)
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


    def test_a_reviewer_revise_stands_over_a_green_suite(self):
        """A REVISE is a verdict, and a passing suite does not overturn it.

        The coercion in the other direction stays: a PASS on a failing suite is
        demoted to REVISE by the case above, because the reviewer voted against
        a measurement it could have read. This direction is not symmetric. The
        sealed suite measures the cases it contains; a reviewer reads the code
        against the plan, and almost everything it can see -- a field left
        optional against a contract that requires it, a handler that never
        reads the error it catches, an inverted config precedence -- is
        something no green suite contradicts.

        Coercing those to PASS published wrong code, which is not recoverable.
        Letting a REVISE stand can cost a round when a reviewer asks for a
        change the sealed suite refuses, and the suite refuses it: the next
        candidate measures red and the finding is answered by machinery that
        already exists. A recoverable cost, in place of an unrecoverable one.
        """
        draft = self._draft(_digest("revise-stands-draft"))
        passed = self._review(
            draft, _digest("revise-stands-review"), st.ReviewerVerdict.PASS
        )
        builder = self._builder()
        sealed = self._seal(draft, passed, builder)
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        located = {
            "implementation_area": "refund.py",
            "observed_behavior": "the guard returns None rather than the amount",
            "required_behavior": "return the amount unchanged for every input",
            "violated_requirement": "negative amounts are refused",
        }

        artifact = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("revise-stands-code-review"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=good_sha,
            candidate_ref=good_ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.REVISE,
            findings=(located,),
            scratch_root=self.root / "scratch-revise-stands",
            architecture_constraints=CONSTRAINTS,
        )

        # The suite really did pass. Without that this proves nothing.
        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertEqual(artifact.payload["public_result_summary"]["errored"], 0)
        self.assertGreater(artifact.payload["public_result_summary"]["passed"], 0)

        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(artifact.payload["verdict"], st.ReviewerVerdict.REVISE.value)
        # The finding is actionable: it reaches the builder and sends it back.
        self.assertEqual(len(artifact.payload["findings"]), 1)
        self.assertEqual(
            set(artifact.payload["findings"][0]),
            set(st.REVISE_FINDING_KEYS),
        )
        self.assertNotIn("advisory_findings", artifact.payload)
        public = json.dumps(artifact.payload)
        self.assertNotIn(SECRET_LITERAL, public)
        self.assertNotIn(SECRET_SELECTOR, public)
        self.assertNotIn(TEST_PATH, public)

    def test_a_red_suite_still_keeps_the_reviewer_findings_actionable(self):
        """A failing suite sends the reviewer's located finding back unchanged.

        The runner-failed branch rewrites a PASS and substitutes a finding when
        the reviewer offered none. It must not touch a REVISE that arrived with
        one.
        """
        draft = self._draft(_digest("red-keeps-draft"))
        passed = self._review(
            draft, _digest("red-keeps-review"), st.ReviewerVerdict.PASS
        )
        sealed = self._seal(draft, passed, self._builder())
        base = _git(self.repo, "rev-parse", "HEAD")
        bad_sha, bad_ref = self._candidate(PRODUCT)
        located = {
            "implementation_area": "refund.py",
            "observed_behavior": "negative amounts are returned unchanged",
            "required_behavior": "return None below zero",
            "violated_requirement": "negative amounts are refused",
        }

        artifact = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("red-keeps-code-review"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=bad_sha,
            candidate_ref=bad_ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.REVISE,
            findings=(located,),
            scratch_root=self.root / "scratch-red-keeps",
            architecture_constraints=CONSTRAINTS,
        )

        self.assertGreater(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(len(artifact.payload["findings"]), 1)

    def test_code_review_refuses_sealed_path_colliding_with_candidate(self):
        product_path = "refund.py"
        contract = {
            "acceptance_criteria": ["negative amounts are refused"],
            "declared_outputs": [TEST_PATH, product_path],
        }
        digest = _digest("collide-draft")
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=digest,
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE, product_path: FIXED},
            public_contract=contract,
            worktrees_root=self.worktrees / "collide-draft",
        )
        passed = self._review(
            draft, _digest("collide-review"), st.ReviewerVerdict.PASS
        )
        sealed = self._seal(draft, passed, self._builder())
        vault = hv.vault_path(self.state, self.run_id)
        files = tc.sealed_private_files(vault, sealed)
        self.assertEqual(set(files), {TEST_PATH, product_path})
        base = _git(self.repo, "rev-parse", "HEAD")
        bad_sha, bad_ref = self._candidate(PRODUCT)
        review_digest = _digest("collide-code-review")
        scratch = self.root / "scratch-collide"
        with mock.patch.object(tc, "run_private_suite") as runner:
            with self.assertRaises(pr.PrivatePathCollisionError) as ctx:
                cr.review_builder_output(
                    request=_request(
                        run_id=self.run_id,
                        lane_id=self.lane_id,
                        input_digest=review_digest,
                    ),
                    state_root=self.state,
                    candidate_repo=self.repo,
                    candidate_sha=bad_sha,
                    candidate_ref=bad_ref,
                    builder_base_sha=base,
                    sealed_bundle=sealed,
                    verdict=st.ReviewerVerdict.PASS,
                    scratch_root=scratch,
                    architecture_constraints=CONSTRAINTS,
                )
            runner.assert_not_called()
        self.assertIsInstance(ctx.exception, pr.IsolationError)
        self.assertEqual(ctx.exception.code, "PRIVATE_PATH_COLLISION")
        self.assertEqual(ctx.exception.path, product_path)
        message = str(ctx.exception)
        self.assertIn("collides with candidate", message)
        self.assertIn(product_path, message)
        self.assertNotIn(SECRET_LITERAL, message)
        self.assertNotIn(FIXED, message)
        dest = scratch / "review-{0}-{1}".format(self.lane_id, review_digest[:12])
        self.assertEqual((dest / product_path).read_text(), PRODUCT)
        self.assertFalse((dest / TEST_PATH).exists())


    def _review_call(self, sealed, review_digest, good_sha, good_ref, base, scratch_name):
        return cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=review_digest,
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=good_sha,
            candidate_ref=good_ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / scratch_name,
            architecture_constraints=CONSTRAINTS,
        )

    def test_a_review_that_raises_leaves_no_ref_behind(self):
        """A review that never became an artifact pins nothing.

        The ref is pinned after every check, so a refusal on the way leaves
        the vault as it found it. Whether the retry can then record is not
        this test's question: the ref is keyed on the result bytes, so a
        retry records regardless (`test_the_same_input_can_be_reviewed_twice`).
        """
        draft = self._draft(_digest("retry-draft"))
        passed = self._review(draft, _digest("retry-review"), st.ReviewerVerdict.PASS)
        sealed = self._seal(draft, passed, self._builder())
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        review_digest = _digest("retry-code-review")
        vault = hv.vault_path(self.state, self.run_id)
        prefix = "refs/maestro/private-results/{0}/{1}/".format(
            self.run_id, self.lane_id
        )

        boom = RuntimeError("the view blew up after the suite ran")
        with mock.patch.object(cr, "builder_view", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self._review_call(
                    sealed, review_digest, good_sha, good_ref, base, "scratch-retry-1"
                )

        # Nothing durable survives a review that never produced an artifact.
        listed = _git(vault, "for-each-ref", "--format=%(refname)", prefix)
        self.assertEqual(listed, "")

        # The retry re-runs the suite, so its stdout may differ in the
        # duration alone -- and it must still be able to pin.
        accepted = self._review_call(
            sealed, review_digest, good_sha, good_ref, base, "scratch-retry-2"
        )
        self.assertIs(accepted.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(
            accepted.artifact_ref,
            hv.private_results_ref(
                self.run_id, self.lane_id, accepted.payload["private_results_digest"]
            ),
        )
        self.assertTrue(hv.rev_parse(vault, accepted.artifact_ref))

    def test_the_same_input_can_be_reviewed_twice(self):
        """The ref is keyed on what it pins, so re-review cannot collide.

        Two reviews of one input are two observations. Each names its ref by
        the digest of its own result bytes: identical bytes reach the identical
        ref (pinning is idempotent), different bytes reach a different ref. A
        second `review_builder_output` over the same input therefore completes
        and records, with no guard and no mutable ref.
        """
        draft = self._draft(_digest("twice-draft"))
        passed = self._review(draft, _digest("twice-review"), st.ReviewerVerdict.PASS)
        sealed = self._seal(draft, passed, self._builder())
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        review_digest = _digest("twice-code-review")
        vault = hv.vault_path(self.state, self.run_id)

        first = self._review_call(
            sealed, review_digest, good_sha, good_ref, base, "scratch-twice-1"
        )
        second = self._review_call(
            sealed, review_digest, good_sha, good_ref, base, "scratch-twice-2"
        )
        for artifact in (first, second):
            self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
            digest = artifact.payload["private_results_digest"]
            self.assertEqual(
                artifact.artifact_ref,
                hv.private_results_ref(self.run_id, self.lane_id, digest),
            )
            pinned = hv.rev_parse(vault, artifact.artifact_ref)
            self.assertEqual(st.digest_bytes(hv.cat_blob(vault, pinned)), digest)
        # The input digest never names a ref: it is not what the ref pins.
        with self.assertRaises(hv.VaultError):
            hv.rev_parse(
                vault, hv.private_results_ref(self.run_id, self.lane_id, review_digest)
            )
        # Every ref under the lane's prefix is one the two reviews named:
        # nothing was pinned on the way that did not become an artifact.
        prefix = "refs/maestro/private-results/{0}/{1}/".format(
            self.run_id, self.lane_id
        )
        listed = _git(vault, "for-each-ref", "--format=%(refname)", prefix)
        self.assertEqual(
            set(listed.split()), {first.artifact_ref, second.artifact_ref}
        )

    def test_a_pinned_ref_still_refuses_a_different_result(self):
        """Moving the pin must not turn the ref mutable.

        The point of pinning last is that a review which never became an
        artifact leaves nothing behind, not that a recorded result can be
        overwritten by a later one.
        """
        vault = hv.ensure_vault(self.state, self.run_id)
        ref = hv.private_results_ref(self.run_id, self.lane_id, _digest("pin-1"))
        first = hv.hash_blob(vault, b'{"counts": 1}')
        second = hv.hash_blob(vault, b'{"counts": 2}')
        hv.pin_object_ref(vault, ref, first)
        hv.pin_object_ref(vault, ref, first)  # idempotent
        with self.assertRaises(hv.VaultError):
            hv.pin_object_ref(vault, ref, second)

    def test_code_review_copies_absent_private_tests_and_runs_them(self):
        draft = self._draft(_digest("absent-path-draft"))
        passed = self._review(
            draft, _digest("absent-path-review"), st.ReviewerVerdict.PASS
        )
        sealed = self._seal(draft, passed, self._builder())
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        review_digest = _digest("absent-path-code-review")
        scratch = self.root / "scratch-absent"
        accepted = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=review_digest,
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=good_sha,
            candidate_ref=good_ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=scratch,
            architecture_constraints=CONSTRAINTS,
        )
        self.assertIs(accepted.verdict, st.ReviewerVerdict.PASS)
        self.assertGreater(accepted.payload["public_result_summary"]["passed"], 0)
        self.assertEqual(accepted.payload["public_result_summary"]["failed"], 0)
        dest = scratch / "review-{0}-{1}".format(self.lane_id, review_digest[:12])
        self.assertTrue((dest / TEST_PATH).is_file())
        self.assertEqual((dest / "refund.py").read_text(), FIXED)
        self.assertNotEqual((dest / TEST_PATH).read_text(), FIXED)

    def test_commit_all_names_a_commit_by_its_content_not_the_clock(self):
        """Two commits of one tree, parent and message are one sha.

        Red when `commit_all` lets the inherited clock into the commit: the
        two worktrees below are committed under different `GIT_*_DATE`
        values, which is what any two runs a second apart look like.
        """
        vault = hv.ensure_vault(self.state, self.run_id)
        base = hv.seed(vault, self.repo, INTEGRATION_REF)
        shas = []
        for stamp in ("@100 +0000", "@200 +0000"):
            dest = self.worktrees / ("clock-" + stamp[1:4])
            hv.checkout_vault_worktree(vault, base, dest)
            pr.write_files(dest, {TEST_PATH: TEST_SOURCE})
            with mock.patch.dict(
                os.environ,
                {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
            ):
                shas.append(hv.commit_all(dest, "same message", paths=[TEST_PATH]))
        self.assertEqual(shas[0], shas[1])

    def test_a_draft_ref_is_named_by_what_it_pins(self):
        draft = self._draft(_digest("named-by-content"))
        vault = hv.vault_path(self.state, self.run_id)
        self.assertEqual(
            draft.artifact_ref,
            hv.draft_ref(
                self.run_id, self.lane_id, draft.payload["private_draft_digest"]
            ),
        )
        self.assertEqual(draft.payload["private_draft_ref"], draft.artifact_ref)
        with self.assertRaises(hv.VaultError):
            hv.rev_parse(
                vault, hv.draft_ref(self.run_id, self.lane_id, draft.input_digest)
            )

    def test_one_input_can_be_drafted_twice(self):
        """The resume shape: a crashed round drafted, the retry drafts again.

        Same input, same bytes, same worktrees root, a different wall clock.
        Red under input-keyed refs (`already pins X, not Y`) and red under an
        input-keyed worktree name (`refusing to adopt existing worktree`).
        """
        digest = _digest("drafted-twice")
        with mock.patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "@100 +0000", "GIT_COMMITTER_DATE": "@100 +0000"},
        ):
            first = self._draft(digest)
        with mock.patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "@200 +0000", "GIT_COMMITTER_DATE": "@200 +0000"},
        ):
            second = self._draft(digest)
        self.assertEqual(first.artifact_ref, second.artifact_ref)
        self.assertEqual(first.payload, second.payload)
        vault = hv.vault_path(self.state, self.run_id)
        self.assertEqual(
            hv.rev_parse(vault, first.artifact_ref),
            hv.rev_parse(vault, second.artifact_ref),
        )
        # The draft worktree is scaffolding and does not outlive the call.
        self.assertEqual(list(self.worktrees.rglob("draft-*")), [])

    def test_two_drafts_of_one_input_both_record(self):
        """A tester asked twice writes twice; neither answer is refused."""
        digest = _digest("drafted-differently")
        first = self._draft(digest)
        other = TEST_SOURCE + "\n# the tester answered differently the second time\n"
        second = tc.write_test_draft(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=digest
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: other},
            public_contract=CONTRACT,
            worktrees_root=self.worktrees / digest[:8],
        )
        self.assertNotEqual(first.artifact_ref, second.artifact_ref)
        vault = hv.vault_path(self.state, self.run_id)
        self.assertNotEqual(
            hv.rev_parse(vault, first.artifact_ref),
            hv.rev_parse(vault, second.artifact_ref),
        )

    def test_sealed_files_survive_the_integration_seed_moving(self):
        """A bundle's manifest is a function of its commit, not of the seed.

        Red when the private files are diffed against the current
        `integration-seed`: once integration moves, files the draft never
        touched read as private and the recorded digest no longer matches.
        """
        draft = self._draft(_digest("seed-moves-draft"))
        passed = self._review(draft, _digest("seed-moves-review"), st.ReviewerVerdict.PASS)
        sealed = self._seal(draft, passed, self._builder())
        (self.repo / "refund.py").write_text(FIXED)
        _git(self.repo, "add", "refund.py")
        _git(self.repo, "commit", "-qm", "integration moved")
        vault = hv.vault_path(self.state, self.run_id)
        moved = hv.seed(vault, self.repo, INTEGRATION_REF)
        self.assertNotEqual(moved, hv.rev_parse(vault, sealed.artifact_ref + "^"))
        files = tc.sealed_private_files(vault, sealed)
        self.assertEqual(set(files), {TEST_PATH})

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


    def _install_fake_vitest(self) -> Path:
        binary = self.repo / "node_modules" / ".bin" / "vitest"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(
            "#!{python}\n"
            "import sys\n"
            "from pathlib import Path\n"
            "stamp = Path(__file__).with_name('vitest.calls')\n"
            "prior = stamp.read_text() if stamp.exists() else ''\n"
            "stamp.write_text(prior + ' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('vitest/3.2.7')\n"
            "    raise SystemExit(0)\n"
            "if 'list' in args:\n"
            "    print('suite.test.ts > ok')\n"
            "    raise SystemExit(0)\n"
            "if 'run' in args:\n"
            "    fail = Path(__file__).with_name('vitest.fail').exists()\n"
            "    if fail:\n"
            "        print(' Test Files  1 failed (1)')\n"
            "        print('      Tests  1 failed | 0 passed (1)')\n"
            "        raise SystemExit(1)\n"
            "    print(' Test Files  1 passed (1)')\n"
            "    print('      Tests  1 passed (1)')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n".format(python=sys.executable),
            encoding="utf-8",
        )
        binary.chmod(0o755)
        return binary

    def test_code_review_runs_vitest_gate_not_pytest(self):
        suite_path = "suite.test.ts"
        contract = {
            "acceptance_criteria": ["typed suite binds the candidate"],
            "declared_outputs": [suite_path],
        }
        digest = _digest("vitest-draft")
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=digest,
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={suite_path: "test('ok', () => {})\n"},
            public_contract=contract,
            worktrees_root=self.worktrees / "vitest-draft",
        )
        passed = self._review(draft, _digest("vitest-review"), st.ReviewerVerdict.PASS)
        sealed = self._seal(draft, passed, self._builder())
        binary = self._install_fake_vitest()
        stamp = binary.with_name("vitest.calls")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._candidate(FIXED)
        gate = {
            "runner": "vitest",
            "argv": [suite_path],
            "cwd": ".",
            "min_cases": 1,
        }
        accepted = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("vitest-code-review"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / "scratch-vitest",
            architecture_constraints=CONSTRAINTS,
            gate=gate,
            runtime_root=self.repo,
        )
        self.assertIs(accepted.verdict, st.ReviewerVerdict.PASS)
        self.assertGreaterEqual(accepted.payload["public_result_summary"]["passed"], 1)
        self.assertEqual(accepted.payload["public_result_summary"]["failed"], 0)
        calls = stamp.read_text(encoding="utf-8")
        self.assertIn("run", calls)
        self.assertIn(suite_path, calls)
        self.assertNotIn("pytest", calls)
        binary.with_name("vitest.fail").write_text("1", encoding="utf-8")
        demoted = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("vitest-code-review-red"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / "scratch-vitest-red",
            architecture_constraints=CONSTRAINTS,
            gate=gate,
            runtime_root=self.repo,
        )
        self.assertIs(demoted.verdict, st.ReviewerVerdict.REVISE)
        self.assertGreater(demoted.payload["public_result_summary"]["failed"], 0)

if __name__ == "__main__":
    unittest.main()
