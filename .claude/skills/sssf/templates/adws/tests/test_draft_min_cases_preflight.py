"""The lane's gate measures the private TEST_DRAFT, and answers with a verdict.

Native collect/list against `gate.min_cases` and `gate.required_cases`. What the
measurement produces is a REVISE recorded on the tests lane -- never an
exception out of the scheduler, which is what cost FDAdb run a2ea7355 four
lanes that had nothing to do with the draft that broke.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from typing import Any, Optional, Sequence, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ADWS))
from adw_modules import git_publication as gitpub
from adw_modules import hidden_vault as hv
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import runner_resolution as rr
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from test_actor_delegation_capability import RecordingLauncher, _ROLE_ROUTES, _lane
from tests.test_no_progress_block import _merge_dependency, _with_dependency_lane

import maestro

LANE_ID = "lane-a"
RUN_ID = "run-draft-gate"
PRIVATE = "tests/test_private.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


def _plan_bytes(runner: str = "pytest", selector: str = PRIVATE) -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": LANE_ID,
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": "refs/heads/main"},
                    "gate": {
                        "runner": runner,
                        "argv": [selector],
                        "cwd": ".",
                        "min_cases": 9,
                    },
                },
                "acceptance": ["a.txt is written"],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _cases(count: int) -> str:
    lines = []
    for index in range(count):
        lines.append("def test_case_{0}():".format(index))
        lines.append("    assert True")
        lines.append("")
    return "\n".join(lines)

VITEST_PRIVATE = "src/example.test.ts"

_FAKE_VITEST = """#!/usr/bin/env python3
import sys
from pathlib import Path
cwd = Path.cwd()
if not (cwd / "node_modules").exists():
    sys.stderr.write(
        "failed to load config from {0}/vitest.config.ts\\n"
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'vitest'\\n".format(cwd)
    )
    sys.exit(1)
args = sys.argv[1:]
if "--testNamePattern" in args:
    sys.exit(0)
filters = [item for item in args if item != "list" and not item.startswith("-")]
for rel in filters:
    path = cwd / rel
    if not path.is_file():
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("it("):
            continue
        title = stripped[3:].strip()
        if title[:1] in ("'", '"'):
            quote = title[0]
            end = title.find(quote, 1)
            name = title[1:end] if end > 0 else "case"
        else:
            name = "case"
        sys.stdout.write("{0} > {1}\\n".format(rel, name))
sys.exit(0)
"""


def _vitest_cases(count: int) -> str:
    lines = [
        'import { describe, it, expect } from "vitest";',
        'describe("private", () => {',
    ]
    for index in range(count):
        lines.append(
            '  it("case {0}", () => {{ throw new Error("must not run"); }});'.format(
                index
            )
        )
    lines.append("});")
    return "\n".join(lines)


def _install_fake_vitest(product: Path) -> Path:
    dest = product / "node_modules" / ".bin" / "vitest"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_FAKE_VITEST, encoding="utf-8")
    dest.chmod(0o755)
    return dest



class DraftActor:
    def __init__(
        self,
        bodies: list[str],
        *,
        selector: str = PRIVATE,
        required_cases: Sequence[str] = (),
        review_verdict: st.ReviewerVerdict = st.ReviewerVerdict.PASS,
        review_findings: Optional[Sequence[int]] = None,
    ) -> None:
        self.selector = selector
        #: One findings count per reviewed round. A tests round's outcome is
        #: this number, so a suite that shrinks its reviewer's list is making
        #: progress even when it collects the same cases every time.
        self.review_findings = list(review_findings or ())
        self.bodies = list(bodies)
        self.write_contexts: list[sch.LaneContext] = []
        self.review_contexts: list[sch.LaneContext] = []
        self.review_verdict = review_verdict
        gate: dict[str, Any] = {
            "runner": "pytest" if selector.endswith(".py") else "vitest",
            "argv": [selector],
            "cwd": ".",
            "min_cases": 9,
        }
        if required_cases:
            gate["required_cases"] = list(required_cases)
        self.lane_specs = {
            LANE_ID: {
                "goal": "emit a.txt",
                "integration": {"integration_branch": "refs/heads/main"},
                "gate": gate,
            }
        }

    @property
    def review_calls(self) -> int:
        return len(self.review_contexts)

    def write_tests(self, ctx: sch.LaneContext) -> dict[str, Any]:
        self.write_contexts.append(ctx)
        if not self.bodies:
            raise AssertionError("write_tests called with no remaining bodies")
        return {"files": {self.selector: self.bodies.pop(0)}}

    def review_tests(self, ctx: sch.LaneContext) -> Any:
        self.review_contexts.append(ctx)
        if self.review_findings:
            count = self.review_findings.pop(0)
            return st.ReviewerVerdict.REVISE, tuple(
                self._finding(index) for index in range(count)
            )
        if self.review_verdict is st.ReviewerVerdict.PASS:
            return st.ReviewerVerdict.PASS, ()
        return st.ReviewerVerdict.REVISE, (self._finding(0),)

    @staticmethod
    def _finding(index: int) -> dict:
        return {
            "implementation_area": "private tests",
            "observed_behavior": "case {0} asserts nothing about refusal".format(index),
            "required_behavior": "assert the refusal path too",
            "violated_requirement": "acceptance",
        }

    def build(self, ctx: sch.LaneContext) -> dict:
        del ctx
        raise AssertionError("builder must not run")

    def review_code(self, ctx: sch.LaneContext) -> Any:
        del ctx
        raise AssertionError("code-reviewer must not run")

    def review_integration(self, ctx, lanes, integration_sha):
        del ctx, lanes, integration_sha
        return st.ReviewerVerdict.PASS, (), ()

    def publish(self, ctx, *, fingerprint, expected_before, published_sha):
        del ctx, fingerprint, expected_before, published_sha
        return {}

    def complete_run_spaces(self, run_id: str) -> None:
        del run_id


class NativeQuietCollectionTests(unittest.TestCase):
    def test_authored_verbosity_collects_sixteen_native_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "test_product_codes.py").write_text(_cases(16))
            resolved = rr.ResolvedRunner("pytest", sys.executable, ("-m", "pytest"))
            for options in (("-q",), ("--quiet",), ("-qq",), ("-v",), ("-xq",), ("-qv",), ("--verbosity=2",), ("-q", "--")):
                with self.subTest(options=options):
                    gate = SimpleNamespace(argv=(*options, "test_product_codes.py"), cwd=".")
                    executed = resolved.execute_argv(gate.argv)
                    native = subprocess.run(resolved.collect_argv(gate), cwd=tree, capture_output=True, text=True)
                    self.assertEqual(native.returncode, 0, native.stdout + native.stderr)
                    expected = tuple("test_product_codes.py::test_case_" + str(n) for n in range(16))
                    self.assertEqual(rr.collected_identifiers(native.stdout), expected)
                    self.assertEqual(rr.collect_cases(resolved, gate, tree), expected)
                    self.assertEqual(resolved.execute_argv(gate.argv), executed)

    def test_real_empty_selection_is_zero_not_unreadable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "test_product_codes.py").write_text(_cases(16))
            resolved = rr.ResolvedRunner("pytest", sys.executable, ("-m", "pytest"))
            gate = SimpleNamespace(argv=("test_product_codes.py", "-k", "missing_case"), cwd=".")
            self.assertEqual(rr.collect_cases(resolved, gate, tree), ())


    def test_vitest_empty_listing_is_zero_but_unknown_output_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolved = rr.ResolvedRunner("vitest", "/unused/vitest")
            gate = SimpleNamespace(argv=(), cwd=".")
            for stdout in ("", "unsupported listing format\n"):
                with self.subTest(stdout=stdout):
                    result = SimpleNamespace(stdout=stdout, stderr="", returncode=0, timed_out=False)
                    with mock.patch.object(rr, "run_bounded", return_value=result):
                        if stdout:
                            with self.assertRaises(rr.CollectFailed) as caught:
                                rr.collect_cases(resolved, gate, Path(directory))
                            self.assertIn("unsupported listing format", caught.exception.detail)
                        else:
                            self.assertEqual(rr.collect_cases(resolved, gate, Path(directory)), ())


class DraftGateVerdictTests(unittest.TestCase):
    """A gate measurement about a draft is a verdict, never a dead run.

    Measured 2026-09-05, FDAdb run a2ea7355699c4dff93bc82ac89415475:
    `lane-wp8r-route-tests` submitted a module that deadlocked vitest at load,
    `DraftCollectionRefused` left the scheduler, and the run ended holding two
    MERGED lanes and two READY_TO_MERGE lanes that had nothing to do with it.
    It happened twice in a row because the tester's single in-turn correction
    was the only channel a refusal had.
    """

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

    def _start(
        self,
        actor: DraftActor,
        *,
        runner: str = "pytest",
        dependency: bool = False,
        regression_on_findings: bool = False,
    ) -> sch.FactoryScheduler:
        plan = _plan_bytes(runner=runner, selector=actor.selector)
        if dependency:
            plan = _with_dependency_lane(plan)
        compiled = plan_compiler.compile_plan(
            plan,
            plan_revision=1,
            plan_artifact_ref="plan:draft-gate",
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=RUN_ID,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        scheduler = sch.FactoryScheduler(
            self.store,
            RUN_ID,
            actor,
            self.runtime,
            target,
            regression_on_findings=regression_on_findings,
        )
        scheduler._planned(LANE_ID)
        return scheduler

    def _round(self, scheduler: sch.FactoryScheduler) -> None:
        """One WRITING_TESTS + REVIEWING_TESTS pass, the way the loop drives it."""
        scheduler._writing_tests(LANE_ID)
        scheduler._reviewing_tests(LANE_ID)

    def _stage(self) -> st.LaneStage:
        return self.store.lane_stage(RUN_ID, LANE_ID)

    def _artifacts(self, kind: st.ArtifactKind) -> list[dict]:
        rows = []
        for (payload,) in self.store.conn.execute(
            "SELECT payload_json FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
            (RUN_ID, LANE_ID, kind.value),
        ):
            rows.append(json.loads(payload))
        return rows

    def _drafts(self) -> list[dict]:
        return self._artifacts(st.ArtifactKind.TEST_DRAFT)

    def _reviews(self) -> list[dict]:
        return self._artifacts(st.ArtifactKind.TEST_REVIEW)

    def _only_finding(self) -> dict:
        reviews = self._reviews()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["verdict"], "REVISE")
        self.assertEqual(len(reviews[0]["findings"]), 1)
        return reviews[0]["findings"][0]

    def _vault(self) -> Path:
        return hv.vault_path(self.runtime.path, RUN_ID)

    # -- a measured refusal is a REVISE, not the end of the run ------------

    def test_short_draft_records_a_revise_and_sends_the_lane_back(self) -> None:
        actor = DraftActor([_cases(8)])
        scheduler = self._start(actor)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 0)
        finding = self._only_finding()
        self.assertEqual(finding["violated_requirement"], "gate.min_cases")
        self.assertIn("8", finding["observed_behavior"])
        self.assertIn("9", finding["required_behavior"])
        blob = json.dumps(finding)
        self.assertNotIn("assert True", blob)
        self.assertNotIn("test_case_0", blob)

    def test_uncollectable_draft_records_a_revise_and_sends_the_lane_back(
        self,
    ) -> None:
        actor = DraftActor(["def broken(\n"])
        scheduler = self._start(actor)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 0)
        finding = self._only_finding()
        self.assertEqual(finding["violated_requirement"], "gate collection")
        self.assertIn("could not list", finding["observed_behavior"])
        self.assertNotIn("def broken", json.dumps(finding))

    def test_the_refusal_reaches_the_tester_as_the_next_round_review(self) -> None:
        actor = DraftActor([_cases(8), _cases(9)])
        scheduler = self._start(actor)
        self._round(scheduler)
        scheduler._writing_tests(LANE_ID)
        self.assertEqual(len(actor.write_contexts), 2)
        review = actor.write_contexts[1].artifacts.get("TEST_REVIEW")
        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review.payload["verdict"], "REVISE")
        self.assertEqual(
            review.payload["findings"][0]["violated_requirement"], "gate.min_cases"
        )

    # -- the artifact a verdict is about outlives the verdict ---------------

    def test_the_refused_draft_is_pinned_in_the_vault(self) -> None:
        body = "def broken(\n"
        actor = DraftActor([body])
        scheduler = self._start(actor)
        self._round(scheduler)
        draft = self._drafts()[0]
        ref = draft["private_draft_ref"]
        self.assertTrue(
            ref.startswith("refs/maestro/drafts/{0}/{1}/".format(RUN_ID, LANE_ID))
        )
        vault = self._vault()
        refs = _git(vault, "for-each-ref", "--format=%(refname)", "refs/maestro/drafts")
        self.assertIn(ref, refs.splitlines())
        self.assertEqual(
            _git(vault, "show", "{0}:{1}".format(ref, PRIVATE)) + "\n", body
        )

    def test_the_collect_tree_is_still_discarded(self) -> None:
        actor = DraftActor([_cases(8)])
        scheduler = self._start(actor)
        self._round(scheduler)
        leftover = list((self.runtime.path / "worktrees").glob("draft-collect-*"))
        self.assertEqual(leftover, [])
        self.assertFalse((self.repo / PRIVATE).exists())

    # -- repeated draft content parks; different unsuccessful work continues --

    def test_repeated_refused_draft_parks_after_grace(self) -> None:
        actor = DraftActor([_cases(8), _cases(8), _cases(8)])
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        waits = self._artifacts(st.ArtifactKind.USER_WAIT)
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)
        self.assertEqual(waits[0]["resume_stage"], st.LaneStage.WRITING_TESTS.value)
        self.assertEqual(len(self._reviews()), 3)
        self.assertEqual(actor.review_calls, 0)
        drafts = self._drafts()
        self.assertEqual(len({d["private_draft_digest"] for d in drafts}), 3)
        self.assertEqual(sch._review_content_history(
            self.store, RUN_ID, LANE_ID, st.ArtifactKind.TEST_REVIEW, self._vault()
        ), [])

    def test_test_review_a_b_a_cycle_parks_on_the_same_path(self) -> None:
        actor = DraftActor(
            [_cases(9), _cases(10), _cases(9)],
            review_verdict=st.ReviewerVerdict.REVISE,
        )
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(actor.review_calls, 3)

    def test_shrinking_findings_over_a_flat_case_count_continue(self) -> None:
        # FDAdb run d246ae95, `lane-faq-producer-tests`: twelve cases every
        # round, findings 7, 5, 4, and the lane was parked for making no
        # progress. What a tests round moves is its reviewer's list.
        actor = DraftActor(
            [_cases(12).replace("assert True", "assert " + str(n)) for n in (1, 2, 3, 4)],
            review_findings=[7, 5, 4, 3],
        )
        scheduler = self._start(actor)
        for _ in range(4):
            self._round(scheduler)
            self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self.assertEqual(self._artifacts(st.ArtifactKind.USER_WAIT), [])
        self.assertEqual(
            [r["public_result_summary"]["collected"] for r in self._reviews()],
            [12, 12, 12, 12],
        )
        self.assertEqual(actor.review_calls, 4)

    def test_flat_findings_park_at_the_grace_window(self) -> None:
        actor = DraftActor(
            [_cases(12).replace("assert True", "assert " + str(n)) for n in (1, 2, 3)],
            review_findings=[4, 4, 4],
        )
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        waits = self._artifacts(st.ArtifactKind.USER_WAIT)
        self.assertEqual(waits[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)

    def _one_worse_round(self, opted_in: bool) -> st.LaneStage:
        actor = DraftActor(
            [_cases(12).replace("assert True", "assert " + str(n)) for n in (1, 2)],
            review_findings=[4, 6],
        )
        scheduler = self._start(actor, regression_on_findings=opted_in)
        self.assertIs(scheduler.regression_on_findings, opted_in)
        self._round(scheduler)
        self._round(scheduler)
        return self._stage()

    def test_one_worse_round_continues_by_default(self) -> None:
        self.assertEqual(self._one_worse_round(False), st.LaneStage.WRITING_TESTS)

    def test_one_worse_round_parks_where_the_deployment_opted_in(self) -> None:
        self.assertEqual(
            self._one_worse_round(True), st.LaneStage.WAITING_FOR_USER
        )

    def test_a_rising_case_count_under_repeated_refusals_continues(self) -> None:
        # A draft the harness itself refuses carries one substituted finding
        # every round, so findings alone would read a draft climbing toward
        # min_cases as flat. The case count is the half that moved.
        actor = DraftActor([_cases(4), _cases(6), _cases(8)])
        scheduler = self._start(actor)
        for _ in range(3):
            self._round(scheduler)
            self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self.assertEqual(self._artifacts(st.ArtifactKind.USER_WAIT), [])
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(
            [r["public_result_summary"]["collected"] for r in self._reviews()],
            [4, 6, 8],
        )

    def test_test_history_uses_named_draft_not_newest_unreviewed_draft(self) -> None:
        actor = DraftActor(
            [_cases(12).replace("assert True", "assert " + str(n)) for n in (1, 2, 3, 1)],
            review_findings=[7, 5, 4, 3],
        )
        scheduler = self._start(actor)
        for _ in range(3):
            self._round(scheduler)
        scheduler._writing_tests(LANE_ID)
        history = sch._review_content_history(
            self.store, RUN_ID, LANE_ID, st.ArtifactKind.TEST_REVIEW, self._vault()
        )
        self.assertFalse(sch._stalled(history, st.ArtifactKind.TEST_REVIEW))

    def test_changed_draft_base_resets_grace(self) -> None:
        actor = DraftActor([_cases(8)] * 5)
        scheduler = self._start(actor, dependency=True)
        self._round(scheduler)
        self._round(scheduler)
        _merge_dependency(scheduler, self.repo, self.runtime)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)

    def test_revision_resets_private_draft_window(self) -> None:
        actor = DraftActor([_cases(7), _cases(8)])
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        document = json.loads(_plan_bytes())
        document["lanes"][0]["spec"]["goal"] = "emit a.txt with detail"
        amended = plan_compiler.compile_plan(
            json.dumps(document).encode("utf-8"),
            plan_revision=2, plan_artifact_ref="plan:draft-gate-2",
        )
        sch.apply_factory_amendment(
            self.store, RUN_ID, amended, runtime=self.runtime, target=scheduler.target
        )
        self.assertEqual(sch._review_content_history(
            self.store, RUN_ID, LANE_ID, st.ArtifactKind.TEST_REVIEW, self._vault()
        ), [])

    def test_collected_plateau_despite_executable_edits_pauses(self) -> None:
        actor = DraftActor([_cases(8).replace("assert True", "assert " + str(n)) for n in (1, 2, 3)])
        scheduler = self._start(actor)
        for _ in range(3):
            self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual([r["public_result_summary"]["collected"] for r in self._reviews()], [8, 8, 8])

    def test_findings_regression_after_improvement_pauses_when_opted_in(self) -> None:
        actor = DraftActor(
            [_cases(12).replace("assert True", "assert " + str(n)) for n in (1, 2, 3, 4)],
            review_findings=[8, 6, 4, 6],
        )
        scheduler = self._start(actor, regression_on_findings=True)
        for _ in range(3):
            self._round(scheduler)
            self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)

    def test_cosmetic_invalid_drafts_repeat_without_fabricated_outcomes(self) -> None:
        # Valid Python, but the import fails before collection produces cases.
        # No numeric samples exist, so only substantive repetition can stop it.
        actor = DraftActor([
            '"""Envelope' + str(n) + '."""\nimport missing_private_dependency\n' + _cases(8)
            for n in range(3)
        ])
        scheduler = self._start(actor)
        for _ in range(3):
            self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertTrue(all("public_result_summary" not in r for r in self._reviews()))

    def test_pass_on_third_flat_outcome_advances(self) -> None:
        actor = DraftActor([_cases(9)] * 3, review_verdict=st.ReviewerVerdict.REVISE)
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        actor.review_verdict = st.ReviewerVerdict.PASS
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.TESTS_SEALED)

    # -- a draft that satisfies the gate is the reviewer's to judge ---------

    def test_adequate_draft_reaches_the_test_reviewer_and_seals(self) -> None:
        actor = DraftActor([_cases(9)])
        scheduler = self._start(actor)
        self._round(scheduler)
        self.assertEqual(actor.review_calls, 1)
        self.assertEqual(self._reviews()[0]["verdict"], "PASS")
        self.assertEqual(self._stage(), st.LaneStage.TESTS_SEALED)

    def test_required_case_names_are_reported_before_the_count(self) -> None:
        actor = DraftActor([_cases(9)], required_cases=["test_refuses_a_null_key"])
        scheduler = self._start(actor)
        self._round(scheduler)
        finding = self._only_finding()
        self.assertEqual(finding["violated_requirement"], "gate.required_cases")
        self.assertIn("test_refuses_a_null_key", finding["observed_behavior"])
        self.assertEqual(actor.review_calls, 0)

    def test_envelope_declared_count_is_not_authority(self) -> None:
        class ClaimNine(DraftActor):
            def write_tests(self, ctx: sch.LaneContext) -> dict:
                payload = super().write_tests(ctx)
                return {"files": payload["files"], "case_count": 9, "min_cases": 9}

        actor = ClaimNine([_cases(8)])
        scheduler = self._start(actor)
        self._round(scheduler)
        finding = self._only_finding()
        self.assertEqual(finding["violated_requirement"], "gate.min_cases")
        self.assertIn("8", finding["observed_behavior"])

    # -- the finding still carries the runner, never the draft --------------

    def _provisioned_fake_vitest(self) -> tuple[str, ...]:
        """A `provision_argv` that installs the fake vitest into the tree it runs in.

        The fake lives outside the product checkout. Nothing is bridged in from
        `self.repo`: the collect tree holds exactly what provisioning put there,
        which is what the tester's own tree will hold.
        """
        fake = _install_fake_vitest(self.root / "fake-runtime")
        return (
            "/bin/sh",
            "-c",
            "mkdir -p node_modules/.bin && cp {0} node_modules/.bin/vitest".format(fake),
        )

    def test_vitest_collect_refused_forwards_stderr_not_private_source(self) -> None:
        secret = "SECRET_ORACLE_LITERAL"
        body = _vitest_cases(1).replace("case 0", secret)
        actor = DraftActor([body], selector=VITEST_PRIVATE)
        scheduler = self._start(actor, runner="vitest")
        scheduler._provision_argv = self._provisioned_fake_vitest()
        failed = rr.CollectFailed(
            "vitest",
            returncode=1,
            detail=(
                "vitest collect refused exit 1: "
                "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'vitest' "
                + secret
            ),
        )
        with mock.patch.object(rr, "collect_cases", side_effect=failed):
            self._round(scheduler)
        finding = self._only_finding()
        observed = finding["observed_behavior"]
        self.assertIn("ERR_MODULE_NOT_FOUND", observed)
        self.assertIn("Cannot find package 'vitest'", observed)
        self.assertNotIn(secret, json.dumps(finding))
        self.assertIn("[redacted]", observed)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self.assertEqual(actor.review_calls, 0)

    def test_vitest_draft_collects_in_the_provisioned_tree(self) -> None:
        actor = DraftActor([_vitest_cases(9)], selector=VITEST_PRIVATE)
        scheduler = self._start(actor, runner="vitest")
        scheduler._provision_argv = self._provisioned_fake_vitest()
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.TESTS_SEALED)
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 1)
        self.assertFalse((self.repo / VITEST_PRIVATE).exists())
        self.assertFalse((self.repo / "node_modules").exists())
        leftover = list((self.runtime.path / "worktrees").glob("draft-collect-*"))
        self.assertEqual(leftover, [])



class CollectIdentifierTests(unittest.TestCase):
    def test_pytest_collect_only_counts_path_case_ids(self) -> None:
        stdout = (
            "tests/test_private.py::test_case_0\n"
            "tests/test_private.py::test_case_1\n"
            "8 tests collected in 0.01s\n"
        )
        self.assertEqual(len(rr.collected_identifiers(stdout)), 2)

    def test_vitest_list_counts_title_lines(self) -> None:
        stdout = (
            "tests/example.test.ts > case 0\n"
            "tests/example.test.ts > case 1\n"
            "tests/example.test.ts::case 2\n"
        )
        self.assertEqual(len(rr.collected_identifiers(stdout)), 3)

    def test_collect_cases_does_not_execute_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            target = tree / PRIVATE
            target.parent.mkdir(parents=True)
            target.write_text(
                "def test_case_0():\n"
                "    raise AssertionError('must not run')\n"
                "def test_case_1():\n"
                "    raise AssertionError('must not run')\n",
                encoding="utf-8",
            )
            resolved = rr.resolve("pytest", tree, ".", ())
            gate = SimpleNamespace(runner="pytest", argv=(PRIVATE,), cwd=".")
            ids = rr.collect_cases(resolved, gate, tree)
            self.assertEqual(len(ids), 2)

    def test_vitest_collect_in_unprovisioned_tree_keeps_module_error(self) -> None:
        # No bridge repairs the tree: what the runner printed is the refusal.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            vault = root / "vault"
            fake = _install_fake_vitest(product)
            target = vault / VITEST_PRIVATE
            target.parent.mkdir(parents=True)
            target.write_text(_vitest_cases(2), encoding="utf-8")
            resolved = rr.ResolvedRunner(runner="vitest", executable=str(fake))
            gate = SimpleNamespace(runner="vitest", argv=(VITEST_PRIVATE,), cwd=".")
            with self.assertRaises(rr.CollectFailed) as raised:
                rr.collect_cases(resolved, gate, vault)
            self.assertEqual(raised.exception.returncode, 1)
            detail = raised.exception.detail
            self.assertIn("vitest collect refused exit 1", detail)
            self.assertIn("ERR_MODULE_NOT_FOUND", detail)
            self.assertIn("Cannot find package 'vitest'", detail)
            self.assertNotIn("must not run", detail)
            self.assertNotIn(str(vault), detail)
            self.assertIn("$tree", detail)

    def test_bounded_collect_output_strips_ansi_and_caps(self) -> None:
        stderr = (
            "\x1b[31mError [ERR_MODULE_NOT_FOUND]: Cannot find package 'vitest'\x1b[0m\n"
            "    tsconfig.json:2:13:\n"
            '      2 │   "extends": "astro/tsconfigs/strict",\n'
            + ("SECRET_SOURCE_LINE\n" * 80)
        )
        text = rr.bounded_collect_output("", stderr, hide=("/secret/tree",))
        self.assertIn("ERR_MODULE_NOT_FOUND", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("extends", text)
        self.assertLessEqual(len(text), rr.COLLECT_DETAIL_CHARS)
        self.assertLessEqual(len(text.splitlines()), rr.COLLECT_DETAIL_LINES)


class TesterPromptCorrectionTests(unittest.TestCase):
    def test_persistent_tester_prompt_carries_measured_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product) or _git(product, "rev-parse", "HEAD")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = sch.LaneContext(
                run_id="run-prompt",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="11" * 32,
                stage=st.LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
                draft_correction=sch._draft_min_cases_findings(8, 9),
            )
            recorder = RecordingLauncher(files={PRIVATE: _cases(9)})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder),
                state,
                target,
                _ROLE_ROUTES,
            )
            actor.write_tests(ctx)
            prompt = recorder.launches[0]["prompt"]
            findings = prompt["revise_findings"]
            self.assertEqual(len(findings), 1)
            self.assertIn("8", findings[0]["observed_behavior"])
            self.assertIn("9", findings[0]["required_behavior"])
            dumped = json.dumps(prompt, sort_keys=True)
            self.assertNotIn("def test_case_0", dumped)


if __name__ == "__main__":
    unittest.main()
