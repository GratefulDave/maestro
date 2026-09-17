"""A candidate never owns, and a review never runs, anything but the accepted suite.

Three checks, one idea: the bytes a lane is graded against are immutable with
respect to the lane being graded.

  * ``OUTPUT_OVERLAPS_TEST_SUITE`` (plan) -- no lane may declare an output that
    covers another lane's accepted test suite. Its sibling cases live in
    ``test_plan_compiler.py``, beside the other objective plan refusals.
  * ``CANDIDATE_TEST_PATH_REFUSED`` (candidate) -- a tree delta that touches one
    of those paths is refused before any reviewer reads it.
  * ``TEST_SUITE_TAMPERED`` (review) -- the suite present in the review tree is
    hashed against the accepted blob ids before the runner is invoked, and a
    mismatch refuses as an environment fault rather than a candidate defect.

The accepted suite is carried in the candidate's own tree -- the tests lane
merged it, or the untyped lane built on it -- so the third check reads the bytes
the candidate's history put there. There is no overlay.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import git_publication as gp  # noqa: E402
from adw_modules import review_contract as rc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import test_binding as tb  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_refund_secret.py"
SECRET_LITERAL = "SECRET_EXPECTED_LITERAL_NEGATIVE_REFUND"
PRODUCT = "def refund(amount):\n    return amount\n"
TEST_SOURCE = """\
from refund import refund


def test_refund_rejects_secret_negative():
    assert refund(-1) is None, "{literal}"
""".format(literal=SECRET_LITERAL)
CONTRACT = {
    "acceptance_criteria": ["negative amounts are refused"],
    "declared_outputs": [TEST_PATH],
}
CONSTRAINTS = ("change only declared outputs",)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(
                " ".join(args), result.returncode, result.stderr
            )
        )
    return result.stdout.strip()


#: What `tests_chain.run_suite` returns for a suite that ran and passed.
_GREEN_RUN = {
    "counts": {"passed": 1, "failed": 0, "errored": 0, "skipped": 0},
    "executed": 1,
    "min_cases": 1,
    "output": "1 passed",
    "returncode": 0,
    "runner": "pytest",
}


def _entry(**overrides) -> gp.TreeDeltaEntry:
    row = {
        "status": "M",
        "score": None,
        "old_path": "src/app.py",
        "new_path": "src/app.py",
        "old_mode": "100644",
        "new_mode": "100644",
        "old_oid": "a" * 40,
        "new_oid": "b" * 40,
    }
    row.update(overrides)
    return gp.TreeDeltaEntry(**row)


class SuiteDigestIsAPropertyOfTheFiles(unittest.TestCase):
    """The digest binds path-to-blob pairs and nothing about a commit.

    It can be recomputed from any checkout, which is what lets the same value
    be asserted against the review tree, the base tree and the integration gate.
    """

    def test_the_digest_is_order_independent(self):
        forward = {"tests/a.py": "a" * 40, "tests/b.py": "b" * 40}
        backward = {"tests/b.py": "b" * 40, "tests/a.py": "a" * 40}

        self.assertEqual(tb.suite_digest(forward), tb.suite_digest(backward))

    def test_the_digest_is_versioned(self):
        self.assertTrue(
            tb.suite_digest({"tests/a.py": "a" * 40}).startswith("test-suite.v1:")
        )

    def test_one_changed_blob_changes_the_digest(self):
        before = {"tests/a.py": "a" * 40, "tests/b.py": "b" * 40}
        after = {"tests/a.py": "a" * 40, "tests/b.py": "c" * 40}

        self.assertNotEqual(tb.suite_digest(before), tb.suite_digest(after))

    def test_a_renamed_path_changes_the_digest(self):
        before = {"tests/a.py": "a" * 40}
        after = {"tests/moved.py": "a" * 40}

        self.assertNotEqual(tb.suite_digest(before), tb.suite_digest(after))


class VerifySuiteHashesTheTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self.tmp.name)
        _git(self.tree, "init", "-q", "-b", "main")
        self.path = "tests/suite_test.py"
        target = self.tree / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_one():\n    assert True\n")
        self.blob = _git(self.tree, "hash-object", self.path)
        self.expected = {self.path: self.blob}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_blob_id_matches_git_hash_object(self):
        """Not a reimplementation with its own opinion: the same bytes as git."""
        data = (self.tree / self.path).read_bytes()

        self.assertEqual(self.blob, tb.blob_id(data))

    def test_an_untouched_tree_verifies(self):
        self.assertIsNone(tb.verify_suite(self.expected, self.tree))

    def test_one_changed_byte_is_refused_by_path(self):
        target = self.tree / self.path
        target.write_text(target.read_text().replace("True", "False"))

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertEqual(self.path, caught.exception.path)
        self.assertIn("TEST_SUITE_TAMPERED:{0}:{1}:".format(self.path, self.blob),
                      str(caught.exception))

    def test_a_deleted_file_is_refused_by_path(self):
        (self.tree / self.path).unlink()

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertEqual(self.path, caught.exception.path)
        self.assertIn("ABSENT", str(caught.exception))

    def test_a_symlink_at_a_suite_path_is_refused(self):
        target = self.tree / self.path
        elsewhere = self.tree / "elsewhere.py"
        elsewhere.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(elsewhere)

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertIn("SYMLINK", str(caught.exception))

    def test_a_symlinked_ancestor_directory_is_refused(self):
        """The leaf is a regular file; the directory it is reached through is not.

        `tests` -> `elsewhere/` holding byte-identical content hashes to the
        accepted blob, yet the runner would read a path the tree does not own.
        """
        moved = self.tree / "elsewhere"
        (self.tree / "tests").rename(moved)
        (self.tree / "tests").symlink_to(moved, target_is_directory=True)
        self.assertEqual(
            self.blob, tb.blob_id((self.tree / self.path).read_bytes())
        )

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertEqual(self.path, caught.exception.path)
        self.assertEqual(tb.SYMLINK, caught.exception.actual)

    def test_a_symlinked_ancestor_escaping_the_tree_is_refused(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        moved = Path(outside.name) / "tests"
        (self.tree / "tests").rename(moved)
        (self.tree / "tests").symlink_to(moved, target_is_directory=True)

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertEqual(tb.SYMLINK, caught.exception.actual)

    def test_a_directory_at_a_suite_path_is_refused(self):
        target = self.tree / self.path
        target.unlink()
        target.mkdir()

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertEqual(self.path, caught.exception.path)
        self.assertEqual(tb.ABSENT, caught.exception.actual)

    def test_a_refusal_never_carries_the_file_contents(self):
        target = self.tree / self.path
        target.write_text("def test_one():\n    assert {0!r}\n".format(SECRET_LITERAL))

        with self.assertRaises(tb.TestSuiteTampered) as caught:
            tb.verify_suite(self.expected, self.tree)

        self.assertNotIn(SECRET_LITERAL, str(caught.exception))

    def test_the_refusal_is_an_environment_fault_not_a_candidate_defect(self):
        """`SUITE_*` surfacing, so it never reaches the builder as REVISE."""
        error = tb.TestSuiteTampered(self.path, self.blob, "ABSENT")

        self.assertIsInstance(error, rc.SuiteEnvironmentError)
        self.assertIsNotNone(cr.suite_environment_detail(error))


class UntypedTestsMayNotSitOnProductOutputs(unittest.TestCase):
    """`TEST_FILE_ON_DECLARED_OUTPUT` is ancestry-aware, not string equality.

    A test file `pkg` and a product output `pkg/mod.py` cannot both exist in
    one Git tree, in either direction, so each is the same collision as an
    exact match and refuses at admission rather than at tree construction.
    """

    def _refuse(self, outputs, files):
        from types import SimpleNamespace

        lane = SimpleNamespace(lane_kind=None, declared_outputs=tuple(outputs))
        sch.FactoryScheduler._refuse_test_files_on_outputs(
            None, lane, {path: "x" for path in files}
        )

    def test_an_exact_match_refuses(self):
        with self.assertRaises(sch.TestFileOnDeclaredOutput) as caught:
            self._refuse(["pkg/mod.py"], ["pkg/mod.py"])
        self.assertEqual("TEST_FILE_ON_DECLARED_OUTPUT", caught.exception.code)

    def test_a_test_file_at_an_output_ancestor_refuses(self):
        with self.assertRaises(sch.TestFileOnDeclaredOutput) as caught:
            self._refuse(["pkg/mod.py"], ["pkg"])
        self.assertIn("pkg", str(caught.exception))

    def test_a_test_file_under_an_output_directory_refuses(self):
        with self.assertRaises(sch.TestFileOnDeclaredOutput) as caught:
            self._refuse(["pkg"], ["pkg/test_mod.py"])
        self.assertIn("pkg/test_mod.py", str(caught.exception))

    def test_a_shared_name_prefix_is_not_ancestry(self):
        self.assertIsNone(self._refuse(["pkg/mod.py"], ["pkg/mod.py_test.py", "pk"]))


class ProtectedPathsAreNotOwnable(unittest.TestCase):
    """M1b: a candidate delta that touches the suite it is graded against."""

    def test_a_modified_protected_path_is_refused(self):
        delta = [_entry(old_path=TEST_PATH, new_path=TEST_PATH)]

        with self.assertRaises(gp.GitPublicationRefused) as caught:
            gp.validate_declared_ownership(
                delta, [TEST_PATH], changed=True, protected_paths=(TEST_PATH,)
            )

        self.assertEqual("CANDIDATE_TEST_PATH_REFUSED", caught.exception.code)
        self.assertEqual(TEST_PATH, caught.exception.detail)

    def test_a_rename_away_from_a_protected_path_is_refused(self):
        delta = [
            _entry(
                status="R",
                score=100,
                old_path=TEST_PATH,
                new_path="tests/renamed_test.py",
            )
        ]

        with self.assertRaises(gp.GitPublicationRefused) as caught:
            gp.validate_declared_ownership(
                delta,
                [TEST_PATH, "tests/renamed_test.py"],
                changed=True,
                protected_paths=(TEST_PATH,),
            )

        self.assertEqual("CANDIDATE_TEST_PATH_REFUSED", caught.exception.code)

    def test_a_rename_onto_a_protected_path_is_refused(self):
        delta = [
            _entry(
                status="R",
                score=100,
                old_path="src/app.py",
                new_path=TEST_PATH,
            )
        ]

        with self.assertRaises(gp.GitPublicationRefused) as caught:
            gp.validate_declared_ownership(
                delta,
                ["src/app.py", TEST_PATH],
                changed=True,
                protected_paths=(TEST_PATH,),
            )

        self.assertEqual("CANDIDATE_TEST_PATH_REFUSED", caught.exception.code)

    def test_a_copy_out_of_a_protected_path_is_refused(self):
        """A copy READS the accepted suite even though it does not write it.

        `represented_paths` deliberately omits a copy's source: the source blob
        is unchanged, the lane did not write it, and counting it refused a
        builder for a path it never touched on FDAdb run d246ae95. That reading
        is right for ownership and wrong here. `C<score> <suite> <output>` means
        the candidate's declared output now HOLDS the accepted suite's content,
        which is the bytes it is graded against landing in a file it owns. So
        the protected check reads `content_paths`, which keeps the source, and
        ownership still reads `represented_paths`, which drops it.
        """
        delta = [
            _entry(
                status="C",
                score=95,
                old_path=TEST_PATH,
                new_path="src/copied_suite.py",
            )
        ]

        with self.assertRaises(gp.GitPublicationRefused) as caught:
            gp.validate_declared_ownership(
                delta,
                ["src/copied_suite.py"],
                changed=True,
                protected_paths=(TEST_PATH,),
            )

        self.assertEqual("CANDIDATE_TEST_PATH_REFUSED", caught.exception.code)
        self.assertEqual(TEST_PATH, caught.exception.detail)

    def test_a_copy_source_is_still_not_an_owned_path(self):
        """The d246ae95 fix is untouched: ownership never sees a copy source."""
        delta = [
            _entry(
                status="C",
                score=95,
                old_path="tests/wp7/vitest.config.ts",
                new_path="tests/wp6/vitest.config.ts",
            )
        ]

        self.assertIsNone(
            gp.validate_declared_ownership(
                delta,
                ["tests/wp6/vitest.config.ts"],
                changed=True,
                protected_paths=(TEST_PATH,),
            )
        )

    def test_a_delta_that_touches_nothing_protected_is_unchanged(self):
        delta = [_entry()]

        self.assertIsNone(
            gp.validate_declared_ownership(
                delta, ["src/app.py"], changed=True, protected_paths=(TEST_PATH,)
            )
        )

    def test_the_guard_is_absent_by_default(self):
        """No caller is forced to pass it; the ownership check is unchanged."""
        delta = [_entry(old_path=TEST_PATH, new_path=TEST_PATH)]

        self.assertIsNone(
            gp.validate_declared_ownership(delta, [TEST_PATH], changed=True)
        )

    def test_admit_candidate_forwards_the_protected_paths(self):
        recorded = {}

        def _record(delta, declared, *, changed, protected_paths=()):
            recorded["protected"] = tuple(protected_paths)

        with mock.patch.object(gp, "revalidate_binding"), mock.patch.object(
            gp, "measure_tree_delta", return_value=[]
        ), mock.patch.object(
            gp, "validate_declared_ownership", side_effect=_record
        ), mock.patch.object(
            gp, "pin_candidate_ref", return_value={"candidate_ref": "refs/x"}
        ), mock.patch.object(
            gp, "require_oid", side_effect=lambda value, **_kw: value
        ):
            binding = mock.MagicMock()
            binding.target_object_format = "sha1"
            gp.admit_candidate(
                binding,
                run_id="run1",
                lane_id="lane-a",
                input_digest=_digest("input"),
                builder_base_sha="a" * 40,
                candidate_sha="a" * 40,
                changed=False,
                declared_outputs=["src/app.py"],
                protected_paths=(TEST_PATH,),
            )

        self.assertEqual((TEST_PATH,), recorded["protected"])

    def test_the_scheduler_passes_every_lanes_own_test_paths(self):
        """The protected set is the plan's, never a path-glob over names.

        Every lane kind. An untyped lane's tests are in its builder's base too
        (`_untyped_builder_base`), so a candidate landing on one is the same
        violation a typed lane's is, not the collision the retired
        `TEST_INVALIDATION` reset used to answer.
        """
        source = (ADWS / "adw_modules" / "scheduler.py").read_text()
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "admit_candidate"
        ]
        self.assertTrue(calls, "the scheduler no longer admits a candidate")
        for call in calls:
            keywords = {kw.arg: kw for kw in call.keywords}
            self.assertIn("protected_paths", keywords)
            expression = ast.unparse(keywords["protected_paths"].value)
            self.assertIn("ctx.protected_test_paths", expression)
            self.assertNotIn("LANE_KIND_BUILD", expression)


class TheReviewRunsTheAcceptedSuite(unittest.TestCase):
    """M2 end to end: a tampered review tree never reaches the runner."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.worktrees = self.root / "worktrees"
        self.run_id = "run1"
        self.lane_id = "lane-a"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "Harness")
        (self.repo / "refund.py").write_text(PRODUCT)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.sealed = self._seal()
        self.candidate_sha, self.candidate_ref = self._candidate()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, input_digest: str) -> rc.LaneRequest:
        return rc.LaneRequest(
            run_id=self.run_id,
            lane_id=self.lane_id,
            plan_revision=1,
            spec_digest=_digest("spec"),
            lane_projection_digest=_digest("projection"),
            input_digest=input_digest,
        )

    def _binding(self) -> gp.TargetBinding:
        return gp.bind_target_worktree(self.repo, INTEGRATION_REF)

    def _seal(self) -> st.LaneArtifact:
        """The accepted suite: a draft admitted on the base and accepted as-is."""
        draft = tc.write_test_draft(
            request=self._request(_digest("draft")),
            binding=self._binding(),
            integration_head=self.base,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=CONTRACT,
            declared_outputs=[TEST_PATH],
        )
        review = tc.review_test_draft(
            request=self._request(_digest("test-review")),
            verdict=st.ReviewerVerdict.PASS,
            findings=(),
            test_draft=draft,
        )
        return tc.accept_tests(
            request=self._request(_digest("accept")),
            test_draft=draft,
            test_review=review,
        )

    def _candidate(self) -> tuple[str, str]:
        """A candidate built on the accepted suite, as a build lane's is."""
        suite_sha = str(self.sealed.payload["candidate_sha"])
        sha = gp.commit_files_on_base(
            self._binding(),
            base_sha=suite_sha,
            files={
                "refund.py": (
                    "def refund(amount):\n"
                    "    if amount < 0:\n"
                    "        return None\n"
                    "    return amount\n"
                ).encode("utf-8")
            },
            message=b"candidate\n",
        )
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("build-" + sha))
        _git(self.repo, "update-ref", ref, sha)
        self.base = suite_sha
        return sha, ref

    def _review(self, digest_label: str):
        return cr.review_builder_output(
            request=self._request(_digest(digest_label)),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=self.candidate_sha,
            candidate_ref=self.candidate_ref,
            builder_base_sha=self.base,
            accepted_suite=self.sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.state / ("scratch-" + digest_label),
            architecture_constraints=CONSTRAINTS,
        )

    def test_the_code_review_records_the_suite_digest(self):
        # The runner is stubbed here, and only here, because this case is about
        # what the review RECORDS. A real `pytest` subprocess would make the
        # assertion a statement about this machine's interpreter resolution --
        # which is exactly the environment fault the suite already carries a
        # dozen of. The tampering case below stubs it for a different and
        # stronger reason: to prove it is never reached.
        with mock.patch.object(cr.tc, "run_suite", return_value=_GREEN_RUN):
            artifact = self._review("clean")
        files = tc.suite_files(self.sealed)

        self.assertEqual(
            tb.suite_digest(files), artifact.payload["test_suite_digest"]
        )
        self.assertNotIn("sealed_digest", artifact.payload)
        self.assertEqual(
            artifact.payload["test_suite_digest"],
            self.sealed.payload["test_suite_digest"],
        )

    def _tampering_tree(self):
        """A `_review_tree` that materializes the tree, then edits the suite in it.

        The bytes under a test path are whatever the tree carries; this makes
        them not the accepted ones after materialization and before the check.
        """
        real_tree = cr._review_tree

        def _tamper(repo, sha, dest, *args, **kwargs):
            tree = real_tree(repo, sha, dest, *args, **kwargs)
            target = Path(tree) / TEST_PATH
            target.write_text(target.read_text().replace("is None", "is not None"))
            return tree

        return _tamper

    def _ancestor_symlink_tree(self):
        """Materialize, then reach the unchanged suite through a symlinked directory.

        Every byte the runner would read hashes to the accepted blob; only the
        route to it changed.
        """
        real_tree = cr._review_tree

        def _relink(repo, sha, dest, *args, **kwargs):
            tree = Path(real_tree(repo, sha, dest, *args, **kwargs))
            top = TEST_PATH.split("/")[0]
            moved = tree / ("relinked-" + top)
            (tree / top).rename(moved)
            (tree / top).symlink_to(moved, target_is_directory=True)
            return tree

        return _relink

    def test_a_symlinked_suite_ancestor_refuses_before_the_runner(self):
        runner = mock.Mock(name="run_suite")
        with mock.patch.object(
            cr, "_review_tree", side_effect=self._ancestor_symlink_tree()
        ):
            with mock.patch.object(cr.tc, "run_suite", runner):
                with self.assertRaises(tb.TestSuiteTampered) as caught:
                    self._review("ancestor-symlink")

        runner.assert_not_called()
        self.assertEqual(TEST_PATH, caught.exception.path)
        self.assertEqual(tb.SYMLINK, caught.exception.actual)

    def test_a_tampered_base_tree_refuses_rather_than_absolving_the_builder(self):
        """`_collect_at_base` must not swallow this into "not the candidate's".

        Its broad `except (ReviewContractError, MaterializeError, OSError):
        return False` is a deliberate absolution: a base that cannot run the suite
        proves the fault predates the candidate. `TestSuiteTampered` is a
        `ReviewContractError` by inheritance and would be absorbed by that same
        clause -- turning an operator fault into the quieter and wrong claim
        that the builder is blameless, and then measuring the candidate against
        a suite nobody accepted.
        """
        files = tc.suite_files(self.sealed)
        runner = mock.Mock(name="run_suite")

        with mock.patch.object(cr, "_review_tree", side_effect=self._tampering_tree()):
            with mock.patch.object(cr.tc, "run_suite", runner):
                with self.assertRaises(tb.TestSuiteTampered) as caught:
                    cr._collect_at_base(
                        candidate_repo=self.repo,
                        builder_base_sha=self.base,
                        files=files,
                        scratch_root=self.state / "scratch-base-tampered",
                        lane_id=self.lane_id,
                        input_digest=_digest("base-tampered"),
                        gate=None,
                        provision_argv=(),
                        provision_timeout_s=None,
                        state_root=self.state,
                    )

        runner.assert_not_called()
        self.assertEqual(TEST_PATH, caught.exception.path)

    def test_a_tampered_suite_refuses_before_the_runner_is_invoked(self):
        runner = mock.Mock(name="run_suite")
        with mock.patch.object(cr, "_review_tree", side_effect=self._tampering_tree()):
            with mock.patch.object(cr.tc, "run_suite", runner):
                with self.assertRaises(tb.TestSuiteTampered) as caught:
                    self._review("tampered")

        runner.assert_not_called()
        self.assertEqual(TEST_PATH, caught.exception.path)
        self.assertNotIn(SECRET_LITERAL, str(caught.exception))
        self.assertIsNotNone(cr.suite_environment_detail(caught.exception))


class TheIntegrationGateRunsTheAcceptedSuite(TheReviewRunsTheAcceptedSuite):
    """The run-level gate reads the same suite off the head and verifies it the same way.

    `run_integration_gate` is the site that matters most now that the suite
    lives in the integration head's own history: its `failed` flag is read by
    `FactoryScheduler._failed_run_gates`, which stands between a merged surface
    and both the final review and publication. An unverified gate there is a
    publication decision made about bytes nobody accepted.

    Inherits the fixture, not the assertions -- the two review sites are set up
    from the same accepted suite and the same repository.
    """

    def _gate(self, digest_label: str):
        return cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest(digest_label),
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=self.candidate_sha,
            accepted_suite=self.sealed,
            scratch_root=self.state / ("gate-" + digest_label),
        )

    def test_the_gate_reports_the_suite_digest_it_ran(self):
        with mock.patch.object(cr.tc, "run_suite", return_value=_GREEN_RUN):
            result = self._gate("gate-clean")
        files = tc.suite_files(self.sealed)

        self.assertEqual(tb.suite_digest(files), result["test_suite_digest"])
        self.assertFalse(result["failed"])

    def test_a_tampered_gate_tree_refuses_before_the_runner(self):
        runner = mock.Mock(name="run_suite")
        with mock.patch.object(cr, "_review_tree", side_effect=self._tampering_tree()):
            with mock.patch.object(cr.tc, "run_suite", runner):
                with self.assertRaises(tb.TestSuiteTampered) as caught:
                    self._gate("gate-tampered")

        runner.assert_not_called()
        self.assertEqual(TEST_PATH, caught.exception.path)
        self.assertNotIn(SECRET_LITERAL, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
