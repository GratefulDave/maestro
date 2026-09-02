"""The builder can measure its own working tree against the sealed suite.

`sealed_probe` exists because the builder's checkout holds every test file
except the one that decides its lane, so each round was a blind guess followed
by a three-minute wait and another model's prose about one redacted line.

These cases pin the parts that are this module's own work rather than the
review path's:

* the working-tree overlay -- modified, untracked, deleted and renamed paths
  all reach the tree that gets measured, which is what makes the probe usable
  before a commit exists;
* the scratch tree is created outside the checkout and removed afterwards;
* the printed text carries neither the scratch path nor the vault path, and a
  refusal from `refuse_private_leak` withholds the lines rather than widening
  anything to make them print;
* `resolve_lane` reads the *current* sealed bundle for the lane out of the
  ledger, across the build-lane-needs-tests-lane edge.

The suite runner is stubbed here on purpose: what the real runners do with
their own argv, and that the probe leaves the checkout byte-identical, are
proved elsewhere against the real binaries.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import sealed_probe as sp  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules.lifecycle import ArtifactStore, now_iso  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_refund_secret.py"
SECRET_LITERAL = "SECRET_EXPECTED_LITERAL_NEGATIVE_REFUND"
SECRET_SELECTOR = "test_refund_rejects_secret_negative"

TEST_SOURCE = """\
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
    (repo / "refund.py").write_text("def refund(amount):\n    return amount\n")
    (repo / "doomed.py").write_text("DOOMED = 1\n")
    (repo / "old.py").write_text("OLD = 1\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.py").write_text("KEEP = 1\n")
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


def _fake_run(output: str = "", *, executed: int = 2, failed: int = 1) -> dict:
    return {
        "counts": {
            "passed": executed - failed,
            "failed": failed,
            "errored": 0,
            "skipped": 0,
        },
        "executed": executed,
        "min_cases": 1,
        "output": output,
        "returncode": 1 if failed else 0,
        "runner": "pytest",
    }


class OverlayWorkingTree(unittest.TestCase):
    """A materialized HEAD plus the working tree, without needing a commit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _repo(self.root)
        self.tree = self.root / "tree"
        head = _git(self.repo, "rev-parse", "HEAD")
        hv.materialize_commit(self.repo, head, self.tree)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overlay_carries_every_uncommitted_shape(self):
        (self.repo / "refund.py").write_text("def refund(amount):\n    return None\n")
        (self.repo / "doomed.py").unlink()
        _git(self.repo, "mv", "old.py", "new.py")
        (self.repo / "untracked.py").write_text("UNTRACKED = 1\n")
        (self.repo / "udir").mkdir()
        (self.repo / "udir" / "nested.py").write_text("NESTED = 1\n")

        sp.overlay_working_tree(self.repo, self.tree)

        self.assertEqual(
            (self.tree / "refund.py").read_text(),
            "def refund(amount):\n    return None\n",
            "a modified file did not reach the measured tree",
        )
        self.assertFalse(
            (self.tree / "doomed.py").exists(), "a deleted file survived the overlay"
        )
        self.assertFalse(
            (self.tree / "old.py").exists(), "a rename left its original path behind"
        )
        self.assertEqual((self.tree / "new.py").read_text(), "OLD = 1\n")
        self.assertEqual((self.tree / "untracked.py").read_text(), "UNTRACKED = 1\n")
        self.assertEqual(
            (self.tree / "udir" / "nested.py").read_text(),
            "NESTED = 1\n",
            "an untracked directory was reported as one entry and not walked",
        )
        # Untouched tracked files come from the materialized commit, not the
        # overlay, so the overlay must not have disturbed them.
        self.assertEqual((self.tree / "sub" / "keep.py").read_text(), "KEEP = 1\n")

    def test_overlay_reports_both_halves_of_a_rename(self):
        _git(self.repo, "mv", "old.py", "new.py")
        self.assertEqual(
            sorted(sp.worktree_paths(self.repo)), ["new.py", "old.py"]
        )

    def test_overlay_refuses_a_path_outside_the_tree(self):
        with mock.patch.object(sp, "worktree_paths", return_value=("../escape.py",)):
            sp.overlay_working_tree(self.repo, self.tree)
        self.assertFalse((self.root / "escape.py").exists())


class SealedFixture(unittest.TestCase):
    """A real vault and sealed bundle, with the suite runner stubbed."""

    run_id = "run-probe"
    lane_id = "lane-tests"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _repo(self.root)
        self.state = self.root / "state"
        self.worktrees = self.root / "worktrees"
        self.sealed = self._seal("seal")
        self.vault = hv.ensure_vault(self.state, self.run_id)
        self.files = tc.sealed_private_files(self.vault, self.sealed)
        head = _git(self.repo, "rev-parse", "HEAD")
        self.checkout = hv.linked_worktree(self.repo, self.root / "checkout", head)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seal(self, tag: str, source: str = TEST_SOURCE) -> st.LaneArtifact:
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

    def _probe(self, *, output: str = "", scratch_parent: Path | None = None):
        """Run the probe with the suite stubbed, recording what it was handed."""
        seen: dict = {}

        def fake_suite(tree, files, *, gate, runtime_root):
            tree = Path(tree)
            seen["tree"] = tree
            seen["gate"] = gate
            seen["runtime_root"] = Path(runtime_root)
            seen["sealed_present"] = (tree / TEST_PATH).is_file()
            seen["materialized"] = (tree / "sub" / "keep.py").is_file()
            seen["refund"] = (tree / "refund.py").read_text()
            seen["untracked"] = (tree / "candidate.py").is_file()
            return _fake_run(output), None

        with mock.patch.object(cr, "_run_sealed_suite", side_effect=fake_suite):
            result = sp.probe_tree(
                self.checkout,
                repo=self.repo,
                vault=self.vault,
                private_files=self.files,
                gate=None,
                provision_argv=(),
                provision_timeout_s=None,
                runtime_root=self.repo,
                scratch_parent=scratch_parent,
                run_id=self.run_id,
                lane_id=self.lane_id,
                sealed_ref=self.sealed.artifact_ref,
                sealed_digest=self.sealed.payload["sealed_digest"],
            )
        return result, seen


class ProbeMeasuresTheWorkingTree(SealedFixture):
    def test_measured_tree_is_head_plus_the_uncommitted_edit_plus_the_seal(self):
        (self.checkout / "refund.py").write_text(
            "def refund(amount):\n    return None\n"
        )
        (self.checkout / "candidate.py").write_text("CANDIDATE = 1\n")

        result, seen = self._probe()

        self.assertTrue(seen["materialized"], "HEAD was not materialized")
        self.assertEqual(
            seen["refund"],
            "def refund(amount):\n    return None\n",
            "the uncommitted edit did not reach the measured tree",
        )
        self.assertTrue(seen["untracked"], "the untracked new file was not overlaid")
        self.assertTrue(seen["sealed_present"], "the sealed blob was not copied in")
        self.assertEqual(result.counts["executed"], 2)
        self.assertEqual(result.counts["failed"], 1)
        self.assertEqual(result.counts["passed"], 1)

    def test_scratch_tree_is_outside_the_checkout_and_is_removed(self):
        parent = self.root / "scratch"
        _result, seen = self._probe(scratch_parent=parent)

        tree = seen["tree"]
        self.assertFalse(
            sp._is_inside(tree, self.checkout),
            "the probe measured inside the checkout it was measuring",
        )
        self.assertTrue(sp._is_inside(tree, parent))
        self.assertFalse(tree.exists(), "the scratch tree outlived the probe")
        self.assertEqual(
            list(parent.iterdir()), [], "the scratch parent was left dirty"
        )

    def test_default_scratch_parent_is_not_under_the_checkout(self):
        _result, seen = self._probe()
        self.assertFalse(sp._is_inside(seen["tree"], self.checkout))

    def test_the_probe_adds_no_path_to_the_checkout(self):
        """No stray path is created in the checkout, not even a marker.

        The factory runs `git clean -fdx` on the builder checkout between
        rounds and commits untracked paths falling under a declared output
        (`_clean_checkout` and `_commit_declared`, maestro.py). A file the
        probe drops there is therefore either destroyed or committed as the
        builder's work.

        This is a weaker statement than it looks and is not the scratch-
        placement guard: a scratch tree created *inside* the checkout and then
        removed would leave this path set intact. What proves placement is the
        tree observed mid-run in
        `test_scratch_tree_is_outside_the_checkout_and_is_removed`.
        """
        (self.checkout / "candidate.py").write_text("CANDIDATE = 1\n")
        before = {
            path.relative_to(self.checkout).as_posix()
            for path in self.checkout.rglob("*")
            if ".git" not in path.parts
        }

        self._probe()

        after = {
            path.relative_to(self.checkout).as_posix()
            for path in self.checkout.rglob("*")
            if ".git" not in path.parts
        }
        self.assertEqual(after, before, "the probe left a path in the checkout")

    def test_failure_lines_are_redacted_and_name_no_private_path(self):
        output = "\n".join(
            [
                "E   AssertionError: {0}".format(SECRET_LITERAL),
                "E   ValueError: refund returned a value",
            ]
        )
        result, seen = self._probe(output=output)

        rendered = sp.render(result)
        self.assertNotIn(SECRET_LITERAL, rendered)
        self.assertNotIn(str(self.vault), rendered)
        self.assertNotIn(str(seen["tree"]), rendered)
        self.assertNotIn(SECRET_SELECTOR, rendered)
        self.assertIn("ValueError", rendered)
        self.assertFalse(result.withheld)

    def test_render_prints_the_five_counts_and_nothing_else(self):
        result, _seen = self._probe()
        lines = sp.render(result).splitlines()
        self.assertEqual(
            lines,
            [
                "executed: 2",
                "passed: 1",
                "failed: 1",
                "errored: 0",
                "skipped: 0",
            ],
        )

    def test_a_refused_leak_check_withholds_the_lines_and_keeps_the_counts(self):
        with mock.patch.object(
            sp.pr,
            "refuse_private_leak",
            side_effect=pr.PrivateLeakError("public payload leaked private token"),
        ):
            result, _seen = self._probe(output="E   AssertionError: boom")

        self.assertTrue(result.withheld)
        self.assertEqual(result.failure_lines, ())
        rendered = sp.render(result)
        self.assertIn(sp.WITHHELD_LINE, rendered)
        self.assertNotIn("AssertionError", rendered)
        self.assertIn("failed: 1", rendered)

    def test_a_colliding_working_tree_refuses_instead_of_measuring(self):
        """A candidate occupying a sealed path cannot pass, so it cannot be green.

        `measure_candidate` refuses this candidate outright. A probe that let
        the sealed blob overwrite the builder's file and reported the suite
        result would hand back a green reading of a candidate the factory is
        about to refuse, which is the wrong-oracle defect the probe exists to
        remove.
        """
        colliding = self.checkout / TEST_PATH
        colliding.parent.mkdir(parents=True, exist_ok=True)
        colliding.write_text("def test_i_wrote_this_myself():\n    assert True\n")

        result, seen = self._probe()

        self.assertTrue(result.collision)
        self.assertEqual(seen, {}, "the suite ran despite the collision")
        rendered = sp.render(result)
        self.assertEqual(rendered, sp.COLLISION_LINE)
        self.assertNotIn(TEST_PATH, rendered)
        self.assertNotIn("test_refund_secret", rendered)
        self.assertNotIn(SECRET_LITERAL, rendered)
        self.assertNotIn(SECRET_SELECTOR, rendered)
        self.assertNotIn(str(self.vault), rendered)

    def test_an_empty_sealed_bundle_is_refused(self):
        with self.assertRaises(sp.ProbeRefused):
            sp.probe_tree(
                self.checkout,
                repo=self.repo,
                vault=self.vault,
                private_files={},
                gate=None,
                provision_argv=(),
                provision_timeout_s=None,
                runtime_root=self.repo,
            )


class ResolveLane(SealedFixture):
    """The ledger, not the registry, decides which bundle binds the lane."""

    build_lane = "lane-build"

    def _plan_document(self) -> dict:
        return {
            "schema_version": "maestro-plan.artifact-factory.v1",
            "lanes": [
                {
                    "id": self.lane_id,
                    "lane_kind": "tests",
                    "needs": [],
                    "outputs": [TEST_PATH],
                    "spec": {
                        "goal": "seal the refund cases",
                        "integration": {"integration_branch": INTEGRATION_REF},
                        "gate": {
                            "runner": "pytest",
                            "argv": [TEST_PATH],
                            "cwd": ".",
                            "min_cases": 1,
                        },
                    },
                    "acceptance": ["negative amounts are refused"],
                },
                {
                    "id": self.build_lane,
                    "lane_kind": "build",
                    "needs": [self.lane_id],
                    "outputs": ["refund.py"],
                    "spec": {
                        "goal": "refuse negative refunds",
                        "integration": {"integration_branch": INTEGRATION_REF},
                    },
                    "acceptance": ["negative amounts are refused"],
                },
            ],
        }

    def _ledger(self) -> tuple[Path, ArtifactStore]:
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(
            json.dumps(
                self._plan_document(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        compiled = plan_compiler.compile_plan(
            plan_path.read_bytes(), plan_revision=1, plan_artifact_ref=str(plan_path)
        )
        self.compiled = compiled
        main = _digest("main")[:40]
        binding = st.RunBinding(
            runtime_state_root=str(self.state),
            runtime_state_fingerprint=_digest("state"),
            integration_ref=st.integration_ref(self.run_id),
            integration_initial_sha=main,
            target_repository_root=str(self.repo),
            target_git_common_dir=str(self.repo / ".git"),
            target_worktree_git_dir=str(self.repo / ".git"),
            target_object_format="sha1",
            target_repository_fingerprint=_digest("repo"),
            target_sync_journal_fingerprint=_digest("journal"),
            target_initial_main_sha=main,
            target_main_ref=INTEGRATION_REF,
        )
        database = self.state / "lifecycle.sqlite3"
        store = ArtifactStore(database)
        store.create_run(self.run_id, compiled, binding)
        return database, store

    def _insert_bundle(
        self,
        store: ArtifactStore,
        sealed: st.LaneArtifact,
        *,
        sequence: int,
        tag: str,
    ) -> None:
        lane = next(
            lane for lane in self.compiled.lanes if lane.lane_id == self.lane_id
        )
        store.conn.execute(
            "INSERT INTO lane_artifacts(artifact_id, run_id, lane_id, sequence, "
            "completed_stage, artifact_kind, plan_revision, spec_digest, "
            "lane_projection_digest, input_digest, output_digest, artifact_ref, "
            "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _digest("artifact:" + tag),
                self.run_id,
                self.lane_id,
                sequence,
                st.LaneStage.TESTS_SEALED.value,
                st.ArtifactKind.SEALED_TEST_BUNDLE.value,
                1,
                lane.spec_digest,
                lane.lane_projection_digest,
                sealed.input_digest,
                sealed.output_digest,
                sealed.artifact_ref,
                json.dumps(st.json_ready(sealed.payload), sort_keys=True),
                now_iso(),
            ),
        )

    def _registry(self, database: Path) -> Path:
        other = self.root / "other" / "lifecycle.sqlite3"
        ArtifactStore(other).close()
        path = self.root / "registry.json"
        path.write_text(
            json.dumps(
                {
                    "installations": [
                        {
                            "database": str(other),
                            "plans_dir": str(self.root),
                            "repository": str(self.root / "other-repo"),
                            "state": str(self.root / "other"),
                        },
                        {
                            "database": str(database),
                            "plans_dir": str(self.root),
                            "repository": str(self.repo),
                            "state": str(self.state),
                        },
                    ]
                }
            )
        )
        return path

    def test_installation_for_run_skips_a_ledger_that_lacks_the_run(self):
        database, store = self._ledger()
        store.close()
        registry = self._registry(database)

        entry = sp.installation_for_run(self.run_id, registry_path=registry)

        self.assertEqual(entry["database"], str(database))
        self.assertEqual(entry["repository"], str(self.repo))

    def test_an_unknown_run_is_refused_without_naming_a_path(self):
        database, store = self._ledger()
        store.close()
        registry = self._registry(database)

        with self.assertRaises(sp.ProbeRefused) as caught:
            sp.installation_for_run("run-absent", registry_path=registry)
        self.assertNotIn(str(self.state), str(caught.exception))

    def test_resolve_lane_takes_the_current_bundle_across_the_build_edge(self):
        stale = self._seal("stale", TEST_SOURCE.replace("refund(-1)", "refund(-2)"))
        database, store = self._ledger()
        self._insert_bundle(store, stale, sequence=1, tag="stale")
        self._insert_bundle(store, self.sealed, sequence=2, tag="current")
        store.close()
        registry = self._registry(database)

        with mock.patch.object(
            sp, "_provisioning", return_value=(("install",), 42.0)
        ):
            binding = sp.resolve_lane(
                self.run_id, self.build_lane, registry_path=registry
            )

        self.assertEqual(binding.sealed_ref, self.sealed.artifact_ref)
        self.assertNotEqual(binding.sealed_ref, stale.artifact_ref)
        self.assertEqual(dict(binding.private_files), dict(self.files))
        self.assertEqual(binding.lane_id, self.lane_id)
        self.assertEqual(binding.repo, self.repo)
        self.assertEqual(binding.runtime_root, self.repo)
        self.assertEqual(binding.provision_argv, ("install",))
        self.assertEqual(binding.provision_timeout_s, 42.0)

    def test_the_gate_comes_from_the_tests_lane_the_build_lane_needs(self):
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)

        with mock.patch.object(sp, "_provisioning", return_value=((), None)):
            binding = sp.resolve_lane(
                self.run_id, self.build_lane, registry_path=registry
            )

        self.assertEqual(binding.gate.runner, "pytest")
        self.assertEqual(binding.gate.argv, (TEST_PATH,))
        self.assertEqual(binding.gate.min_cases, 1)

    def test_an_edited_plan_artifact_is_refused_rather_than_measured(self):
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)
        document = self._plan_document()
        document["lanes"][0]["spec"]["gate"]["runner"] = "vitest"
        (self.root / "plan.json").write_bytes(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

        with mock.patch.object(sp, "_provisioning", return_value=((), None)):
            with self.assertRaises(sp.ProbeRefused) as caught:
                sp.resolve_lane(self.run_id, self.build_lane, registry_path=registry)
        self.assertIn("no longer matches", str(caught.exception))

    def test_a_lane_with_no_sealed_bundle_is_refused(self):
        database, store = self._ledger()
        store.close()
        registry = self._registry(database)

        with mock.patch.object(sp, "_provisioning", return_value=((), None)):
            with self.assertRaises(sp.ProbeRefused):
                sp.resolve_lane(self.run_id, self.build_lane, registry_path=registry)

    def test_main_prints_the_render_and_exits_zero(self):
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)
        (self.checkout / "refund.py").write_text(
            "def refund(amount):\n    return None\n"
        )

        def fake_suite(tree, files, *, gate, runtime_root):
            return _fake_run("E   AssertionError: boom", executed=3, failed=0), None

        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), mock.patch.object(
            sp, "_provisioning", return_value=((), None)
        ), mock.patch.object(
            cr, "_run_sealed_suite", side_effect=fake_suite
        ), contextlib.redirect_stdout(
            stdout
        ):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )

        self.assertEqual(code, 0)
        printed = stdout.getvalue()
        self.assertEqual(printed.splitlines()[0], "executed: 3")
        self.assertIn("AssertionError", printed)
        self.assertNotIn(str(self.vault), printed)
        self.assertNotIn(str(self.state), printed)

    def _main_stderr(self, registry: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), contextlib.redirect_stderr(stderr):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )
        return code, stderr.getvalue()

    def test_a_vault_failure_never_prints_the_vault_or_state_root(self):
        """The redaction set must be seeded before anything under the root opens.

        `hv.ensure_vault` runs inside `resolve_lane` and raises `VaultError`
        carrying the vault path. When `main` only filled its secret set from
        the value `resolve_lane` returns, that set was empty at the moment the
        exception was raised, and the builder's stderr got the runtime state
        root in full.
        """
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)
        doomed = hv.vault_path(self.state, self.run_id)

        with mock.patch.object(
            sp.hv,
            "ensure_vault",
            side_effect=hv.VaultError(
                "git init -q --bare {0} exited 1: permission denied".format(doomed)
            ),
        ):
            code, printed = self._main_stderr(registry)

        self.assertEqual(code, 2)
        self.assertNotIn(str(doomed), printed)
        self.assertNotIn(str(self.state), printed)
        self.assertNotIn(str(Path(os.path.realpath(self.state))), printed)
        self.assertIn("VaultError", printed)

    def test_a_ledger_failure_after_registry_resolution_prints_no_path(self):
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)

        with mock.patch.object(
            sp,
            "ArtifactStore",
            side_effect=OSError("unable to open database file {0}".format(database)),
        ):
            code, printed = self._main_stderr(registry)

        self.assertEqual(code, 2)
        self.assertNotIn(str(database), printed)
        self.assertNotIn(str(self.state), printed)

    def test_an_unreadable_registry_names_at_most_the_registry(self):
        registry = self.root / "bent.json"
        registry.write_text("{not json")

        code, printed = self._main_stderr(registry)

        self.assertEqual(code, 2)
        self.assertNotIn(str(self.state), printed)
        self.assertIn("registry", printed)

    def test_a_collision_reaches_the_builder_on_stdout_with_exit_zero(self):
        """The builder is told, on the success channel, not via a stderr refusal.

        A collision is a fact about its candidate, not a failure of the probe,
        so it exits 0 and prints. The real suite runner is left unstubbed here:
        it must never be reached.
        """
        database, store = self._ledger()
        self._insert_bundle(store, self.sealed, sequence=1, tag="current")
        store.close()
        registry = self._registry(database)
        colliding = self.checkout / TEST_PATH
        colliding.parent.mkdir(parents=True, exist_ok=True)
        colliding.write_text("def test_i_wrote_this_myself():\n    assert True\n")

        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), mock.patch.object(
            sp, "_provisioning", return_value=((), None)
        ), contextlib.redirect_stdout(
            stdout
        ):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), sp.COLLISION_LINE)


class ErrorLines(unittest.TestCase):
    def test_a_refusal_reaches_stderr_without_a_state_root_path(self):
        secret = "/private/state/root"
        line = sp._error_line(
            hv.VaultError("git failed in {0}/vaults/x.git".format(secret)),
            (secret,),
        )
        self.assertNotIn(secret, line)
        self.assertIn("VaultError", line)

    def test_main_returns_nonzero_when_the_probe_cannot_run(self):
        with mock.patch.object(
            sp, "resolve_lane", side_effect=sp.ProbeRefused("no such lane")
        ):
            code = sp.main(["--run", "r", "--lane", "l", "--checkout", "."])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
