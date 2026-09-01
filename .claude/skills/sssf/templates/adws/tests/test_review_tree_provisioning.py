"""The review tree is provisioned before its sealed suite runs.

`_review_tree` used to be a bare source snapshot: it materialized a commit and
handed the tree straight to `run_private_suite`, with none of the candidate's
declared dependencies installed. A project whose conftest imports `bcrypt`
therefore collected zero cases, `runner_failed` read `executed < min_cases`,
and a reviewer's PASS was overridden to REVISE with "sealed private tests
failed, errored, or did not execute" -- blaming the builder for a suite that
never ran.

These cases pin the fix from the outside: the deployment's `provision_argv`
runs inside the review tree, after materialization and before the suite, on
both call sites; an absent `provision_argv` changes nothing; a provisioning
failure is a typed refusal rather than a verdict; and provisioning writes only
into the review tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
STAMP = "provision-stamp.json"
SECRET_LITERAL = "SECRET_EXPECTED_LITERAL_NEGATIVE_REFUND"
SECRET_SELECTOR = "test_refund_rejects_secret_negative"

PRODUCT = "def refund(amount):\n    return amount\n"
FIXED = (
    "def refund(amount):\n    if amount < 0:\n        return None\n    return amount\n"
)

TEST_PATH = "tests/test_refund_secret.py"

#: Asserts the provisioning stamp exists, so it can only pass if provisioning
#: ran *before* the suite. It reads the stamp by path from the test file rather
#: than from the process cwd, which the runner owns.
PROVISIONED_TEST_SOURCE = """\
import json
import pathlib

from refund import refund

_STAMP = pathlib.Path(__file__).resolve().parents[1] / "{stamp}"


def {selector}():
    stamp = json.loads(_STAMP.read_text())
    assert stamp["materialized"] is True
    assert stamp["sealed_present"] is False
    assert refund(-1) is None, "{literal}"
""".format(
    stamp=STAMP,
    selector=SECRET_SELECTOR,
    literal=SECRET_LITERAL,
)

#: No provisioning dependency at all: used for the "no provision_argv" case, so
#: an unprovisioned tree is expected to pass.
PLAIN_TEST_SOURCE = """\
from refund import refund


def {selector}():
    assert refund(-1) is None, "{literal}"
""".format(
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


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-hooks"))
    (repo / "refund.py").write_text(FIXED)
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


def _tree_bytes(root: Path) -> dict[str, str]:
    """Every file under `root`, by relative path, hashed."""
    out: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class ReviewTreeProvisioning(unittest.TestCase):
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

    # -- fixtures ---------------------------------------------------------

    def _seal(self, source: str, tag: str) -> st.LaneArtifact:
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("draft-" + tag),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={TEST_PATH: source},
            public_contract=CONTRACT,
            worktrees_root=self.worktrees / tag,
        )
        tokens = tc.draft_private_tokens(
            state_root=self.state, run_id=self.run_id, draft=draft
        )
        review = tc.review_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("review-" + tag),
            ),
            verdict=st.ReviewerVerdict.PASS,
            findings=(),
            test_draft=draft,
            private_tokens=tokens,
        )
        head = _git(self.repo, "rev-parse", "HEAD")
        builder = hv.linked_worktree(self.repo, self.root / ("builder-" + tag), head)
        return tc.seal_accepted_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("seal-" + tag),
            ),
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=review,
        )

    def _head(self) -> tuple[str, str]:
        sha = _git(self.repo, "rev-parse", "HEAD")
        ref = st.candidate_ref(self.run_id, self.lane_id, _digest("build-" + sha))
        _git(self.repo, "update-ref", ref, sha)
        return sha, ref

    def _provision_script(self, tag: str, *, exit_code: int = 0) -> tuple[str, ...]:
        """A provisioning command that records what the tree looked like.

        It stands in for `bun install` / `uv sync`: it runs in the review tree,
        writes into it, and reports what it could see. `materialized` proves the
        commit was extracted first; `sealed_present` proves the sealed blobs had
        not been copied in yet, which is the containment ordering.
        """
        script = self.root / ("provision-" + tag + ".py")
        script.write_text(
            "import json, pathlib, sys\n"
            "cwd = pathlib.Path.cwd()\n"
            "stamp = {\n"
            '    "materialized": (cwd / "refund.py").is_file(),\n'
            '    "sealed_present": (cwd / "%s").exists(),\n'
            '    "cwd": str(cwd),\n'
            "}\n"
            'if %d != 0:\n'
            '    sys.stderr.write("provisioner refused\\n")\n'
            "    sys.exit(%d)\n"
            'pathlib.Path("%s").write_text(json.dumps(stamp))\n'
            % (TEST_PATH, exit_code, exit_code, STAMP)
        )
        return (sys.executable, str(script))

    # -- B1/B5: provisioning runs, on both call sites ---------------------

    def test_review_builder_output_provisions_after_materialize_before_suite(self):
        sealed = self._seal(PROVISIONED_TEST_SOURCE, "cr")
        base = _git(self.repo, "rev-parse", "HEAD~0")
        sha, ref = self._head()
        scratch = self.root / "scratch-cr"

        artifact = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("code-review"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=scratch,
            architecture_constraints=CONSTRAINTS,
            provision_argv=self._provision_script("cr"),
        )

        # The sealed case asserts the stamp exists, so a PASS with a real
        # executed case is only reachable if provisioning ran before the suite.
        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        summary = artifact.payload["public_result_summary"]
        self.assertGreater(summary["passed"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["errored"], 0)

        stamps = list(scratch.glob("*/" + STAMP))
        self.assertEqual(len(stamps), 1, stamps)
        stamp = json.loads(stamps[0].read_text())
        self.assertTrue(stamp["materialized"], "provisioning ran before materialize")
        self.assertFalse(stamp["sealed_present"], "sealed blobs copied before provision")

    def test_integration_gate_provisions_after_materialize_before_suite(self):
        sealed = self._seal(PROVISIONED_TEST_SOURCE, "gate")
        head = _git(self.repo, "rev-parse", "HEAD")
        scratch = self.root / "scratch-gate"

        result = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest("integration-gate"),
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=self._provision_script("gate"),
        )

        self.assertFalse(result["failed"])
        self.assertGreaterEqual(result["executed"], result["min_cases"])

        stamps = list(scratch.glob("*/" + STAMP))
        self.assertEqual(len(stamps), 1, stamps)
        stamp = json.loads(stamps[0].read_text())
        self.assertTrue(stamp["materialized"])
        self.assertFalse(stamp["sealed_present"])

    # -- B2/B5: absent provision_argv changes nothing ---------------------

    def test_absent_provision_argv_attempts_no_provisioning(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "empty")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._head()

        with mock.patch.object(cr, "run_harness_process") as harness:
            artifact = cr.review_builder_output(
                request=_request(
                    run_id=self.run_id,
                    lane_id=self.lane_id,
                    input_digest=_digest("code-review-empty"),
                ),
                state_root=self.state,
                candidate_repo=self.repo,
                candidate_sha=sha,
                candidate_ref=ref,
                builder_base_sha=base,
                sealed_bundle=sealed,
                verdict=st.ReviewerVerdict.PASS,
                scratch_root=self.root / "scratch-empty",
                architecture_constraints=CONSTRAINTS,
            )
            harness.assert_not_called()

        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        self.assertGreater(artifact.payload["public_result_summary"]["passed"], 0)

    def test_blank_provision_argv_entries_attempt_no_provisioning(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "blank")
        head = _git(self.repo, "rev-parse", "HEAD")

        with mock.patch.object(cr, "run_harness_process") as harness:
            result = cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-blank"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-blank",
                provision_argv=("", ""),
            )
            harness.assert_not_called()

        self.assertFalse(result["failed"])

    # -- B3/B5: a provisioning failure is never a builder test failure ----

    def test_provisioning_failure_refuses_and_never_blames_the_builder(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "fail")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._head()

        with mock.patch.object(tc, "run_private_suite") as runner:
            with self.assertRaises(cr.ReviewProvisioningError) as ctx:
                cr.review_builder_output(
                    request=_request(
                        run_id=self.run_id,
                        lane_id=self.lane_id,
                        input_digest=_digest("code-review-fail"),
                    ),
                    state_root=self.state,
                    candidate_repo=self.repo,
                    candidate_sha=sha,
                    candidate_ref=ref,
                    builder_base_sha=base,
                    sealed_bundle=sealed,
                    verdict=st.ReviewerVerdict.PASS,
                    scratch_root=self.root / "scratch-fail",
                    architecture_constraints=CONSTRAINTS,
                    provision_argv=self._provision_script("fail", exit_code=3),
                )
            # No suite ran, so no verdict about the candidate exists to record.
            runner.assert_not_called()

        exc = ctx.exception
        self.assertEqual(exc.returncode, 3)
        self.assertIn("REVIEW_TREE_PROVISION_FAILED", str(exc))
        # The distinguishing property: this is not the runner-failure finding.
        self.assertNotIn(cr._RUNNER_REVISE["observed_behavior"], str(exc))
        self.assertNotIn("sealed private tests", str(exc))

    def test_provisioning_failure_refuses_the_integration_gate(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "gatefail")
        head = _git(self.repo, "rev-parse", "HEAD")

        with mock.patch.object(tc, "run_private_suite") as runner:
            with self.assertRaises(cr.ReviewProvisioningError):
                cr.run_integration_gate(
                    run_id=self.run_id,
                    lane_id=self.lane_id,
                    input_digest=_digest("integration-gate-fail"),
                    state_root=self.state,
                    integration_repo=self.repo,
                    integration_sha=head,
                    sealed_bundle=sealed,
                    scratch_root=self.root / "scratch-gatefail",
                    provision_argv=self._provision_script("gatefail", exit_code=3),
                )
            runner.assert_not_called()

    def test_missing_provisioning_executable_is_a_typed_refusal(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "absent")
        head = _git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(cr.ReviewProvisioningError) as ctx:
            cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-absent"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-absent",
                provision_argv=("maestro-no-such-provisioner",),
            )

        self.assertIsNone(ctx.exception.returncode)
        self.assertIsInstance(ctx.exception, pr.PrivateReviewError)

    # -- hard constraint: the sealed boundary survives --------------------

    def test_provisioning_refusal_carries_no_private_test_content(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "leak")
        head = _git(self.repo, "rev-parse", "HEAD")
        vault = hv.vault_path(self.state, self.run_id)

        with self.assertRaises(cr.ReviewProvisioningError) as ctx:
            cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-leak"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-leak",
                provision_argv=self._provision_script("leak", exit_code=3),
            )

        text = str(ctx.exception)
        self.assertNotIn(SECRET_LITERAL, text)
        self.assertNotIn(SECRET_SELECTOR, text)
        self.assertNotIn(str(vault), text)
        self.assertNotIn(sealed.artifact_ref, text)

    def test_provisioning_writes_only_into_the_review_tree(self):
        sealed = self._seal(PROVISIONED_TEST_SOURCE, "isolate")
        head = _git(self.repo, "rev-parse", "HEAD")
        vault = hv.vault_path(self.state, self.run_id)
        scratch = self.root / "scratch-isolate"

        repo_before = _tree_bytes(self.repo)
        vault_before = _tree_bytes(vault)

        result = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest("integration-gate-isolate"),
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=self._provision_script("isolate"),
        )

        self.assertFalse(result["failed"])
        # Provisioning did happen -- and it landed inside the review tree only.
        stamps = list(scratch.glob("*/" + STAMP))
        self.assertEqual(len(stamps), 1, stamps)
        self.assertEqual(_tree_bytes(self.repo), repo_before)
        self.assertEqual(_tree_bytes(vault), vault_before)
        self.assertEqual(
            json.loads(stamps[0].read_text())["cwd"],
            str(stamps[0].parent.resolve()),
        )

    # -- shell-form, multi-step provisioning ------------------------------

    def test_multi_step_shell_form_provision_argv_runs_faithfully(self):
        """`["bash","-lc", "a && b"]` -- a JS install *and* a Python sync.

        One `bun install --frozen-lockfile` cannot provision a Python
        subproject, so the declared command has to be able to be several steps
        over several manifests. `run_harness_process` hands argv to Popen as a
        list with no `shell=True` and no single-binary assumption, so the shell
        is just the first element. This runs the real bash rather than a stub:
        a stubbed subprocess cannot observe an argv parser.
        """
        # A subproject with its own manifest, the shape that motivated this.
        sub = self.repo / "services" / "api-gateway"
        sub.mkdir(parents=True)
        (sub / "pyproject.toml").write_text(
            '[project]\nname = "api-gateway"\nrequires-python = ">=3.12"\n'
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "subproject")

        sealed = self._seal(PROVISIONED_TEST_SOURCE, "shell")
        head = _git(self.repo, "rev-parse", "HEAD")
        scratch = self.root / "scratch-shell"
        stamp_writer = self.root / "provision-shell.py"
        stamp_writer.write_text(
            "import json, pathlib\n"
            "cwd = pathlib.Path.cwd()\n"
            "stamp = {\n"
            '    "materialized": (cwd / "refund.py").is_file(),\n'
            '    "sealed_present": (cwd / "%s").exists(),\n'
            '    "cwd": str(cwd),\n'
            "}\n"
            'pathlib.Path("%s").write_text(json.dumps(stamp))\n' % (TEST_PATH, STAMP)
        )
        script = " && ".join(
            (
                "test -f services/api-gateway/pyproject.toml",
                "cd services/api-gateway",
                'cd "$OLDPWD"',
                "{0} {1}".format(
                    shlex.quote(sys.executable), shlex.quote(str(stamp_writer))
                ),
            )
        )

        result = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest("integration-gate-shell"),
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=("bash", "-lc", script),
        )

        # Every step ran, in order, in the review tree: the sealed case only
        # passes if the last one wrote the stamp.
        self.assertFalse(result["failed"])
        stamps = list(scratch.glob("*/" + STAMP))
        self.assertEqual(len(stamps), 1, stamps)
        self.assertTrue(json.loads(stamps[0].read_text())["materialized"])

    def test_a_failing_step_inside_a_shell_form_command_still_refuses(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "shellfail")
        head = _git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(cr.ReviewProvisioningError) as ctx:
            cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-shellfail"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-shellfail",
                provision_argv=("bash", "-lc", "true && exit 7"),
            )

        self.assertEqual(ctx.exception.returncode, 7)

    # -- an environment failure is never a candidate defect ---------------

    def test_environment_failure_records_no_code_review_at_all(self):
        """The distinction the builder loop depends on.

        A builder cannot fix a broken install or an interpreter that does not
        satisfy `requires-python`. So an environment failure must not reach the
        recorded surface as a candidate defect: `review_builder_output` records
        nothing, rather than a CODE_REVIEW whose finding says the sealed tests
        failed.
        """
        sealed = self._seal(PLAIN_TEST_SOURCE, "envfail")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._head()
        recorded: list[object] = []

        with mock.patch.object(
            pr, "make_lane_artifact", side_effect=lambda **kw: recorded.append(kw)
        ):
            with self.assertRaises(cr.ReviewProvisioningError) as ctx:
                cr.review_builder_output(
                    request=_request(
                        run_id=self.run_id,
                        lane_id=self.lane_id,
                        input_digest=_digest("code-review-envfail"),
                    ),
                    state_root=self.state,
                    candidate_repo=self.repo,
                    candidate_sha=sha,
                    candidate_ref=ref,
                    builder_base_sha=base,
                    sealed_bundle=sealed,
                    verdict=st.ReviewerVerdict.PASS,
                    scratch_root=self.root / "scratch-envfail",
                    architecture_constraints=CONSTRAINTS,
                    provision_argv=self._provision_script("envfail", exit_code=3),
                )

        self.assertEqual(recorded, [])
        # It names the environment, and it is not the candidate-defect finding.
        self.assertIn("REVIEW_TREE_PROVISION_FAILED", str(ctx.exception))
        self.assertNotIn(cr._RUNNER_REVISE["observed_behavior"], str(ctx.exception))
        self.assertNotIn(
            cr._RUNNER_REVISE["violated_requirement"], str(ctx.exception)
        )

    def test_an_unusable_interpreter_is_not_demoted_to_a_candidate_defect(self):
        """The peer's `requires-python` refusal shares this boundary.

        `run_private_suite` raises `SEALED_SUITE_RUNNER_UNUSABLE` when no
        interpreter satisfies the project. That refusal has to propagate:
        converting it into `runner_failed` would blame the builder for the
        harness's Python version.
        """
        sealed = self._seal(PLAIN_TEST_SOURCE, "unusable")
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._head()

        unusable = pr.SealedEnvironmentError("SEALED_SUITE_RUNNER_UNUSABLE:pytest")
        with mock.patch.object(tc, "run_private_suite", side_effect=unusable):
            with self.assertRaises(pr.SealedEnvironmentError) as ctx:
                cr.review_builder_output(
                    request=_request(
                        run_id=self.run_id,
                        lane_id=self.lane_id,
                        input_digest=_digest("code-review-unusable"),
                    ),
                    state_root=self.state,
                    candidate_repo=self.repo,
                    candidate_sha=sha,
                    candidate_ref=ref,
                    builder_base_sha=base,
                    sealed_bundle=sealed,
                    verdict=st.ReviewerVerdict.PASS,
                    scratch_root=self.root / "scratch-unusable",
                    architecture_constraints=CONSTRAINTS,
                )

        self.assertIn("SEALED_SUITE_RUNNER_UNUSABLE", str(ctx.exception))
        self.assertNotIn(cr._RUNNER_REVISE["observed_behavior"], str(ctx.exception))

    # -- collection failure vs test failure: different actors -------------

    def _uncollectable(self, tag: str, *, break_at_base: bool) -> st.LaneArtifact:
        """A sealed suite whose import fails, optionally only at the candidate.

        `break_at_base` False means the module the suite imports is missing at
        the base commit too -- an undeclared dependency. True means the base
        imports fine and only the candidate broke it.
        """
        source = "import maestro_absent_dependency\n\n\ndef {0}():\n    assert True\n"
        return self._seal(source.format(SECRET_SELECTOR), tag)

    def test_an_undeclared_dependency_is_refused_not_blamed_on_the_builder(self):
        # The f50638ab shape: provisioning succeeds, the suite still cannot
        # import, and the module is missing at the base commit too.
        sealed = self._uncollectable("undeclared", break_at_base=False)
        base = _git(self.repo, "rev-parse", "HEAD")
        sha, ref = self._head()
        recorded: list[object] = []

        with mock.patch.object(
            pr, "make_lane_artifact", side_effect=lambda **kw: recorded.append(kw)
        ):
            with self.assertRaises(cr.SealedEnvironmentError) as ctx:
                cr.review_builder_output(
                    request=_request(
                        run_id=self.run_id,
                        lane_id=self.lane_id,
                        input_digest=_digest("code-review-undeclared"),
                    ),
                    state_root=self.state,
                    candidate_repo=self.repo,
                    candidate_sha=sha,
                    candidate_ref=ref,
                    builder_base_sha=base,
                    sealed_bundle=sealed,
                    verdict=st.ReviewerVerdict.PASS,
                    scratch_root=self.root / "scratch-undeclared",
                    architecture_constraints=CONSTRAINTS,
                )

        self.assertEqual(recorded, [])
        detail = str(ctx.exception)
        # Either shape is correct and both are non-blaming: the runner's own
        # refusal re-raised (its interpreter detail preserved), or the
        # counts-based refusal when a runner resolved but measured nothing.
        self.assertIsNotNone(cr.sealed_environment_detail(ctx.exception))
        self.assertNotIn(cr._RUNNER_REVISE["observed_behavior"], detail)
        self.assertNotIn(cr._COLLECTION_REVISE["observed_behavior"], detail)

    def test_the_typed_environment_boundary_covers_a_collection_refusal(self):
        self.assertIn(
            "SEALED_SUITE_NOT_COLLECTED",
            cr.sealed_environment_detail(cr.SealedSuiteNotCollectedError(4, "x")) or "",
        )

    def test_a_candidate_that_breaks_collection_still_gets_a_revise(self):
        """The real case must not be weakened.

        The suite imports the product module. The base imports it fine, so
        collection works there; the candidate makes it raise on import. That is
        the builder's doing and must come back as REVISE.
        """
        sealed = self._seal(PLAIN_TEST_SOURCE, "broke")
        base = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "refund.py").write_text('raise RuntimeError("boom at import")\n')
        _git(self.repo, "add", "refund.py")
        _git(self.repo, "commit", "-qm", "candidate breaks import")
        sha, ref = self._head()

        artifact = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("code-review-broke"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / "scratch-broke",
            architecture_constraints=CONSTRAINTS,
        )

        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        finding = artifact.payload["findings"][0]
        self.assertEqual(
            finding["observed_behavior"], cr._COLLECTION_REVISE["observed_behavior"]
        )
        # Named for what happened, not the generic "tests failed" wording.
        self.assertNotEqual(
            finding["observed_behavior"], cr._RUNNER_REVISE["observed_behavior"]
        )

    def test_a_suite_that_runs_and_fails_is_still_a_plain_revise(self):
        """The path sealed tests exist for is untouched."""
        sealed = self._seal(PLAIN_TEST_SOURCE, "genuine")
        base = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "refund.py").write_text(PRODUCT)  # accepts negatives again
        _git(self.repo, "add", "refund.py")
        _git(self.repo, "commit", "-qm", "candidate is wrong")
        sha, ref = self._head()

        artifact = cr.review_builder_output(
            request=_request(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("code-review-genuine"),
            ),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            sealed_bundle=sealed,
            verdict=st.ReviewerVerdict.PASS,
            scratch_root=self.root / "scratch-genuine",
            architecture_constraints=CONSTRAINTS,
        )

        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 1)
        self.assertEqual(
            artifact.payload["findings"][0]["observed_behavior"],
            cr._RUNNER_REVISE["observed_behavior"],
        )

    def test_collected_no_case_reads_only_a_total_absence_of_outcomes(self):
        zero = {"counts": dict(passed=0, failed=0, errored=0, skipped=0), "executed": 0}
        self.assertTrue(cr._collected_no_case(zero))
        for key in ("passed", "failed", "errored", "skipped"):
            run = {
                "counts": dict(passed=0, failed=0, errored=0, skipped=0),
                "executed": 1,
            }
            run["counts"][key] = 1
            self.assertFalse(cr._collected_no_case(run), key)

    # -- the provisioning timeout is a deployment setting -----------------

    def test_the_provisioning_timeout_is_config_driven(self):
        self.assertEqual(maestro._config_timeout(None, "t"), lch.PROVISION_TIMEOUT_S)
        self.assertEqual(maestro._config_timeout(5400, "t"), 5400.0)
        self.assertEqual(maestro._config_timeout(5400.5, "t"), 5400.5)
        for bad in (0, -1, "600", True, [600]):
            with self.subTest(value=bad):
                with self.assertRaises(maestro._MaestroConfigurationError):
                    maestro._config_timeout(bad, "provision_timeout_s")

    def test_the_configured_timeout_bounds_the_review_tree_run(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "timeout")
        head = _git(self.repo, "rev-parse", "HEAD")

        with mock.patch.object(cr, "run_harness_process") as harness:
            harness.return_value = subprocess.CompletedProcess([], 0, "", "")
            cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-timeout"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-timeout",
                provision_argv=("bun", "install"),
                provision_timeout_s=5400.0,
            )

        self.assertEqual(harness.call_args.kwargs["timeout"], 5400.0)

    def test_an_absent_timeout_keeps_the_default(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "deftimeout")
        head = _git(self.repo, "rev-parse", "HEAD")

        with mock.patch.object(cr, "run_harness_process") as harness:
            harness.return_value = subprocess.CompletedProcess([], 0, "", "")
            cr.run_integration_gate(
                run_id=self.run_id,
                lane_id=self.lane_id,
                input_digest=_digest("integration-gate-deftimeout"),
                state_root=self.state,
                integration_repo=self.repo,
                integration_sha=head,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-deftimeout",
                provision_argv=("bun", "install"),
            )

        self.assertEqual(
            harness.call_args.kwargs["timeout"], lch.PROVISION_TIMEOUT_S
        )

    # -- B4: materialization destroys any provisioned state ---------------

    def test_reused_review_tree_is_reprovisioned(self):
        """`refresh_materialized_commit` unlinks every child of the tree.

        That is why provisioning is keyed to materialization rather than
        cached inside the tree: on the second review the stamp -- and any
        installed dependency beside it -- is gone before the suite runs.
        """
        sealed = self._seal(PROVISIONED_TEST_SOURCE, "reuse")
        head = _git(self.repo, "rev-parse", "HEAD")
        scratch = self.root / "scratch-reuse"
        digest = _digest("integration-gate-reuse")
        argv = self._provision_script("reuse")

        first = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=digest,
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=argv,
        )
        self.assertFalse(first["failed"])
        tree = next(iter(scratch.iterdir()))
        self.assertTrue((tree / STAMP).is_file())

        # Same dest: the second call takes the refresh branch, which wipes the
        # stamp. Without re-provisioning the sealed case cannot pass.
        second = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=digest,
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=argv,
        )
        self.assertFalse(second["failed"])
        self.assertTrue((tree / STAMP).is_file())

    def test_reused_review_tree_without_reprovisioning_would_fail(self):
        """The falsifier for the case above: a tree refreshed and left bare.

        If provisioning were cached rather than re-run, this is exactly the
        state the second review would execute against.
        """
        sealed = self._seal(PROVISIONED_TEST_SOURCE, "bare")
        head = _git(self.repo, "rev-parse", "HEAD")
        scratch = self.root / "scratch-bare"

        result = cr.run_integration_gate(
            run_id=self.run_id,
            lane_id=self.lane_id,
            input_digest=_digest("integration-gate-bare"),
            state_root=self.state,
            integration_repo=self.repo,
            integration_sha=head,
            sealed_bundle=sealed,
            scratch_root=scratch,
            provision_argv=(),
        )

        self.assertTrue(result["failed"])

    # -- collision detection stays unprovisioned --------------------------

    def test_collision_detection_does_not_provision(self):
        sealed = self._seal(PLAIN_TEST_SOURCE, "collide")
        sha, _ref = self._head()

        with mock.patch.object(cr, "run_harness_process") as harness:
            cr.detect_candidate_private_collisions(
                request=_request(
                    run_id=self.run_id,
                    lane_id=self.lane_id,
                    input_digest=_digest("collide"),
                ),
                state_root=self.state,
                candidate_repo=self.repo,
                candidate_sha=sha,
                sealed_bundle=sealed,
                scratch_root=self.root / "scratch-collide",
            )
            harness.assert_not_called()


class OperatorErrorBoundary(unittest.TestCase):
    """An environment fault leaves `main()` as typed JSON, not a traceback.

    Nothing between the review call and `main()` catches `PrivateReviewError`:
    the only `except` around `cr.review_builder_output` is
    `PrivatePathCollisionError`, and `scheduler.run()` catches only
    `KeyboardInterrupt`. So the operator boundary is the one place this can be
    typed, and these cases pin that it is.
    """

    def test_nothing_between_the_review_and_main_catches_it(self):
        # `scheduler.py` wraps `cr.review_builder_output` in exactly one
        # `except prv.PrivatePathCollisionError`. An environment fault must not
        # be a member of that type, or it would be recorded as a test
        # invalidation instead of reaching the operator.
        exc = cr.ReviewProvisioningError(("bun", "install"), 1)
        self.assertNotIsInstance(exc, pr.PrivatePathCollisionError)
        self.assertNotIsInstance(exc, pr.IsolationError)
        self.assertIsInstance(exc, cr.SealedEnvironmentError)

    def _main_with(self, exc: BaseException) -> tuple[int, dict]:
        buffer = io.StringIO()
        with mock.patch.object(maestro, "build_parser") as parser:
            args = SimpleNamespace(
                command="start", plan_name=None, handler=mock.Mock(side_effect=exc)
            )
            parser.return_value = SimpleNamespace(
                parse_args=lambda raw: args, error=lambda msg: None
            )
            with contextlib.redirect_stdout(buffer):
                code = maestro.main([])
        return code, json.loads(buffer.getvalue())

    def test_a_provisioning_failure_exits_with_the_typed_outcome(self):
        exc = cr.ReviewProvisioningError(("bun", "install"), 3, "lockfile is stale")

        code, emitted = self._main_with(exc)

        self.assertEqual(code, 3)
        self.assertEqual(emitted["outcome"], cr.SEALED_ENVIRONMENT_OUTCOME)
        self.assertIn("REVIEW_TREE_PROVISION_FAILED", emitted["detail"])
        self.assertIn("bun install", emitted["detail"])
        self.assertIn("lockfile is stale", emitted["detail"])
        self.assertIn("builder cannot fix it", emitted["detail"])
        self.assertNotIn(cr._RUNNER_REVISE["observed_behavior"], emitted["detail"])

    def test_an_unsatisfiable_interpreter_exits_with_the_typed_outcome(self):
        # The message worker-runner raises: resolved invocation, measured
        # version, the specifier, and the declaring file all have to survive.
        message = (
            "SEALED_SUITE_PYTHON_UNSUPPORTED:pytest:"
            "/usr/local/bin/python3.11 -m pytest:3.11.9:>=3.12:"
            "services/api-gateway/pyproject.toml"
        )

        code, emitted = self._main_with(pr.SealedEnvironmentError(message))

        self.assertEqual(code, 3)
        self.assertEqual(emitted["outcome"], cr.SEALED_ENVIRONMENT_OUTCOME)
        for fragment in (
            "/usr/local/bin/python3.11 -m pytest",
            "3.11.9",
            ">=3.12",
            "services/api-gateway/pyproject.toml",
        ):
            self.assertIn(fragment, emitted["detail"])
        self.assertIn("never executed", emitted["detail"])

    def test_recognition_is_by_class_not_by_the_code_a_message_opens_with(self):
        """A code this module has never heard of is still typed.

        The reason the prefix match had to go: it recognised four names, and a
        fifth arriving anywhere in the runner chain would have fallen through to
        a traceback. Class membership has no such list to fall out of date.
        """
        unknown = pr.SealedEnvironmentError("A_CODE_THIS_MODULE_NEVER_HEARD_OF:x")
        self.assertIsNotNone(cr.sealed_environment_detail(unknown))

        exit_code, emitted = self._main_with(unknown)
        self.assertEqual(exit_code, 3)
        self.assertEqual(emitted["outcome"], cr.SEALED_ENVIRONMENT_OUTCOME)

    def test_every_environment_code_the_runner_chain_raises_is_typed(self):
        # Including the two that postdate the deleted prefix list.
        for code_name in (
            "REVIEW_TREE_PROVISION_FAILED",
            "SEALED_SUITE_PYTHON_UNSUPPORTED",
            "SEALED_SUITE_RUNNER_UNUSABLE",
            "SEALED_SUITE_RUNNER_AMBIGUOUS",
            "SEALED_SUITE_COUNTS_UNPARSEABLE",
            "SEALED_SUITE_ALL_CASES_SKIPPED",
        ):
            with self.subTest(code=code_name):
                exit_code, emitted = self._main_with(
                    pr.SealedEnvironmentError(code_name + ":detail")
                )
                self.assertEqual(exit_code, 3)
                self.assertEqual(emitted["outcome"], cr.SEALED_ENVIRONMENT_OUTCOME)
                self.assertIn(code_name, emitted["detail"])

    def test_the_outcome_string_is_the_base_class_code(self):
        # One definition. A rename in private_review moves both together.
        self.assertEqual(
            cr.SEALED_ENVIRONMENT_OUTCOME, pr.SealedEnvironmentError.code
        )

    def test_both_review_refusals_are_that_one_class(self):
        self.assertIsInstance(
            cr.ReviewProvisioningError(("bun",), 1), pr.SealedEnvironmentError
        )
        self.assertIsInstance(
            cr.SealedSuiteNotCollectedError(4, "x"), pr.SealedEnvironmentError
        )

    def test_a_contract_refusal_is_not_relabelled_as_an_environment_fault(self):
        # Every other PrivateReviewError names a factory invariant. Mapping
        # those to an environment outcome would tell the operator to go install
        # something, which would be a lie.
        with self.assertRaises(pr.PrivateReviewError):
            self._main_with(pr.PrivateReviewError("code review requires SEALED"))

        self.assertIsNone(
            cr.sealed_environment_detail(
                pr.PrivateReviewError("sealed bundle has no private tests")
            )
        )
        self.assertIsNone(cr.sealed_environment_detail(RuntimeError("unrelated")))

    def test_the_typed_detail_carries_no_sealed_file_name(self):
        # The detail reaches the operator's console. Provisioning runs before
        # any sealed blob is copied in, so its stderr cannot name one -- this
        # pins that the boundary does not add one either.
        exc = cr.ReviewProvisioningError(("uv", "sync"), 1, "resolution failed")

        _code, emitted = self._main_with(exc)

        self.assertNotIn(TEST_PATH, emitted["detail"])
        self.assertNotIn(SECRET_SELECTOR, emitted["detail"])
        self.assertNotIn(SECRET_LITERAL, emitted["detail"])


class HarnessArgvFidelity(unittest.TestCase):
    """`run_harness_process` is argv-faithful; it assumes no single binary."""

    def test_argv_reaches_the_process_unmodified(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lch.run_harness_process(
                (
                    "bash",
                    "-lc",
                    'printf "%s|" "$0" "$@"; echo "cwd=$PWD"',
                    "first arg",
                    "second && arg",
                ),
                cwd=Path(tmp),
                timeout=60,
            )

        self.assertEqual(result.returncode, 0)
        # No shell=True, no re-quoting, no word splitting of our arguments.
        self.assertIn("first arg|second && arg|", result.stdout)
        self.assertIn("cwd=", result.stdout)

    def test_a_chained_shell_command_reports_the_failing_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = lch.run_harness_process(
                ("bash", "-lc", "set -e; echo one; exit 9; echo two"),
                cwd=Path(tmp),
                timeout=60,
            )

        self.assertEqual(result.returncode, 9)
        self.assertIn("one", result.stdout)
        self.assertNotIn("two", result.stdout)


class SchedulerProvisionArgvWiring(unittest.TestCase):
    """B2: the deployment's `provision_argv` actually reaches both call sites.

    `HerdrLauncher` already carries it for agent worktrees. These pin the two
    things that make the review-tree path work: the attribute chain is real on
    the production classes, and the scheduler forwards what it resolved.
    """

    def test_the_attribute_chain_exists_on_the_production_classes(self):
        # Not a fake. A double proves a field is permitted, never that the
        # object the run is built with actually carries it.
        self.assertIn("provision_argv", lch.HerdrLauncher.__init__.__code__.co_varnames)
        self.assertIn("launcher", maestro.HerdrStageActor.__init__.__code__.co_varnames)

        launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
        launcher.provision_argv = ("bun", "install", "--frozen-lockfile")
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.launcher = launcher

        self.assertEqual(
            sch._resolved_provision_argv(actor, None),
            ("bun", "install", "--frozen-lockfile"),
        )

    def test_an_actor_without_a_launcher_resolves_to_no_provisioning(self):
        self.assertEqual(sch._resolved_provision_argv(object(), None), ())

    def test_an_explicit_argument_overrides_the_launcher(self):
        actor = SimpleNamespace(launcher=SimpleNamespace(provision_argv=("bun",)))
        self.assertEqual(sch._resolved_provision_argv(actor, ("uv", "sync")), ("uv", "sync"))
        self.assertEqual(sch._resolved_provision_argv(actor, ()), ())

    def test_run_gate_forwards_the_resolved_argv(self):
        scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
        scheduler.run_id = "run1"
        scheduler._provision_argv = ("bun", "install", "--frozen-lockfile")
        scheduler._provision_timeout_s = 1800.0
        scheduler.runtime = SimpleNamespace(path=Path("/state"))
        scheduler.target = SimpleNamespace(target_repository_root="/repo")
        lane = SimpleNamespace(lane_id="lane-a")
        scheduler._run_gate_lanes = lambda lanes: (lane,)
        scheduler._current_tests_sealed = lambda lane_id: SimpleNamespace(
            payload={}, artifact_id="a1"
        )
        scheduler._sealed_suite_gate = lambda lane: None

        with mock.patch.object(sch, "_record_as_lane_artifact", return_value=None):
            with mock.patch.object(
                sch.cr, "run_integration_gate", return_value={"failed": False}
            ) as gate:
                scheduler._failed_run_gates((lane,), "0" * 40, "f" * 64)

        self.assertEqual(
            gate.call_args.kwargs["provision_argv"],
            ("bun", "install", "--frozen-lockfile"),
        )
        self.assertEqual(gate.call_args.kwargs["provision_timeout_s"], 1800.0)

    def test_reviewing_code_forwards_the_resolved_argv(self):
        scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
        scheduler.run_id = "run1"
        scheduler.store = object()
        scheduler._provision_argv = ("uv", "sync", "--frozen")
        scheduler._provision_timeout_s = 1800.0
        scheduler.runtime = SimpleNamespace(path=Path("/state"))
        scheduler.target = SimpleNamespace(target_repository_root="/repo")
        scheduler.actor = SimpleNamespace(
            review_code=lambda ctx: (st.ReviewerVerdict.PASS, ())
        )
        lane = SimpleNamespace(
            lane_id="lane-a",
            spec_digest=_digest("spec"),
            lane_projection_digest=_digest("projection"),
            public_acceptance=("negative amounts are refused",),
            declared_outputs=(TEST_PATH,),
            lane_kind=st.LANE_KIND_BUILD,
            needs=(),
        )
        row = {"plan_revision": 1, "plan_digest": _digest("plan")}
        artifact = SimpleNamespace(
            artifact_id="art-1",
            payload={
                "builder_base_sha": "1" * 40,
                "candidate_ref": st.candidate_ref("run1", "lane-a", _digest("b")),
                "candidate_sha": "2" * 40,
                "sealed_digest": "3" * 64,
            },
        )
        scheduler._common = lambda lane_id: (row, lane)
        scheduler._sealed_for = lambda lane_arg: artifact
        scheduler._plan_artifact_ref = lambda row_arg: "plan:ref"
        scheduler._sealed_suite_gate = lambda lane_arg: None

        with mock.patch.object(sch, "_latest", return_value=artifact), mock.patch.object(
            sch, "_record_as_lane_artifact", return_value=None
        ), mock.patch.object(
            sch, "_with_input_artifact_ids", side_effect=lambda art, ids: art
        ), mock.patch.object(
            sch, "_complete"
        ), mock.patch.object(
            sch.cr, "review_builder_output", return_value=None
        ) as review:
            scheduler._reviewing_code("lane-a")

        self.assertEqual(
            review.call_args.kwargs["provision_argv"], ("uv", "sync", "--frozen")
        )
        self.assertEqual(review.call_args.kwargs["provision_timeout_s"], 1800.0)


if __name__ == "__main__":
    unittest.main()
