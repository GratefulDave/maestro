"""§16.3 item 42 — the launcher's typed failure class reaches the classifier.

Every test here drives the **real** adapter path. The two tests that let this
defect live (`test_retry_policy.py:121`, `:390`) build an `rp.FailureSignal`
by hand, which is green over a path production cannot reach; a new test that
also bypassed the adapter would prove exactly as little.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import launcher
from adw_modules import lifecycle as lc
from adw_modules import retry_policy as rp
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules import worktree as wt
from adw_modules.launcher import (FakeLauncher, LaunchHandle, LaunchSpec,
                                  PollResult, PollState)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, run_id: str):
    repo = root / ("repo-" + run_id)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "maestro@example.invalid")
    git(repo, "config", "user.name", "Maestro Launcher")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    branch = "integration/{}".format(run_id)
    integration = root / ("integration-" + run_id)
    git(repo, "worktree", "add", "-q", "-b", branch, str(integration), "HEAD")
    return repo, integration, branch


def red_gate() -> "wt.GateResult":
    """A failing gate — §7.4's falsifiability clause, satisfied at the pre-run."""
    return wt.GateResult(label="node", scope="node", selector="k",
                         command=("pytest",), exit_code=1, green=False,
                         counts={"passed": 0, "failed": 1, "skipped": 0,
                                 "errored": 0})


def agent_node(node_id: str = "a") -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
                       outputs=(node_id + ".py",),
                       gate_command=("pytest",), gate_selector="k")


def gone_handle(token: str, worktree: Path) -> LaunchHandle:
    """A handle the fake launcher holds no record of.

    `FakeLauncher.poll` answers an unknown token with GONE/`AGENT_GONE`, which
    is the same shape `HerdrLauncher.poll` produces when `herdr agent get`
    answers `agent_not_found` and nothing was declared.
    """
    return LaunchHandle(token, "fake:" + token, "agent-" + token, worktree)


class LauncherClassificationFixture(unittest.TestCase):
    """A real `Scheduler`, a real store, a real repo — only `run_node` varies."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "lifecycle.db"
        self.store = lc.LifecycleStore(self.database)
        self.addCleanup(self.store.close)

    def config(self, **overrides) -> st.SchedulerConfig:
        base = dict(concurrency=1, node_timeout_s=30, turn_timeout_s=10,
                    final_acceptance_timeout_s=30, backstop_t_s=120,
                    semantic_ceiling=2)
        base.update(overrides)
        return st.SchedulerConfig(**base)

    def run_with(self, run_id: str, run_node, **cfg) -> None:
        repo, integration, branch = make_repo(self.root, run_id)
        deps = sch.SchedulerDeps(
            store=self.store, repo=repo, integration_path=integration,
            integration_branch=branch,
            worktrees_root=self.root / (run_id + "-worktrees"),
            scratch_root=self.root / (run_id + "-scratch"), run_node=run_node,
            run_gate=lambda *a: red_gate(),
            run_integration_gate=lambda *a: red_gate(),
            quiesce_attempt=lambda record, phase: None,
            kill_attempt=lambda *a: None)
        scheduler = sch.Scheduler(run_id, [agent_node()], self.config(**cfg),
                                  deps, plan_digest="launcher-digest")
        self.addCleanup(scheduler.shutdown)
        scheduler.run()

    def attempt_classes(self, run_id: str):
        return {a.retry_class
                for a in self.store.attempts_for(run_id, "a")}

    def block_reason(self, run_id: str):
        return self.store.get_node(run_id, "a").block_reason

    def failure_details(self, run_id: str):
        reader = lc.LifecycleReader.open(self.database)
        try:
            return [t.get("detail", {}) for t in reader.transitions(run_id)
                    if t.get("detail")]
        finally:
            reader.close()


# ── R2 — GONE terminates the loop instead of spinning ───────────────────────

class GonePollTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_gone_poll_returns_rather_than_spinning(self):
        """§16.3 item 42's liveness half.

        Bounded by a sleep that raises: without the GONE branch the loop
        re-polls forever, and an unbounded test would hang the suite rather
        than fail it.
        """
        adapter = FakeLauncher()
        handle = gone_handle("gone-token", self.root)

        slept = []

        def sleep(_seconds):
            slept.append(1)
            if len(slept) > 3:
                raise AssertionError(
                    "the poll loop spun on PollState.GONE instead of returning")

        execution = maestro._poll_agent_execution(
            adapter, handle, self.root / "envelope.json", None,
            lambda: False, lambda *a: None, sleep=sleep)

        self.assertEqual(slept, [])
        self.assertIs(execution.launcher_failure, rp.LauncherFailure.TRANSPORT)
        self.assertEqual(execution.launch_detail, "AGENT_GONE")
        self.assertFalse(execution.envelope_parsed)

    def test_exited_poll_still_returns_the_envelope_verdict(self):
        """The control: the branch that already worked is unchanged."""
        adapter = FakeLauncher()
        spec = LaunchSpec(correlation_token="ok", worktree=self.root,
                          prompt_path=self.root / "p",
                          envelope_path=self.root / "e", route="omp",
                          model="m", effort="low", profile="f",
                          session_dir=self.root / "s")
        handle = adapter.launch(spec)
        adapter.complete("ok", exit_code=0)
        envelope = self.root / "e"
        envelope.write_text('{"success": true}', encoding="utf-8")

        execution = maestro._poll_agent_execution(
            adapter, handle, envelope, None, lambda: False, lambda *a: None,
            sleep=lambda _s: None)

        self.assertTrue(execution.envelope_parsed)
        self.assertIsNone(execution.launcher_failure)
        self.assertEqual(execution.launch_detail, "ENVELOPE_SUCCESS")
        # `state.exit_code or 1` maps a zero exit to 1. That predates this
        # repair and is deliberately left alone: an agent node's predicate is
        # its envelope and its gates (§7.3), and `verify_agent_node` never
        # reads `exit_code`. Pinned here so the quirk is recorded rather than
        # rediscovered.
        self.assertEqual(execution.exit_code, 1)


# ── R1 — the typed class reaches `classify` through the real adapter ────────

class LauncherFailureReachesClassifyTest(LauncherClassificationFixture):
    def test_gone_execution_classifies_launcher_transient(self):
        """The return path: a `NodeExecution` the real poll loop produced."""
        adapter = FakeLauncher()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            handle = gone_handle(
                "{}-{}".format(node.node_id, record.attempt_no), attempt.path)
            on_launch(None)
            return maestro._poll_agent_execution(
                adapter, handle, attempt.scratch / "envelope.json", record,
                cancelled, lambda *a: None, sleep=lambda _s: None)

        self.run_with("run-gone", run_node)
        self.assertEqual(self.attempt_classes("run-gone"),
                         {st.RetryClass.LAUNCHER_TRANSIENT})
        self.assertIs(self.block_reason("run-gone"),
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        details = self.failure_details("run-gone")
        self.assertIn("TRANSPORT",
                      [d.get("launcher_failure") for d in details])
        self.assertTrue(
            any("AGENT_GONE" in d.get("reason", "") for d in details),
            "the poll result's own account never reached detail_json")

    def test_session_path_missing_classifies_launcher_transient(self):
        """The exception path — F1a, verbatim from run-f31686ea4.

        Drives the production refusal itself. An earlier version of this test
        raised `LaunchFailed` from the fake `run_node`, which asserted only
        that the scheduler classifies what it is handed; reverting the
        production line left it green. That is the same bypass
        `test_retry_policy.py:121` is guilty of, caught by its own mutation
        check.
        """
        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            handle = gone_handle(node.node_id, attempt.path)
            self.assertIsNone(handle.transcript_path)
            maestro._require_session_path(handle, node.node_id, record.attempt_no)
            raise AssertionError("the session-path refusal did not fire")

        self.run_with("run-session", run_node)
        self.assertEqual(self.attempt_classes("run-session"),
                         {st.RetryClass.LAUNCHER_TRANSIENT})
        self.assertIs(self.block_reason("run-session"),
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)

    def test_scratch_redirect_refusal_classifies_launcher_transient(self):
        """F2 — the real `pane_env_flags` refusal, through the real map.

        `pane_env_flags` raises a bare `RuntimeError` carrying its code inside
        the message. Nothing on this path reads that message: `classify_error`
        dispatches on the exception's type (§7.5).
        """
        adapter = FakeLauncher()

        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            return maestro._typed_launch(adapter, launcher.pane_env_flags, {})

        self.run_with("run-scratch", run_node)
        self.assertEqual(self.attempt_classes("run-scratch"),
                         {st.RetryClass.LAUNCHER_TRANSIENT})

    def test_credential_refusal_blocks_with_zero_budget(self):
        """§7.5's zero-retry rule, end to end and reachable for the first time."""
        adapter = FakeLauncher()
        attempts_made = []

        def refuse():
            raise PermissionError("herdr refused: not authorized")

        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            attempts_made.append(record.attempt_no)
            return maestro._typed_launch(adapter, refuse)

        self.run_with("run-cred", run_node)
        self.assertEqual(attempts_made, [1])
        self.assertIs(self.block_reason("run-cred"),
                      st.BlockReason.CREDENTIAL_REFUSED)

    def test_transient_launch_failure_keeps_its_launcher_budget(self):
        """The control for the zero-budget arm: TRANSPORT still gets its retries."""
        adapter = FakeLauncher()
        attempts_made = []

        def drop():
            raise TimeoutError("herdr call timed out")

        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            attempts_made.append(record.attempt_no)
            return maestro._typed_launch(adapter, drop)

        self.run_with("run-transport", run_node)
        self.assertEqual(attempts_made, [1, 2, 3])
        self.assertIs(self.block_reason("run-transport"),
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)

    def test_launcher_detail_reaches_detail_json(self):
        """§1.1 item 4 — typed member and prose account, both durable."""
        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            raise sch.LaunchFailed(rp.LauncherFailure.CREDENTIAL,
                                   "PermissionError: herdr refused")

        self.run_with("run-detail", run_node)
        details = self.failure_details("run-detail")
        self.assertTrue(details, "no failure detail was recorded at all")
        typed = [d.get("launcher_failure") for d in details
                 if d.get("launcher_failure")]
        prose = [d.get("reason", "") for d in details if d.get("reason")]
        self.assertIn("CREDENTIAL", typed)
        self.assertTrue(any("PermissionError" in r for r in prose),
                        "the adapter's account never reached detail_json")


# ── §7.5's own rule, enforced the way §7.5 enforces the others ──────────────

class ClassificationIsNotLexicalTest(unittest.TestCase):
    #: The three functions that stand between a launcher fault and a retry
    #: class. §7.5 forbids classification from reading process output; these
    #: are where a message string would be read if anything read one.
    CLASSIFYING = ("_launcher_failure_for", "_typed_launch",
                   "_poll_agent_execution")
    TEXT_METHODS = ("startswith", "endswith", "find", "index", "split",
                    "lower", "upper", "strip")

    #: The scheduler-side half of the same seam. §16.3 items 45 and 46 added
    #: two readers of a launcher refusal — one deciding whether §8.3's
    #: quiescence proof is owed, one deciding which budget the refusal spends
    #: — and both are lifecycle decisions about a failed launch. The refusal
    #: codes travel in the exception's message, so this is exactly where a
    #: `startswith('LAUNCH_REFUSED:')` would be written next. Guarding
    #: `maestro.py` alone would have left the rule stated at one site and
    #: unenforced at the two newest ones.
    SCHEDULER_CLASSIFYING = ("pane_created", "classified_failure",
                             "_launch_left_nothing_to_reap")

    def _lexical_offenders(self, source: str, names) -> list:
        offenders = []
        for fn in ast.walk(ast.parse(source)):
            if not isinstance(fn, ast.FunctionDef) or fn.name not in names:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in self.TEXT_METHODS):
                    offenders.append("{}: .{}()".format(fn.name, node.func.attr))
                if isinstance(node, ast.Compare) and any(
                        isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                    offenders.append("{}: membership test".format(fn.name))
        return offenders

    def test_classification_reads_no_message_text(self):
        """§7.5: "classification is structural, never lexical"."""
        self.assertEqual(
            self._lexical_offenders(
                Path(maestro.__file__).read_text(encoding="utf-8"),
                self.CLASSIFYING),
            [])

    def test_the_schedulers_refusal_readers_are_structural_too(self):
        """Both read the exception's *type* and a typed field on it, which
        §7.5 permits, and neither touches the refusal's message."""
        source = Path(sch.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            self._lexical_offenders(source, self.SCHEDULER_CLASSIFYING), [])
        # ... and the names really are present, or the check above is vacuous.
        for name in self.SCHEDULER_CLASSIFYING:
            self.assertIn("def {}(".format(name), source)

    def test_the_scheduler_guard_would_convict_a_planted_violation(self):
        planted = (
            "def classified_failure(self):\n"
            "    if str(self.__cause__).startswith('LAUNCH_REFUSED:'):\n"
            "        return 1\n")
        self.assertEqual(
            self._lexical_offenders(planted, self.SCHEDULER_CLASSIFYING),
            ["classified_failure: .startswith()"])

    def test_the_guard_would_convict_a_planted_violation(self):
        """The mutation control: the guard is wired, not decorative."""
        planted = ast.parse(
            "def _typed_launch(a, b):\n"
            "    if str(b).startswith('LAUNCH_REFUSED:'):\n"
            "        return 1\n")
        offenders = []
        for fn in ast.walk(planted):
            if not isinstance(fn, ast.FunctionDef) or fn.name not in self.CLASSIFYING:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in self.TEXT_METHODS):
                    offenders.append("{}: .{}()".format(fn.name, node.func.attr))
        self.assertEqual(offenders, ["_typed_launch: .startswith()"])


class PaneLaunchIsTypedTest(unittest.TestCase):
    """`_typed_launch_pane` — the three non-agent-node launch sites.

    The cause behind run-f31686ea4's refusal is already fixed: every
    `LaunchSpec` carries a redirected environment. What these cover is the
    classification gap that outlived it — the next refusal at these sites
    spending the launcher budget and naming itself rather than silently
    burning ENVIRONMENTAL.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def spec(self) -> LaunchSpec:
        return LaunchSpec(correlation_token="review-abc", worktree=self.root,
                          prompt_path=self.root / "p",
                          envelope_path=self.root / "e", route="omp",
                          model="m", effort="low", profile="f",
                          session_dir=self.root / "s")

    class _RefusingRunner(FakeLauncher):
        """A real adapter whose launch refuses — `classify` is the real one."""

        def __init__(self, exc: BaseException) -> None:
            super().__init__()
            self._exc = exc

        def launch(self, spec):
            raise self._exc

    def test_credential_refusal_becomes_a_typed_launch_failure(self):
        """T-A: a real PermissionError through the real classify_error chain."""
        runner = self._RefusingRunner(PermissionError("herdr: not authorized"))
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(runner, self.spec())
        self.assertIs(caught.exception.failure, rp.LauncherFailure.CREDENTIAL)
        self.assertIn("PermissionError", caught.exception.detail)

    def test_scratch_redirect_refusal_becomes_a_typed_launch_failure(self):
        """T-A: the bare RuntimeError shape that refused in run-f31686ea4.

        `pane_env_flags` raises a bare `RuntimeError` carrying its code inside
        the message. Nothing here reads the message.
        """
        try:
            launcher.pane_env_flags({})
        except RuntimeError as exc:
            refusal = exc
        else:  # pragma: no cover - the refusal is the point
            self.fail("pane_env_flags did not refuse an empty environment")

        runner = self._RefusingRunner(refusal)
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(runner, self.spec())
        self.assertIs(caught.exception.failure, rp.LauncherFailure.STARTUP)
        self.assertIn("SCRATCH_REDIRECT_MISSING", caught.exception.detail)

    def test_the_wrapper_carries_the_refusals_typed_facts_through(self):
        """§16.3 items 45 and 46 end to end, through the real wrapper.

        Both repairs read the launcher's typed refusal off `LaunchFailed`'s
        `__cause__`, and that chaining is `_typed_launch`'s `raise ... from
        exc` — a production line neither repair touches. If it were ever
        changed to a bare `raise scheduler.LaunchFailed(...)`, both would
        silently fall back to their conservative defaults: every refusal
        quiesced and every refusal retried, which is exactly today's
        behaviour, so nothing else would notice. Asserted here rather than
        assumed, and against the real `_typed_launch_pane` rather than a
        reproduction of it.
        """
        try:
            launcher.pane_env_flags({})
        except launcher.LaunchRefused as exc:
            refusal = exc
        else:  # pragma: no cover - the refusal is the point
            self.fail("pane_env_flags did not refuse an empty environment")

        runner = self._RefusingRunner(refusal)
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(runner, self.spec())
        self.assertIs(caught.exception.__cause__, refusal)
        self.assertFalse(caught.exception.pane_created)
        self.assertIs(caught.exception.classified_failure,
                      rp.LauncherFailure.DETERMINISTIC_REFUSAL)

    def test_a_post_split_refusal_keeps_both_conservative_answers(self):
        """The control on the same wrapper: a pane may exist and another
        attempt may survive, so neither repair fires."""
        runner = self._RefusingRunner(
            launcher.LaunchRefused(launcher.LaunchRefusal.SHELL_NOT_READY))
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(runner, self.spec())
        self.assertTrue(caught.exception.pane_created)
        self.assertIsNot(caught.exception.classified_failure,
                         rp.LauncherFailure.DETERMINISTIC_REFUSAL)

    def test_a_successful_launch_is_returned_untouched(self):
        """The control: the helper is a classifier, not a filter."""
        runner = FakeLauncher()
        handle = maestro._typed_launch_pane(runner, self.spec())
        self.assertEqual(handle.correlation_token, "review-abc")

    def test_launch_failed_is_still_a_runtime_error(self):
        """Callers that catch RuntimeError around these sites keep working."""
        runner = self._RefusingRunner(PermissionError("nope"))
        with self.assertRaises(RuntimeError):
            maestro._typed_launch_pane(runner, self.spec())


class EveryLaunchSiteIsTypedTest(unittest.TestCase):
    """T-B: a structural guard over maestro.py's launch sites.

    **What this proves and what it does not.** It convicts a reverted call
    site: revert any `_typed_launch_pane(...)` back to `runner.launch(...)`
    and this goes red naming the line. It does NOT prove the reviewer path
    runs — no test here executes the finalization window, the node reviewer,
    or the author lane, because all three are closures nested inside CLI
    command functions with heavy scaffolding, and extracting them was
    deliberately left to a later pass.

    That distinction is the whole reason this docstring exists. A structural
    guard sold as coverage is the same defect as a test that asserts the
    scheduler classifies what it is handed — which is exactly what an earlier
    version of `test_session_path_missing_classifies_launcher_transient` did,
    and which only its mutation check caught. A guard sold as a guard is
    honest and useful; the same guard sold as execution is a lie that reads
    green forever.
    """

    #: Every launch must go through one of these. The list is deliberately
    #: not an allowlist of exempt call sites: an allowlist with one entry on
    #: day one is how a guard like this stops meaning anything.
    TYPED_LAUNCHERS = ("_typed_launch", "_typed_launch_pane")

    def test_no_raw_launch_call_survives_in_maestro(self):
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()

        enclosing = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    enclosing.setdefault(id(node), fn.name)

        raw = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "launch"):
                continue
            if enclosing.get(id(node)) in self.TYPED_LAUNCHERS:
                continue
            raw.append("maestro.py:{0}: {1}".format(
                node.lineno, lines[node.lineno - 1].strip()[:80]))

        self.assertEqual(
            raw, [],
            "every launch must go through _typed_launch/_typed_launch_pane, "
            "or its refusal reaches the scheduler with nothing structural in "
            "it and spends the ENVIRONMENTAL budget (§7.5)")

    def test_the_guard_would_convict_a_planted_raw_launch(self):
        """The mutation control: this guard is wired, not decorative."""
        planted = ast.parse(
            "def window_factory(m):\n"
            "    def launch_reviewer():\n"
            "        return runner.launch(spec)\n")
        enclosing = {}
        for fn in ast.walk(planted):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    enclosing.setdefault(id(node), fn.name)
        raw = [node for node in ast.walk(planted)
               if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute)
               and node.func.attr == "launch"
               and enclosing.get(id(node)) not in self.TYPED_LAUNCHERS]
        self.assertEqual(len(raw), 1)


if __name__ == "__main__":
    unittest.main()
