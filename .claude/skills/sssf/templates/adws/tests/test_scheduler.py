"""Executable proof of §12.2 step 6 — the scheduler that makes the rest run.

Everything before this step was a part: a vocabulary, a lifecycle store, a
classifier, a watchdog, a measurement bracket. This is the piece that
executes them against a real dependency graph, and it is therefore the first
place several of the design's central claims can be run at all rather than
argued.

One of those claims is the reason this file exists in the shape it does.
§16.3 item 1 has named the same gap through three rewrites: **a node that
legitimately fails, so that §7.5's retry and block classification runs end
to end**, was unexecuted by any means because no scheduler existed to run
it. `SemanticRetryTests` below is that gap closed — a node whose post-node
gate is genuinely red on its first attempt, retried with a mutated prompt,
green on its second, merged; and a node that never succeeds, spending its
cumulative ceiling and blocking `SEMANTIC_BUDGET_EXHAUSTED`.

Every test builds a real git repository in a temporary directory and drives
real worktrees, real commits, and real merges. What is injected is the agent
and the gate — the two things that would otherwise need a pane and a
subprocess suite — and they are injected behind the same protocol the real
adapters will implement, never patched in.

Grouped by the section each settles:

  §7.1  the ready set, and MERGED rather than VERIFIED as its predicate
  §7.2  concurrency, and the pane limit that comes with it
  §7.6  the attempt window opens before the worktree exists
  §7.4  the falsifiable gate, orchestrated
  §7.5  semantic retry end to end, the ceiling, and worker containment
  §7.3  the code-node predicate under the scheduler
  §8.7  conflict, the cascade, and independent branches finishing
  §7.8  cancellation and resume
  §8.8  final acceptance at the candidate-ACCEPTED quiescence
  §7.3  the declared run outcome

Run with:  uv run adws/adw_test.py        (the whole suite; `-k` filters on
                                           test *method* names, not modules)
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import verification as vf  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from test_watchdog import FakeClock  # noqa: E402

# How long a test here waits for the scheduler's watchdog to *arrive* at a
# quiescence phase. Every wait expressed in terms of this constant is a
# precondition: overrunning it means "the watchdog has not fired yet", never
# "the watchdog is wrong". Reaching that phase costs real work — a provision
# call, a node timeout expiring, a kill and its quiescence proof — and this
# suite's default is `-n auto` (see pytest.ini), so on an 18-core machine
# eighteen workers contend for one disk while the operator's other work runs
# alongside.
#
# The bounds this replaced were 3.0s and 30.0s, and the 30.0s one was itself
# already a *raised* bound carrying a comment explaining why 3.0s had been too
# low. It failed anyway, in a full-suite `-n auto` run, which is the argument
# against picking a number by measuring: a bound placed at roughly the
# duration it measures is a coin toss whoever measures it. This one is a hang
# detector instead — generous enough that only a genuine deadlock reaches it,
# bounded so a deadlock still reports rather than hanging the suite (#57,
# same treatment as `ARRIVAL_TIMEOUT_S` in tests/test_coordinator.py for #50).
#
# A bound that asserts a latency *property* must not be folded in here. The
# two sites below wait for arrival only; nothing in this module asserts that
# the watchdog is fast.
ARRIVAL_TIMEOUT_S = float(os.environ.get("MAESTRO_TEST_ARRIVAL_TIMEOUT_S", "60.0"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "maestro@example.invalid")
    _git(repo, "config", "user.name", "Maestro Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def green(passed: int = 2) -> wt.GateResult:
    return wt.GateResult(
        label="gate",
        scope="node",
        selector="sel",
        command=("gate",),
        exit_code=0,
        green=True,
        counts={"passed": passed},
    )


def red(passed: int = 1, failed: int = 1) -> wt.GateResult:
    return wt.GateResult(
        label="gate",
        scope="node",
        selector="sel",
        command=("gate",),
        exit_code=1,
        green=False,
        counts={"passed": passed, "failed": failed},
    )


def unparseable() -> wt.GateResult:
    """A gate that exited nonzero and reported no countable result — §10.2's
    ENVIRONMENTAL case, which is neither a red gate nor a green one."""
    return wt.GateResult(
        label="gate",
        scope="node",
        selector="sel",
        command=("gate",),
        exit_code=1,
        green=False,
        counts={},
    )


class Recorder:
    """A callable that remembers how it was called, for order assertions."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class PassingReview:
    """Typed fixture evidence for the mandatory derived review edge."""

    def __init__(self, candidate_sha):
        self.passed = True
        self.subject_digest = "fixture-review-" + candidate_sha
        self.findings = ()
        self.advisories = ()
        self.unreachable = ()


class SchedulerFixture(unittest.TestCase):
    """A two-node plan over a real repository, with the agent and the gate
    injected behind the protocols the real adapters implement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = _make_repo(self.root)
        self.store = lc.LifecycleStore(self.root / "lifecycle.db")
        self.addCleanup(self.store.close)

        # The integration branch is a worktree of its own, so merges never
        # touch the developer's checkout (§11.2 projects it at run start).
        self.integration = self.root / "integration"
        _git(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            "integration/run1",
            str(self.integration),
            "HEAD",
        )

        self.written = {}  # node_id -> {relpath: content}
        self.prompts = {}  # node_id -> [retry_prompt per attempt]
        self.gate_script = {}  # (node_id, phase) -> [GateResult, ...]
        self.raise_for = {}  # node_id -> exception to raise, once
        self.exit_codes = {}  # node_id -> exit code for a code node
        self.quiesce_calls = []  # (attempt identity, phase) -> []
        # Epoch-based, frozen until a test advances it. Production watchdog
        # and backstop compare against `started_at` / `last_transition_at`
        # (also epoch). Starting at 0.0 would silently disarm the backstop
        # tests that backdate `last_transition_at` to 1990.
        self.clock = FakeClock(time.time())

    def config(self, **kw) -> st.SchedulerConfig:
        base = dict(
            concurrency=2,
            node_timeout_s=60.0,
            turn_timeout_s=30.0,
            final_acceptance_timeout_s=60.0,
            backstop_t_s=600.0,
            semantic_ceiling=2,
        )
        base.update(kw)
        return st.SchedulerConfig(**base)

    # ── the injected adapter seams ──────────────────────────────────────────

    def run_node(
        self, attempt, node, record, retry_prompt, on_launch, cancel_requested
    ):
        self.prompts.setdefault(node.node_id, []).append(retry_prompt)
        on_launch(None)
        boom = self.raise_for.pop(node.node_id, None)
        if boom is not None:
            raise boom
        for rel, content in self.written.get(node.node_id, {}).items():
            target = attempt.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return sch.NodeExecution(
            envelope_parsed=True,
            exit_code=self.exit_codes.get(node.node_id, 0),
            launched_pid=None,
        )

    def review_attempt(
        self, attempt, node, record, base_sha, candidate_sha, _resume_existing
    ):
        return PassingReview(candidate_sha)

    def continue_node(
        self,
        attempt,
        node,
        record,
        repair_prompt,
        rejected_candidate_sha,
        builder_generation,
        cancel_requested,
    ):
        execution = self.run_node(
            attempt, node, record, repair_prompt, lambda _pid: None, cancel_requested
        )
        for rel in self.written.get(node.node_id, {}):
            target = attempt.path / rel
            if target.exists():
                target.write_text(target.read_text() + "# repaired\n")
        return sch.RepairExecution(
            execution=execution,
            acknowledged_rejected_sha=rejected_candidate_sha,
            builder_generation=builder_generation,
        )

    def run_gate(self, attempt, node, phase, cancel_requested):
        scripted = self.gate_script.get((node.node_id, phase))
        if scripted:
            return scripted.pop(0)
        # The default shape a healthy agent node produces: the pre-gate is red
        # because the behaviour is absent, the post-gate green because the
        # agent supplied it, and the falsify-gate red again because taking the
        # production file back out takes the behaviour with it (§7.4).
        # Falsifiability, in fixture form, from both sides.
        return red() if phase in ("pre", "falsify") else green()

    def quiesce_attempt(self, record, phase):
        self.assertIsInstance(record, st.AttemptRecord)
        self.quiesce_calls.append((record.key, phase))

    def deps(self, **kw):
        base = dict(
            store=self.store,
            repo=self.repo,
            integration_path=self.integration,
            integration_branch="integration/run1",
            worktrees_root=self.root / "wt",
            scratch_root=self.root / "scratch",
            run_node=self.run_node,
            run_gate=self.run_gate,
            review_attempt=self.review_attempt,
            continue_node=self.continue_node,
            receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
            run_integration_gate=lambda path, specs, cancel_requested: green(3),
            quiesce_attempt=self.quiesce_attempt,
        )
        base.update(kw)
        return sch.SchedulerDeps(**base)

    # ── plan helpers ────────────────────────────────────────────────────────

    def agent(self, node_id, depth=0, needs=(), outputs=None, specs=()):
        return st.PlanNode(
            node_id=node_id,
            kind=st.NodeKind.AGENT,
            depth=depth,
            needs=tuple(needs),
            outputs=tuple(outputs if outputs is not None else (f"{node_id}.py",)),
            specs=tuple(specs),
            instruction=f"Build {node_id}.",
            gate_command=("gate",),
            gate_selector=f"tests/{node_id}",
        )

    def code(self, node_id, depth=0, needs=(), outputs=(), expects_changes=False):
        return st.PlanNode(
            node_id=node_id,
            kind=st.NodeKind.CODE,
            depth=depth,
            needs=tuple(needs),
            outputs=tuple(outputs),
            command=("true",),
            expects_changes=expects_changes,
        )

    def schedule(self, nodes, config=None, deps=None, run_id="run1"):
        scheduler = sch.Scheduler(
            run_id=run_id,
            nodes=list(nodes),
            config=config or self.config(),
            deps=deps or self.deps(),
            plan_digest="digest-" + run_id,
            time_source=self.clock,
        )
        self.addCleanup(scheduler.shutdown)
        return scheduler

    def expire_running_attempts(self, extra_s: float, run_id: str = "run1") -> None:
        """Jump the injected clock past every RUNNING attempt's `started_at`.

        Watchdog elapsed time is `time_source() - started_at`. `started_at`
        is the store's real epoch, so `clock.advance(timeout)` from the
        freeze point is not enough on a slow machine: the attempt may have
        started after the freeze by more than `timeout`. Set relative to
        the attempt itself.
        """
        running = [
            a
            for a in self.store.attempts_for(run_id)
            if a.state is st.NodeState.RUNNING
        ]
        self.assertTrue(running, "no RUNNING attempt to expire")
        latest = max(a.started_at for a in running)
        self.clock.set(latest + extra_s)

    def states(self, run_id="run1"):
        return {r.node_id: r.state for r in self.store.node_records(run_id)}


class AttemptIdentityRecoveryTests(SchedulerFixture):
    def test_ready_node_advances_past_recovery_rows_without_redispatch_spin(self):
        """A restored pointer may lag cancelled audit rows after recovery."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        for attempt_no in (1, 2):
            self.store.conn.execute(
                "INSERT INTO attempts "
                "(run_id,node_id,attempt_no,base_sha,state,started_at,"
                " turn_count,extra_json) VALUES (?,?,?,?,?,?,0,'{}')",
                (
                    "run1",
                    "a",
                    attempt_no,
                    base,
                    st.NodeState.CANCELLED.value,
                    time.time(),
                ),
            )
        self.store.conn.execute(
            "UPDATE node_lifecycle SET state=?, attempt_no=?"
            " WHERE run_id=? AND node_id=?",
            (st.NodeState.PENDING.value, 1, "run1", "a"),
        )

        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(
            [row.attempt_no for row in self.store.attempts_for("run1", "a")], [1, 2, 3]
        )
        self.assertEqual(len(self.prompts["a"]), 1)

    def test_future_level_scheduler_defect_is_not_silently_retried(self):
        future = mock.Mock()
        future.done.return_value = True
        future.result.side_effect = RuntimeError("escaped worker defect")

        with self.assertRaisesRegex(RuntimeError, "escaped worker defect"):
            sch._wait_any([("a", future)])


# ── §7.1 scheduler-derived review projection ───────────────────────────────


class ReviewProjectionTests(SchedulerFixture):
    def review_deps(self):
        return self.deps(
            review_attempt=lambda *args: None,
            receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
        )

    def test_authored_review_node_remains_refused(self):
        with self.assertRaisesRegex(ValueError, "derived by the scheduler"):
            st.PlanNode(node_id="build::review", kind=st.NodeKind.REVIEW, depth=1)

    def test_authored_tests_node_is_preserved_unchanged(self):
        tests = self.agent("tests", outputs=("tests/test_build.py",))
        # The authored kind is the only difference relevant to projection.
        tests = st.PlanNode(
            node_id=tests.node_id,
            kind=st.NodeKind.TESTS,
            depth=tests.depth,
            needs=tests.needs,
            outputs=tests.outputs,
            specs=tests.specs,
            instruction=tests.instruction,
            gate_command=tests.gate_command,
            gate_selector=tests.gate_selector,
        )
        build = self.agent("build", depth=1, needs=("tests",))

        scheduler = self.schedule([tests, build], deps=self.review_deps())

        self.assertEqual(tuple(scheduler.authored_nodes), ("tests", "build"))
        self.assertIs(scheduler.authored_nodes["tests"], tests)
        self.assertEqual(scheduler.nodes["tests"], tests)
        self.assertEqual(scheduler.nodes["build"].needs, ("tests",))

    def test_projects_one_stable_review_per_reviewable_build(self):
        template = self.agent("tests", outputs=("tests/test_build.py",))
        tests = replace(template, kind=st.NodeKind.TESTS)
        build = self.agent("build", depth=1, needs=("tests",))
        code = self.code("code", depth=0)

        scheduler = self.schedule([tests, build, code], deps=self.review_deps())

        reviews = scheduler._derived_review_nodes()
        self.assertEqual({review.node_id for review in reviews}, {"build::review"})
        self.assertNotIn("code::review", scheduler.nodes)
        build_review = scheduler.nodes["build::review"]
        self.assertIs(build_review.kind, st.NodeKind.REVIEW)
        self.assertEqual(build_review.review_of, "build")
        self.assertEqual(build_review.needs, ("build",))
        self.assertEqual(build_review.outputs, ())
        self.assertEqual(build_review.depth, scheduler.nodes["build"].depth + 1)

    def test_downstream_projection_passes_through_review_edge(self):
        tests = self.agent("tests", outputs=("tests/test_build.py",))
        build = self.agent("build", depth=1, needs=("tests",))
        downstream = self.code("integration", depth=2, needs=("build",))

        scheduler = self.schedule([tests, build, downstream], deps=self.review_deps())

        self.assertEqual(scheduler.authored_nodes["integration"].needs, ("build",))
        self.assertEqual(scheduler.nodes["build::review"].needs, ("build",))
        self.assertEqual(scheduler.nodes["integration"].needs, ("build::review",))
        self.assertGreater(
            scheduler.nodes["integration"].depth, scheduler.nodes["build::review"].depth
        )

    def test_report_exposes_derived_review_state_without_merging_it(self):
        scheduler = self.schedule([self.agent("build")], deps=self.review_deps())
        scheduler.project()

        report = scheduler._declare()

        self.assertEqual(report.review_nodes, {"build::review": "PENDING"})
        self.assertNotIn("build::review", report.merged)

    def test_cancellation_includes_derived_review_nodes(self):
        scheduler = self.schedule([self.agent("build")], deps=self.review_deps())
        scheduler.cancel()

        scheduler.run()

        self.assertEqual(
            self.states(), {"build": "CANCELLED", "build::review": "CANCELLED"}
        )

    def test_resume_adopts_the_existing_derived_projection_once(self):
        tests = self.agent("tests", outputs=("tests/test_build.py",))
        build = self.agent("build", depth=1, needs=("tests",))
        downstream = self.code("integration", depth=2, needs=("build",))

        first = self.schedule([tests, build, downstream], deps=self.review_deps())
        first.project()
        before = self.store.node_records("run1")
        self.assertEqual(
            next(record.needs for record in before if record.node_id == "integration"),
            ("build::review",),
        )
        self.assertEqual(self.store.node_outputs("run1", "build::review"), ())

        resumed = self.schedule([tests, build, downstream], deps=self.review_deps())
        resumed.project()

        self.assertEqual(resumed.nodes, first.nodes)
        self.assertEqual(self.store.node_records("run1"), before)
        self.assertEqual(resumed.nodes["integration"].needs, ("build::review",))

    def test_a_derived_review_is_never_a_merge_candidate(self):
        build = self.agent("build")
        scheduler = self.schedule([build], deps=self.review_deps())
        scheduler.project()
        review = next(
            record
            for record in self.store.node_records("run1")
            if record.node_id == "build::review"
        )

        with (
            mock.patch.object(wt, "merge_ready", return_value=review),
            mock.patch.object(wt, "merge_verified_node") as merge,
        ):
            scheduler._merge_frontier()

        merge.assert_not_called()


# ── §7.1 / §7.2 the ready set and concurrency ───────────────────────────────


class ReadySetTests(SchedulerFixture):
    def test_two_independent_nodes_both_merge(self):
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        report = self.schedule([self.agent("a"), self.agent("b")]).run()
        self.assertEqual(
            self.states(),
            {
                "a": "MERGED",
                "a::review": "ACCEPTED",
                "b": "MERGED",
                "b::review": "ACCEPTED",
            },
        )
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_dependent_node_waits_for_its_dependency_to_merge(self):
        """§7.1 — the predicate is MERGED, not VERIFIED or SUCCEEDED. A
        dependent must not start against work that has not landed."""
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        order = []
        inner = self.run_node

        def recording(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            order.append(("start", node.node_id, dict(self.states())))
            return inner(
                attempt, node, record, retry_prompt, on_launch, cancel_requested
            )

        self.schedule(
            [self.agent("a"), self.agent("b", depth=1, needs=("a",))],
            deps=self.deps(run_node=recording),
        ).run()
        started_b = [snapshot for phase, node, snapshot in order if node == "b"]
        self.assertEqual(len(started_b), 1)
        self.assertEqual(started_b[0]["a"], "MERGED")

    def test_merges_land_in_depth_then_node_id_order(self):
        """§8.5 — order is a function of the graph, never of finish order."""
        self.written = {n: {f"{n}.py": n} for n in ("c", "a", "b")}
        self.schedule([self.agent("c"), self.agent("a"), self.agent("b")]).run()
        subjects = _git(self.integration, "log", "--format=%s", "--merges").splitlines()
        self.assertEqual([s.split()[-1] for s in reversed(subjects)], ["a", "b", "c"])

    def test_concurrency_is_never_exceeded(self):
        """§7.2 — the concurrency limit is also the pane limit, because one
        in-flight node holds at most one launch."""
        self.written = {n: {f"{n}.py": n} for n in "abcde"}
        live = []
        peak = []
        guard = threading.Lock()
        inner = self.run_node

        def counting(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            with guard:
                live.append(node.node_id)
                peak.append(len(live))
            try:
                return inner(
                    attempt, node, record, retry_prompt, on_launch, cancel_requested
                )
            finally:
                with guard:
                    live.remove(node.node_id)

        self.schedule(
            [self.agent(n) for n in "abcde"],
            config=self.config(concurrency=2),
            deps=self.deps(run_node=counting),
        ).run()
        self.assertLessEqual(max(peak), 2)


# ── §8.2 the integration branch may not shadow the attempt namespace ────────


class IntegrationBranchGuardTests(SchedulerFixture):
    """Found by running it, and the trap is that the colliding name is the
    obvious one to choose."""

    def test_the_obvious_integration_branch_name_is_refused(self):
        """Attempt branches are `maestro/{run_id}/{node_id}/a{n}`, and git
        stores a ref as a file. A branch named `maestro/{run_id}` therefore
        occupies the path every attempt branch needs as a directory, and
        `git worktree add -b` dies on the first node with a message naming a
        ref rather than the branch the operator picked."""
        with self.assertRaises(sch.IntegrationBranchCollision):
            sch.Scheduler(
                run_id="run1",
                nodes=[self.agent("a")],
                config=self.config(),
                deps=self.deps(integration_branch="maestro/run1"),
            )

    def test_a_nested_name_under_the_run_namespace_is_also_refused(self):
        with self.assertRaises(sch.IntegrationBranchCollision):
            sch.Scheduler(
                run_id="run1",
                nodes=[self.agent("a")],
                config=self.config(),
                deps=self.deps(integration_branch="maestro/run1/integration"),
            )

    def test_a_name_outside_the_namespace_is_accepted(self):
        scheduler = sch.Scheduler(
            run_id="run1",
            nodes=[self.agent("a")],
            config=self.config(),
            deps=self.deps(),
        )
        self.addCleanup(scheduler.shutdown)
        self.assertEqual(scheduler.run_id, "run1")

    def test_another_runs_namespace_does_not_collide(self):
        """The guard is scoped to *this* run's namespace, not to the word
        `maestro` — refusing more than git does would be a different rule."""
        scheduler = sch.Scheduler(
            run_id="run2",
            nodes=[self.agent("a")],
            config=self.config(),
            deps=self.deps(integration_branch="maestro/run1"),
        )
        self.addCleanup(scheduler.shutdown)
        self.assertEqual(scheduler.run_id, "run2")


# ── §7.6 the attempt window opens before the worktree exists ────────────────


class AttemptWindowTests(SchedulerFixture):
    def test_running_is_written_before_the_worktree_is_created(self):
        """§7.6 — `PENDING→RUNNING` is written before worktree creation,
        provision, the pre-gate, and the baseline inventory, so the attempt
        window covers all of them. A window that opened after the worktree
        would leave a hung `git worktree add` unwatched by anything."""
        self.written = {"a": {"a.py": "A\n"}}
        seen = {}

        def observing(attempt, node, phase, cancel_requested):
            seen.setdefault(
                "state_at_pre_gate", self.store.get_node("run1", node.node_id).state
            )
            return self.run_gate(attempt, node, phase, cancel_requested)

        self.schedule([self.agent("a")], deps=self.deps(run_gate=observing)).run()
        self.assertIs(seen["state_at_pre_gate"], st.NodeState.RUNNING)


# ── §7.4 the falsifiable gate, orchestrated ─────────────────────────────────


class FalsifiableGateTests(SchedulerFixture):
    def test_a_green_pre_gate_blocks_the_node_and_never_runs_the_agent(self):
        """§7.4 — `GATE_NOT_FALSIFIABLE`, terminal and non-retryable. And the
        agent must not run: a gate that cannot fail proves nothing about work
        that has not happened, so spending an agent on it is pure waste."""
        self.gate_script[("a", "pre")] = [green()]
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule([self.agent("a")]).run()
        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.GATE_NOT_FALSIFIABLE)
        self.assertNotIn("a", self.prompts)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_a_green_pre_gate_is_not_retried(self):
        """Re-running an agent cannot make a gate falsifiable."""
        self.gate_script[("a", "pre")] = [green(), green(), green()]
        self.schedule([self.agent("a")]).run()
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 1)

    def test_an_independent_branch_finishes_while_a_sibling_blocks(self):
        """§8.7 — BLOCKED is terminal for the node, not for the run."""
        self.gate_script[("a", "pre")] = [green()]
        self.written = {"b": {"b.py": "B\n"}}
        self.schedule([self.agent("a"), self.agent("b")]).run()
        self.assertEqual(
            self.states(),
            {
                "a": "BLOCKED",
                "a::review": "PENDING",
                "b": "MERGED",
                "b::review": "ACCEPTED",
            },
        )


# ── §7.5 semantic retry, end to end — §16.3 item 1's last open claim ────────


class SemanticRetryTests(SchedulerFixture):
    """A node that legitimately fails, so §7.5 runs end to end.

    Named as unexecuted through three rewrites of §16.3 item 1, because no
    scheduler existed to run it. These are that item's discharge.
    """

    def test_a_red_post_gate_retries_with_a_mutated_prompt_and_then_merges(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red(), green()]
        report = self.schedule([self.agent("a")]).run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 1)
        # SEMANTIC is the only class that mutates the repair prompt (§7.5).
        first, second = self.prompts["a"]
        self.assertIsNone(first)
        self.assertIsNotNone(second)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_the_retry_prompt_names_the_offending_paths_for_a_permission_failure(self):
        """§7.5 — the offending paths are named in the retry prompt, and the
        naming is what makes the retry genuinely new instructions rather than
        the same request repeated."""
        self.written = {"a": {"a.py": "A\n", "not-declared.py": "X\n"}}

        def second_attempt_is_clean(
            attempt, node, record, retry_prompt, on_launch, cancel_requested
        ):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            repairing = len(self.prompts[node.node_id]) > 1
            if repairing:
                (attempt.path / "not-declared.py").unlink()
            files = (
                {"a.py": "A\n"}
                if repairing
                else {"a.py": "A\n", "not-declared.py": "X\n"}
            )
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def continue_existing(
            attempt,
            node,
            record,
            repair_prompt,
            rejected_candidate_sha,
            builder_generation,
            cancel_requested,
        ):
            execution = second_attempt_is_clean(
                attempt,
                node,
                record,
                repair_prompt,
                lambda _pid: None,
                cancel_requested,
            )
            return sch.RepairExecution(
                execution=execution,
                acknowledged_rejected_sha=rejected_candidate_sha,
                builder_generation=builder_generation,
            )

        self.schedule(
            [self.agent("a")],
            deps=self.deps(
                run_node=second_attempt_is_clean, continue_node=continue_existing
            ),
        ).run()
        self.assertIn("not-declared.py", self.prompts["a"][1])
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 1)

    def test_the_cumulative_ceiling_blocks_a_node_that_never_succeeds(self):
        """§7.5 — at most K SEMANTIC attempts per `(run_id, node_id)` across
        all bases, then `SEMANTIC_BUDGET_EXHAUSTED`."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        report = self.schedule(
            [self.agent("a")], config=self.config(semantic_ceiling=2)
        ).run()
        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)
        self.assertEqual(node.attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_a_blocked_node_is_reported_with_its_reason(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        report = self.schedule(
            [self.agent("a")], config=self.config(semantic_ceiling=1)
        ).run()
        self.assertEqual(
            dict(report.blocked)["a"], st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED
        )

    def _blocked_transition(self, node_id: str = "a"):
        """The one BLOCKED transition row for a node, from the audit tier."""
        rows = [
            row
            for row in self.store.audit_transitions("run1", node_id)
            if row.get("to_state") == st.NodeState.BLOCKED.value
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_the_blocking_transition_records_which_clause_failed(self):
        """§1.1 item 4 — a block is terminal, so its transition row is the
        ledger's last chance to say what the attempt failed on.

        An observed run wrote `blocked:SEMANTIC_BUDGET_EXHAUSTED` with
        `detail_json == {}`, and the reason its last attempt failed existed
        nowhere in the store — it was recoverable only from the worktree's git
        history. The earlier attempts' reasons survived only because they had
        been rendered into the next attempt's prompt, which makes prose the
        carrier of the evidence (§1.2).
        """
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        self.schedule([self.agent("a")], config=self.config(semantic_ceiling=1)).run()

        detail = self._blocked_transition().get("detail", {})
        self.assertEqual(detail.get("clause"), 3, "the red post-node gate is clause 3")
        self.assertNotEqual(detail, {})

    def test_the_blocking_transition_names_the_offending_paths(self):
        """§1.1 item 4 / §7.5 — the paths that justified calling a permission
        failure SEMANTIC are recorded where the block is recorded, not only in
        the retry prompt that the blocking attempt never gets."""
        self.written = {"a": {"a.py": "A\n", "not-declared.py": "X\n"}}
        self.schedule([self.agent("a")], config=self.config(semantic_ceiling=1)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        detail = self._blocked_transition().get("detail", {})
        self.assertEqual(detail.get("clause"), 4)
        self.assertIn("not-declared.py", detail.get("offending_paths", []))

    def test_an_exhausted_environmental_budget_records_its_reason(self):
        """The same evidence on the non-SEMANTIC arm: a node blocked on its
        environmental budget records what its last attempt failed as."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "pre")] = [unparseable()] * 10
        self.schedule(
            [self.agent("a")], config=self.config(environmental_retries=0)
        ).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.block_reason, st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
        detail = self._blocked_transition().get("detail", {})
        self.assertEqual(detail.get("clause"), 2)
        self.assertNotEqual(detail, {})


class ContainmentTests(SchedulerFixture):
    """§7.5 / §13.3 — the negative test for an unanticipated worker failure."""

    def test_an_unexpected_exception_leaves_siblings_running_and_the_run_alive(self):
        """A `ThreadPoolExecutor` swallows an unhandled exception into a
        future where nobody looks. The worker's top-level handler is what
        stops one node's collapse from being silent, and a worker failure
        writes only its own node's state."""
        self.raise_for = {"a": RuntimeError("something nobody anticipated")}
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        report = self.schedule(
            [self.agent("a"), self.agent("b")],
            config=self.config(environmental_retries=0),
        ).run()
        self.assertIs(self.store.get_node("run1", "b").state, st.NodeState.MERGED)
        self.assertIsNotNone(report.outcome)

    def test_an_unclassified_failure_is_environmental_never_semantic(self):
        """Fail-closed: an engine bug must never be recorded as a verdict
        about the code under test."""
        self.raise_for = {"a": RuntimeError("engine bug")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule(
            [self.agent("a")], config=self.config(environmental_retries=0)
        ).run()
        attempt = self.store.get_attempt("run1", "a", 1)
        self.assertIs(attempt.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_an_environmental_failure_is_retried_and_can_then_succeed(self):
        self.raise_for = {"a": OSError("transient")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 2)


class EnvironmentalDetailTests(SchedulerFixture):
    """§1.1 item 4 on the ENVIRONMENTAL arm, which had no evidence at all.

    An observed run spent three ENVIRONMENTAL retries and blocked
    `ENVIRONMENTAL_BUDGET_EXHAUSTED` with `detail_json == {}` on every row, so
    the operator learned the class and nothing else -- not which signal
    convicted the attempt, not what the failure was. The three arms that reach
    the ledger without a verification verdict are the ones that were empty: a
    watchdog stall, an unanticipated worker exception, and a failed
    create-time check. Each now carries the typed observation it already held.
    """

    def _transitions(self, node_id="a"):
        return self.store.audit_transitions("run1", node_id)

    def _row(self, reason_prefix, node_id="a"):
        rows = [
            row
            for row in self._transitions(node_id)
            if str(row.get("reason", "")).startswith(reason_prefix)
        ]
        self.assertTrue(
            rows,
            "no {} row: {}".format(
                reason_prefix, [r.get("reason") for r in self._transitions(node_id)]
            ),
        )
        return rows[0]

    def _running_attempt(self, scheduler):
        """One RUNNING attempt owned by the store, with no thread racing it.

        The watchdog's failure callback is the code under test, so it is
        called directly rather than waited for: a real stall would need a real
        clock, and a test that sleeps to observe a timeout is a test that
        flakes on a loaded machine.
        """
        scheduler.project()
        head = wt.integration_head(self.repo, "integration/run1")
        attempt_no = self.store.start_attempt("run1", "a", head)
        return self.store.get_attempt("run1", "a", attempt_no)

    def test_a_watchdog_stall_records_which_signal_convicted_the_attempt(self):
        """§7.6 names three signals and the watchdog already decides between
        them; the scheduler accepted that answer as an argument and dropped
        it."""
        scheduler = self.schedule([self.agent("a")])
        record = self._running_attempt(scheduler)
        watchdog, _ = scheduler._start_liveness()

        watchdog._fail_attempt(
            record, st.RetryClass.ENVIRONMENTAL, wd.StallReason.NODE_TIMEOUT.value
        )

        detail = self._row("retry:ENVIRONMENTAL").get("detail", {})
        self.assertEqual(detail.get("reason"), "NODE_TIMEOUT")

    def test_an_environmental_block_records_which_signal_convicted_it(self):
        """The same evidence on the terminal row. A block is the ledger's last
        chance to say what happened, and this arm said nothing."""
        scheduler = self.schedule(
            [self.agent("a")], config=self.config(environmental_retries=0)
        )
        record = self._running_attempt(scheduler)
        watchdog, _ = scheduler._start_liveness()

        watchdog._fail_attempt(
            record, st.RetryClass.ENVIRONMENTAL, wd.StallReason.PROCESS_DEAD.value
        )

        node = self.store.get_node("run1", "a")
        self.assertIs(node.block_reason, st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
        detail = self._row("blocked:ENVIRONMENTAL_BUDGET_EXHAUSTED").get("detail", {})
        self.assertEqual(detail.get("reason"), "PROCESS_DEAD")

    def test_the_reason_is_the_watchdogs_and_is_not_invented_here(self):
        """Three signals exist, so a row that always says the same one proves
        nothing. The value travels from the watchdog rather than a constant."""
        observed = []
        for stall in (wd.StallReason.TURN_TIMEOUT, wd.StallReason.NODE_TIMEOUT):
            self.setUp()
            scheduler = self.schedule([self.agent("a")])
            record = self._running_attempt(scheduler)
            watchdog, _ = scheduler._start_liveness()
            watchdog._fail_attempt(record, st.RetryClass.ENVIRONMENTAL, stall.value)
            observed.append(
                self._row("retry:ENVIRONMENTAL").get("detail", {}).get("reason")
            )
        self.assertEqual(observed, ["TURN_TIMEOUT", "NODE_TIMEOUT"])

    def test_an_unanticipated_worker_exception_records_what_it_was(self):
        """`classify` reads no ENVIRONMENTAL evidence by design (§7.5), so the
        containment arm has to record the observation itself. The launcher's
        typed vocabulary arrives inside the exception."""
        self.raise_for = {"a": RuntimeError("LAUNCH_REFUSED:agent_name_taken")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule(
            [self.agent("a")], config=self.config(environmental_retries=0)
        ).run()

        detail = self._row("blocked:ENVIRONMENTAL_BUDGET_EXHAUSTED").get("detail", {})
        self.assertIn("RuntimeError", detail.get("reason", ""))
        self.assertIn("LAUNCH_REFUSED:agent_name_taken", detail.get("reason", ""))

    def test_a_failed_create_check_records_which_check_failed(self):
        """The third empty arm. `CheckResult.detail` already names the failing
        §8.3 check and was discarded at the call site.

        Patched rather than injected because `check_at_create` is a measurement
        this module performs, not one of the adapter seams the fixture wires --
        the alternative is corrupting a real worktree's HEAD to provoke it,
        which tests git rather than this branch.
        """
        broken = wt.CheckResult(
            stage="create",
            branch_checked_out=False,
            head_resolves=True,
            base_is_ancestor=True,
            cleanliness=None,
            ok=False,
            merge_permitted=False,
            detail=("HEAD is not on maestro/a/1: detached",),
        )
        self.written = {"a": {"a.py": "A\n"}}
        with mock.patch.object(sch.wt, "check_at_create", return_value=broken):
            self.schedule(
                [self.agent("a")], config=self.config(environmental_retries=0)
            ).run()

        detail = self._row("blocked:ENVIRONMENTAL_BUDGET_EXHAUSTED").get("detail", {})
        self.assertIn("create check failed", detail.get("reason", ""))
        self.assertIn("HEAD is not on maestro/a/1", detail.get("reason", ""))

    def test_the_retry_row_carries_it_too_not_only_the_block(self):
        """A retried node that later succeeds still has to say why it was
        retried; the retry row is the only place that failure is recorded."""
        self.raise_for = {"a": OSError("transient device failure")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        detail = self._row("retry:ENVIRONMENTAL").get("detail", {})
        self.assertIn("OSError", detail.get("reason", ""))
        self.assertIn("transient device failure", detail.get("reason", ""))


class QuiescenceTests(SchedulerFixture):
    """Every state transition follows a successful, identity-bound proof."""

    def test_an_exception_from_run_node_is_quiesced_before_failure_classification(self):
        phases = []

        def quiesce(record, phase):
            phases.append(
                (record.key, phase, self.store.get_node("run1", record.node_id).state)
            )

        self.raise_for = {"a": RuntimeError("runner leaked")}
        report = self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=0),
            deps=self.deps(quiesce_attempt=quiesce),
        ).run()

        self.assertEqual(
            [phase for _, phase, _ in phases],
            ["pre-baseline", "pre-inventory", "settle"],
        )
        self.assertTrue(all(state is st.NodeState.RUNNING for _, _, state in phases))
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.BLOCKED)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_a_missing_quiescence_dependency_is_refused_before_scheduling(self):
        with self.assertRaises(sch.QuiescenceDependencyError):
            self.deps(quiesce_attempt=None)

    def test_unproven_quiescence_blocks_instead_of_releasing_a_retry(self):
        def quiesce(record, phase):
            if phase == "pre-baseline":
                raise RuntimeError("process group still present")

        report = self.schedule(
            [self.agent("a")], deps=self.deps(quiesce_attempt=quiesce)
        ).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(node.attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_watchdog_kill_exception_blocks_that_generation_without_retry(self):
        """A failed kill is unproven quiescence, not a swallowed watchdog error."""
        kill_called = threading.Event()
        watchdog_quiesced = threading.Event()

        def provision(_path):
            # The watchdog reads the injected clock. Jump past the 0.01s
            # node timeout; do not wait it out on the machine.
            self.expire_running_attempts(1.0)
            self.assertTrue(watchdog_quiesced.wait(ARRIVAL_TIMEOUT_S))

        def kill_attempt(_record):
            kill_called.set()
            raise RuntimeError("launcher kill failed")

        def quiesce(_record, phase):
            if phase == "watchdog":
                watchdog_quiesced.set()

        report = self.schedule(
            [self.agent("a")],
            config=self.config(
                node_timeout_s=0.01,
                turn_timeout_s=0.01,
                final_acceptance_timeout_s=1.0,
                backstop_t_s=5.0,
                environmental_retries=3,
            ),
            deps=self.deps(
                provision=provision, kill_attempt=kill_attempt, quiesce_attempt=quiesce
            ),
        ).run()

        node = self.store.get_node("run1", "a")
        transitions = self.store.audit_transitions("run1", "a")
        self.assertTrue(kill_called.is_set())
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(node.attempt_no, 1)
        self.assertEqual(transitions[-1]["detail"]["phase"], "watchdog-kill")
        self.assertEqual(transitions[-1]["detail"]["exception_type"], "RuntimeError")
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_node_gate_quiescence_error_blocks_instead_of_becoming_a_retry(self):
        def gate(_attempt, _node, _phase, _cancel_requested):
            raise wt.HarnessQuiescenceError("HARNESS_CONTEXT_QUIESCENCE_UNPROVEN")

        report = self.schedule([self.agent("a")], deps=self.deps(run_gate=gate)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(node.attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)


class GenerationFenceTests(SchedulerFixture):
    def test_a_stale_worker_cannot_commit_after_losing_running_ownership(self):
        def runner(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            if record.attempt_no == 1:
                (attempt.path / "stale.py").write_text("stale\n")
                self.store.fail_attempt(
                    "run1", node.node_id, st.RetryClass.ENVIRONMENTAL
                )
            else:
                (attempt.path / "fresh.py").write_text("fresh\n")
            return sch.NodeExecution()

        report = self.schedule(
            [self.agent("a", outputs=("fresh.py",))], deps=self.deps(run_node=runner)
        ).run()

        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 2)
        self.assertTrue((self.integration / "fresh.py").is_file())
        self.assertFalse((self.integration / "stale.py").exists())
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_timed_out_provision_cannot_reach_a_gate_or_commit_before_retry(self):
        watchdog_quiesced = threading.Event()
        provision_calls = []
        gate_attempts = []
        runner_attempts = []

        def provision(path):
            provision_calls.append(path)
            if len(provision_calls) == 1:
                # The watchdog, not this wait, must release attempt 1.
                # Jump the injected clock past node_timeout_s; waiting
                # four real seconds is the flake this suite is closing.
                self.expire_running_attempts(5.0)
                self.assertTrue(watchdog_quiesced.wait(ARRIVAL_TIMEOUT_S))
            else:
                # Attempt 2 needs headroom. The jump above left the clock
                # ~5s past attempt 1, which is also past attempt 2's
                # 4s node timeout.
                self.clock.set(time.time())

        def quiesce(record, phase):
            if phase == "watchdog":
                watchdog_quiesced.set()

        def gate(attempt, node, phase, cancel_requested):
            # `attempt.attempt_no` — the attempt this *worker* belongs to —
            # and never the node's current attempt number. The two differ for
            # exactly the worker this test fences: a stale generation the
            # watchdog revoked still carries attempt 1 while the node row
            # already reads 2. Reading the node made a fenced worker's gate
            # indistinguishable from the live attempt's, so the assertion
            # below could not convict the fence's removal — verified, by
            # removing both `_require_running` guards on this path and
            # watching the test still pass. With the worker's own number it
            # convicts as `[1, 2, 2] != [2, 2]`.
            gate_attempts.append(attempt.attempt_no)
            return self.run_gate(attempt, node, phase, cancel_requested)

        def runner(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            runner_attempts.append(record.attempt_no)
            return self.run_node(
                attempt, node, record, retry_prompt, on_launch, cancel_requested
            )

        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            # Timeouts sized so attempt 1 is convicted while it is still
            # blocked in provision, and attempt 2 then has real headroom.
            # At 0.01s the second attempt could not finish either: it does
            # genuine git work — worktree creation, both gates, a commit —
            # and was killed mid-flight on roughly 19 runs in 20, which made
            # this a flake rather than a test. `environmental_retries=1`
            # means attempt 1's timeout spends the whole budget, so any
            # timeout landing on attempt 2 blocks the node.
            config=self.config(
                node_timeout_s=4.0,
                turn_timeout_s=4.0,
                final_acceptance_timeout_s=4.0,
                backstop_t_s=30.0,
                environmental_retries=1,
            ),
            deps=self.deps(
                provision=provision,
                quiesce_attempt=quiesce,
                run_gate=gate,
                run_node=runner,
            ),
        ).run()

        self.assertEqual(len(provision_calls), 2)
        # Three gate runs on the surviving attempt and none on the convicted
        # one: pre, post, and §7.4's post-work falsify.
        self.assertEqual(gate_attempts, [2, 2, 2])
        self.assertEqual(runner_attempts, [2])
        self.assertIs(
            self.store.get_attempt("run1", "a", 1).retry_class,
            st.RetryClass.ENVIRONMENTAL,
        )
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


# ── §7.3 code nodes under the scheduler ─────────────────────────────────────


class CodeNodeTests(SchedulerFixture):
    def test_a_code_node_with_an_empty_diff_merges(self):
        """§6.7's assertive "nothing broke" node, which the four agent
        clauses would have wedged."""
        report = self.schedule([self.code("fmt")]).run()
        self.assertIs(self.store.get_node("run1", "fmt").state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_code_node_never_runs_a_gate(self):
        gate = Recorder(green())
        self.schedule([self.code("fmt")], deps=self.deps(run_gate=gate)).run()
        self.assertEqual(gate.calls, [])

    def test_expects_changes_with_an_empty_diff_blocks_no_effect(self):
        report = self.schedule([self.code("fmt", expects_changes=True)]).run()
        node = self.store.get_node("run1", "fmt")
        self.assertIs(node.block_reason, st.BlockReason.CODE_NODE_NO_EFFECT)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_no_effect_is_not_retried(self):
        """Re-running a deterministic command against an unchanged base
        cannot produce a different answer."""
        self.schedule([self.code("fmt", expects_changes=True)]).run()
        self.assertEqual(self.store.get_node("run1", "fmt").attempt_no, 1)

    def test_a_code_node_writing_outside_its_declaration_blocks(self):
        self.written = {"fmt": {"stray.py": "X\n"}}
        node = self.code("fmt", outputs=("declared.py",))
        self.schedule([node]).run()
        stored = self.store.get_node("run1", "fmt")
        self.assertIs(stored.block_reason, st.BlockReason.PERMISSION_SCOPE_VIOLATION)
        self.assertEqual(stored.attempt_no, 1)


# ── §8.7 conflict and the derived cascade ───────────────────────────────────


class ConflictTests(SchedulerFixture):
    def test_a_conflict_blocks_the_node_and_strands_its_descendants(self):
        """§8.7 — the cascade is derived, so the descendant is never written
        to a terminal state; it simply never becomes ready."""
        self.written = {
            "a": {"shared.py": "A\n"},
            "b": {"shared.py": "B\n"},
            "c": {"c.py": "C\n"},
        }
        nodes = [
            self.agent("a", outputs=("shared.py",)),
            self.agent("b", outputs=("shared.py",)),
            self.agent("c", depth=1, needs=("b",)),
        ]
        report = self.schedule(nodes).run()

        states = self.states()
        self.assertEqual(states["a"], "MERGED")
        self.assertEqual(states["b"], "BLOCKED")
        self.assertIs(
            self.store.get_node("run1", "b").block_reason, st.BlockReason.MERGE_CONFLICT
        )
        # Derived-unready, never stored terminal — that is what keeps the
        # cascade reversible for an operator rescue.
        self.assertEqual(states["c"], "PENDING")
        self.assertIn("c", self.store.upstream_blocked("run1"))
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_the_integration_head_is_byte_identical_after_a_conflict(self):
        self.written = {"a": {"shared.py": "A\n"}, "b": {"shared.py": "B\n"}}
        self.schedule(
            [
                self.agent("a", outputs=("shared.py",)),
                self.agent("b", outputs=("shared.py",)),
            ]
        ).run()
        self.assertEqual(_git(self.integration, "status", "--porcelain"), "")


# ── §7.8 cancellation and resume ────────────────────────────────────────────


class CancellationTests(SchedulerFixture):
    def test_cancel_writes_cancelled_for_every_non_terminal_node(self):
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule(
            [self.agent("a"), self.agent("b", depth=1, needs=("a",))]
        )
        scheduler.cancel()
        report = scheduler.run()
        self.assertEqual(set(self.states().values()), {"CANCELLED"})
        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)

    def test_pause_quiesces_without_declaring_an_outcome(self):
        """§1.2 — a pause is not a lifecycle transition, so nothing durable
        may move: no outcome, no node rewritten, no cancellation requested.
        That is precisely what leaves the run resumable afterwards."""
        started = threading.Event()
        release = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            self.assertTrue(release.wait(ARRIVAL_TIMEOUT_S))
            (attempt.path / "a.py").write_text("A\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        scheduler = self.schedule([self.agent("a")], deps=self.deps(run_node=run_node))

        def pause_when_running():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            scheduler.request_pause()
            release.set()

        waiter = threading.Thread(target=pause_when_running)
        waiter.start()
        with self.assertRaises(sch.RunPaused):
            scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)

        self.assertIsNone(self.store.latest_outcome("run1"))
        self.assertIsNot(self.store.get_node("run1", "a").state, st.NodeState.CANCELLED)
        self.assertFalse(
            self.store.conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()[0]
        )
        # And the whole point of writing nothing: the run is still resumable.
        self.store.resume_run("run1")
        self.assertIsNone(self.store.latest_outcome("run1"))

    def test_pause_quiescence_failure_leaves_a_running_lane_resumable(self):
        """Pause requests interruption but does not durably contain its failure."""
        started = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            while not cancel_requested():
                time.sleep(0.001)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def failing_quiesce(record, phase):
            self.quiesce_attempt(record, phase)
            if phase in ("cancel", "settle"):
                raise RuntimeError("process group remained unproven")

        scheduler = self.schedule(
            [self.agent("build")],
            deps=self.deps(
                run_node=run_node,
                quiesce_attempt=failing_quiesce,
                review_attempt=lambda *args: None,
            ),
        )

        def pause_when_running():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            scheduler.request_pause()

        waiter = threading.Thread(target=pause_when_running)
        waiter.start()
        with self.assertRaises(sch.RunPaused):
            scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)

        lifecycle = self.store.get_node("run1", "build")
        self.assertIs(lifecycle.state, st.NodeState.RUNNING)
        self.assertIs(lifecycle.block_reason, None)
        self.assertIsNot(lifecycle.lane_phase, st.LanePhase.BLOCKED)
        self.assertFalse(self.store.lane_retry_spends("run1", "build"))
        self.assertTrue(
            any(
                key == ("run1", "build", lifecycle.attempt_no)
                and phase in ("cancel", "settle")
                for key, phase in self.quiesce_calls
            )
        )

    def test_cancel_quiescence_failure_is_recorded_before_cancellation(self):
        """Cancellation stays fail-closed without escaping its run loop."""
        started = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            while not cancel_requested():
                time.sleep(0.001)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def failing_quiesce(record, phase):
            self.quiesce_attempt(record, phase)
            if phase in ("cancel", "settle"):
                raise RuntimeError("process group remained unproven")

        scheduler = self.schedule(
            [self.agent("build")],
            deps=self.deps(
                run_node=run_node,
                quiesce_attempt=failing_quiesce,
                review_attempt=lambda *args: None,
            ),
        )

        def cancel_when_running():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            scheduler.cancel()

        waiter = threading.Thread(target=cancel_when_running)
        waiter.start()
        report = scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)

        lifecycle = self.store.get_node("run1", "build")
        blocked = [
            transition
            for transition in self.store.audit_transitions("run1", "build")
            if transition.get("to_state") == st.NodeState.BLOCKED.value
        ]
        self.assertIs(lifecycle.state, st.NodeState.CANCELLED)
        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)
        self.assertTrue(blocked)
        self.assertIn(blocked[-1]["detail"]["phase"], ("cancel", "settle"))

    def test_a_pause_restores_the_callers_sigint_handler(self):
        """The handler is installed for the loop's duration only — a
        scheduler embedded in a longer-lived process must not keep the
        signal after it stops."""
        previous = signal.getsignal(signal.SIGINT)
        scheduler = self.schedule([self.agent("a")])
        scheduler.request_pause()
        with self.assertRaises(sch.RunPaused):
            scheduler.run()
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_a_real_sigint_during_an_in_flight_attempt_quiesces(self):
        """Issue #110 — deliver signal.SIGINT, not a direct request_pause()."""
        started = threading.Event()
        release = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            deadline = time.time() + ARRIVAL_TIMEOUT_S
            while time.time() < deadline:
                if release.is_set() or cancel_requested():
                    break
                time.sleep(0.01)
            else:
                self.fail("in-flight run_node was not released")
            (attempt.path / "a.py").write_text("A\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        scheduler = self.schedule([self.agent("a")], deps=self.deps(run_node=run_node))

        def fire():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            os.kill(os.getpid(), signal.SIGINT)
            release.set()

        waiter = threading.Thread(target=fire)
        waiter.start()
        with self.assertRaises(sch.RunPaused):
            scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)

        self.assertTrue(scheduler._sigint_pause_latched)
        self.assertFalse(scheduler._sigint_escalated)
        self.assertIsNone(self.store.latest_outcome("run1"))
        self.assertIsNot(self.store.get_node("run1", "a").state, st.NodeState.CANCELLED)
        self.assertFalse(
            self.store.conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()[0]
        )

    def test_a_real_sigint_between_attempts_does_not_start_the_next_node(self):
        """Issue #110 — SIGINT after one attempt has merged, before the next."""
        entered = []
        orig_ready = self.store.ready_nodes
        sent = []

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            entered.append(node.node_id)
            on_launch(None)
            (attempt.path / f"{node.node_id}.py").write_text(
                node.node_id.upper() + "\n"
            )
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def ready_nodes(run_id):
            nodes = orig_ready(run_id)
            if (
                not sent
                and self.store.get_node("run1", "a").state is st.NodeState.MERGED
            ):
                sent.append(1)
                os.kill(os.getpid(), signal.SIGINT)
            return nodes

        self.store.ready_nodes = ready_nodes
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        scheduler = self.schedule(
            [self.agent("a"), self.agent("b")],
            config=self.config(concurrency=1),
            deps=self.deps(run_node=run_node),
        )
        with self.assertRaises(sch.RunPaused):
            scheduler.run()

        self.assertEqual(entered, ["a"])
        self.assertIsNone(self.store.latest_outcome("run1"))
        self.assertNotIn(
            "b",
            [
                r.node_id
                for r in self.store.node_records("run1")
                if r.state == st.NodeState.RUNNING.value
            ],
        )

    def test_a_second_sigint_escalates_past_a_hung_quiesce(self):
        """Issue #110 — second SIGINT is stronger than another polite request."""
        started = threading.Event()
        release = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            deadline = time.time() + ARRIVAL_TIMEOUT_S
            while time.time() < deadline:
                if release.is_set() or cancel_requested():
                    break
                time.sleep(0.01)
            else:
                self.fail("escalation run_node was not released")
            (attempt.path / "a.py").write_text("A\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def quiesce(record, phase):
            self.assertIsInstance(record, st.AttemptRecord)
            self.quiesce_calls.append((record.key, phase))
            if threading.current_thread() is not threading.main_thread():
                return
            # Escalation is armed just before this call. A second SIGINT
            # here must raise KeyboardInterrupt rather than request_pause.
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(ARRIVAL_TIMEOUT_S)
            self.fail("second SIGINT did not interrupt hung quiesce")

        def fire():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            os.kill(os.getpid(), signal.SIGINT)
            release.set()

        scheduler = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=run_node, quiesce_attempt=quiesce),
        )
        self.addCleanup(release.set)
        waiter = threading.Thread(target=fire, daemon=True)
        waiter.start()
        with self.assertRaises(sch.RunPaused):
            scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)

        self.assertTrue(scheduler._sigint_escalated)
        self.assertIsNone(self.store.latest_outcome("run1"))
        self.assertFalse(
            self.store.conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()[0]
        )

    def test_failed_sigint_handler_install_is_surfaced(self):
        """Issue #110 / brief — ValueError on install is not swallowed."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")])
        real = signal.signal

        def wrapped(sig, handler):
            if getattr(handler, "__self__", None) is scheduler:
                raise ValueError(
                    "signal only works in main thread of the main interpreter"
                )
            return real(sig, handler)

        buf = io.StringIO()
        with mock.patch("adw_modules.scheduler.signal.signal", wrapped):
            with mock.patch("adw_modules.scheduler.sys.stderr", buf):
                report = scheduler.run()
        self.assertIn("SIGINT_HANDLER_NOT_INSTALLED", buf.getvalue())
        self.assertFalse(scheduler._sigint_handler_installed)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_cancellation_during_the_pre_node_gate_cannot_run_the_node(self):
        holder = {}

        def gate(attempt, node, phase, cancel_requested):
            self.assertEqual(phase, "pre")
            holder["scheduler"].cancel()
            self.assertTrue(cancel_requested())
            return red()

        scheduler = self.schedule([self.agent("a")], deps=self.deps(run_gate=gate))
        holder["scheduler"] = scheduler
        report = scheduler.run()

        self.assertNotIn("a", self.prompts)
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.CANCELLED)
        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)

    def test_cancellation_during_the_post_node_gate_cannot_verify_or_merge(self):
        holder = {}
        self.written = {"a": {"a.py": "A\n"}}

        def gate(attempt, node, phase, cancel_requested):
            if phase == "post":
                holder["scheduler"].cancel()
                self.assertTrue(cancel_requested())
            return self.run_gate(attempt, node, phase, cancel_requested)

        scheduler = self.schedule([self.agent("a")], deps=self.deps(run_gate=gate))
        holder["scheduler"] = scheduler
        report = scheduler.run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.CANCELLED)
        self.assertFalse((self.integration / "a.py").exists())
        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)

    def test_cancellation_during_the_final_gate_cannot_declare_accepted(self):
        holder = {}
        self.written = {"a": {"a.py": "A\n"}}

        def final_gate(path, specs, cancel_requested):
            holder["scheduler"].cancel()
            self.assertTrue(cancel_requested())
            return green(3)

        scheduler = self.schedule(
            [self.agent("a")], deps=self.deps(run_integration_gate=final_gate)
        )
        holder["scheduler"] = scheduler
        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)
        self.assertIs(self.store.latest_outcome("run1"), st.RunOutcome.CANCELLED)


class ResumeTests(SchedulerFixture):
    def test_an_inherited_running_attempt_is_failed_and_relaunched_never_adopted(self):
        """§7.8 — even a provably-live pane's in-flight work is not adopted,
        which removes the motive to guess."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        self.store.start_attempt(
            "run1", "a", _git(self.integration, "rev-parse", "HEAD")
        )
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.RUNNING)

        reclaimed = self.store.resume_run("run1")
        self.assertEqual(reclaimed, ("a",))
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.PENDING)

        report = scheduler.run()
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertGreater(self.store.get_node("run1", "a").attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_late_success_recovers_same_attempt_without_relaunch(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "a", attempt_no, baseline, attempt.ignored_at_base
        )
        (attempt.path / "a.py").write_text("late\n")
        self.store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
        self.store.declare_outcome("run1")
        self.store.resume_run("run1")
        self.store.prepare_late_envelope_recovery("run1", "a", attempt_no)
        self.assertIsNone(self.store.get_node("run1", "a").lane_phase)

        recovered = []

        def recover(reopened, record):
            recovered.append(record.key)
            return sch.NodeExecution(
                envelope_parsed=True, envelope_payload={"success": True}, exit_code=0
            )

        report = self.schedule(
            [node],
            deps=self.deps(
                run_node=mock.Mock(side_effect=AssertionError("relaunch")),
                recover_node=recover,
            ),
        ).run()

        self.assertEqual(recovered, [("run1", "a", attempt_no)])
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, attempt_no)
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual((self.integration / "a.py").read_text(), "late\n")
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_inherited_running_success_recovers_without_relaunch(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "a", attempt_no, baseline, attempt.ignored_at_base
        )
        (attempt.path / "a.py").write_text("declared\n")
        args = argparse.Namespace(
            run_id="run1",
            repo=str(self.repo),
            worktrees_root=str(self.root / "wt"),
            scratch_root=str(self.root / "scratch"),
        )
        route = mock.Mock()
        route.agent_presence.return_value = False
        envelope = attempt.scratch / "agent-envelope.json"
        envelope.write_text('{"success": false}', encoding="utf-8")
        self.assertIsNone(
            maestro._running_late_agent_execution(
                args, route, self.store, "a", attempt_no
            )
        )
        route.agent_presence.assert_not_called()

        envelope.write_text(
            '{"success": true, "summary": "finished"}', encoding="utf-8"
        )
        execution = maestro._running_late_agent_execution(
            args, route, self.store, "a", attempt_no
        )
        self.assertIsNotNone(execution)
        self.assertTrue(execution.envelope_parsed)
        route.agent_presence.assert_called_once()
        route.agent_presence.reset_mock()
        route.agent_presence.return_value = True
        with self.assertRaises(maestro._RunRefused) as refused:
            maestro._running_late_agent_execution(
                args, route, self.store, "a", attempt_no
            )
        self.assertEqual(refused.exception.outcome, "RESUME_AGENT_STILL_LIVE")
        execution = maestro._running_late_agent_execution(
            args, route, self.store, "a", attempt_no, allow_live_builder=True
        )
        self.assertTrue(execution.envelope_parsed)
        self.store.record_result(
            "run1",
            st.ResultRecord(
                node_id="a",
                attempt_no=attempt_no,
                subject_sha=base,
                payload={"success": True, "summary": "finished"},
                adjudication=st.Adjudication.ACCEPTED,
            ),
        )
        envelope.unlink()
        self.store.resume_run("run1")

        recover = mock.Mock(return_value=None)
        report = self.schedule(
            [node],
            deps=self.deps(
                run_node=mock.Mock(side_effect=AssertionError("relaunch")),
                recover_node=recover,
            ),
        ).run()

        recover.assert_called_once()
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, attempt_no)
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual((self.integration / "a.py").read_text(), "declared\n")
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_post_gate_failure_after_late_recovery_repairs_same_attempt(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "a", attempt_no, baseline, attempt.ignored_at_base
        )
        (attempt.path / "a.py").write_text("late\n")
        self.store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
        self.store.declare_outcome("run1")
        self.store.resume_run("run1")
        self.store.prepare_late_envelope_recovery("run1", "a", attempt_no)

        self.written = {"a": {"a.py": "fresh\n"}}
        self.gate_script[("a", "post")] = [red(), green()]
        recover = mock.Mock(
            return_value=sch.NodeExecution(
                envelope_parsed=True, envelope_payload={"success": True}, exit_code=0
            )
        )
        relaunch = mock.Mock(side_effect=self.run_node)
        continue_node = mock.Mock(side_effect=self.continue_node)
        report = self.schedule(
            [node],
            deps=self.deps(
                run_node=relaunch, recover_node=recover, continue_node=continue_node
            ),
        ).run()

        self.assertEqual(recover.call_count, 1)
        self.assertEqual(relaunch.call_count, 0)
        self.assertEqual(continue_node.call_count, 1)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, attempt_no)
        self.assertNotIn(
            lc.LATE_ENVELOPE_RECOVERY_KEY,
            self.store.get_attempt("run1", "a", attempt_no).extra,
        )
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_late_recovery_failure_consumes_marker_then_relaunches(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        self.store.record_baseline(
            "run1", "a", attempt_no, baseline, attempt.ignored_at_base
        )
        self.store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
        self.store.declare_outcome("run1")
        self.store.resume_run("run1")
        self.store.prepare_late_envelope_recovery("run1", "a", attempt_no)
        self.written = {"a": {"a.py": "fresh\n"}}

        recover = mock.Mock(side_effect=RuntimeError("unreadable envelope"))
        relaunch = mock.Mock(side_effect=self.run_node)
        report = self.schedule(
            [node], deps=self.deps(run_node=relaunch, recover_node=recover)
        ).run()

        self.assertEqual(recover.call_count, 1)
        self.assertEqual(relaunch.call_count, 1)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, attempt_no + 1)
        self.assertNotIn(
            lc.LATE_ENVELOPE_RECOVERY_KEY,
            self.store.get_attempt("run1", "a", attempt_no).extra,
        )
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_crash_after_lane_acceptance_reconciles_and_merges_once(self):
        node = self.agent("a")
        crashed = self.schedule([node])
        crashed.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        (attempt.path / "a.py").write_text("A\n")
        after = wt.inventory(attempt.path)
        output_sha = wt.commit_measured_delta(
            attempt, wt.delta(baseline, after), after, "a attempt 1"
        )
        self.store.record_sealed_output("run1", "a", attempt_no, output_sha)
        self.store.publish_candidate(
            "run1", "a", output_sha, builder_generation=1, repo_path=attempt.repo
        )
        self.store.begin_review("run1", "a::review", output_sha, reviewer_generation=1)
        self.store.complete_review(
            "run1",
            "a::review",
            output_sha,
            reviewer_generation=1,
            verdict=st.ReviewVerdict.PASS,
            review_digest="recovery-review",
            receipt_path=str(self.root / "receipts" / "recovery-review"),
            findings=(),
        )
        self.store.mark_review_accepted("run1", "a::review", output_sha)
        self.assertTrue(
            self.store.set_lane_phase("run1", "a", st.LanePhase.ACCEPTED, expected=None)
        )
        stranded = self.store.get_node("run1", "a")
        self.assertIs(stranded.state, st.NodeState.RUNNING)
        self.assertIs(stranded.lane_phase, st.LanePhase.ACCEPTED)

        restarted = self.schedule([node])
        first_resume = restarted.run()
        second_resume = self.schedule([node]).run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").output_sha, output_sha)
        self.assertEqual(
            _git(self.integration, "log", "--format=%s").splitlines().count("merge a"),
            1,
        )
        self.assertIs(first_resume.outcome, st.RunOutcome.ACCEPTED)
        self.assertIs(second_resume.outcome, st.RunOutcome.ACCEPTED)

    def test_pass_replay_keeps_candidate_generation_after_builder_replacement(self):
        """A replacement builder does not rewrite an immutable candidate replay."""
        node = self.agent("build")
        scheduler = self.schedule(
            [node], deps=self.deps(review_attempt=lambda *args: None)
        )
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "build", base)
        record = self.store.get_attempt("run1", "build", attempt_no)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "build",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        (attempt.path / "build.py").write_text("replayed\n")
        after = wt.inventory(attempt.path)
        candidate_sha = wt.commit_measured_delta(
            attempt, wt.delta(baseline, after), after, "build attempt 1"
        )
        self.store.publish_candidate(
            "run1",
            "build",
            candidate_sha,
            builder_generation=attempt_no,
            repo_path=attempt.repo,
        )
        self.store.begin_review(
            "run1", "build::review", candidate_sha, reviewer_generation=attempt_no
        )
        self.store.complete_review(
            "run1",
            "build::review",
            candidate_sha,
            reviewer_generation=attempt_no,
            verdict=st.ReviewVerdict.PASS,
            review_digest="replayed-pass",
            receipt_path=str(self.root / "receipts" / "replayed-pass"),
            findings=(),
        )
        self.store.mark_review_accepted("run1", "build::review", candidate_sha)
        self.store.register_actor_session(
            "run1",
            "build",
            "builder",
            generation=attempt_no,
            pane_id="original-builder-pane",
            session_path="/sessions/original-builder",
            correlation_token="original-builder",
        )
        self.assertTrue(
            self.store.close_actor_session(
                "run1", "build", "builder", generation=attempt_no
            )
        )
        self.store.register_actor_session(
            "run1",
            "build",
            "builder",
            generation=attempt_no + 1,
            pane_id="replacement-builder-pane",
            session_path="/sessions/replacement-builder",
            correlation_token="replacement-builder",
        )

        with mock.patch.object(
            self.store,
            "mark_review_accepted",
            wraps=self.store.mark_review_accepted,
        ) as accepted:
            scheduler._complete_attempt(
                node,
                sch._AttemptContext(record=record),
                attempt,
                record,
                None,
                vf.GateVerdict(
                    green=False,
                    unparseable=False,
                    counts=None,
                    reason="candidate was absent before this attempt",
                ),
                baseline,
                sch.NodeExecution(envelope_parsed=True, exit_code=0),
                base,
                sealed_output_sha=candidate_sha,
            )

        scheduler._merge_frontier()
        accepted.assert_not_called()
        self.assertIs(self.store.get_node("run1", "build").state, st.NodeState.MERGED)
        self.assertEqual(
            self.store.candidate("run1", "build", candidate_sha).builder_generation,
            attempt_no,
        )
        self.assertIs(
            self.store.get_node("run1", "build::review").state, st.NodeState.ACCEPTED
        )

    def test_unreviewed_candidate_cannot_be_verified(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        self.store.start_attempt(
            "run1", "a", _git(self.integration, "rev-parse", "HEAD")
        )

        with self.assertRaises(lc.LifecycleError):
            self.store.mark_verified("run1", "a", "not-a-commit-digest")

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.RUNNING)

    def test_rehydration_refuses_a_descendant_not_owned_by_the_attempt(self):
        """A forged row may name a real descendant, but not this attempt's ref."""
        node = self.agent("a")
        crashed = self.schedule([node])
        crashed.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "wt",
            self.root / "scratch",
        )
        baseline = wt.take_baseline(attempt)
        (attempt.path / "a.py").write_text("A\n")
        after = wt.inventory(attempt.path)
        output_sha = wt.commit_measured_delta(
            attempt, wt.delta(baseline, after), after, "a attempt 1"
        )
        forged_descendant = _git(
            self.repo,
            "commit-tree",
            _git(self.repo, "rev-parse", "{}^{{tree}}".format(output_sha)),
            "-p",
            output_sha,
            "-m",
            "forged descendant",
        )
        self.store.publish_candidate(
            "run1", "a", forged_descendant, builder_generation=1
        )
        self.store.begin_review(
            "run1", "a::review", forged_descendant, reviewer_generation=1
        )
        self.store.complete_review(
            "run1",
            "a::review",
            forged_descendant,
            reviewer_generation=1,
            verdict=st.ReviewVerdict.PASS,
            review_digest="forged-review",
            receipt_path=str(self.root / "receipts" / "forged-review"),
            findings=(),
        )
        self.store.mark_review_accepted("run1", "a::review", forged_descendant)
        self.assertTrue(
            self.store.set_lane_phase("run1", "a", st.LanePhase.ACCEPTED, expected=None)
        )
        self.store.mark_verified("run1", "a", forged_descendant)
        report = self.schedule([node]).run()

        stored = self.store.get_node("run1", "a")
        self.assertIs(stored.state, st.NodeState.BLOCKED)
        self.assertIs(stored.block_reason, st.BlockReason.OUTPUT_IDENTITY_INVALID)
        self.assertFalse((self.integration / "a.py").exists())
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)


# ── §8.8 final acceptance ───────────────────────────────────────────────────


class FinalAcceptanceTests(SchedulerFixture):
    def test_acceptance_runs_the_union_of_merged_specs_plus_the_integration_gate(self):
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        seen = {}

        def integration_gate(path, specs, cancel_requested):
            seen["specs"] = specs
            return green(3)

        report = self.schedule(
            [
                self.agent("a", specs=("spec_a", "shared")),
                self.agent("b", specs=("spec_b", "shared")),
            ],
            deps=self.deps(run_integration_gate=integration_gate),
        ).run()
        self.assertEqual(seen["specs"], ("shared", "spec_a", "spec_b"))
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_red_integration_gate_declares_blocked_with_the_result(self):
        """§8.8 — everything merged but the integration gate failed is the
        BLOCKED outcome, not an undefined state. The merged work stays on the
        integration branch and publishing remains the operator's decision."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_integration_gate=lambda p, s, c: red()),
        ).run()
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        self.assertIsNotNone(report.acceptance)
        self.assertFalse(report.acceptance.green)
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)

    def test_acceptance_reproves_every_merged_node_against_the_final_head(self):
        """§8.6 — test PASS is structurally never merge provenance."""
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        report = self.schedule([self.agent("a"), self.agent("b")]).run()
        self.assertEqual(report.ancestry, {"a": True, "b": True})

    def test_acceptance_does_not_run_when_a_node_never_merged(self):
        """§8.8 — on every other quiescent shape there is no final head that
        represents the plan, so the integration gate never runs and the
        branch is integration-untested. `run status` must say so rather than
        leave it to be inferred from a missing gate result."""
        self.gate_script[("a", "pre")] = [green()]
        self.written = {"b": {"b.py": "B\n"}}
        gate = Recorder(green(3))
        report = self.schedule(
            [self.agent("a"), self.agent("b")],
            deps=self.deps(run_integration_gate=gate),
        ).run()
        self.assertEqual(gate.calls, [])
        self.assertIsNone(report.acceptance)
        self.assertTrue(report.integration_untested)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_the_acceptance_start_refreshes_the_run_timer(self):
        """§11.2 — nothing transitions between the last node's MERGED and the
        outcome declaration, and that gap is as long as everything acceptance
        executes. The refresh is what stops the backstop firing inside it."""
        self.written = {"a": {"a.py": "A\n"}}
        stamps = []

        def integration_gate(path, specs, cancel_requested):
            stamps.append(self.store.last_transition_at("run1"))
            return green(3)

        merged_at = []

        def gate(attempt, node, phase, cancel_requested):
            if phase == "post":
                merged_at.append(self.store.last_transition_at("run1"))
            return self.run_gate(attempt, node, phase, cancel_requested)

        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_gate=gate, run_integration_gate=integration_gate),
        ).run()
        self.assertGreaterEqual(stamps[0], merged_at[0])


# ── §7.6 / §11.2 the liveness machinery, actually wired ─────────────────────


class LivenessWiringTests(SchedulerFixture):
    """The watchdog and the backstop existing is not the same as the
    scheduler owning them. These assert the wiring, because a module that is
    built and never started is indistinguishable at runtime from one that was
    never written."""

    def test_the_run_owns_a_single_watchdog_thread(self):
        self.written = {"a": {"a.py": "A\n"}}
        names = []

        def observing(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            names.extend(
                t.name for t in threading.enumerate() if t.name == "maestro-watchdog"
            )
            return self.run_node(
                attempt, node, record, retry_prompt, on_launch, cancel_requested
            )

        self.schedule([self.agent("a")], deps=self.deps(run_node=observing)).run()
        self.assertEqual(names, ["maestro-watchdog"])

    def test_the_watchdog_thread_does_not_outlive_the_run(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()
        self.assertNotIn("maestro-watchdog", [t.name for t in threading.enumerate()])

    def test_the_backstop_reads_the_same_clock_the_column_uses(self):
        """A real defect this caught: the backstop defaults its time source to
        `time.monotonic` while the store's column is epoch seconds.
        Subtracting one from the other is meaningless and, on this machine,
        large and negative — so the timer would never fire and the design's
        last-resort liveness net would be silently absent."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        elapsed = time.time() - self.store.last_transition_at("run1")
        self.assertLess(
            abs(elapsed),
            300,
            "last_transition_at is not on the epoch clock the "
            "backstop compares against",
        )

    def test_a_silent_run_declares_stuck_with_a_diagnostic(self):
        """§11.2 — the backstop fires on transition silence within T
        regardless of how many panes are open, and the scheduler prints the
        same diagnostic `run status` prints rather than exiting silently."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule(
            [self.agent("a")],
            config=self.config(
                backstop_t_s=61.0, node_timeout_s=60.0, final_acceptance_timeout_s=60.0
            ),
        )
        scheduler.project()
        # Backdate the run's silence past T without waiting it out.
        self.store.conn.execute(
            "UPDATE runs SET last_transition_at=? WHERE run_id=?",
            ("1990-01-01T00:00:00.000+00:00", "run1"),
        )
        report = scheduler.run()
        self.assertIs(report.outcome, st.RunOutcome.STUCK)
        self.assertIn("no lifecycle transition within", scheduler.status_diagnostic())

    def test_the_backstop_quiesces_in_flight_workers(self):
        """§11.2 — STUCK is declared about a run that still has workers, and
        the backstop still has to stop them. `Future.cancel` cannot: a running
        worker is not cancellable, so cancelling the futures alone left the
        run waiting on the pool until each worker hit its own node timeout."""
        started = threading.Event()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            on_launch(None)
            started.set()
            deadline = time.time() + ARRIVAL_TIMEOUT_S
            while time.time() < deadline:
                if any(phase == "cancel" for _, phase in self.quiesce_calls):
                    (attempt.path / "a.py").write_text("A\n")
                    return sch.NodeExecution(envelope_parsed=True, exit_code=0)
                time.sleep(0.01)
            self.fail("backstop did not quiesce the in-flight worker")

        scheduler = self.schedule(
            [self.agent("a")],
            config=self.config(
                backstop_t_s=61.0, node_timeout_s=60.0, final_acceptance_timeout_s=60.0
            ),
            deps=self.deps(run_node=run_node),
        )
        scheduler.project()

        def fire_after_launch():
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            self.store.conn.execute(
                "UPDATE runs SET last_transition_at=? WHERE run_id=?",
                ("1990-01-01T00:00:00.000+00:00", "run1"),
            )

        waiter = threading.Thread(target=fire_after_launch)
        waiter.start()
        report = scheduler.run()
        waiter.join(timeout=ARRIVAL_TIMEOUT_S)
        self.assertIs(report.outcome, st.RunOutcome.STUCK)
        self.assertIn("cancel", [phase for _, phase in self.quiesce_calls])

    def test_the_diagnostic_says_why_each_node_is_not_ready(self):
        """§11.2 — `run status` must answer "why is nothing happening"
        without reading the database by hand."""
        self.gate_script[("a", "pre")] = [green()]
        scheduler = self.schedule(
            [self.agent("a"), self.agent("b", depth=1, needs=("a",))]
        )
        scheduler.run()
        diagnostic = scheduler.status_diagnostic()
        self.assertIn("a: BLOCKED", diagnostic)
        self.assertIn("ancestor is blocked", diagnostic)

    def test_the_launch_report_arms_the_first_two_signals(self):
        """§7.6 — process-alive and turn-count arm when the adapter reports
        launch, and nothing else arms them. The columns existed from the
        start and nothing wrote them, which left the watchdog silently
        running on its wall clock alone: an agent that died at once would
        have been caught not immediately, as §7.6 requires, but only at the
        node timeout."""
        self.written = {"a": {"a.py": "A\n"}}
        armed = {}

        def reporting(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            armed["before"] = self.store.get_attempt(
                "run1", node.node_id, record.attempt_no
            ).armed
            on_launch(4321)
            armed["after"] = self.store.get_attempt(
                "run1", node.node_id, record.attempt_no
            ).armed
            return self.run_node(
                attempt, node, record, retry_prompt, lambda pid: None, cancel_requested
            )

        self.schedule([self.agent("a")], deps=self.deps(run_node=reporting)).run()
        self.assertFalse(armed["before"], "armed before the adapter reported launch")
        self.assertTrue(armed["after"])
        self.assertEqual(self.store.get_attempt("run1", "a", 1).pid, 4321)

    def test_an_attempt_is_unarmed_until_launch_is_reported(self):
        """The pre-launch segment is signal-less by construction, not by
        omission: no process and no transcript exist yet."""
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        attempt_no = self.store.start_attempt(
            "run1", "a", _git(self.integration, "rev-parse", "HEAD")
        )
        self.assertFalse(self.store.get_attempt("run1", "a", attempt_no).armed)

    def test_a_heartbeat_is_not_a_transition(self):
        """Writing a heartbeat as a transition would refresh
        `last_transition_at` and silently disarm the backstop — the run would
        never look silent because a healthy watchdog was chattering into the
        column the backstop measures."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        attempt_no = self.store.start_attempt(
            "run1", "a", _git(self.integration, "rev-parse", "HEAD")
        )
        attempt = self.store.get_attempt("run1", "a", attempt_no)
        before = self.store.last_transition_at("run1")
        self.store.record_heartbeat(attempt, turn_count=3, observed_at=time.time())
        self.assertEqual(self.store.last_transition_at("run1"), before)
        self.assertEqual(self.store.get_attempt("run1", "a", attempt_no).turn_count, 3)


# ── §7.3 the declared outcome ───────────────────────────────────────────────


class OutcomeTests(SchedulerFixture):
    def test_the_outcome_is_declared_exactly_once_and_recorded(self):
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule([self.agent("a")]).run()
        self.assertIs(self.store.latest_outcome("run1"), report.outcome)

    def test_a_run_that_declared_blocked_can_still_be_rescued(self):
        """§7.3 — a run outcome is a record, not a tombstone. The scheduler
        declares at quiescence and exits rather than lingering, so the escape
        verbs must be legal afterwards or the rescue flow §8.7 tests would be
        one nobody could reach."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        report = self.schedule(
            [self.agent("a")], config=self.config(semantic_ceiling=1)
        ).run()
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        node = self.store.retry("run1", "a", force=True)
        self.assertIs(node.state, st.NodeState.PENDING)
        self.assertEqual(node.granted_extra_attempts, 1)


if __name__ == "__main__":
    unittest.main()
