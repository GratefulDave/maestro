"""Observable review contracts against scheduler_types.LaneArtifact.

The producers under test -- `write_test_draft`, `review_test_draft`,
`accept_tests`, `review_builder_output`, `builder_view` -- write no lane
state. A draft is a candidate admitted on the integration head; acceptance
pins it; a code review runs the accepted suite from the candidate's own tree
and records the runner's failure output verbatim.

This file was `test_private_review_contract.py`. Every case that pinned the
vault -- object absence, leak refusal, redaction, private manifests, draft
refs -- went with the vault. What survives is the contract shape.
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
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import review_contract as rc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import test_binding as tb  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
ASSERTION_LITERAL = "EXPECTED_LITERAL_NEGATIVE_REFUND"
CASE = "test_refund_rejects_negative"
FIXTURE = "NEGATIVE_FIXTURE"
TEST_PATH = "tests/test_refund_contract.py"
PRODUCT = "def refund(amount):\n    return amount\n"
FIXED = (
    "def refund(amount):\n    if amount < 0:\n        return None\n    return amount\n"
)
TEST_SOURCE = """\
from refund import refund

{fixture} = {{"amount": -1}}


def {case}():
    assert refund({fixture}["amount"]) is None, "{literal}"
""".format(fixture=FIXTURE, case=CASE, literal=ASSERTION_LITERAL)
CONTRACT = {
    "acceptance_criteria": ["negative amounts are refused"],
    "declared_outputs": [TEST_PATH],
}
CONSTRAINTS = ("change only declared outputs",)

TAUTOLOGY_PATH = "tests/test_add_tautology.py"
TAUTOLOGY_CASE = "test_add_two_and_three"
TAUTOLOGY_SOURCE = """\
from adder import add


def {case}():
    assert add(2, 3) == 2 + 3
""".format(case=TAUTOLOGY_CASE)
TAUTOLOGY_CONTRACT = {
    "acceptance_criteria": ["adding two whole numbers yields their total"],
    "declared_outputs": [TAUTOLOGY_PATH],
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
    (repo / "refund.py").write_text(PRODUCT)
    # The suite imports `refund` from the tree root, so the tree says so. The
    # runner's environment strips an ambient PYTHONPATH (`tree_env`), and a
    # relative `PYTHONPATH=.` leaking in from the harness shell was the only
    # thing that made this import work before.
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _request(*, run_id: str, lane_id: str, input_digest: str) -> rc.LaneRequest:
    return rc.LaneRequest(
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


class ReviewContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _repo(self.root)
        self.state = self.root / "state"
        self.run_id = "run1"
        self.lane_id = "lane-a"
        self.binding = gitpub.bind_target_worktree(self.repo, INTEGRATION_REF)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def _draft(
        self, input_digest: str, files=None, contract=None
    ) -> st.LaneArtifact:
        files = files if files is not None else {TEST_PATH: TEST_SOURCE}
        contract = contract or CONTRACT
        return tc.write_test_draft(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=input_digest
            ),
            binding=self.binding,
            integration_head=_git(self.repo, "rev-parse", INTEGRATION_REF),
            files=files,
            public_contract=contract,
            declared_outputs=list(contract["declared_outputs"]),
        )

    def _review(self, draft, input_digest, verdict, findings=()) -> st.LaneArtifact:
        return tc.review_test_draft(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=input_digest
            ),
            verdict=verdict,
            findings=findings,
            test_draft=draft,
        )

    def _accept(self, draft, review) -> st.LaneArtifact:
        accepted = tc.accept_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("accept-" + draft.input_digest),
            ),
            test_draft=draft,
            test_review=review,
        )
        # What the tests lane's merge does to the integration ref: every
        # candidate below descends from a head that carries the suite.
        _git(self.repo, "update-ref", INTEGRATION_REF, str(accepted.payload["candidate_sha"]))
        _git(self.repo, "reset", "-q", "--hard", "HEAD")
        return accepted

    def _accepted(self, tag: str) -> st.LaneArtifact:
        draft = self._draft(_digest(tag + "-draft"))
        passed = self._review(draft, _digest(tag + "-review"), st.ReviewerVerdict.PASS)
        return self._accept(draft, passed)

    def _candidate(self, source: str) -> tuple[str, str]:
        (self.repo / "refund.py").write_text(source)
        if _git(self.repo, "status", "--porcelain"):
            _git(self.repo, "add", "refund.py")
            _git(self.repo, "commit", "-qm", "candidate")
        sha = _git(self.repo, "rev-parse", "HEAD")
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("build-" + sha))
        _git(self.repo, "update-ref", ref, sha)
        return sha, ref

    def _code_review(self, accepted, label, sha, ref, base, verdict, findings=(), **kw):
        return cr.review_builder_output(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=_digest(label)
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            accepted_suite=accepted,
            verdict=verdict,
            findings=findings,
            scratch_root=self.state / ("scratch-" + label),
            architecture_constraints=CONSTRAINTS,
            **kw,
        )

    # -- the draft is a candidate -----------------------------------------

    def test_a_draft_is_an_admitted_candidate_on_the_integration_head(self):
        head = _git(self.repo, "rev-parse", INTEGRATION_REF)
        draft = self._draft(_digest("draft-1"))

        self.assertIs(draft.kind, st.ArtifactKind.TEST_DRAFT)
        self.assertEqual(
            set(draft.payload),
            {
                "builder_base_sha",
                "candidate_ref",
                "candidate_sha",
                "changed",
                "files",
                "input_artifact_ids",
                "input_digest",
                "public_contract",
                "test_suite_digest",
            },
        )
        self.assertIs(draft.payload["changed"], True)
        self.assertEqual(draft.payload["builder_base_sha"], head)
        self.assertTrue(draft.payload["candidate_ref"].startswith("refs/maestro/candidates/"))
        self.assertEqual(draft.artifact_ref, draft.payload["candidate_ref"])
        sha = draft.payload["candidate_sha"]
        self.assertEqual(_git(self.repo, "rev-parse", draft.artifact_ref), sha)
        self.assertEqual(_git(self.repo, "rev-parse", sha + "^"), head)
        blob = _git(self.repo, "rev-parse", "{0}:{1}".format(sha, TEST_PATH))
        self.assertEqual(draft.payload["files"], {TEST_PATH: blob})
        self.assertEqual(
            draft.payload["test_suite_digest"], tb.suite_digest({TEST_PATH: blob})
        )
        self.assertEqual(
            _git(self.repo, "show", "{0}:{1}".format(sha, TEST_PATH)) + "\n", TEST_SOURCE
        )

    def test_a_draft_the_head_already_carries_is_the_head_itself(self):
        """A tests lane re-drafted after its suite merged changes nothing.

        Committing it would be an empty commit, which admission refuses as
        `changed=true empty delta`; it is admitted as the head, unchanged, and
        takes the zero-delta merge edge.
        """
        first = self._accepted("merged")
        head = _git(self.repo, "rev-parse", INTEGRATION_REF)
        self.assertEqual(head, first.payload["candidate_sha"])

        again = self._draft(_digest("redraft"))

        self.assertIs(again.payload["changed"], False)
        self.assertEqual(again.payload["candidate_sha"], head)
        self.assertEqual(again.payload["builder_base_sha"], head)
        self.assertEqual(again.payload["files"], first.payload["files"])
        accepted = tc.accept_tests(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=_digest("acc-2")
            ),
            test_draft=again,
            test_review=self._review(again, _digest("rev-2"), st.ReviewerVerdict.PASS),
        )
        self.assertIs(accepted.payload["changed"], False)

    def test_identical_bytes_on_one_head_name_one_candidate(self):
        first = self._draft(_digest("same-1"))
        second = self._draft(_digest("same-2"))

        self.assertEqual(first.payload["candidate_sha"], second.payload["candidate_sha"])
        self.assertNotEqual(first.payload["candidate_ref"], second.payload["candidate_ref"])

    def test_a_draft_may_not_own_a_path_it_did_not_declare(self):
        with self.assertRaises(gitpub.GitPublicationRefused) as caught:
            self._draft(
                _digest("undeclared"),
                files={TEST_PATH: TEST_SOURCE, "tests/extra.py": "x = 1\n"},
            )
        self.assertEqual(caught.exception.code, "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED")

    def test_draft_refuses_empty_envelope(self):
        with self.assertRaises(rc.ReviewContractError):
            self._draft(_digest("empty"), files={})

    def test_the_public_contract_is_recorded_as_given(self):
        contract = {
            "acceptance_criteria": [
                "Nine cases cover the FAQ block and the indexation threshold."
            ],
            "declared_outputs": [TEST_PATH],
        }
        draft = self._draft(_digest("phrases"), contract=contract)
        self.assertEqual(draft.payload["public_contract"], contract)

    # -- review and acceptance --------------------------------------------

    def test_author_reviewer_revise_then_pass_uses_distinct_inputs(self):
        first = self._draft(_digest("draft-1"))
        revise = self._review(
            first,
            _digest("review-1"),
            st.ReviewerVerdict.REVISE,
            (_finding(observed_behavior=ASSERTION_LITERAL + " still happens"),),
        )
        self.assertIs(revise.kind, st.ArtifactKind.TEST_REVIEW)
        self.assertIs(revise.verdict, st.ReviewerVerdict.REVISE)
        # The finding quotes the assertion and it arrives intact.
        self.assertIn(ASSERTION_LITERAL, revise.payload["findings"][0]["observed_behavior"])
        self.assertNotIn("[redacted]", json.dumps(revise.payload))

        second = self._draft(_digest("draft-2"), files={TEST_PATH: TEST_SOURCE + "# v2\n"})
        self.assertNotEqual(first.input_digest, second.input_digest)
        self.assertNotEqual(first.payload["candidate_sha"], second.payload["candidate_sha"])
        passed = self._review(second, _digest("review-2"), st.ReviewerVerdict.PASS)
        self.assertIs(passed.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(passed.payload["findings"], [])

    def test_acceptance_pins_the_reviewed_candidate_and_its_digest(self):
        draft = self._draft(_digest("accept-draft"))
        passed = self._review(draft, _digest("accept-review"), st.ReviewerVerdict.PASS)
        accepted = tc.accept_tests(
            request=_request(
                run_id=self.run_id, lane_id=self.lane_id, input_digest=_digest("accept")
            ),
            test_draft=draft,
            test_review=passed,
        )
        self.assertIs(accepted.kind, st.ArtifactKind.ACCEPTED_TEST_SUITE)
        self.assertEqual(accepted.payload["candidate_sha"], draft.payload["candidate_sha"])
        self.assertEqual(accepted.payload["candidate_ref"], draft.payload["candidate_ref"])
        self.assertEqual(accepted.payload["files"], draft.payload["files"])
        self.assertEqual(
            accepted.payload["test_suite_digest"], draft.payload["test_suite_digest"]
        )
        self.assertEqual(accepted.artifact_ref, draft.payload["candidate_ref"])
        self.assertNotIn("sealed_digest", accepted.payload)

    def test_acceptance_without_pass_is_refused(self):
        draft = self._draft(_digest("nopass-draft"))
        revise = self._review(
            draft, _digest("nopass-review"), st.ReviewerVerdict.REVISE, (_finding(),)
        )
        with self.assertRaises(rc.ReviewContractError):
            tc.accept_tests(
                request=_request(
                    run_id=self.run_id, lane_id=self.lane_id, input_digest=_digest("nopass")
                ),
                test_draft=draft,
                test_review=revise,
            )

    def test_a_tautological_case_named_by_id_survives_as_a_located_finding(self):
        draft = self._draft(
            _digest("tautology-draft"),
            files={TAUTOLOGY_PATH: TAUTOLOGY_SOURCE},
            contract=TAUTOLOGY_CONTRACT,
        )
        review = self._review(
            draft,
            _digest("tautology-review"),
            st.ReviewerVerdict.REVISE,
            findings=(
                {
                    "violated_requirement": (
                        "every case must be able to disagree with the implementation"
                    ),
                    "observed_behavior": (
                        "case {0} recomputes the expected value the way the "
                        "implementation does: assert add(2, 3) == 2 + 3"
                    ).format(TAUTOLOGY_CASE),
                    "required_behavior": (
                        "case {0} must assert a known-good literal taken from "
                        "the spec, not a recomputation"
                    ).format(TAUTOLOGY_CASE),
                    "implementation_area": "case {0}".format(TAUTOLOGY_CASE),
                },
            ),
        )
        located = review.payload["findings"][0]
        self.assertEqual(located["implementation_area"], "case " + TAUTOLOGY_CASE)
        # Quoting the tautological line is fine now: the tester wrote it.
        self.assertIn("assert add(2, 3) == 2 + 3", located["observed_behavior"])

    def test_incomplete_findings_and_rejected_verdict_are_refused(self):
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings(())
        with self.assertRaises(rc.ReviewContractError):
            rc.actionable_findings("REJECTED", (_finding(),))  # type: ignore[arg-type]
        with self.assertRaises(st.CanonicalIdentityError):
            st.require_revise_findings(({"violated_requirement": "x"},))
        with self.assertRaises(rc.ReviewContractError):
            rc.actionable_findings(st.ReviewerVerdict.PASS, (_finding(),))

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
        self.assertEqual(
            st.digest_bytes(st.canonical_bytes(first.payload)), first.output_digest
        )

    def test_owned_producers_do_not_write_stage_or_sqlite(self):
        self._draft(_digest("draft-1"))
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    # -- code review runs the suite from the candidate tree ----------------

    def test_code_review_revise_then_pass_with_real_runner(self):
        accepted = self._accepted("real")
        base = _git(self.repo, "rev-parse", "HEAD")
        bad_sha, bad_ref = self._candidate(PRODUCT)

        revise = self._code_review(
            accepted, "code-review-1", bad_sha, bad_ref, base,
            st.ReviewerVerdict.REVISE, (_finding(),),
        )
        self.assertIs(revise.kind, st.ArtifactKind.CODE_REVIEW)
        self.assertIs(revise.verdict, st.ReviewerVerdict.REVISE)
        self.assertGreater(revise.payload["public_result_summary"]["failed"], 0)
        self.assertEqual(revise.payload["test_suite_digest"], accepted.payload["test_suite_digest"])
        self.assertNotIn("redacted_failures", revise.payload)
        self.assertNotIn("sealed_digest", revise.payload)
        # The runner's failure lines, verbatim: the case and the assertion.
        lines = revise.payload["failure_output"]
        self.assertTrue(any(ASSERTION_LITERAL in line for line in lines), lines)
        self.assertTrue(any(CASE in line for line in lines), lines)
        self.assertNotIn("[redacted]", json.dumps(lines))
        self.assertEqual(revise.artifact_ref, "code-review:" + revise.payload["results_digest"])

        view = cr.builder_view(
            public_contract=accepted.payload["public_contract"],
            architecture_constraints=CONSTRAINTS,
            test_suite_digest=accepted.payload["test_suite_digest"],
            test_paths=(TEST_PATH,),
            prior_code_review=revise,
        )
        self.assertEqual(view["prior_code_review"]["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertEqual(view["prior_code_review"]["failure_output"], lines)
        self.assertEqual(view["test_paths"], [TEST_PATH])
        self.assertEqual(view["test_suite_digest"], accepted.payload["test_suite_digest"])
        self.assertIn(cr._FINDINGS_FRAMING, view["prior_code_review"]["findings"])

        demoted = self._code_review(
            accepted, "code-review-pass-on-red", bad_sha, bad_ref, base,
            st.ReviewerVerdict.PASS,
        )
        self.assertIs(demoted.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(set(demoted.payload["findings"][0]), set(st.REVISE_FINDING_KEYS))

        good_sha, good_ref = self._candidate(FIXED)
        passed = self._code_review(
            accepted, "code-review-2", good_sha, good_ref, bad_sha, st.ReviewerVerdict.PASS
        )
        self.assertIs(passed.verdict, st.ReviewerVerdict.PASS)
        self.assertGreater(passed.payload["public_result_summary"]["passed"], 0)
        self.assertEqual(passed.payload["public_result_summary"]["failed"], 0)
        self.assertEqual(passed.payload["failure_output"], [])
        self.assertFalse((self.state / "scratch-code-review-2" / ".git").exists())

    def test_a_reviewer_revise_stands_over_a_green_suite(self):
        """A REVISE is a verdict, and a passing suite does not overturn it.

        The coercion in the other direction stays: a PASS on a failing suite is
        demoted to REVISE, because the reviewer voted against a measurement it
        could have read. This direction is not symmetric: a reviewer reads the
        code against the plan, and almost everything it can see -- a field
        left optional against a contract that requires it, a handler that
        never reads the error it catches -- is something no green suite
        contradicts.
        """
        accepted = self._accepted("stands")
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        located = {
            "implementation_area": "refund.py",
            "observed_behavior": "the guard returns None rather than the amount",
            "required_behavior": "return the amount unchanged for every input",
            "violated_requirement": "negative amounts are refused",
        }
        artifact = self._code_review(
            accepted, "revise-stands", good_sha, good_ref, base,
            st.ReviewerVerdict.REVISE, (located,),
        )
        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertGreater(artifact.payload["public_result_summary"]["passed"], 0)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(len(artifact.payload["findings"]), 1)
        self.assertNotIn("advisory_findings", artifact.payload)

    def test_a_red_suite_still_keeps_the_reviewer_findings_actionable(self):
        accepted = self._accepted("red-keeps")
        base = _git(self.repo, "rev-parse", "HEAD")
        bad_sha, bad_ref = self._candidate(PRODUCT)
        located = {
            "implementation_area": "refund.py",
            "observed_behavior": "negative amounts are returned unchanged",
            "required_behavior": "return None below zero",
            "violated_requirement": "negative amounts are refused",
        }
        artifact = self._code_review(
            accepted, "red-keeps", bad_sha, bad_ref, base,
            st.ReviewerVerdict.REVISE, (located,),
        )
        self.assertGreater(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(artifact.payload["findings"], [located])

    def test_the_same_input_can_be_reviewed_twice(self):
        """Two reviews of one input are two observations, each its own artifact."""
        accepted = self._accepted("twice")
        base = _git(self.repo, "rev-parse", "HEAD")
        good_sha, good_ref = self._candidate(FIXED)
        first = self._code_review(accepted, "twice", good_sha, good_ref, base, st.ReviewerVerdict.PASS)
        second = self._code_review(accepted, "twice", good_sha, good_ref, base, st.ReviewerVerdict.PASS)
        for artifact in (first, second):
            self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
            self.assertEqual(
                artifact.artifact_ref, "code-review:" + artifact.payload["results_digest"]
            )

    def test_a_candidate_whose_tree_lost_the_suite_is_refused_not_measured(self):
        """The candidate descends from a base without the suite: TEST_SUITE_TAMPERED."""
        accepted = self._accepted("lost")
        orphan = gitpub.commit_files_on_base(
            self.binding,
            base_sha=str(accepted.payload["builder_base_sha"]),
            files={"refund.py": FIXED.encode("utf-8")},
            message=b"candidate off the pre-suite head\n",
        )
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("orphan"))
        _git(self.repo, "update-ref", ref, orphan)
        with self.assertRaises(tb.TestSuiteTampered) as caught:
            self._code_review(
                accepted, "lost", orphan, ref,
                str(accepted.payload["builder_base_sha"]), st.ReviewerVerdict.PASS,
            )
        self.assertEqual(caught.exception.path, TEST_PATH)
        self.assertIn("ABSENT", str(caught.exception))

    # -- runner selection --------------------------------------------------

    def _install_fake_vitest(self) -> Path:
        binary = self.root / "fake-vitest" / "vitest"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(
            "#!{python}\n"
            "import sys\n"
            "from pathlib import Path\n"
            "stamp = Path({stamp!r})\n"
            "fail_flag = Path({fail!r})\n"
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
            "    if fail_flag.exists():\n"
            "        print(' Test Files  1 failed (1)')\n"
            "        print('      Tests  1 failed | 0 passed (1)')\n"
            "        raise SystemExit(1)\n"
            "    print(' Test Files  1 passed (1)')\n"
            "    print('      Tests  1 passed (1)')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n".format(
                python=sys.executable,
                stamp=str(binary.with_name("vitest.calls")),
                fail=str(binary.with_name("vitest.fail")),
            ),
            encoding="utf-8",
        )
        binary.chmod(0o755)
        return binary

    @staticmethod
    def _provision_fake_vitest(binary: Path) -> tuple[str, ...]:
        return (
            "sh",
            "-c",
            "mkdir -p node_modules/.bin && cp {0} node_modules/.bin/vitest".format(binary),
        )

    def test_code_review_runs_vitest_gate_not_pytest(self):
        suite_path = "suite.test.ts"
        contract = {
            "acceptance_criteria": ["typed suite binds the candidate"],
            "declared_outputs": [suite_path],
        }
        draft = self._draft(
            _digest("vitest-draft"),
            files={suite_path: "test('ok', () => {})\n"},
            contract=contract,
        )
        passed = self._review(draft, _digest("vitest-review"), st.ReviewerVerdict.PASS)
        accepted = self._accept(draft, passed)
        binary = self._install_fake_vitest()
        stamp = binary.with_name("vitest.calls")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._candidate(FIXED)
        gate = {"runner": "vitest", "argv": [suite_path], "cwd": ".", "min_cases": 1}

        green = self._code_review(
            accepted, "vitest", sha, ref, base, st.ReviewerVerdict.PASS,
            gate=gate, provision_argv=self._provision_fake_vitest(binary),
        )
        self.assertIs(green.verdict, st.ReviewerVerdict.PASS)
        calls = stamp.read_text(encoding="utf-8")
        self.assertIn("run", calls)
        self.assertIn(suite_path, calls)
        self.assertNotIn("pytest", calls)

        binary.with_name("vitest.fail").write_text("1", encoding="utf-8")
        red = self._code_review(
            accepted, "vitest-red", sha, ref, base, st.ReviewerVerdict.PASS,
            gate=gate, provision_argv=self._provision_fake_vitest(binary),
        )
        self.assertIs(red.verdict, st.ReviewerVerdict.REVISE)
        self.assertGreater(red.payload["public_result_summary"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
