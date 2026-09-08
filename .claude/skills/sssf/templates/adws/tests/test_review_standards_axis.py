"""The Standards axis: a hygiene report that can never gate a green candidate.

Three contracts, none of them about the reviewer's judgement -- the reviewer is
a model and its taste is not testable here. What is testable is the shape of
what it is asked and the shape of what its answer is allowed to do:

* the rubric it is handed names every baseline item and every bounding rule;
* a standards finding rides in `advisory_findings`, keeps its axis, and leaves
  `findings` empty, so nothing sends a passing candidate back to `BUILDING`;
* a standards finding submitted as ERROR arrives as WARNING.
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
from adw_modules import review_standards as rvs  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_refund_secret.py"
SECRET_LITERAL = "SECRET_EXPECTED_LITERAL_NEGATIVE_REFUND"
SECRET_SELECTOR = "test_refund_rejects_secret_negative"
SECRET_FIXTURE = "SECRET_FIXTURE"
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


def _request(*, run_id: str, lane_id: str, input_digest: str) -> pr.VaultLaneRequest:
    return pr.VaultLaneRequest(
        run_id=run_id,
        lane_id=lane_id,
        plan_revision=1,
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        input_digest=input_digest,
    )


def _standards_finding(**overrides: str) -> dict[str, str]:
    row = {
        "violated_requirement": "possible Duplicated Code",
        "observed_behavior": (
            "the same forty-line normalization appears in refund.py and in "
            "the adjacent hunk"
        ),
        "required_behavior": (
            "extract the shared structure into one named function and call it "
            "from both sites"
        ),
        "implementation_area": "refund.py, hunk 2",
        "axis": st.FINDING_AXIS_STANDARDS,
    }
    row.update(overrides)
    return row


class StandardsRubricText(unittest.TestCase):
    """The reviewer is handed the whole baseline, not a summary of it."""

    def test_rubric_names_every_baseline_item_and_every_rule(self):
        rendered = rvs.standards_section()
        for name in (
            "Mysterious Name",
            "Duplicated Code",
            "Feature Envy",
            "Data Clumps",
            "Primitive Obsession",
            "Repeated Switches",
            "Shotgun Surgery",
            "Divergent Change",
            "Speculative Generality",
            "Message Chains",
            "Middle Man",
            "Refused Bequest",
            "Shallow module",
        ):
            self.assertIn(name, rendered, name)
        # Twelve Fowler smells plus the one deep-module item.
        self.assertEqual(len(rvs.STANDARDS_SMELLS), 13)
        # Every item states what it is and how to fix it.
        for name, what, fix in rvs.STANDARDS_SMELLS:
            self.assertTrue(what.strip(), name)
            self.assertTrue(fix.strip(), name)
            self.assertIn(what, rendered, name)
            self.assertIn(fix, rendered, name)
        self.assertEqual(len(rvs.STANDARDS_RULES), 3)
        for rule in rvs.STANDARDS_RULES:
            self.assertIn(rule, rendered)
        # The rules, by the words that carry them.
        self.assertIn("overrides this baseline", rendered)
        self.assertIn("judgement call", rendered)
        self.assertIn("Skip anything tooling already enforces", rendered)
        # And the axis contract the findings must satisfy.
        self.assertIn('axis: "standards"', rendered)
        self.assertIn("WARNING", rendered)

    def test_repo_standards_files_are_named_by_path_not_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CONTRIBUTING.md").write_text(
                "our convention: never raise bare Exception\n", encoding="utf-8"
            )
            found = rvs.discover_standards_files(root)
            self.assertEqual(found, ("CONTRIBUTING.md",))
            section = rvs.standards_section(found)
            self.assertIn("CONTRIBUTING.md", section)
            self.assertNotIn("never raise bare Exception", section)

    def test_no_standards_file_leaves_the_baseline_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rvs.discover_standards_files(Path(tmp)), ())
        self.assertEqual(rvs.standards_section(()), rvs.STANDARDS_RUBRIC)


class StandardsSeverityCap(unittest.TestCase):
    """The cap is arithmetic on one ordered tuple, testable without a suite."""

    def test_error_on_the_standards_axis_is_capped_to_warning(self):
        spec, standards = st.partition_findings_by_axis(
            (_standards_finding(severity=st.SEVERITY_ERROR),)
        )
        self.assertEqual(spec, ())
        self.assertEqual(len(standards), 1)
        self.assertEqual(standards[0]["severity"], st.SEVERITY_WARNING)
        self.assertEqual(standards[0]["axis"], st.FINDING_AXIS_STANDARDS)

    def test_an_absent_severity_becomes_warning(self):
        _spec, standards = st.partition_findings_by_axis((_standards_finding(),))
        self.assertEqual(standards[0]["severity"], st.SEVERITY_WARNING)

    def test_a_lower_severity_is_left_alone(self):
        _spec, standards = st.partition_findings_by_axis(
            (_standards_finding(severity=st.SEVERITY_INFO),)
        )
        self.assertEqual(standards[0]["severity"], st.SEVERITY_INFO)

    def test_a_finding_naming_no_axis_stays_on_the_spec_axis(self):
        row = _standards_finding()
        del row["axis"]
        spec, standards = st.partition_findings_by_axis((row,))
        self.assertEqual(standards, ())
        self.assertEqual(len(spec), 1)
        self.assertNotIn("axis", spec[0])
        self.assertNotIn("severity", spec[0])

    def test_an_unknown_axis_is_refused(self):
        with self.assertRaises(st.CanonicalIdentityError):
            st.partition_findings_by_axis((_standards_finding(axis="vibes"),))

    def test_an_old_four_key_finding_still_normalizes(self):
        row = _standards_finding()
        del row["axis"]
        normalized = st.require_revise_findings((row,))
        self.assertEqual(set(normalized[0]), set(st.REVISE_FINDING_KEYS))


class StandardsAxisAgainstAGreenSuite(unittest.TestCase):
    """A green candidate with hygiene findings merges, and keeps the findings."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "Harness")
        _git(self.repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        (self.repo / "refund.py").write_text(PRODUCT)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.state = self.root / "state"
        self.worktrees = self.root / "worktrees"
        self.run_id = "run1"
        self.lane_id = "lane-a"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _sealed(self) -> tuple[st.LaneArtifact, str]:
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("standards-draft"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=CONTRACT,
            worktrees_root=self.worktrees / "standards-draft",
        )
        tokens = tc.draft_private_tokens(
            state_root=self.state, run_id=self.run_id, draft=draft
        )
        review = tc.review_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("standards-test-review"),
            ),
            verdict=st.ReviewerVerdict.PASS,
            findings=(),
            test_draft=draft,
            private_tokens=tokens,
        )
        head = _git(self.repo, "rev-parse", "HEAD")
        builder = hv.linked_worktree(self.repo, self.root / "builder", head)
        sealed = tc.seal_accepted_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("standards-seal"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=review,
        )
        return sealed, head

    def _candidate(self, source: str) -> tuple[str, str]:
        (self.repo / "refund.py").write_text(source)
        if _git(self.repo, "status", "--porcelain"):
            _git(self.repo, "add", "refund.py")
            _git(self.repo, "commit", "-qm", "candidate")
        sha = _git(self.repo, "rev-parse", "HEAD")
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("build-" + sha))
        _git(self.repo, "update-ref", ref, sha)
        return sha, ref

    def _review_candidate(self, label, verdict, findings):
        sealed, base = self._sealed()
        sha, ref = self._candidate(FIXED)
        return cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest(label),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=verdict,
            findings=findings,
            scratch_root=self.root / ("scratch-" + label),
            architecture_constraints=CONSTRAINTS,
        )

    def test_two_standards_findings_ride_advisory_and_leave_the_verdict_green(self):
        swallowed = _standards_finding(
            violated_requirement="possible Mysterious Name",
            observed_behavior=(
                "the except block binds the error and returns None with no "
                "record that anything failed"
            ),
            required_behavior=(
                "name the failure or let it propagate; a swallowed exception "
                "hides the cause from the next reader"
            ),
            implementation_area="refund.py, hunk 1",
        )
        duplicated = _standards_finding()

        artifact = self._review_candidate(
            "standards-advisory",
            st.ReviewerVerdict.PASS,
            (swallowed, duplicated),
        )

        # The suite really did pass. Without this the rest proves nothing.
        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertEqual(artifact.payload["public_result_summary"]["errored"], 0)
        self.assertGreater(artifact.payload["public_result_summary"]["passed"], 0)

        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(artifact.payload["verdict"], st.ReviewerVerdict.PASS.value)
        # Nothing actionable, so nothing sends the lane back to BUILDING.
        self.assertEqual(artifact.payload["findings"], [])

        advisory = artifact.payload["advisory_findings"]
        self.assertEqual(len(advisory), 2)
        for row in advisory:
            self.assertEqual(row["axis"], st.FINDING_AXIS_STANDARDS)
            self.assertLessEqual(
                st.FINDING_SEVERITIES.index(row["severity"]),
                st.FINDING_SEVERITIES.index(st.SEVERITY_WARNING),
            )
            for key in st.REVISE_FINDING_KEYS:
                self.assertTrue(row[key].strip())
        self.assertIn("hunk 1", advisory[0]["implementation_area"])
        self.assertIn("hunk 2", advisory[1]["implementation_area"])

        public = json.dumps(artifact.payload)
        self.assertNotIn(SECRET_LITERAL, public)
        self.assertNotIn(SECRET_SELECTOR, public)
        self.assertNotIn(TEST_PATH, public)

    def test_a_standards_error_is_capped_and_does_not_return_the_lane(self):
        artifact = self._review_candidate(
            "standards-error-capped",
            st.ReviewerVerdict.REVISE,
            (_standards_finding(severity=st.SEVERITY_ERROR),),
        )

        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 0)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        self.assertNotEqual(artifact.payload["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertEqual(artifact.payload["findings"], [])
        advisory = artifact.payload["advisory_findings"]
        self.assertEqual(len(advisory), 1)
        self.assertEqual(advisory[0]["severity"], st.SEVERITY_WARNING)
        self.assertEqual(advisory[0]["axis"], st.FINDING_AXIS_STANDARDS)


if __name__ == "__main__":
    unittest.main()
