"""The tests-node clause-3/4 chain measures with the gate's own runner.

`run-8a200af7f9044ce7a11a51b6908f37e3` executed a `tests` node whose gate was
`npx vitest run src/lib/seo/geo-entity-page.test.ts`. Ten attempts were
dispatched and none reached `lane-wp6-tests::review`: the two that ran to a
parsed envelope were refused

    TESTS_NO_NEW_CASES: no new collected case versus the parent commit

because `_prove_tests_red_at_parent` collected with `pytest --collect-only -q`
whatever the gate declared, and pytest collects nothing from a `.test.ts`
file. Zero collected became zero new, clause 3 refused SEMANTIC, the node
never verified, never merged, and its derived reviewer never dispatched.

`tests_chain.RunnerUnsupported` already names that exact failure -- "silently
measuring a vitest node's coverage with pytest would report zero cases for
every obligation" -- and the strength contract beside this chain was already
dispatching on the gate's runner. This chain was the one path that was not,
so it reached the silent zero the refusal exists to prevent.

Each case below fails when the dispatch is removed and `_pytest_prefix()` is
restored in its place.
"""

import unittest
from pathlib import Path
from typing import Sequence, Tuple

from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules import tests_chain as tc


VITEST_PATH = "src/lib/seo/geo-entity-page.test.ts"
VITEST_IDS = (
    "src/lib/seo/geo-entity-page.test.ts > renders the FAQ block",
    "src/lib/seo/geo-entity-page.test.ts > refuses below the threshold",
)


class _RecordingRunner(tc.CaseRunner):
    """A vitest-shaped runner that records what it was asked to measure."""

    name = "vitest"

    def __init__(self, ids: Tuple[str, ...], outcomes: Tuple[tc.CaseOutcome, ...]):
        self._ids = ids
        self._outcomes = outcomes
        self.collected_from: list = []
        self.ran_nodeids: list = []

    def collect(self, tree: Path, paths: Sequence[str],
                timeout_s: float = 120.0) -> Tuple[str, ...]:
        self.collected_from.append(tuple(paths))
        return self._ids

    def run(self, tree: Path, nodeids: Sequence[str],
            timeout_s: float = 300.0) -> tc.CaseRun:
        self.ran_nodeids.append(tuple(nodeids))
        return tc.CaseRun(
            outcomes=tuple(o for o in self._outcomes if o.nodeid in set(nodeids)),
            exit_code=1,
            collection_failed=False,
            command=("vitest", "run", "--reporter=json"),
        )


class _Delta:
    def __init__(self, touched: Tuple[str, ...]):
        self.touched = touched


class _Attempt:
    def __init__(self, path: Path):
        self.path = path
        self.base = "0" * 40
        self.repo = str(path)


def _tests_node(runner: str) -> st.PlanNode:
    return st.PlanNode(
        node_id="lane-wp6-tests",
        kind=st.NodeKind.TESTS,
        depth=0,
        gate_command=(runner, "run", VITEST_PATH) if runner else (),
        gate_selector=VITEST_PATH,
        instruction="Write the GEO entity-page gate suite.",
    )


class _dispatch:
    """Resolve `case_runner` to `runner`, and treat the parent tree as empty.

    The parent-collection half has its own case below, over a real git repo.
    Here it is held at "no parent nodeids", which is the ordinary shape of a
    newly created test file and keeps each case about the dispatch.
    """

    def __init__(self, runner):
        self.runner = runner
        self.seen = {}

    def __enter__(self):
        self._case_runner = tc.case_runner
        self._parent = tc.collect_parent_nodeids

        def _resolve(name):
            self.seen["name"] = name
            return self.runner

        tc.case_runner = _resolve
        tc.collect_parent_nodeids = lambda *a, **k: ()
        return self

    def __exit__(self, *exc):
        tc.case_runner = self._case_runner
        tc.collect_parent_nodeids = self._parent
        return False


def _prove(node, attempt, measured):
    """Call the chain directly. It reads nothing off `self`."""
    return sch.Scheduler._prove_tests_red_at_parent(
        object(), node, attempt, measured
    )


class ParentRedUsesTheGateRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.red = tuple(
            tc.CaseOutcome(nodeid=nodeid, status="failed",
                           reason="geoEntityPage is not defined")
            for nodeid in VITEST_IDS
        )

    def test_a_vitest_tests_node_is_collected_with_vitest_not_pytest(self) -> None:
        runner = _RecordingRunner(VITEST_IDS, self.red)
        with _dispatch(runner) as bound:
            verdict = _prove(_tests_node("vitest"),
                             _Attempt(Path(".")),
                             _Delta((VITEST_PATH,)))

        self.assertEqual(bound.seen.get("name"), "vitest")
        self.assertEqual(runner.collected_from, [(VITEST_PATH,)])
        self.assertTrue(
            verdict.verified,
            "two new vitest cases, both red at the parent, must satisfy "
            "clause 4; got {0!r}".format(verdict.reason),
        )

    def test_the_vitest_node_no_longer_refuses_no_new_cases(self) -> None:
        """The precise refusal run-8a200af looped on."""
        runner = _RecordingRunner(VITEST_IDS, self.red)
        with _dispatch(runner):
            verdict = _prove(_tests_node("vitest"),
                             _Attempt(Path(".")),
                             _Delta((VITEST_PATH,)))
        self.assertNotIn(tc.TestsRefusal.NO_NEW_CASES.value,
                         verdict.reason or "")

    def test_the_new_cases_are_executed_at_the_parent_by_that_runner(self) -> None:
        runner = _RecordingRunner(VITEST_IDS, self.red)
        with _dispatch(runner):
            _prove(_tests_node("vitest"), _Attempt(Path(".")),
                   _Delta((VITEST_PATH,)))
        self.assertEqual(runner.ran_nodeids, [VITEST_IDS])

    def test_a_green_new_case_is_still_refused_hollow_at_parent(self) -> None:
        """Dispatch must not weaken clause 4 -- a case that passes where the
        implementation is absent proves nothing, under either runner."""
        green = (tc.CaseOutcome(nodeid=VITEST_IDS[0], status="passed"),)
        runner = _RecordingRunner((VITEST_IDS[0],), green)
        with _dispatch(runner):
            verdict = _prove(_tests_node("vitest"), _Attempt(Path(".")),
                             _Delta((VITEST_PATH,)))
        self.assertFalse(verdict.verified)
        self.assertIn(tc.TestsRefusal.HOLLOW_AT_PARENT.value, verdict.reason)

    def test_an_unsupported_runner_is_refused_not_defaulted_to_pytest(self) -> None:
        verdict = _prove(_tests_node("go"), _Attempt(Path(".")),
                         _Delta((VITEST_PATH,)))
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.refusal_code,
                         tc.StrengthRefusal.RUNNER_UNSUPPORTED.value)
        self.assertEqual(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)


class RunCasesForCarriesTheSameCounts(unittest.TestCase):
    def test_a_vitest_run_produces_the_five_counts_clause_four_reads(self) -> None:
        outcomes = (
            tc.CaseOutcome(nodeid=VITEST_IDS[0], status="failed", reason="x"),
            tc.CaseOutcome(nodeid=VITEST_IDS[1], status="failed", reason="y"),
        )
        runner = _RecordingRunner(VITEST_IDS, outcomes)
        result = tc.run_cases_for(runner, Path("."), VITEST_IDS)
        self.assertEqual(
            result.counts,
            {"collected": 2, "passed": 0, "failed": 2,
             "skipped": 0, "errored": 0},
        )
        self.assertTrue(tc.adjudicate_parent_red(result, 2).verified)

    def test_a_collection_failure_stays_unparseable_rather_than_zero(self) -> None:
        """`GateCounts.parse` reads empty counts as "no report", which clause 4
        sends to COLLECTION_FAILED/ENVIRONMENTAL. Zeroes would read as a red
        suite that collected nothing, which is a different fact."""

        class _Broken(_RecordingRunner):
            def run(self, tree, nodeids, timeout_s=300.0):
                return tc.CaseRun(outcomes=(), exit_code=1,
                                  collection_failed=True,
                                  command=("vitest", "run"))

        result = tc.run_cases_for(_Broken((), ()), Path("."), VITEST_IDS)
        self.assertEqual(result.counts, {})
        verdict = tc.adjudicate_parent_red(result, 2)
        self.assertEqual(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)
        self.assertIn(tc.TestsRefusal.COLLECTION_FAILED.value, verdict.reason)


class ParentCollectionUsesTheSameRunner(unittest.TestCase):
    def test_collect_parent_nodeids_delegates_to_the_runner(self) -> None:
        """A parent tree collected with the wrong runner returns nothing, which
        silently reclassifies every edited case as new."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            target = root / VITEST_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("// cases\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t", "-c",
                 "user.name=t", "commit", "-q", "-m", "seed"], check=True)
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()

            runner = _RecordingRunner(VITEST_IDS, ())
            parent = tc.collect_parent_nodeids(
                root, head, (VITEST_PATH,), runner=runner)

        self.assertEqual(parent, VITEST_IDS)
        self.assertEqual(runner.collected_from, [(VITEST_PATH,)])


if __name__ == "__main__":
    unittest.main()
