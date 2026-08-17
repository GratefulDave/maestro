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

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


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
    return wt.GateResult(label="gate", scope="node", selector="sel",
                         command=("gate",), exit_code=0, green=True,
                         counts={"passed": passed})


def red(passed: int = 1, failed: int = 1) -> wt.GateResult:
    return wt.GateResult(label="gate", scope="node", selector="sel",
                         command=("gate",), exit_code=1, green=False,
                         counts={"passed": passed, "failed": failed})


def unparseable() -> wt.GateResult:
    """A gate that exited nonzero and reported no countable result — §10.2's
    ENVIRONMENTAL case, which is neither a red gate nor a green one."""
    return wt.GateResult(label="gate", scope="node", selector="sel",
                         command=("gate",), exit_code=1, green=False,
                         counts={})


class Recorder:
    """A callable that remembers how it was called, for order assertions."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


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
        _git(self.repo, "worktree", "add", "-q", "-b", "integration/run1",
             str(self.integration), "HEAD")

        self.written = {}          # node_id -> {relpath: content}
        self.prompts = {}          # node_id -> [retry_prompt per attempt]
        self.gate_script = {}      # (node_id, phase) -> [GateResult, ...]
        self.raise_for = {}        # node_id -> exception to raise, once
        self.exit_codes = {}       # node_id -> exit code for a code node
        self.quiesce_calls = []    # (attempt identity, phase) -> []

    def config(self, **kw) -> st.SchedulerConfig:
        base = dict(concurrency=2, node_timeout_s=60.0, turn_timeout_s=30.0,
                    final_acceptance_timeout_s=60.0, backstop_t_s=600.0,
                    semantic_ceiling=2)
        base.update(kw)
        return st.SchedulerConfig(**base)

    # ── the injected adapter seams ──────────────────────────────────────────

    def run_node(self, attempt, node, record, retry_prompt, on_launch,
                 cancel_requested):
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
            launched_pid=None)

    def run_gate(self, attempt, node, phase, cancel_requested):
        scripted = self.gate_script.get((node.node_id, phase))
        if scripted:
            return scripted.pop(0)
        # The default shape a healthy agent node produces: the pre-gate is red
        # because the behaviour is absent, the post-gate green because the
        # agent supplied it. Falsifiability, in fixture form.
        return red() if phase == "pre" else green()

    def quiesce_attempt(self, record, phase):
        self.assertIsInstance(record, st.AttemptRecord)
        self.quiesce_calls.append((record.key, phase))

    def deps(self, **kw):
        base = dict(store=self.store, repo=self.repo,
                    integration_path=self.integration,
                    integration_branch="integration/run1",
                    worktrees_root=self.root / "wt",
                    scratch_root=self.root / "scratch",
                    run_node=self.run_node,
                    run_gate=self.run_gate,
                    run_integration_gate=lambda path, specs, cancel_requested: green(3),
                    quiesce_attempt=self.quiesce_attempt)
        base.update(kw)
        return sch.SchedulerDeps(**base)

    # ── plan helpers ────────────────────────────────────────────────────────

    def agent(self, node_id, depth=0, needs=(), outputs=None, specs=()):
        return st.PlanNode(node_id=node_id, kind=st.NodeKind.AGENT, depth=depth,
                           needs=tuple(needs),
                           outputs=tuple(outputs if outputs is not None
                                         else (f"{node_id}.py",)),
                           specs=tuple(specs),
                           gate_command=("gate",), gate_selector=f"tests/{node_id}")

    def code(self, node_id, depth=0, needs=(), outputs=(), expects_changes=False):
        return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                           needs=tuple(needs), outputs=tuple(outputs),
                           command=("true",), expects_changes=expects_changes)

    def schedule(self, nodes, config=None, deps=None, run_id="run1"):
        scheduler = sch.Scheduler(run_id=run_id, nodes=list(nodes),
                                  config=config or self.config(),
                                  deps=deps or self.deps(),
                                  plan_digest="digest-" + run_id)
        self.addCleanup(scheduler.shutdown)
        return scheduler

    def states(self, run_id="run1"):
        return {r.node_id: r.state for r in self.store.node_records(run_id)}


# ── §7.1 / §7.2 the ready set and concurrency ───────────────────────────────

class ReadySetTests(SchedulerFixture):

    def test_two_independent_nodes_both_merge(self):
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        report = self.schedule([self.agent("a"), self.agent("b")]).run()
        self.assertEqual(self.states(), {"a": "MERGED", "b": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_dependent_node_waits_for_its_dependency_to_merge(self):
        """§7.1 — the predicate is MERGED, not VERIFIED or SUCCEEDED. A
        dependent must not start against work that has not landed."""
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}
        order = []
        inner = self.run_node

        def recording(attempt, node, record, retry_prompt, on_launch,
                      cancel_requested):
            order.append(("start", node.node_id,
                          dict(self.states())))
            return inner(attempt, node, record, retry_prompt, on_launch,
                         cancel_requested)

        self.schedule([self.agent("a"), self.agent("b", depth=1, needs=("a",))],
                      deps=self.deps(run_node=recording)).run()
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

        def counting(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            with guard:
                live.append(node.node_id)
                peak.append(len(live))
            try:
                return inner(attempt, node, record, retry_prompt, on_launch,
                             cancel_requested)
            finally:
                with guard:
                    live.remove(node.node_id)

        self.schedule([self.agent(n) for n in "abcde"],
                      config=self.config(concurrency=2),
                      deps=self.deps(run_node=counting)).run()
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
            sch.Scheduler(run_id="run1", nodes=[self.agent("a")],
                          config=self.config(),
                          deps=self.deps(integration_branch="maestro/run1"))

    def test_a_nested_name_under_the_run_namespace_is_also_refused(self):
        with self.assertRaises(sch.IntegrationBranchCollision):
            sch.Scheduler(run_id="run1", nodes=[self.agent("a")],
                          config=self.config(),
                          deps=self.deps(integration_branch="maestro/run1/integration"))

    def test_a_name_outside_the_namespace_is_accepted(self):
        scheduler = sch.Scheduler(run_id="run1", nodes=[self.agent("a")],
                                  config=self.config(), deps=self.deps())
        self.addCleanup(scheduler.shutdown)
        self.assertEqual(scheduler.run_id, "run1")

    def test_another_runs_namespace_does_not_collide(self):
        """The guard is scoped to *this* run's namespace, not to the word
        `maestro` — refusing more than git does would be a different rule."""
        scheduler = sch.Scheduler(run_id="run2", nodes=[self.agent("a")],
                                  config=self.config(),
                                  deps=self.deps(integration_branch="maestro/run1"))
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
            seen.setdefault("state_at_pre_gate",
                            self.store.get_node("run1", node.node_id).state)
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
        self.assertEqual(self.states(), {"a": "BLOCKED", "b": "MERGED"})


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
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 2)
        # SEMANTIC is the only class that mutates the prompt (§7.5).
        first, second = self.prompts["a"]
        self.assertIsNone(first)
        self.assertIsNotNone(second)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_the_retry_prompt_names_the_offending_paths_for_a_permission_failure(self):
        """§7.5 — the offending paths are named in the retry prompt, and the
        naming is what makes the retry genuinely new instructions rather than
        the same request repeated."""
        self.written = {"a": {"a.py": "A\n", "not-declared.py": "X\n"}}

        def second_attempt_is_clean(attempt, node, record, retry_prompt,
                                     on_launch, cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            files = ({"a.py": "A\n"} if record.attempt_no > 1
                     else {"a.py": "A\n", "not-declared.py": "X\n"})
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=second_attempt_is_clean)).run()
        self.assertIn("not-declared.py", self.prompts["a"][1])
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)

    def test_the_cumulative_ceiling_blocks_a_node_that_never_succeeds(self):
        """§7.5 — at most K SEMANTIC attempts per `(run_id, node_id)` across
        all bases, then `SEMANTIC_BUDGET_EXHAUSTED`."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        report = self.schedule([self.agent("a")],
                               config=self.config(semantic_ceiling=2)).run()
        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)
        self.assertEqual(node.attempt_no, 2)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_a_blocked_node_is_reported_with_its_reason(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script[("a", "post")] = [red()] * 10
        report = self.schedule([self.agent("a")],
                               config=self.config(semantic_ceiling=1)).run()
        self.assertEqual(dict(report.blocked)["a"],
                         st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)

    def _blocked_transition(self, node_id: str = "a"):
        """The one BLOCKED transition row for a node, from the audit tier."""
        rows = [row for row in self.store.audit_transitions("run1", node_id)
                if row.get("to_state") == st.NodeState.BLOCKED.value]
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
        self.schedule([self.agent("a")],
                      config=self.config(semantic_ceiling=1)).run()

        detail = self._blocked_transition().get("detail", {})
        self.assertEqual(detail.get("clause"), 3,
                         "the red post-node gate is clause 3")
        self.assertNotEqual(detail, {})

    def test_the_blocking_transition_names_the_offending_paths(self):
        """§1.1 item 4 / §7.5 — the paths that justified calling a permission
        failure SEMANTIC are recorded where the block is recorded, not only in
        the retry prompt that the blocking attempt never gets."""
        self.written = {"a": {"a.py": "A\n", "not-declared.py": "X\n"}}
        self.schedule([self.agent("a")],
                      config=self.config(semantic_ceiling=1)).run()

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
        self.schedule([self.agent("a")],
                      config=self.config(environmental_retries=0)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.block_reason,
                      st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
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
        report = self.schedule([self.agent("a"), self.agent("b")],
                               config=self.config(environmental_retries=0)).run()
        self.assertIs(self.store.get_node("run1", "b").state, st.NodeState.MERGED)
        self.assertIsNotNone(report.outcome)

    def test_an_unclassified_failure_is_environmental_never_semantic(self):
        """Fail-closed: an engine bug must never be recorded as a verdict
        about the code under test."""
        self.raise_for = {"a": RuntimeError("engine bug")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")],
                      config=self.config(environmental_retries=0)).run()
        attempt = self.store.get_attempt("run1", "a", 1)
        self.assertIs(attempt.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_an_environmental_failure_is_retried_and_can_then_succeed(self):
        self.raise_for = {"a": OSError("transient")}
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").attempt_no, 2)



class QuiescenceTests(SchedulerFixture):
    """Every state transition follows a successful, identity-bound proof."""

    def test_an_exception_from_run_node_is_quiesced_before_failure_classification(self):
        phases = []

        def quiesce(record, phase):
            phases.append((record.key, phase,
                           self.store.get_node("run1", record.node_id).state))

        self.raise_for = {"a": RuntimeError("runner leaked")}
        report = self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=0),
            deps=self.deps(quiesce_attempt=quiesce)).run()

        self.assertEqual([phase for _, phase, _ in phases],
                         ["pre-baseline", "pre-inventory", "settle"])
        self.assertTrue(all(state is st.NodeState.RUNNING
                            for _, _, state in phases))
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
            [self.agent("a")],
            deps=self.deps(quiesce_attempt=quiesce)).run()

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
            self.assertTrue(watchdog_quiesced.wait(timeout=3.0))

        def kill_attempt(_record):
            kill_called.set()
            raise RuntimeError("launcher kill failed")

        def quiesce(_record, phase):
            if phase == "watchdog":
                watchdog_quiesced.set()

        report = self.schedule(
            [self.agent("a")],
            config=self.config(node_timeout_s=0.01, turn_timeout_s=0.01,
                               final_acceptance_timeout_s=1.0,
                               backstop_t_s=5.0, environmental_retries=3),
            deps=self.deps(provision=provision, kill_attempt=kill_attempt,
                           quiesce_attempt=quiesce)).run()

        node = self.store.get_node("run1", "a")
        transitions = self.store.audit_transitions("run1", "a")
        self.assertTrue(kill_called.is_set())
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(node.attempt_no, 1)
        self.assertEqual(transitions[-1]["detail"]["phase"], "watchdog-kill")
        self.assertEqual(
            transitions[-1]["detail"]["exception_type"], "RuntimeError")
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)


    def test_node_gate_quiescence_error_blocks_instead_of_becoming_a_retry(self):
        def gate(_attempt, _node, _phase, _cancel_requested):
            raise wt.HarnessQuiescenceError(
                "HARNESS_CONTEXT_QUIESCENCE_UNPROVEN")

        report = self.schedule(
            [self.agent("a")], deps=self.deps(run_gate=gate)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(node.attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

class GenerationFenceTests(SchedulerFixture):

    def test_a_stale_worker_cannot_commit_after_losing_running_ownership(self):
        def runner(attempt, node, record, retry_prompt, on_launch,
                   cancel_requested):
            on_launch(None)
            if record.attempt_no == 1:
                (attempt.path / "stale.py").write_text("stale\n")
                self.store.fail_attempt("run1", node.node_id,
                                        st.RetryClass.ENVIRONMENTAL)
            else:
                (attempt.path / "fresh.py").write_text("fresh\n")
            return sch.NodeExecution()

        report = self.schedule(
            [self.agent("a", outputs=("fresh.py",))],
            deps=self.deps(run_node=runner)).run()

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
                self.assertTrue(watchdog_quiesced.wait(timeout=3.0))

        def quiesce(record, phase):
            if phase == "watchdog":
                watchdog_quiesced.set()

        def gate(attempt, node, phase, cancel_requested):
            gate_attempts.append(self.store.get_node("run1", node.node_id).attempt_no)
            return self.run_gate(attempt, node, phase, cancel_requested)

        def runner(attempt, node, record, retry_prompt, on_launch,
                   cancel_requested):
            runner_attempts.append(record.attempt_no)
            return self.run_node(attempt, node, record, retry_prompt, on_launch,
                                 cancel_requested)

        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            config=self.config(node_timeout_s=0.01, turn_timeout_s=0.01,
                               final_acceptance_timeout_s=1.0,
                               backstop_t_s=5.0, environmental_retries=1),
            deps=self.deps(provision=provision, quiesce_attempt=quiesce,
                           run_gate=gate, run_node=runner)).run()

        self.assertEqual(len(provision_calls), 2)
        self.assertEqual(gate_attempts, [2, 2])
        self.assertEqual(runner_attempts, [2])
        self.assertIs(self.store.get_attempt("run1", "a", 1).retry_class,
                      st.RetryClass.ENVIRONMENTAL)
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
        self.written = {"a": {"shared.py": "A\n"},
                        "b": {"shared.py": "B\n"},
                        "c": {"c.py": "C\n"}}
        nodes = [self.agent("a", outputs=("shared.py",)),
                 self.agent("b", outputs=("shared.py",)),
                 self.agent("c", depth=1, needs=("b",))]
        report = self.schedule(nodes).run()

        states = self.states()
        self.assertEqual(states["a"], "MERGED")
        self.assertEqual(states["b"], "BLOCKED")
        self.assertIs(self.store.get_node("run1", "b").block_reason,
                      st.BlockReason.MERGE_CONFLICT)
        # Derived-unready, never stored terminal — that is what keeps the
        # cascade reversible for an operator rescue.
        self.assertEqual(states["c"], "PENDING")
        self.assertIn("c", self.store.upstream_blocked("run1"))
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_the_integration_head_is_byte_identical_after_a_conflict(self):
        self.written = {"a": {"shared.py": "A\n"}, "b": {"shared.py": "B\n"}}
        self.schedule([self.agent("a", outputs=("shared.py",)),
                       self.agent("b", outputs=("shared.py",))]).run()
        self.assertEqual(_git(self.integration, "status", "--porcelain"), "")


# ── §7.8 cancellation and resume ────────────────────────────────────────────

class CancellationTests(SchedulerFixture):

    def test_cancel_writes_cancelled_for_every_non_terminal_node(self):
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a"), self.agent("b", depth=1,
                                                               needs=("a",))])
        scheduler.cancel()
        report = scheduler.run()
        self.assertEqual(set(self.states().values()), {"CANCELLED"})
        self.assertIs(report.outcome, st.RunOutcome.CANCELLED)



    def test_cancellation_during_the_pre_node_gate_cannot_run_the_node(self):
        holder = {}

        def gate(attempt, node, phase, cancel_requested):
            self.assertEqual(phase, "pre")
            holder["scheduler"].cancel()
            self.assertTrue(cancel_requested())
            return red()

        scheduler = self.schedule([self.agent("a")],
                                  deps=self.deps(run_gate=gate))
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

        scheduler = self.schedule([self.agent("a")],
                                  deps=self.deps(run_gate=gate))
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
            [self.agent("a")],
            deps=self.deps(run_integration_gate=final_gate))
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
        self.store.start_attempt("run1", "a", _git(self.integration, "rev-parse", "HEAD"))
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.RUNNING)

        reclaimed = self.store.resume_run("run1")
        self.assertEqual(reclaimed, ("a",))
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.PENDING)

        report = scheduler.run()
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertGreater(self.store.get_node("run1", "a").attempt_no, 1)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)



    def test_crash_after_verification_rehydrates_the_verified_sha_and_merges_once(self):
        node = self.agent("a")
        crashed = self.schedule([node])
        crashed.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo, "run1", "a", attempt_no, base, self.root / "wt",
            self.root / "scratch")
        baseline = wt.take_baseline(attempt)
        (attempt.path / "a.py").write_text("A\n")
        after = wt.inventory(attempt.path)
        output_sha = wt.commit_measured_delta(
            attempt, wt.delta(baseline, after), after, "a attempt 1")
        self.store.mark_verified("run1", "a", output_sha)

        restarted = self.schedule([node])
        first_resume = restarted.run()
        second_resume = self.schedule([node]).run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(self.store.get_node("run1", "a").output_sha, output_sha)
        self.assertEqual(
            _git(self.integration, "log", "--format=%s").splitlines().count("merge a"),
            1)
        self.assertIs(first_resume.outcome, st.RunOutcome.ACCEPTED)
        self.assertIs(second_resume.outcome, st.RunOutcome.ACCEPTED)

    def test_unverifiable_durable_verified_sha_blocks_before_merge(self):
        node = self.agent("a")
        scheduler = self.schedule([node])
        scheduler.project()
        self.store.start_attempt("run1", "a",
                                 _git(self.integration, "rev-parse", "HEAD"))
        self.store.mark_verified("run1", "a", "not-a-commit-digest")

        report = self.schedule([node]).run()

        stored = self.store.get_node("run1", "a")
        self.assertIs(stored.state, st.NodeState.BLOCKED)
        self.assertIs(stored.block_reason, st.BlockReason.OUTPUT_IDENTITY_INVALID)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_rehydration_refuses_a_descendant_not_owned_by_the_attempt(self):
        """A forged row may name a real descendant, but not this attempt's ref."""
        node = self.agent("a")
        crashed = self.schedule([node])
        crashed.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", "a", base)
        attempt = wt.create_attempt_worktree(
            self.repo, "run1", "a", attempt_no, base, self.root / "wt",
            self.root / "scratch")
        baseline = wt.take_baseline(attempt)
        (attempt.path / "a.py").write_text("A\n")
        after = wt.inventory(attempt.path)
        output_sha = wt.commit_measured_delta(
            attempt, wt.delta(baseline, after), after, "a attempt 1")
        forged_descendant = _git(
            self.repo, "commit-tree",
            _git(self.repo, "rev-parse", "{}^{{tree}}".format(output_sha)),
            "-p", output_sha, "-m", "forged descendant")
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
            [self.agent("a", specs=("spec_a", "shared")),
             self.agent("b", specs=("spec_b", "shared"))],
            deps=self.deps(run_integration_gate=integration_gate)).run()
        self.assertEqual(seen["specs"], ("shared", "spec_a", "spec_b"))
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_red_integration_gate_declares_blocked_with_the_result(self):
        """§8.8 — everything merged but the integration gate failed is the
        BLOCKED outcome, not an undefined state. The merged work stays on the
        integration branch and publishing remains the operator's decision."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_integration_gate=lambda p, s, c: red())).run()
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
        report = self.schedule([self.agent("a"), self.agent("b")],
                               deps=self.deps(run_integration_gate=gate)).run()
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

        self.schedule([self.agent("a")],
                      deps=self.deps(run_gate=gate,
                                     run_integration_gate=integration_gate)).run()
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

        def observing(attempt, node, record, retry_prompt, on_launch,
                      cancel_requested):
            names.extend(t.name for t in threading.enumerate()
                         if t.name == "maestro-watchdog")
            return self.run_node(attempt, node, record, retry_prompt, on_launch,
                                 cancel_requested)

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=observing)).run()
        self.assertEqual(names, ["maestro-watchdog"])

    def test_the_watchdog_thread_does_not_outlive_the_run(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()
        self.assertNotIn("maestro-watchdog",
                         [t.name for t in threading.enumerate()])

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
        self.assertLess(abs(elapsed), 300,
                        "last_transition_at is not on the epoch clock the "
                        "backstop compares against")

    def test_a_silent_run_declares_stuck_with_a_diagnostic(self):
        """§11.2 — the backstop fires on transition silence within T
        regardless of how many panes are open, and the scheduler prints the
        same diagnostic `run status` prints rather than exiting silently."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler = self.schedule([self.agent("a")],
                                  config=self.config(backstop_t_s=61.0,
                                                     node_timeout_s=60.0,
                                                     final_acceptance_timeout_s=60.0))
        scheduler.project()
        # Backdate the run's silence past T without waiting it out.
        self.store.conn.execute(
            "UPDATE runs SET last_transition_at=? WHERE run_id=?",
            ("1990-01-01T00:00:00.000+00:00", "run1"))
        report = scheduler.run()
        self.assertIs(report.outcome, st.RunOutcome.STUCK)
        self.assertIn("no lifecycle transition within", scheduler.status_diagnostic())

    def test_the_diagnostic_says_why_each_node_is_not_ready(self):
        """§11.2 — `run status` must answer "why is nothing happening"
        without reading the database by hand."""
        self.gate_script[("a", "pre")] = [green()]
        scheduler = self.schedule([self.agent("a"),
                                   self.agent("b", depth=1, needs=("a",))])
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

        def reporting(attempt, node, record, retry_prompt, on_launch,
                      cancel_requested):
            armed["before"] = self.store.get_attempt(
                "run1", node.node_id, record.attempt_no).armed
            on_launch(4321)
            armed["after"] = self.store.get_attempt(
                "run1", node.node_id, record.attempt_no).armed
            return self.run_node(attempt, node, record, retry_prompt,
                                 lambda pid: None, cancel_requested)

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=reporting)).run()
        self.assertFalse(armed["before"], "armed before the adapter reported launch")
        self.assertTrue(armed["after"])
        self.assertEqual(self.store.get_attempt("run1", "a", 1).pid, 4321)

    def test_an_attempt_is_unarmed_until_launch_is_reported(self):
        """The pre-launch segment is signal-less by construction, not by
        omission: no process and no transcript exist yet."""
        scheduler = self.schedule([self.agent("a")])
        scheduler.project()
        attempt_no = self.store.start_attempt(
            "run1", "a", _git(self.integration, "rev-parse", "HEAD"))
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
            "run1", "a", _git(self.integration, "rev-parse", "HEAD"))
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
        report = self.schedule([self.agent("a")],
                               config=self.config(semantic_ceiling=1)).run()
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        node = self.store.retry("run1", "a", force=True)
        self.assertIs(node.state, st.NodeState.PENDING)
        self.assertEqual(node.granted_extra_attempts, 1)


if __name__ == "__main__":
    unittest.main()
