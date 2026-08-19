"""§7.6's third liveness signal, made reachable for an agent node.

§7.6 names three signals that can convict a running attempt. For an agent node
only two of them ever fired. `watchdog.Watchdog._poll` guards its PROCESS_DEAD
branch on `attempt.pid is not None`, and `attempts.pid` was written from
`LaunchHandle.process_group`, which is absent for every herdr-spawned agent —
so the branch was unreachable, TURN_TIMEOUT and NODE_TIMEOUT carried the whole
burden, and an agent whose process died silently waited out the node clock
before anything noticed (#20). On a 1800s node timeout that is half an hour of
a run doing nothing, unattended, with no signal that anything is wrong.

`herdr pane process-info` reports a foreground process group. This module is
the executable statement of what may and may not be done with it.

**Read, never signal.** The group is recorded on a *separate* handle field,
`liveness_pid`, and reaches `attempts.pid` — whose only reader is the
watchdog's `process_is_alive` call. It is deliberately not written to
`LaunchHandle.process_group`, because that field is §8.3's kill target and
§8.3 conditions writing it on an executed §9.8 receipt proving the group
excludes the pane shell and every sibling attempt (§16.3 items 17 and 30). No
such receipt exists. The two directions of failure are not comparable: a wrong
answer here reports a live attempt dead or a dead one live, and the design
already survives the latter because it is the behaviour being replaced; a
wrong answer on the kill path sends SIGKILL to the operator's shell.

**Declining is a real answer.** Recording the pane's own shell group would
make PROCESS_DEAD unreachable *and* silent — permanently satisfied, never
convicting — which is strictly worse than the gap it replaces, because it
would look fixed. Every case below where the foreground cannot be told apart
from the shell returns `None`, which leaves the attempt with exactly the two
clocks it has today.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import tempfile  # noqa: E402

from adw_modules import launcher  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402
from adw_modules import worktree as worktree_module  # noqa: E402
from adw_modules.route_receipts import (  # noqa: E402
    load_admitted_routes, load_public_key)
from test_launch_refusal_cleanup import FakeHerdr  # noqa: E402
from test_watchdog import (  # noqa: E402
    FakeClock, Recorder, make_attempt, make_config)


def _payload(**info: object) -> dict:
    return {"result": {"process_info": dict(info)}}


def _agent_pane(pgid: int = 90123, shell_pid: int = 88001) -> dict:
    """A pane whose foreground is an agent, not its shell.

    Shaped after a live `herdr pane process-info` reading: the foreground
    group holds several processes, none of them the shell, and the group id
    differs from `shell_pid`.
    """
    return _payload(
        foreground_process_group_id=pgid,
        shell_pid=shell_pid,
        foreground_processes=[
            {"name": "node", "argv": ["node", "cli.js"], "pid": pgid},
            {"name": "caffeinate", "argv": ["caffeinate", "-i"], "pid": pgid + 7},
        ])


def _idle_shell_pane(pid: int = 88001) -> dict:
    """A pane sitting at its prompt: one process, a shell, group == shell."""
    return _payload(
        foreground_process_group_id=pid,
        shell_pid=pid,
        foreground_processes=[{"name": "zsh", "argv": ["-zsh"], "pid": pid}])


class PaneLivenessPidTests(unittest.TestCase):
    def _resolve(self, payload, pane_id="w1:p2"):
        calls = []

        def herdr_call(*args, **kwargs):
            calls.append(args)
            if isinstance(payload, Exception):
                raise payload
            return payload

        result = launcher.pane_liveness_pid(herdr_call, pane_id)
        return result, calls

    def test_a_running_agent_yields_its_foreground_group(self):
        """The case the signal exists for, and the whole of #20's remedy."""
        pid, calls = self._resolve(_agent_pane())
        self.assertEqual(pid, 90123)
        self.assertEqual(calls, [("pane", "process-info", "--pane", "w1:p2")])

    def test_an_idle_shell_is_declined_rather_than_recorded(self):
        """Recording the shell would make PROCESS_DEAD silent, not reachable.

        The shell outlives every attempt in its pane, so an attempt carrying
        its group is never convicted by absence — and unlike today's `None`,
        it would read as a working signal to anyone auditing the column.
        """
        self.assertIsNone(self._resolve(_idle_shell_pane())[0])

    def test_a_group_equal_to_the_shell_pid_is_declined_without_names(self):
        """The same conclusion where the process names are unavailable.

        `_available_shell` needs a name to recognise a shell. The group-equals-
        shell test needs only two integers, so it still answers when the
        payload carries no usable `foreground_processes` entry.
        """
        payload = _payload(foreground_process_group_id=4242, shell_pid=4242,
                           foreground_processes=[])
        self.assertIsNone(self._resolve(payload)[0])

    def test_a_missing_group_is_declined(self):
        payload = _payload(shell_pid=4242, foreground_processes=[])
        self.assertIsNone(self._resolve(payload)[0])

    def test_a_nonsense_group_is_declined(self):
        """`0` and negatives address process groups no attempt owns.

        `kill(0, 0)` addresses the caller's own group -- the scheduler -- which
        is always alive, and a negative is not a group id at all. Either would
        answer the liveness question about the wrong processes.
        """
        for pgid in (0, -1):
            with self.subTest(pgid=pgid):
                payload = _payload(foreground_process_group_id=pgid,
                                   shell_pid=4242, foreground_processes=[])
                self.assertIsNone(self._resolve(payload)[0])

    def test_a_payload_without_process_info_is_declined(self):
        self.assertIsNone(self._resolve({"result": {}})[0])

    def test_a_failed_call_is_declined_rather_than_raised(self):
        """A launch must not fail because an advisory probe did.

        The attempt is already running by the time this is resolved; raising
        here would throw away work over a signal that is an improvement on
        having none.
        """
        self.assertIsNone(self._resolve(RuntimeError("herdr is gone"))[0])


class LivenessPidIsNotAKillTargetTests(unittest.TestCase):
    """The separation §8.3 and §16.3 items 17 and 30 require, asserted.

    A future change that "tidies" the two fields into one would pass every
    test above. This is the one that fails.
    """

    def test_the_handle_keeps_the_two_pids_in_separate_fields(self):
        handle = launcher.LaunchHandle(
            "token", "w1:p2", "agent-1", Path("/tmp"), liveness_pid=90123)
        self.assertEqual(handle.liveness_pid, 90123)
        self.assertIsNone(
            handle.process_group,
            "liveness_pid must never populate process_group: that field is "
            "§8.3's kill target and writing it is conditional on a §9.8 "
            "receipt proving the group excludes the pane shell and every "
            "sibling attempt (§16.3 items 17 and 30)")

    def test_cancel_aims_at_process_group_and_ignores_liveness_pid(self):
        """`HerdrLauncher.cancel` must not read the liveness field.

        Its quiesce path is the one that sends signals. If it ever resolves
        `liveness_pid`, an unreceipted group becomes a kill target.
        """
        source = Path(launcher.__file__).read_text(encoding="utf-8")
        start = source.index("    def cancel(self, handle")
        end = source.index("    def reclaim(self", start)
        self.assertNotIn("liveness_pid", source[start:end])


class _AgentForegroundHerdr(FakeHerdr):
    """A pane whose foreground is the agent rather than the pane's shell.

    `FakeHerdr` answers `pane process-info` with a lone `zsh` and no group,
    which is the pre-launch state — and is exactly why the resolver passing in
    isolation proves nothing about the launch path. This subclass is the only
    thing in the suite that reports what a *running* agent's pane looks like.
    """

    pgid = 90123
    shell_pid = 88001

    def __call__(self, *args, env=None, timeout=30.0):
        if tuple(args[:2]) == ("pane", "process-info"):
            self.calls.append(list(args))
            return {"result": {"process_info": {
                "pane_id": self.split_pane_id,
                "shell_pid": self.shell_pid,
                "foreground_process_group_id": self.pgid,
                "foreground_processes": [
                    {"name": "node", "argv0": "node",
                     "argv": ["node", "cli.js"], "pid": self.pgid}]}}}
        return super().__call__(*args, env=env, timeout=timeout)


class LaunchRecordsTheLivenessPidTests(unittest.TestCase):
    """The wiring, driven through `HerdrLauncher.launch` itself.

    `pane_liveness_pid` swallows a failed call by design, so a launcher that
    never called it would look identical to one that called it and was
    declined. Only driving the real launch path tells those apart.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        self.transcript = self.root / "session.jsonl"
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))

    def _spec(self) -> launcher.LaunchSpec:
        return launcher.LaunchSpec(
            correlation_token="run1-node_a-1", worktree=self.worktree,
            prompt_path=self.prompt, envelope_path=self.root / "envelope.json",
            route="omp", model="openai-codex/gpt-5.6-sol", effort="high",
            profile="openai-performance", session_dir=self.root / "session",
            context_window_tokens=400_000,
            environment=worktree_module.launch_env(self.scratch))

    def _launch(self, fake_class):
        harness = launcher.HerdrLauncher(
            herdr_path=self.root / "herdr", omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"), admitted_routes=self.admitted)
        harness.agent_start_busy_window_s = 0.0
        fake = fake_class(worktree=self.worktree, transcript=self.transcript)
        harness._herdr = fake
        return harness.launch(self._spec()), fake

    def test_a_launched_agent_carries_its_pane_group(self):
        handle, fake = self._launch(_AgentForegroundHerdr)
        self.assertEqual(handle.liveness_pid, _AgentForegroundHerdr.pgid)
        self.assertTrue(
            fake.argv_for(("pane", "process-info")),
            "launch must actually ask herdr; a resolver nothing calls is the "
            "same unreachable branch under a new name")

    def test_the_kill_target_stays_empty(self):
        """§8.3's field is untouched by a launch under the recorded surface."""
        handle, _ = self._launch(_AgentForegroundHerdr)
        self.assertIsNone(handle.process_group)

    def test_a_pane_still_at_its_shell_records_nothing(self):
        """`FakeHerdr`'s bare zsh: declined, and the launch succeeds anyway."""
        handle, _ = self._launch(FakeHerdr)
        self.assertIsNone(handle.liveness_pid)
        self.assertEqual(handle.pane_id, "w0:p2")


class ProcessDeadIsReachableForAnAgentAttemptTests(unittest.TestCase):
    """End of the chain: a recorded pid makes the watchdog able to convict.

    Without it this branch never runs for an agent node, which is the whole of
    #20. Asserting the branch rather than the plumbing is what stops a later
    change from recording the pid somewhere the watchdog does not read.
    """

    def _watchdog(self, pid, alive, declared_result=False):
        """A watchdog with only the clocks disarmed, so PROCESS_DEAD is the
        only signal that can convict — otherwise a passing test would not say
        which of the three fired."""
        fail = Recorder()
        clock = FakeClock()
        watchdog = wd.Watchdog(
            config=make_config(node_timeout_s=1000.0, turn_timeout_s=1000.0),
            attempts_provider=lambda: [
                make_attempt(started_at=0.0, launched_at=0.0, pid=pid)],
            write_heartbeat=Recorder(),
            kill=Recorder(),
            fail_attempt=fail,
            process_alive=lambda _pid: alive,
            declared_result_observed=lambda _attempt: declared_result,
            time_source=clock,
        )
        clock.set(0.001)  # barely any wall clock elapsed
        return watchdog, fail

    def test_a_dead_group_convicts_where_an_absent_pid_cannot(self):
        watchdog, fail = self._watchdog(pid=90123, alive=False)
        watchdog.check_once()
        self.assertEqual([args[2] for args, _ in fail.calls],
                         [wd.StallReason.PROCESS_DEAD.value])

    def test_a_live_group_is_not_convicted(self):
        watchdog, fail = self._watchdog(pid=90123, alive=True)
        watchdog.check_once()
        self.assertEqual(fail.calls, [])

    def test_an_accepted_result_spares_an_attempt_whose_process_is_gone(self):
        """§9.7: an artifact a worker wrote outranks a status about the worker.

        Until this signal became reachable the case could not arise — a code
        node is spared by `exit_status_observed` and an agent node had no pid.
        The measured cost of getting it wrong is on record for the code path:
        a command that exited between two polls was convicted, retried twice
        into the same race and blocked ENVIRONMENTAL_BUDGET_EXHAUSTED, for a
        node that had already succeeded.
        """
        watchdog, fail = self._watchdog(pid=90123, alive=False,
                                        declared_result=True)
        watchdog.check_once()
        self.assertEqual(fail.calls, [])

    def test_an_absent_pid_is_the_state_this_replaces(self):
        """The control. With no pid the branch cannot fire at all, which is
        what every agent attempt looked like before #20."""
        watchdog, fail = self._watchdog(pid=None, alive=False)
        watchdog.check_once()
        self.assertEqual(fail.calls, [])


if __name__ == "__main__":
    unittest.main()
