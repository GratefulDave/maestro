"""Accepted tests are visible and immutable: what replaces the vault.

A tests lane's accepted suite is a candidate that merges into the integration
ref like any other lane's, so the build lane that consumes it starts from a
base that carries the suite byte for byte. The builder is told where the suite
is and handed the runner's failure lines verbatim. What keeps the suite
immutable is not secrecy but two checks that were additive in #288 and are
load-bearing here: `CANDIDATE_TEST_PATH_REFUSED` at admission and
`TEST_SUITE_TAMPERED` before any runner is invoked.

Every case below drives the real scheduler over a typed two-lane plan. The
runner is the one thing stubbed, and the stub reads the tree it is handed --
the accepted test file and the product file both -- so "the suite runs from
the candidate tree" is a statement about the tree, not about this machine's
pytest resolution.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import test_binding as tb  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.runtime_state import RuntimeStateRoot  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from typing import cast  # noqa: E402
from tests.test_actor_delegation_capability import (  # noqa: E402
    _ROLE_ROUTES,
    RecordingLauncher,
)

TEST_PATH = "tests/test_refund_contract.py"
PRODUCT_PATH = "refund.py"
#: Planted in the accepted suite's assertion message. Under the vault this
#: literal was exactly what the redactor scrubbed from every payload and
#: prompt; the cases here assert it arrives intact.
ASSERTION_LITERAL = "REFUND_MUST_REFUSE_A_NEGATIVE_AMOUNT_7f3a"
TEST_SOURCE = (
    "from refund import refund\n"
    "\n"
    "\n"
    "def test_refund_refuses_a_negative_amount():\n"
    "    assert refund(-1) is None, {0!r}\n"
).format(ASSERTION_LITERAL)
READY_PRODUCT = "def refund(amount):\n    if amount < 0:\n        return None\n    return amount\n"
DRAFT_PRODUCT = "def refund(amount):\n    return amount\n"
#: What the product repository holds before any lane runs. It must differ from
#: `DRAFT_PRODUCT`, or the initial build has nothing to commit.
SEED_PRODUCT = "def refund(amount):\n    raise NotImplementedError\n"
FINDING = {
    "implementation_area": PRODUCT_PATH,
    "observed_behavior": "a negative amount is refunded",
    "required_behavior": "a negative amount is refused",
    "violated_requirement": "negative amounts are refused",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / PRODUCT_PATH).write_text(SEED_PRODUCT, encoding="utf-8")
    _git(path, "add", PRODUCT_PATH)
    _git(path, "commit", "-m", "seed")


def _plan_bytes() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-tests",
                "lane_kind": "tests",
                "needs": [],
                "outputs": [TEST_PATH],
                "spec": {
                    "goal": "author the refund acceptance suite",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["negative amounts are refused"],
            },
            {
                "id": "lane-build",
                "lane_kind": "build",
                "needs": ["lane-tests"],
                "outputs": [PRODUCT_PATH],
                "spec": {
                    "goal": "implement refund",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["negative amounts are refused"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _tree_runner(tree: Path, paths, *, gate=None, timeout_s=120.0) -> dict:
    """A runner that reads the tree it is handed and nothing else.

    It executes the accepted case's own logic against the product file in
    `tree`, and its output is shaped the way pytest's `--tb=line -vv` output
    is: a `FAILED` line naming the case, and an `E` line carrying the
    assertion message verbatim.
    """
    del gate, timeout_s
    source = (Path(tree) / TEST_PATH).read_text(encoding="utf-8")
    product = (Path(tree) / PRODUCT_PATH).read_text(encoding="utf-8")
    assert ASSERTION_LITERAL in source, "the runner did not see the accepted suite"
    namespace: dict = {}
    exec(product, namespace)  # noqa: S102 - the product under test, in-process
    if namespace["refund"](-1) is None:
        return {
            "counts": {"passed": 1, "failed": 0, "errored": 0, "skipped": 0},
            "executed": 1,
            "min_cases": 1,
            "output": "{0}::test_refund_refuses_a_negative_amount PASSED\n"
            "1 passed in 0.01s\n".format(TEST_PATH),
            "returncode": 0,
            "runner": "pytest",
        }
    return {
        "counts": {"passed": 0, "failed": 1, "errored": 0, "skipped": 0},
        "executed": 1,
        "min_cases": 1,
        "output": (
            "{0}::test_refund_refuses_a_negative_amount FAILED\n"
            "{0}:5: AssertionError: {1}\n"
            "E   assert -1 is None\n"
            "FAILED {0}::test_refund_refuses_a_negative_amount - AssertionError: {1}\n"
            "1 failed in 0.02s\n"
        ).format(TEST_PATH, ASSERTION_LITERAL),
        "returncode": 1,
        "runner": "pytest",
    }


class Actor:
    """Tester writes the suite; builder writes a draft, then the fix."""

    def __init__(self, repo: Path, worktrees: Path) -> None:
        self.repo = repo
        self.worktrees = worktrees
        self.code_rounds: dict[str, int] = defaultdict(int)
        self.build_contexts: list[sch.LaneContext] = []
        self.builder_checkouts: list[Path] = []
        #: What the builder writes at the suite path, if anything. Set by the
        #: tampering cases; None means the builder leaves the suite alone.
        self.suite_edit: str | None = None

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        return {"test_files": {TEST_PATH: TEST_SOURCE}}

    def review_tests(self, ctx: sch.LaneContext):
        return st.ReviewerVerdict.PASS, ()

    def build(self, ctx: sch.LaneContext) -> dict:
        self.build_contexts.append(ctx)
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        self.builder_checkouts.append(work)
        body = (
            DRAFT_PRODUCT
            if ctx.entry_kind is st.BuildingEntryKind.INITIAL
            else READY_PRODUCT
        )
        (work / PRODUCT_PATH).write_text(body, encoding="utf-8")
        _git(work, "add", PRODUCT_PATH)
        if self.suite_edit is not None:
            (work / TEST_PATH).write_text(self.suite_edit, encoding="utf-8")
            _git(work, "add", TEST_PATH)
        _git(work, "commit", "-m", ctx.lane.lane_id)
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}

    def review_code(self, ctx: sch.LaneContext):
        n = self.code_rounds[ctx.lane.lane_id]
        self.code_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
        return st.ReviewerVerdict.PASS, ()

    def review_integration(self, ctx, lanes, integration_sha):
        return st.ReviewerVerdict.PASS, (), ()

    def publish(self, ctx, *, fingerprint, expected_before, published_sha):
        return {
            "receipt_object": published_sha,
            "receipt_ref": st.publication_ref(ctx.run_id, fingerprint),
        }

    def complete_run_spaces(self, run_id: str) -> None:
        del run_id


class VisibleSuiteBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.run_id = "run-visible"
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:visible"
        )
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )
        self.actor = Actor(self.repo, self.runtime.path / "worktrees")

    def _run(self) -> st.RunStatus:
        with mock.patch.object(tc, "run_suite", side_effect=_tree_runner):
            return sch.FactoryScheduler(
                self.store, self.run_id, self.actor, self.runtime, self.target
            ).run()

    def _rows(self, lane_id: str, kind: st.ArtifactKind) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts WHERE run_id=? "
                "AND lane_id=? AND artifact_kind=? ORDER BY sequence",
                (self.run_id, lane_id, kind.value),
            )
        ]


class TheSuiteIsInTheBuildersCheckout(VisibleSuiteBase):
    def test_the_tests_lane_merges_and_the_build_lane_starts_from_it(self):
        self.assertEqual(self._run(), st.RunStatus.COMPLETE)

        merges = self._rows("lane-tests", st.ArtifactKind.INTEGRATION_MERGE)
        self.assertEqual(len(merges), 1, "a tests lane merges like any lane")
        accepted = self._rows("lane-tests", st.ArtifactKind.ACCEPTED_TEST_SUITE)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(merges[0]["candidate_sha"], accepted[0]["candidate_sha"])
        self.assertEqual(
            self.store.lane_stage(self.run_id, "lane-tests"), st.LaneStage.MERGED
        )
        # (a) the build lane's base is the merged head, which carries the
        # suite byte for byte at the declared path.
        builders = self._rows("lane-build", st.ArtifactKind.BUILDER_OUTPUT)
        self.assertEqual(builders[0]["builder_base_sha"], merges[0]["after_sha"])
        for checkout in self.actor.builder_checkouts:
            self.assertEqual(
                (checkout / TEST_PATH).read_text(encoding="utf-8"), TEST_SOURCE
            )
        blob = _git(self.repo, "rev-parse", "{0}:{1}".format(merges[0]["after_sha"], TEST_PATH))
        self.assertEqual(accepted[0]["files"][TEST_PATH], blob)
        self.assertEqual(
            accepted[0]["test_suite_digest"], tb.suite_digest({TEST_PATH: blob})
        )

    def test_the_builder_is_told_the_paths_and_the_digest(self):
        self._run()

        first, second = self.actor.build_contexts[:2]
        self.assertEqual(first.protected_test_paths, (TEST_PATH,))
        accepted = self._rows("lane-tests", st.ArtifactKind.ACCEPTED_TEST_SUITE)[0]
        self.assertEqual(first.test_suite_digest, accepted["test_suite_digest"])
        self.assertEqual(second.entry_kind, st.BuildingEntryKind.CODE_REVISE)

    def test_nothing_records_a_sealed_kind_or_a_vault(self):
        self._run()

        kinds = {
            row[0]
            for row in self.store.conn.execute(
                "SELECT DISTINCT artifact_kind FROM lane_artifacts WHERE run_id=?",
                (self.run_id,),
            )
        }
        self.assertNotIn("SEALED_TEST_BUNDLE", kinds)
        self.assertNotIn("TEST_INVALIDATION", kinds)
        self.assertIn(st.ArtifactKind.ACCEPTED_TEST_SUITE.value, kinds)
        self.assertFalse((self.state / "vaults").exists())
        self.assertFalse((self.state / "vault").exists())


class TheFailureOutputIsVerbatim(VisibleSuiteBase):
    def test_the_code_review_carries_the_assertion_text(self):
        """(e) the runner's own lines, unredacted."""
        self._run()

        reviews = self._rows("lane-build", st.ArtifactKind.CODE_REVIEW)
        revise = [r for r in reviews if r["verdict"] == st.ReviewerVerdict.REVISE.value]
        self.assertEqual(len(revise), 1)
        lines = revise[0]["failure_output"]
        self.assertTrue(any(ASSERTION_LITERAL in line for line in lines), lines)
        self.assertTrue(
            any("test_refund_refuses_a_negative_amount" in line for line in lines),
            "the failing case is named, not dropped",
        )
        self.assertNotIn("redacted_failures", revise[0])
        self.assertNotIn("[redacted]", json.dumps(revise[0]))
        self.assertNotIn("sealed_digest", revise[0])
        self.assertEqual(revise[0]["test_suite_digest"],
                         self._rows("lane-tests", st.ArtifactKind.ACCEPTED_TEST_SUITE)[0]["test_suite_digest"])

    def test_the_builder_prompt_names_the_paths_and_carries_the_failure(self):
        """(b) what the real actor puts in front of the builder.

        Driven through `HerdrStageActor.build` and its `_launch` seam, with the
        launcher fake capturing the prompt it was handed: nothing is assembled
        by hand here, so a field `build` stops forwarding is a field this case
        stops seeing.
        """
        self._run()
        revise_ctx = self.actor.build_contexts[1]
        prior = revise_ctx.artifacts["CODE_REVIEW"]
        state = self.root / "actor-state"
        state.mkdir(mode=0o700)
        recorder = RecordingLauncher(
            files={PRODUCT_PATH: READY_PRODUCT},
            envelope={"candidate_sha": revise_ctx.builder_base_sha, "changed": True},
        )
        actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, recorder), state, self.target, _ROLE_ROUTES
        )
        actor.build(revise_ctx)

        body = recorder.launches[0]["prompt"]
        self.assertEqual(body["role"], "builder")
        text = body["instructions"]
        self.assertIn(TEST_PATH, text)
        self.assertIn(ASSERTION_LITERAL, text)
        self.assertIn("CANDIDATE_TEST_PATH_REFUSED", text)
        self.assertIn("acceptance criteria", text)
        self.assertNotIn("redacted", text)
        self.assertEqual(body["test_paths"], [TEST_PATH])
        self.assertEqual(body["test_suite_digest"], revise_ctx.test_suite_digest)
        self.assertTrue(any(ASSERTION_LITERAL in line for line in body["failure_output"]))
        self.assertIn(ASSERTION_LITERAL, json.dumps(prior.payload["failure_output"]))


class TheSuiteIsImmutable(VisibleSuiteBase):
    def test_a_candidate_that_edits_the_suite_is_refused_before_review(self):
        """(c) through the scheduler's own admit path, not the unit in #288."""
        self.actor.suite_edit = TEST_SOURCE.replace("is None", "is not None")
        reviewed = mock.Mock(name="measure_candidate")

        with mock.patch.object(cr, "measure_candidate", reviewed):
            with self.assertRaises(gitpub.GitPublicationRefused) as caught:
                self._run()

        self.assertEqual(caught.exception.code, "CANDIDATE_TEST_PATH_REFUSED")
        self.assertEqual(caught.exception.detail, TEST_PATH)
        reviewed.assert_not_called()
        self.assertEqual(
            self.store.lane_stage(self.run_id, "lane-build"), st.LaneStage.BUILDING
        )
        self.assertEqual(self._rows("lane-build", st.ArtifactKind.BUILDER_OUTPUT), [])

    def test_a_rewritten_base_refuses_as_tampered_not_as_a_candidate_defect(self):
        """(d) the review runs the candidate tree itself, and verifies it first.

        Admission is the first line; this case removes it. The candidate's
        tree carries a doctored suite -- committed on the integration head,
        with the product change on top -- and it is admitted against that
        doctored commit, so its own delta does not name the suite. The
        admission record then names the real integration head as the base,
        exactly what the lane's stage digest was computed over, so the ledger
        accepts it. No overlay hides the doctored bytes any more: the review
        reads the candidate tree, and `verify_suite` refuses it before any
        runner is invoked.
        """
        runner = mock.Mock(name="run_suite")
        real_admit = gitpub.admit_candidate

        def _rewrite_base_then_admit(binding, **kwargs):
            if kwargs["lane_id"] != "lane-build":
                return real_admit(binding, **kwargs)
            head = kwargs["builder_base_sha"]
            doctored = gitpub.commit_files_on_base(
                binding,
                base_sha=head,
                files={TEST_PATH: TEST_SOURCE.replace("is None", "is not None").encode()},
                message=b"doctored base\n",
            )
            candidate = gitpub.commit_files_on_base(
                binding,
                base_sha=doctored,
                files={PRODUCT_PATH: READY_PRODUCT.encode()},
                message=b"candidate on doctored base\n",
            )
            admitted = dict(
                real_admit(
                    binding,
                    **dict(kwargs, builder_base_sha=doctored, candidate_sha=candidate),
                )
            )
            admitted["builder_base_sha"] = head
            return admitted

        with mock.patch.object(sch.gitpub, "admit_candidate", side_effect=_rewrite_base_then_admit):
            with mock.patch.object(tc, "run_suite", runner):
                with self.assertRaises(tb.TestSuiteTampered) as caught:
                    sch.FactoryScheduler(
                        self.store, self.run_id, self.actor, self.runtime, self.target
                    ).run()

        runner.assert_not_called()
        self.assertEqual(caught.exception.path, TEST_PATH)
        self.assertIsNotNone(cr.suite_environment_detail(caught.exception))
        self.assertEqual(self._rows("lane-build", st.ArtifactKind.CODE_REVIEW), [])


class TheTestReviewerReadsAnOrdinaryCheckout(unittest.TestCase):
    def test_the_test_reviewer_base_is_the_draft_candidate(self):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.target = SimpleNamespace(integration_initial_sha="0" * 40)
        ctx = SimpleNamespace(
            candidate_sha="a" * 40,
            builder_base_sha="",
            integration_head="b" * 40,
        )
        self.assertEqual(actor._base_sha(ctx, "test-reviewer"), "a" * 40)

    def test_the_actor_no_longer_materializes_a_private_tree(self):
        self.assertFalse(hasattr(maestro.HerdrStageActor, "_refresh_private_tree"))
        self.assertFalse(hasattr(maestro.HerdrStageActor, "_strip_paths"))
        self.assertFalse(hasattr(sch.FactoryScheduler, "_complete_test_invalidation"))
        self.assertFalse(hasattr(cr, "detect_candidate_private_collisions"))
        self.assertFalse(hasattr(cr, "redacted_failure_lines"))


if __name__ == "__main__":
    unittest.main()
