"""Private TEST_DRAFT is gated on native collect/list vs gate.min_cases."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from typing import Any, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ADWS))
from adw_modules import git_publication as gitpub
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
    def __init__(self, bodies: list[str], *, selector: str = PRIVATE) -> None:
        self.selector = selector
        self.bodies = list(bodies)
        self.write_contexts: list[sch.LaneContext] = []
        self.review_calls = 0
        self.lane_specs = {
            LANE_ID: {
                "goal": "emit a.txt",
                "integration": {"integration_branch": "refs/heads/main"},
                "gate": {
                    "runner": "pytest" if selector.endswith(".py") else "vitest",
                    "argv": [selector],
                    "cwd": ".",
                    "min_cases": 9,
                },
            }
        }

    def write_tests(self, ctx: sch.LaneContext) -> dict[str, Any]:
        self.write_contexts.append(ctx)
        if not self.bodies:
            raise AssertionError("write_tests called with no remaining bodies")
        return {"files": {self.selector: self.bodies.pop(0)}}

    def review_tests(self, ctx: sch.LaneContext) -> Any:
        del ctx
        self.review_calls += 1
        raise AssertionError("test-reviewer must not run before TEST_DRAFT")

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


class DraftMinCasesPreflightTests(unittest.TestCase):
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

    def _start(self, actor: DraftActor, *, runner: str = "pytest") -> sch.FactoryScheduler:
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

    def _drafts(self) -> list[tuple[str, dict]]:
        rows = []
        for artifact_id, payload in self.store.conn.execute(
            "SELECT artifact_id, payload_json FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
            (RUN_ID, LANE_ID, st.ArtifactKind.TEST_DRAFT.value),
        ):
            rows.append((artifact_id, json.loads(payload)))
        return rows

    def test_eight_collected_vs_min_cases_nine_refuses_without_draft_or_reviewer(
        self,
    ) -> None:
        actor = DraftActor([_cases(8), _cases(8)])
        scheduler = self._start(actor)
        with self.assertRaises(sch.DraftMinCasesRefused) as raised:
            scheduler._writing_tests(LANE_ID)
        self.assertEqual(raised.exception.collected, 8)
        self.assertEqual(raised.exception.min_cases, 9)
        self.assertEqual(self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.WRITING_TESTS)
        self.assertEqual(self._drafts(), [])
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(len(actor.write_contexts), 2)
        first = actor.write_contexts[0]
        second = actor.write_contexts[1]
        self.assertIsNone(first.draft_correction)
        correction = second.draft_correction
        self.assertIsNotNone(correction)
        assert correction is not None
        finding = correction[0]
        self.assertIn("8", finding["observed_behavior"])
        self.assertIn("9", finding["required_behavior"])
        self.assertNotIn("assert True", json.dumps(finding))
        self.assertNotIn("test_case_0", json.dumps(finding))

    def test_identical_insufficient_output_does_not_loop(self) -> None:
        actor = DraftActor([_cases(8), _cases(8), _cases(8)])
        scheduler = self._start(actor)
        with self.assertRaises(sch.DraftMinCasesRefused):
            scheduler._writing_tests(LANE_ID)
        self.assertEqual(len(actor.write_contexts), 2)
        self.assertEqual(len(actor.bodies), 1)
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(self._drafts(), [])

    def test_nine_collected_advances_to_review_without_reviewer_launch(self) -> None:
        actor = DraftActor([_cases(8), _cases(9)])
        scheduler = self._start(actor)
        scheduler._writing_tests(LANE_ID)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.REVIEWING_TESTS
        )
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(len(actor.write_contexts), 2)
        self.assertIsNotNone(actor.write_contexts[1].draft_correction)

    def test_adequate_first_draft_does_not_reprompt_tester(self) -> None:
        actor = DraftActor([_cases(9), _cases(8)])
        scheduler = self._start(actor)
        scheduler._writing_tests(LANE_ID)
        self.assertEqual(len(actor.write_contexts), 1)
        self.assertIsNone(actor.write_contexts[0].draft_correction)
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(len(actor.bodies), 1)

    def test_collection_error_fails_closed_without_test_draft(self) -> None:
        actor = DraftActor(["def broken(\n"])
        scheduler = self._start(actor)
        with self.assertRaises(sch.DraftCollectionRefused) as raised:
            scheduler._writing_tests(LANE_ID)
        self.assertEqual(len(actor.write_contexts), 1)
        self.assertEqual(self._drafts(), [])
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.WRITING_TESTS
        )
        msg = str(raised.exception)
        self.assertIn("collect refused", msg)
        self.assertNotIn("def broken", msg)

    def test_vitest_collect_refused_includes_stderr_not_private_source(self) -> None:
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
            with self.assertRaises(sch.DraftCollectionRefused) as raised:
                scheduler._writing_tests(LANE_ID)
        msg = str(raised.exception)
        self.assertIn("ERR_MODULE_NOT_FOUND", msg)
        self.assertIn("Cannot find package 'vitest'", msg)
        self.assertNotIn(secret, msg)
        self.assertIn("[redacted]", msg)
        self.assertEqual(self._drafts(), [])
        self.assertEqual(actor.review_calls, 0)

    def test_vitest_draft_collects_via_product_node_modules_link(self) -> None:
        _install_fake_vitest(self.repo)
        actor = DraftActor([_vitest_cases(9)], selector=VITEST_PRIVATE)
        scheduler = self._start(actor, runner="vitest")
        scheduler._writing_tests(LANE_ID)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.REVIEWING_TESTS
        )
        self.assertEqual(len(self._drafts()), 1)
        self.assertEqual(actor.review_calls, 0)
        self.assertFalse((self.repo / VITEST_PRIVATE).exists())
        leftover = list((self.runtime.path / "worktrees").glob("draft-collect-*"))
        self.assertEqual(leftover, [])

    def test_envelope_declared_count_is_not_authority(self) -> None:
        class ClaimNine(DraftActor):
            def write_tests(self, ctx: sch.LaneContext) -> dict:
                payload = super().write_tests(ctx)
                return {"files": payload["files"], "case_count": 9, "min_cases": 9}

        actor = ClaimNine([_cases(8), _cases(8)])
        scheduler = self._start(actor)
        with self.assertRaises(sch.DraftMinCasesRefused) as raised:
            scheduler._writing_tests(LANE_ID)
        self.assertEqual(raised.exception.collected, 8)


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
            resolved = rr.resolve("pytest", tree, ".")
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
