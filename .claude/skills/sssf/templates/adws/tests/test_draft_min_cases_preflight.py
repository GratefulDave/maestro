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
from typing import Any, Sequence, cast

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
    ) -> None:
        self.selector = selector
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
        if self.review_verdict is st.ReviewerVerdict.PASS:
            return st.ReviewerVerdict.PASS, ()
        return st.ReviewerVerdict.REVISE, (
            {
                "implementation_area": "private tests",
                "observed_behavior": "the suite asserts nothing about refusal",
                "required_behavior": "assert the refusal path too",
                "violated_requirement": "acceptance",
            },
        )

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
        self, actor: DraftActor, *, runner: str = "pytest"
    ) -> sch.FactoryScheduler:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(runner=runner, selector=actor.selector),
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
            self.store, RUN_ID, actor, self.runtime, target
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

    # -- a lane that keeps failing measurement parks, it does not loop ------

    def test_three_refused_rounds_park_the_lane_for_the_operator(self) -> None:
        actor = DraftActor([_cases(8), _cases(7), _cases(6)])
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

    def test_a_reviewer_that_never_settles_parks_on_the_same_path(self) -> None:
        actor = DraftActor(
            [_cases(9), _cases(10), _cases(11)],
            review_verdict=st.ReviewerVerdict.REVISE,
        )
        scheduler = self._start(actor)
        self._round(scheduler)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WRITING_TESTS)
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(actor.review_calls, 3)

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

    def test_vitest_collect_refused_forwards_stderr_not_private_source(self) -> None:
        _install_fake_vitest(self.repo)
        secret = "SECRET_ORACLE_LITERAL"
        body = _vitest_cases(1).replace("case 0", secret)
        actor = DraftActor([body], selector=VITEST_PRIVATE)
        scheduler = self._start(actor, runner="vitest")
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

    def test_vitest_draft_collects_via_product_node_modules_link(self) -> None:
        _install_fake_vitest(self.repo)
        actor = DraftActor([_vitest_cases(9)], selector=VITEST_PRIVATE)
        scheduler = self._start(actor, runner="vitest")
        self._round(scheduler)
        self.assertEqual(self._stage(), st.LaneStage.TESTS_SEALED)
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 1)
        self.assertFalse((self.repo / VITEST_PRIVATE).exists())
        leftover = list((self.runtime.path / "worktrees").glob("draft-collect-*"))
        self.assertEqual(leftover, [])


class CollectionFindingWording(unittest.TestCase):
    """A refusal names a stopwatch; the finding must name an obligation.

    `vitest did not finish collecting in 120.0s` told the tester the harness
    gave up. It did not tell it that listing cases is an import, and that an
    import which blocks never reaches the listing -- which is exactly what the
    a2ea7355 draft did.
    """

    def test_the_runner_text_is_forwarded_verbatim(self) -> None:
        detail = "vitest did not finish collecting in 120.0s"
        finding = sch._draft_collection_findings(detail)[0]
        self.assertIn(detail, finding["observed_behavior"])

    def test_it_does_not_hand_the_stopwatch_back_as_the_instruction(self) -> None:
        finding = sch._draft_collection_findings(
            "vitest did not finish collecting in 120.0s"
        )[0]
        required = finding["required_behavior"]
        self.assertNotIn("fix what the refusal names", required)
        self.assertIn("import", required)
        self.assertIn("module scope", required)

    def test_it_is_a_valid_revise_finding(self) -> None:
        findings = sch._draft_collection_findings("collect refused exit 2")
        self.assertEqual(st.require_revise_findings(findings), findings)


class DraftRefusalIsNotFatal(unittest.TestCase):
    """The two draft judgements are no longer members of `FactoryRefused`.

    `RunnerPreflightRefused` still is, and must stay: a missing runner is
    nobody's draft and no finding can answer it.
    """

    def test_the_min_cases_refusal_no_longer_exists(self) -> None:
        self.assertFalse(hasattr(sch, "DraftMinCasesRefused"))

    def test_collection_refusal_never_escapes_the_measurement(self) -> None:
        source = Path(sch.__file__).read_text(encoding="utf-8")
        body = source.split("    def _measure_draft_gate(", 1)[1]
        body = body.split("\n    def ", 1)[0]
        self.assertIn("except DraftCollectionRefused as refused:", body)
        self.assertIn("return _draft_collection_findings(str(refused))", body)

    def test_runner_preflight_stays_a_factory_refusal(self) -> None:
        self.assertTrue(issubclass(sch.RunnerPreflightRefused, sch.FactoryRefused))
        self.assertEqual(sch.RunnerPreflightRefused.code, "RUNNER_PREFLIGHT_REFUSED")


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

    def test_collect_argv_uses_native_modes(self) -> None:
        pytest_runner = rr.ResolvedRunner(runner="pytest", executable="/bin/pytest")
        vitest_runner = rr.ResolvedRunner(runner="vitest", executable="/bin/vitest")
        gate = SimpleNamespace(runner="pytest", argv=(PRIVATE,), cwd=".")
        self.assertEqual(
            pytest_runner.collect_argv(gate),
            ("/bin/pytest", "--collect-only", "-q", "-o", "addopts=", PRIVATE),
        )
        gate.runner = "vitest"
        self.assertEqual(
            vitest_runner.collect_argv(gate),
            ("/bin/vitest", "list", "--run", PRIVATE),
        )

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

    def test_vitest_collect_without_runtime_root_keeps_module_error(self) -> None:
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

    def test_vitest_collect_links_product_node_modules(self) -> None:
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
            ids = rr.collect_cases(resolved, gate, vault, runtime_root=product)
            self.assertEqual(len(ids), 2)
            self.assertTrue((vault / "node_modules").is_symlink())
            self.assertEqual(
                (vault / "node_modules").resolve(),
                (product / "node_modules").resolve(),
            )
            self.assertFalse((product / VITEST_PRIVATE).exists())

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
