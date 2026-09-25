"""The tree a role reads is the artifact the dispatch names, on every path.

`HerdrStageActor._launch` takes a `prepare_cwd` callable from each of the five
role methods and is the one place that materializes a role's working tree. It
used to call it on two of its three paths -- the resubmit, and the one behind
`superseded` -- and skip it entirely on an ordinary first launch. `_prepare`
covered the gap for exactly one role, the tester, with a `sha and role ==
"tester"` refresh that read as a reuse guard.

That left four roles reading whatever the previous round left on disk whenever
the scheduler process that built the tree was not the one dispatching into it.
`_prepare` returns an existing non-empty checkout untouched and only
materializes an absent one, so a first launch in a new process runs an agent
over the previous round's bytes. That is not a rare state: it is every `run
resume` after the scheduler died, which is every resume this factory has.

An *adoption* is not that state and does not have the bug: when the agent's
pane outlives the record, `HerdrLauncher.launch` finds it and calls
`spec.prepare_adopted_cwd`, which refreshes on its way through. The gap is the
route with no live agent to adopt and no stored handle to resubmit into -- the
route a dead process leaves open. The fixture below clears both registries for
that reason, and clearing only the actor's would let these tests pass against
the bug.

Run a2ea7355, lane `lane-wp8r-route-tests`. The test reviewer read the round-1
draft on all three rounds. It reported round-1's two defects each time, in
nearly the same words, and the tester had fixed both in round 2: the three
`import "../wp7/*.test.ts"` side-effect lines were gone, and `lastIndexOf` --
which round 3's finding quotes -- exists only in round 1. Measured afterwards,
the reviewer's materialized tree still hashed to the round-1 draft's bytes.

Nothing could notice. The ledger records the input *artifact id* rather than
the bytes handed to the reader, so `input_digest` moved every round while the
file did not -- which is also what kept the identical-input check from firing. The lane redrafted three
times, made no progress against findings that were already answered, and parked
at WAITING_FOR_USER.

Both cases below restart between two dispatches and then assert on the bytes
the launcher was actually handed.
"""

from __future__ import annotations

import json
from dataclasses import replace
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules.scheduler import LaneContext
from adw_modules.scheduler_types import (
    ArtifactKind,
    LaneArtifact,
    LaneProjection,
    LaneStage,
    ReviewerVerdict,
    lane_projection_digest,
)

_ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}

_SPEC = "ab" * 32
_SUITE = "tests/private/suite.test.ts"
_ROUND_ONE = "// round one\nit('a', () => {});\n"
_ROUND_TWO = "// round two\nit('b', () => {});\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "seed")
    return _git(path, "rev-parse", "HEAD")


def _lane() -> LaneProjection:
    outputs = (_SUITE,)
    return LaneProjection(
        lane_id="lane-a",
        needs=(),
        spec_digest=_SPEC,
        declared_outputs=outputs,
        lane_projection_digest=lane_projection_digest(
            _SPEC, (), outputs, lane_kind=None
        ),
        public_acceptance=("the suite is written",),
    )


def _commit_suite(repo: Path, *, parent: str, body: str) -> str:
    """A draft candidate in the run repository: the parent plus the suite."""
    binding = gitpub.bind_target_worktree(repo, "refs/heads/main")
    return gitpub.commit_files_on_base(
        binding,
        base_sha=parent,
        files={_SUITE: body.encode("utf-8")},
        message=b"draft\n",
    )


def _draft(sha: str, ref: str) -> LaneArtifact:
    return LaneArtifact(
        kind=ArtifactKind.TEST_DRAFT,
        plan_revision=1,
        spec_digest=_SPEC,
        lane_projection_digest=_lane().lane_projection_digest,
        input_digest="11" * 32,
        output_digest="22" * 32,
        artifact_ref=ref,
        payload={
            "candidate_ref": ref,
            "candidate_sha": sha,
            "files": {_SUITE: "00" * 20},
            "input_digest": "11" * 32,
            "public_contract": {
                "acceptance_criteria": ["the suite is written"],
                "declared_outputs": [_SUITE],
            },
        },
    )


class _Launcher:
    """Records the bytes of the suite as the tree stood at dispatch."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        self.launches = 0
        self.resubmits = 0
        self._live: dict[tuple[str, str], SimpleNamespace] = {}
        self._by_token: dict[str, SimpleNamespace] = {}

    @staticmethod
    def invoking_repository(_env: object) -> None:
        # No invoking Herdr Space in a test: the actor anchors on the target.
        return None

    def _observe(self, worktree: Path) -> None:
        suite = Path(worktree) / _SUITE
        self.seen.append(suite.read_text(encoding="utf-8") if suite.is_file() else None)

    @staticmethod
    def _reply(envelope: Path) -> None:
        envelope.parent.mkdir(parents=True, exist_ok=True)
        envelope.write_text(
            json.dumps({"verdict": "PASS", "findings": []}), encoding="utf-8"
        )

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        key = (str(spec.lane_key or ""), str(spec.pane_role or ""))
        live = self._live.get(key)
        if live is not None and key[1]:
            # The adoption `HerdrLauncher.launch` performs, and the reason a
            # dropped `_roles` record does not mean a fresh agent.
            if spec.prepare_adopted_cwd is not None:
                spec.prepare_adopted_cwd(Path(live.launched_cwd))
            return self.resubmit(
                live, spec.prompt_path, envelope_path=spec.envelope_path
            )
        self.launches += 1
        self._observe(Path(spec.worktree))
        self._reply(Path(spec.envelope_path))
        handle = SimpleNamespace(
            correlation_token=spec.correlation_token,
            envelope_path=spec.envelope_path,
            lane_key=key[0],
            launched_cwd=Path(spec.worktree).resolve(),
            pane_id="pane-{0}".format(self.launches),
            pane_role=key[1],
        )
        self._by_token[spec.correlation_token] = handle
        if key[1]:
            self._live[key] = handle
        return handle

    def resubmit(
        self,
        handle: SimpleNamespace,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: str | None = None,
        timeout_s: float = 60.0,
        envelope_path: Path | None = None,
    ) -> SimpleNamespace:
        del prompt_path, route, expected_token, timeout_s
        self.resubmits += 1
        self._observe(Path(handle.launched_cwd))
        dest = Path(envelope_path or handle.envelope_path)
        self._reply(dest)
        handle.envelope_path = dest
        return handle

    def cancel(self, handle: SimpleNamespace, deadline: float) -> None:
        del deadline
        key = (str(handle.lane_key or ""), str(handle.pane_role or ""))
        if self._live.get(key) is handle:
            self._live.pop(key)
        self._by_token.pop(handle.correlation_token, None)

    def poll(self, handle: object) -> object:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s

    def retain(self, handle: SimpleNamespace) -> None:
        del handle


class _Bench:
    def __init__(self, tmp: str) -> None:
        root = Path(tmp)
        self.product = root / "product"
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.head = _init_repo(self.product)
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.launcher = _Launcher()
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, self.launcher),
            self.state,
            self.target,
            _ROLE_ROUTES,
        )
        self.actor.step = lambda lane, message, detail="": None

    def restart(self) -> None:
        """What a `run resume` after the scheduler process died leaves behind.

        The checkouts on disk survive; nothing in memory does. Both registries
        go: the actor's `_roles`, and the launcher's own `(lane_key,
        pane_role)` map, because the panes died with the process. That second
        clear is what makes this test bite. Leave the launcher's map populated
        and the next dispatch is an *adoption* -- `HerdrLauncher.launch` finds
        the live agent and calls `spec.prepare_adopted_cwd`, which refreshes
        the tree on its way through. A first launch with nothing to adopt is
        the one route with no refresh on it, and it is the route a dead
        process leaves open.
        """
        self.actor._roles.clear()
        self.launcher._live.clear()


def _review_ctx(draft: LaneArtifact) -> LaneContext:
    return LaneContext(
        run_id="run-1",
        lane=_lane(),
        plan_revision=1,
        plan_digest="ef" * 32,
        plan_artifact_ref="plan:x",
        input_digest="33" * 32,
        stage=LaneStage.REVIEWING_TESTS,
        artifacts={"TEST_DRAFT": draft},
        candidate_ref=str(draft.payload["candidate_ref"]),
        candidate_sha=str(draft.payload["candidate_sha"]),
        public_contract=dict(draft.payload["public_contract"]),
    )


def _code_ctx(candidate: str) -> LaneContext:
    return LaneContext(
        run_id="run-1",
        lane=_lane(),
        plan_revision=1,
        plan_digest="ef" * 32,
        plan_artifact_ref="plan:x",
        input_digest="44" * 32,
        stage=LaneStage.REVIEWING_CODE,
        artifacts={},
        candidate_sha=candidate,
        public_contract={
            "acceptance_criteria": ["the suite is written"],
            "declared_outputs": [_SUITE],
        },
        test_suite_digest="test-suite.v1:" + "55" * 32,
    )


class ARestartDispatchReuseTest(unittest.TestCase):
    """A completed persisted turn is reusable only for its exact dispatch."""

    @staticmethod
    def _identity_path(bench: _Bench) -> Path:
        return (
            bench.state
            / "worktrees"
            / "run-1"
            / "lane-a"
            / "test-reviewer"
            / "session"
            / "identity-1.json"
        )

    @staticmethod
    def _envelope_path(bench: _Bench, turn: int) -> Path:
        return lch.role_result_path(
            bench.state
            / "worktrees"
            / "run-1"
            / "lane-a"
            / "test-reviewer"
            / "checkout",
            turn,
        )

    def _first_dispatch(self, bench: _Bench) -> tuple[LaneContext, Path]:
        draft = _draft(
            _commit_suite(bench.product, parent=bench.head, body=_ROUND_ONE),
            "refs/maestro/candidates/run-1/lane-a/one",
        )
        context = _review_ctx(draft)
        verdict, findings = bench.actor.review_tests(context)
        self.assertIs(verdict, ReviewerVerdict.PASS)
        self.assertEqual(list(findings), [])
        identity = self._identity_path(bench)
        self.assertTrue(identity.is_file())
        self.assertTrue(self._envelope_path(bench, 1).is_file())
        return context, identity

    def _assert_second_dispatch(
        self, bench: _Bench, context: LaneContext
    ) -> None:
        verdict, findings = bench.actor.review_tests(context)
        self.assertIs(verdict, ReviewerVerdict.PASS)
        self.assertEqual(list(findings), [])
        self.assertEqual(bench.launcher.launches, 2)
        self.assertEqual(bench.launcher.resubmits, 0)
        self.assertTrue(self._envelope_path(bench, 2).is_file())

    def test_matching_identity_returns_the_persisted_payload_without_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            context, identity = self._first_dispatch(bench)
            bench.restart()

            verdict, findings = bench.actor.review_tests(context)

            self.assertIs(verdict, ReviewerVerdict.PASS)
            self.assertEqual(list(findings), [])
            self.assertEqual(bench.launcher.launches, 1)
            self.assertEqual(bench.launcher.resubmits, 0)
            self.assertTrue(identity.is_file())

    def test_different_input_digest_dispatches_turn_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            context, _identity = self._first_dispatch(bench)
            bench.restart()

            self._assert_second_dispatch(
                bench, replace(context, input_digest="34" * 32)
            )


    def test_changed_prompt_context_dispatches_turn_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            context, _identity = self._first_dispatch(bench)
            bench.restart()

            self._assert_second_dispatch(
                bench,
                replace(
                    context,
                    public_contract={
                        "acceptance_criteria": ["a different requirement"],
                        "declared_outputs": [_SUITE],
                    },
                    rejected_citation="the suite is written",
                ),
            )

    def test_missing_identity_keeps_the_legacy_turn_two_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            context, identity = self._first_dispatch(bench)
            identity.unlink()
            bench.restart()

            self._assert_second_dispatch(bench, context)

    def test_invalid_or_missing_envelope_dispatches_turn_two(self) -> None:
        for invalid_envelope in (True, False):
            with self.subTest(invalid_envelope=invalid_envelope):
                with tempfile.TemporaryDirectory() as tmp:
                    bench = _Bench(tmp)
                    context, _identity = self._first_dispatch(bench)
                    envelope = self._envelope_path(bench, 1)
                    if invalid_envelope:
                        envelope.write_text(
                            json.dumps({"verdict": "UNDECIDED"}), encoding="utf-8"
                        )
                    else:
                        envelope.unlink()
                    bench.restart()

                    self._assert_second_dispatch(bench, context)


class ATesterEnvelopeHarvestSurvivesRestart(unittest.TestCase):
    def test_tester_written_file_is_returned_from_the_harvested_envelope(self) -> None:
        agent_written = "// written by tester\nit('survives', () => {});\n"

        class _TesterWritingLauncher(_Launcher):
            def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
                if spec.pane_role == "tester":
                    suite = Path(spec.worktree) / _SUITE
                    suite.parent.mkdir(parents=True, exist_ok=True)
                    suite.write_text(agent_written, encoding="utf-8")
                    handle = super().launch(spec)
                    Path(spec.envelope_path).write_text(
                        json.dumps({"test_files": {_SUITE: agent_written}}),
                        encoding="utf-8",
                    )
                    return handle
                return super().launch(spec)

        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            bench.launcher = _TesterWritingLauncher()
            bench.actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, bench.launcher),
                bench.state,
                bench.target,
                _ROLE_ROUTES,
            )
            bench.actor.step = lambda lane, message, detail="": None
            context = LaneContext(
                run_id="run-1",
                lane=_lane(),
                plan_revision=1,
                plan_digest="ef" * 32,
                plan_artifact_ref="plan:x",
                input_digest="11" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=bench.head,
            )

            first = bench.actor.write_tests(context)
            checkout = (
                bench.state
                / "worktrees"
                / "run-1"
                / "lane-a"
                / "tester"
                / "checkout"
            )
            self.assertEqual(first, {"test_files": {_SUITE: agent_written}})
            self.assertEqual(
                (checkout / _SUITE).read_text(encoding="utf-8"), agent_written
            )
            bench.restart()

            recovered = bench.actor.write_tests(context)

            self.assertEqual(recovered, {"test_files": {_SUITE: agent_written}})
            self.assertEqual(bench.launcher.launches, 1)
            self.assertEqual(bench.launcher.resubmits, 0)
            self.assertEqual(
                (checkout / _SUITE).read_text(encoding="utf-8"), agent_written
            )


class ATestReviewerCheckoutIsTheDispatchedDraftCandidate(unittest.TestCase):
    def test_a_second_round_after_a_resume_reads_the_second_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            first = _commit_suite(bench.product, parent=bench.head, body=_ROUND_ONE)
            second = _commit_suite(bench.product, parent=bench.head, body=_ROUND_TWO)
            one = "refs/maestro/candidates/run-1/lane-a/one"
            two = "refs/maestro/candidates/run-1/lane-a/two"
            _git(bench.product, "update-ref", one, first)
            _git(bench.product, "update-ref", two, second)

            bench.actor.review_tests(_review_ctx(_draft(first, one)))
            bench.restart()
            bench.actor.review_tests(_review_ctx(_draft(second, two)))

            # The bug: this used to read [_ROUND_ONE, _ROUND_ONE]. The second
            # dispatch reached the reviewer with the first draft still on disk.
            self.assertEqual(bench.launcher.seen, [_ROUND_ONE, _ROUND_TWO])

    def test_the_tree_on_disk_holds_the_last_dispatched_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            first = _commit_suite(bench.product, parent=bench.head, body=_ROUND_ONE)
            second = _commit_suite(bench.product, parent=bench.head, body=_ROUND_TWO)
            one = "refs/maestro/candidates/run-1/lane-a/one"
            two = "refs/maestro/candidates/run-1/lane-a/two"
            _git(bench.product, "update-ref", one, first)
            _git(bench.product, "update-ref", two, second)

            bench.actor.review_tests(_review_ctx(_draft(first, one)))
            bench.restart()
            bench.actor.review_tests(_review_ctx(_draft(second, two)))

            tree = (
                bench.state
                / "worktrees"
                / "run-1"
                / "lane-a"
                / "test-reviewer"
                / "checkout"
            )
            self.assertEqual(
                (tree / _SUITE).read_text(encoding="utf-8"), _ROUND_TWO
            )


class AGitCheckoutIsResetToTheDispatchedSha(unittest.TestCase):
    """The same hole, on the roles whose tree is a git worktree.

    Only the tester was covered, by `_prepare`'s `sha and role == "tester"`
    branch. The code reviewer took the identical path and could read whatever
    the previous dispatch left, including a candidate that had since moved.
    """

    def test_a_second_dispatch_after_a_resume_reads_the_new_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp)
            suite = bench.product / _SUITE
            suite.parent.mkdir(parents=True, exist_ok=True)
            suite.write_text(_ROUND_ONE, encoding="utf-8")
            _git(bench.product, "add", _SUITE)
            _git(bench.product, "commit", "-m", "candidate one")
            one = _git(bench.product, "rev-parse", "HEAD")
            suite.write_text(_ROUND_TWO, encoding="utf-8")
            _git(bench.product, "add", _SUITE)
            _git(bench.product, "commit", "-m", "candidate two")
            two = _git(bench.product, "rev-parse", "HEAD")

            bench.actor.review_code(_code_ctx(one))
            bench.restart()
            bench.actor.review_code(_code_ctx(two))

            self.assertEqual(bench.launcher.seen, [_ROUND_ONE, _ROUND_TWO])


if __name__ == "__main__":
    unittest.main()
