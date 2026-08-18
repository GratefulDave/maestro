"""§7.6 / §9.7 — a finished process is not a dead one.

The watchdog's process-alive signal (§7.6) read `attempt.pid` and nothing else:

    if attempt.pid is not None and not self._process_alive(attempt.pid):
        self._stall(attempt, StallReason.PROCESS_DEAD)

Nothing established *not finished* before ruling *dead*. A code node whose
command exits in milliseconds — the measured case writes one file and returns
zero — is therefore convicted PROCESS_DEAD whenever a poll lands after that
exit, retried twice into the same race, and blocked
`ENVIRONMENTAL_BUDGET_EXHAUSTED`. On the real scheduler path that reproduced
roughly one run in three.

**The defect class, and the count.** §9.7 states the rule this violates: *an
artifact a worker wrote outranks any status a supervisor observes about that
worker; absence of a process is not absence of output.* There are three
supervisor sites in the runtime, and two already applied it —
`HerdrLauncher.poll` consults the declared result before it will report `GONE`,
and `FinalizationWindow.poll` reads the reviewer's report before it reads the
reviewer's pid, so the reviewer path carries no equivalent race. `Watchdog.
_check_attempt` was the third and the only one that read the process first.
One of three, and that is the whole count.

**What was actually armed.** The inversion is worth stating because it is the
opposite of how the code reads. An *agent* attempt's `pid` is
`LaunchHandle.process_group`, which §8.3 and §16.3 item 17 establish is never
populated — so for the node kind §7.6 wrote this signal for ("Is the agent
still there?") it was unreachable. A *code* attempt's pid is a real one from
`subprocess.Popen`, and that is the one kind whose exit the harness already
observes directly. The signal fired only where it was wrong.

The fix is `exit_status_observed`: a typed fact about whether some other
component of the harness holds this process's handle and reads its exit code.
Structural, and among the facts §7.5 permits a classifier to read.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import scheduler as sch         # noqa: E402
from adw_modules import scheduler_types as st    # noqa: E402
from adw_modules import watchdog as wd           # noqa: E402

from test_scheduler import SchedulerFixture      # noqa: E402


def attempt(node_id: str = "a", pid: int = 4242) -> st.AttemptRecord:
    return st.AttemptRecord(
        run_id="run1", node_id=node_id, attempt_no=1, base_sha="b" * 40,
        started_at=0.0, launched_at=0.0, pid=pid)


class _Recorder:
    def __init__(self) -> None:
        self.failed = []
        self.killed = []

    def fail(self, att, retry_class, reason) -> None:
        self.failed.append((att.node_id, retry_class, reason))

    def kill(self, att) -> None:
        self.killed.append(att.node_id)


def watchdog_over(att, *, alive: bool, observed: bool, recorder: _Recorder,
                  **config_overrides) -> wd.Watchdog:
    defaults = dict(concurrency=1, node_timeout_s=600.0, turn_timeout_s=600.0,
                    final_acceptance_timeout_s=60.0, backstop_t_s=1800.0,
                    semantic_ceiling=2)
    defaults.update(config_overrides)
    return wd.Watchdog(
        config=st.SchedulerConfig(**defaults),
        attempts_provider=lambda: [att],
        write_heartbeat=lambda a, turns, at: None,
        kill=recorder.kill,
        fail_attempt=recorder.fail,
        process_alive=lambda pid: alive,
        exit_status_observed=lambda a: observed,
        transcript_record_count=lambda a: 0,
        time_source=lambda: 1.0)


# ── the rule, at the signal itself ──────────────────────────────────────────

class ProcessDeadRequiresAnUnaccountedExitTests(unittest.TestCase):

    def test_a_vanished_process_nobody_accounts_for_is_still_process_dead(self):
        """The signal keeps its full force in the case §7.6 wrote it for: an
        agent the herdr server spawned, whose handle Maestro does not hold."""
        recorder = _Recorder()
        watchdog_over(attempt(), alive=False, observed=False,
                      recorder=recorder).check_once()
        self.assertEqual(
            recorder.failed,
            [("a", st.RetryClass.ENVIRONMENTAL, wd.StallReason.PROCESS_DEAD.value)])

    def test_a_process_whose_exit_is_observed_is_never_process_dead(self):
        """The repair. Its disappearance is accounted for elsewhere, so this
        signal is not the one entitled to rule."""
        recorder = _Recorder()
        watchdog_over(attempt(), alive=False, observed=True,
                      recorder=recorder).check_once()
        self.assertEqual(recorder.failed, [])
        self.assertEqual(recorder.killed, [])

    def test_the_wall_clock_still_wins_over_an_observed_exit(self):
        """§7.6: elapsed beyond the node timeout is timed out whatever the
        other two signals say. The narrowing must not reach that."""
        recorder = _Recorder()
        watchdog_over(attempt(), alive=False, observed=True,
                      recorder=recorder, node_timeout_s=0.5).check_once()
        self.assertEqual(
            recorder.failed,
            [("a", st.RetryClass.ENVIRONMENTAL, wd.StallReason.NODE_TIMEOUT.value)])

    def test_a_live_process_is_unaffected_either_way(self):
        for observed in (True, False):
            with self.subTest(observed=observed):
                recorder = _Recorder()
                watchdog_over(attempt(), alive=True, observed=observed,
                              recorder=recorder).check_once()
                self.assertEqual(recorder.failed, [])


# ── the predicate the scheduler supplies ────────────────────────────────────

class ExitStatusObservedWiringTests(SchedulerFixture):
    """The predicate must come from the scheduler: only it knows who holds
    each process handle. A watchdog that guessed would be the wrong component
    answering the question."""

    def _predicate(self, nodes):
        scheduler = self.schedule(nodes)
        watchdog, _backstop = scheduler._start_liveness()
        return watchdog._exit_status_observed

    def test_a_code_nodes_exit_is_observed_by_the_harness(self):
        """The harness starts the command itself and the runner polls that
        handle until it exits, returning the exit code verification reads."""
        predicate = self._predicate([self.code("c"), self.agent("a")])
        self.assertTrue(predicate(attempt("c")))

    def test_an_agent_nodes_exit_is_observed_by_nothing(self):
        """herdr exposes no pid and no process group (§8.3), so absence is
        genuinely the only signal and the watchdog keeps the ruling."""
        predicate = self._predicate([self.code("c"), self.agent("a")])
        self.assertFalse(predicate(attempt("a")))

    def test_an_unknown_node_keeps_the_conservative_answer(self):
        predicate = self._predicate([self.agent("a")])
        self.assertFalse(predicate(attempt("nonesuch")))


# ── the incident, driven through the real scheduler ─────────────────────────

class FastCodeNodeIsNotDeadTests(SchedulerFixture):
    """A code node that succeeds in milliseconds must merge, not block.

    A single green run proves nothing about a race, so this drives the node
    repeatedly and asserts every iteration. Reverting the completion check
    reproduces the original signature: ENVIRONMENTAL retries whose recorded
    reason is PROCESS_DEAD, then ENVIRONMENTAL_BUDGET_EXHAUSTED.
    """

    ITERATIONS = 6

    def _fast_code_node(self, node_id: str, tag: str) -> st.PlanNode:
        """A command that exits in milliseconds, writing one declared output.

        The output path carries the iteration's tag. Each iteration merges
        onto the same integration branch, so a fixed filename would make every
        run after the first an idempotent no-op that blocks
        `CODE_NODE_NO_EFFECT` against its declared `expects_changes` — a
        correct §7.3 rule firing for a reason that has nothing to do with
        liveness, and it would mask what this test is for.
        """
        script = ("from pathlib import Path; "
                  f"Path('{tag}.txt').write_text('done\\n')")
        return st.PlanNode(
            node_id=node_id, kind=st.NodeKind.CODE, depth=0,
            outputs=(f"{tag}.txt",),
            command=(sys.executable, "-c", script),
            expects_changes=True)

    def _run_one(self, run_id: str) -> sch.RunReport:
        def run_node(attempt_wt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            process = subprocess.Popen(node.command, cwd=attempt_wt.path,
                                       start_new_session=True)
            on_launch(process.pid)
            process.wait()
            return sch.NodeExecution(exit_code=process.returncode)

        scheduler = sch.Scheduler(
            run_id=run_id, nodes=[self._fast_code_node("c", run_id)],
            config=self.config(concurrency=1),
            deps=self.deps(run_node=run_node), plan_digest="d-" + run_id)
        self.addCleanup(scheduler.shutdown)
        return scheduler.run()

    def test_a_millisecond_command_merges_on_every_iteration(self):
        for i in range(self.ITERATIONS):
            with self.subTest(iteration=i):
                run_id = f"run1-{i}"
                report = self._run_one(run_id)
                states = {r.node_id: r.state
                          for r in self.store.node_records(run_id)}
                reasons = [
                    t.get("detail", {}).get("reason")
                    for t in self.store.audit_transitions(run_id)
                    if t.get("node_id") == "c"]
                self.assertNotIn(
                    wd.StallReason.PROCESS_DEAD.value, reasons,
                    "a command that exited zero was ruled dead")
                self.assertEqual(states, {"c": "MERGED"})
                self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
