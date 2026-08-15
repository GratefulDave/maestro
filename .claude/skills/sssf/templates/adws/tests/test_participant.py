"""Participant subprocess protocol contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules.launcher import quiesce_process_group
from adw_modules.participant import (
    PARTICIPANT_RESULT_SCHEMA,
    ParticipantCancelled,
    ParticipantContext,
    ParticipantExecutionError,
    ParticipantProtocolError,
    SubprocessParticipantRunner,
    load_participant_result,
)


PARTICIPANT = r'''
import json
import os
import subprocess
import sys
import time

result_path = os.environ["MAESTRO_PARTICIPANT_RESULT_PATH"]
mode = sys.argv[1]
if mode == "sleep":
    time.sleep(float(sys.argv[2]))
    mode = "accepted"
if mode != "absent":
    if mode in ("malformed", "malformed_descendant"):
        raw = b"not json"
    elif mode == "array":
        raw = b"[]"
    else:
        outcome = "accepted" if mode in (
            "accepted", "nonzero", "nonzero_descendant", "descendant") else mode
        result = {
            "schema": "maestro-participant-result.v1",
            "child_run_id": os.environ["MAESTRO_CHILD_RUN_ID"],
            "outcome": outcome,
            "accepted_sha": "a" * 40 if outcome == "accepted" else None,
            "reason": "child completed",
        }
        if mode == "missing_sha":
            result["accepted_sha"] = None
        elif mode == "bad_sha":
            result["accepted_sha"] = "not-a-git-object"
        elif mode == "blocked_with_sha":
            result["outcome"] = "blocked"
            result["accepted_sha"] = "a" * 40
        elif mode == "extra":
            result["unexpected"] = True
        elif mode == "wrong_schema":
            result["schema"] = "wrong"
        elif mode == "wrong_child":
            result["child_run_id"] = "another-child"
        elif mode == "wrong_type":
            result["reason"] = 1
        elif mode == "missing_key":
            del result["reason"]
        raw = json.dumps(result, sort_keys=True).encode("utf-8")
    temporary = result_path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, result_path)
if mode in ("descendant", "malformed_descendant", "nonzero_descendant"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(sys.argv[2], "w") as handle:
        handle.write(str(child.pid))
if mode in ("nonzero", "nonzero_descendant"):
    sys.exit(9)
'''


class ParticipantRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        # The participant always operates in a real repository, rather than a
        # directory that only happens to resemble a worktree.
        subprocess.run(["git", "init", "--quiet"], cwd=str(self.repository), check=True)
        subprocess.run(
            ["git", "config", "user.email", "participant@example.invalid"],
            cwd=str(self.repository), check=True)
        subprocess.run(
            ["git", "config", "user.name", "Participant Test"],
            cwd=str(self.repository), check=True)
        (self.repository / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README"], cwd=str(self.repository), check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=str(self.repository), check=True)
        self.script = self.root / "participant.py"
        self.script.write_text(PARTICIPANT, encoding="utf-8")
        self.result_path = self.root / "state" / "participant-result.json"
        self.runner = SubprocessParticipantRunner(diagnostic_tail_lines=3)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self, mode: str, *extra: str, repository_id: str = "repository") -> ParticipantContext:
        return ParticipantContext(
            workspace_run_id="workspace-run",
            repository_id=repository_id,
            child_run_id="child-" + repository_id,
            plan_path=self.repository / "plan.json",
            plan_digest="b" * 64,
            candidate_branch="maestro/workspace-run/" + repository_id,
            candidate_worktree=self.repository,
            participant_result_path=self.result_path.with_name(repository_id + ".json"),
            run_argv=(sys.executable, str(self.script), mode) + tuple(extra),
        )

    def test_binds_declared_environment_and_candidate_worktree(self) -> None:
        probe = self.root / "probe.py"
        probe.write_text(
            "import json, os, pathlib\n"
            "path = os.environ['MAESTRO_PARTICIPANT_RESULT_PATH']\n"
            "bindings = {name: os.environ[name] for name in (\n"
            "    'MAESTRO_WORKSPACE_RUN_ID', 'MAESTRO_REPOSITORY_ID',\n"
            "    'MAESTRO_CHILD_RUN_ID', 'MAESTRO_PLAN_PATH',\n"
            "    'MAESTRO_PLAN_DIGEST', 'MAESTRO_CANDIDATE_BRANCH',\n"
            "    'MAESTRO_CANDIDATE_WORKTREE', 'MAESTRO_PARTICIPANT_RESULT_PATH')}\n"
            "value = {'schema': 'maestro-participant-result.v1', "
            "'child_run_id': os.environ['MAESTRO_CHILD_RUN_ID'], "
            "'outcome': 'accepted', 'accepted_sha': 'a' * 40, "
            "'reason': json.dumps({'cwd': str(pathlib.Path.cwd()), 'bindings': bindings}, sort_keys=True)}\n"
            "temporary = path + '.tmp'\n"
            "open(temporary, 'w').write(json.dumps(value))\n"
            "os.replace(temporary, path)\n",
            encoding="utf-8",
        )
        context = self.context("accepted")
        context = ParticipantContext(
            workspace_run_id=context.workspace_run_id,
            repository_id=context.repository_id,
            child_run_id=context.child_run_id,
            plan_path=context.plan_path,
            plan_digest=context.plan_digest,
            candidate_branch=context.candidate_branch,
            candidate_worktree=context.candidate_worktree,
            participant_result_path=context.participant_result_path,
            run_argv=(sys.executable, str(probe)),
        )

        result = self.runner.run(context, timeout=2.0)

        payload = json.loads(
            context.participant_result_path.read_text(encoding="utf-8"))
        self.assertEqual(result.outcome, "accepted")
        observed = json.loads(payload["reason"])
        self.assertEqual(observed["cwd"], str(context.candidate_worktree))
        self.assertEqual(observed["bindings"], {
            "MAESTRO_WORKSPACE_RUN_ID": context.workspace_run_id,
            "MAESTRO_REPOSITORY_ID": context.repository_id,
            "MAESTRO_CHILD_RUN_ID": context.child_run_id,
            "MAESTRO_PLAN_PATH": str(context.plan_path),
            "MAESTRO_PLAN_DIGEST": context.plan_digest,
            "MAESTRO_CANDIDATE_BRANCH": context.candidate_branch,
            "MAESTRO_CANDIDATE_WORKTREE": str(context.candidate_worktree),
            "MAESTRO_PARTICIPANT_RESULT_PATH": str(context.participant_result_path),
        })

    def test_parses_exact_accepted_result(self) -> None:
        result = self.runner.run(self.context("accepted"), timeout=2.0)

        self.assertEqual(result.schema, PARTICIPANT_RESULT_SCHEMA)
        self.assertEqual(result.child_run_id, "child-repository")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.accepted_sha, "a" * 40)
        self.assertEqual(result.reason, "child completed")

    def test_success_quiesces_a_surviving_owned_descendant_before_acceptance(self) -> None:
        """A valid receipt is not authority while its writer group remains alive."""
        child_pid = self.root / "descendant.pid"

        result = self.runner.run(
            self.context("descendant", str(child_pid), repository_id="descendant"),
            timeout=2.0)

        self.assertEqual(result.outcome, "accepted")
        pid = int(child_pid.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("participant-owned descendant remained alive after acceptance")
        self.assertEqual(self.runner.active_participant_ids, ())

    def test_rehydrates_a_valid_persisted_result(self) -> None:
        context = self.context("accepted", repository_id="recovered")
        context.participant_result_path.parent.mkdir(parents=True, exist_ok=True)
        context.participant_result_path.write_text(json.dumps({
            "schema": PARTICIPANT_RESULT_SCHEMA,
            "child_run_id": context.child_run_id,
            "outcome": "accepted",
            "accepted_sha": "c" * 40,
            "reason": "persisted child result",
        }), encoding="utf-8")

        result = load_participant_result(
            context.participant_result_path, context.child_run_id)

        self.assertEqual(result.accepted_sha, "c" * 40)

    def test_recovery_parser_rejects_malformed_and_wrong_child_results(self) -> None:
        context = self.context("accepted", repository_id="recovery-refusal")
        context.participant_result_path.parent.mkdir(parents=True, exist_ok=True)
        for raw in (
                b"not json",
                json.dumps({
                    "schema": PARTICIPANT_RESULT_SCHEMA,
                    "child_run_id": "wrong-child",
                    "outcome": "accepted",
                    "accepted_sha": "c" * 40,
                    "reason": "wrong child",
                }).encode("utf-8")):
            with self.subTest(raw=raw):
                context.participant_result_path.write_bytes(raw)
                with self.assertRaises(ParticipantProtocolError):
                    load_participant_result(
                        context.participant_result_path, context.child_run_id)

    def test_returns_valid_blocked_and_cancelled_child_results(self) -> None:
        for outcome in ("blocked", "cancelled"):
            with self.subTest(outcome=outcome):
                result = self.runner.run(
                    self.context(outcome, repository_id="result-" + outcome),
                    timeout=2.0)
                self.assertEqual(result.outcome, outcome)
                self.assertIsNone(result.accepted_sha)

    def test_normalizes_paths_and_refuses_unsafe_path_boundaries(self) -> None:
        context = self.context("accepted")
        self.assertEqual(context.candidate_worktree, self.repository.resolve())
        self.assertEqual(context.plan_path, (self.repository / "plan.json").resolve())
        self.assertEqual(
            context.participant_result_path,
            self.result_path.with_name("repository.json").resolve())

        with self.assertRaises(ValueError):
            replace(context, plan_path=self.root / "plan.json")
        with self.assertRaises(ValueError):
            replace(context, participant_result_path=self.repository / "result.json")

    def test_rejects_every_refusal_and_malformed_result(self) -> None:
        cases = (
            "absent", "malformed", "array", "missing_key", "extra",
            "wrong_schema", "wrong_child", "wrong_type", "invalid_outcome",
            "missing_sha", "bad_sha", "blocked_with_sha",
        )
        for mode in cases:
            with self.subTest(mode=mode):
                with self.assertRaises(ParticipantProtocolError):
                    self.runner.run(
                        self.context(mode, repository_id="invalid-" + mode),
                        timeout=2.0)

    def test_rejects_a_result_file_that_predates_launch(self) -> None:
        context = self.context("accepted")
        context.participant_result_path.parent.mkdir(parents=True, exist_ok=True)
        context.participant_result_path.write_text("{}", encoding="utf-8")

        with self.assertRaises(ParticipantProtocolError):
            self.runner.run(context, timeout=2.0)

    def test_nonzero_exit_refuses_even_valid_result(self) -> None:
        with self.assertRaises(ParticipantExecutionError) as caught:
            self.runner.run(self.context("nonzero"), timeout=2.0)

        self.assertIn("exit_code=9", str(caught.exception))

    def test_malformed_or_nonzero_child_quiesces_owned_descendant_before_failure(self) -> None:
        for mode, error_type in (
                ("malformed_descendant", ParticipantProtocolError),
                ("nonzero_descendant", ParticipantExecutionError)):
            with self.subTest(mode=mode):
                child_pid = self.root / (mode + ".pid")
                with self.assertRaises(error_type):
                    self.runner.run(
                        self.context(mode, str(child_pid), repository_id=mode),
                        timeout=2.0)
                pid = int(child_pid.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("failed participant-owned descendant remained alive")
                self.assertNotIn(
                    ("workspace-run", mode), self.runner.active_participant_ids)

    def test_timeout_quiesces_owned_process_group(self) -> None:
        context = self.context("sleep", "30")
        with mock.patch(
                "adw_modules.participant.quiesce_process_group",
                wraps=quiesce_process_group) as quiesce:
            with self.assertRaises(ParticipantExecutionError) as caught:
                self.runner.run(context, timeout=0.01)

        self.assertIn("timeout", str(caught.exception).lower())
        self.assertEqual(quiesce.call_count, 1)
        self.assertIsInstance(quiesce.call_args[0][0], int)

    def test_cancel_before_run_latches_and_prevents_launch(self) -> None:
        context = self.context("accepted", repository_id="pre-cancelled")

        self.assertTrue(self.runner.cancel(
            context.workspace_run_id, context.repository_id,
            time.monotonic() + 1.0))
        with mock.patch("adw_modules.participant.subprocess.Popen") as popen:
            with self.assertRaises(ParticipantCancelled):
                self.runner.run(context, timeout=2.0)

        popen.assert_not_called()

    def test_later_cancel_recovers_an_identity_retained_after_failed_cleanup(self) -> None:
        context = self.context("accepted", repository_id="retained")

        class FailedParticipant:
            pid = 4243
            returncode = 1

            def communicate(self, timeout=None):
                return "", "failed"

        with mock.patch(
                "adw_modules.participant.subprocess.Popen",
                return_value=FailedParticipant()), mock.patch(
                    "adw_modules.participant._quiesce_owned_group",
                    return_value=False):
            with self.assertRaises(ParticipantExecutionError):
                self.runner.run(context, timeout=2.0)

        self.assertEqual(
            self.runner.active_participant_ids,
            (("workspace-run", "retained"),))
        with mock.patch(
                "adw_modules.participant._quiesce_owned_group",
                return_value=True):
            self.assertTrue(self.runner.cancel(
                context.workspace_run_id, context.repository_id,
                time.monotonic() + 1.0))
        self.assertEqual(self.runner.active_participant_ids, ())

    def test_cancellation_cannot_miss_a_concurrent_launch(self) -> None:
        launch_entered = threading.Event()
        release_launch = threading.Event()
        communicate_entered = threading.Event()
        release_communicate = threading.Event()
        cancel_completed = threading.Event()
        outcome = {}

        class BlockingPopen:
            pid = 4242
            returncode = 0

            def __init__(self, *args, **kwargs) -> None:
                launch_entered.set()
                release_launch.wait(timeout=1.0)

            def poll(self):
                return None

            def communicate(self, timeout=None):
                communicate_entered.set()
                release_communicate.wait(timeout=1.0)
                return "", ""

        def run_participant() -> None:
            try:
                outcome["value"] = self.runner.run(
                    self.context("absent", repository_id="launch-race"),
                    timeout=2.0)
            except BaseException as exc:
                outcome["value"] = exc

        def cancel_participant() -> None:
            outcome["cancelled"] = self.runner.cancel(
                "workspace-run", "launch-race", time.monotonic() + 1.0)
            cancel_completed.set()

        with mock.patch(
                "adw_modules.participant.subprocess.Popen",
                BlockingPopen), mock.patch(
                    "adw_modules.participant._quiesce_owned_group",
                    return_value=True) as quiesce:
            runner_thread = threading.Thread(target=run_participant)
            cancel_thread = threading.Thread(target=cancel_participant)
            runner_thread.start()
            self.assertTrue(launch_entered.wait(timeout=1.0))
            cancel_thread.start()
            self.assertFalse(cancel_completed.wait(timeout=0.05))
            release_launch.set()
            self.assertTrue(communicate_entered.wait(timeout=1.0))
            self.assertTrue(cancel_completed.wait(timeout=1.0))
            release_communicate.set()
            runner_thread.join(timeout=1.0)
            cancel_thread.join(timeout=1.0)

        self.assertTrue(outcome["cancelled"])
        self.assertIsInstance(outcome["value"], ParticipantCancelled)
        quiesce.assert_called_once_with(4242, mock.ANY)

    def test_simultaneous_repositories_and_targeted_cancellation(self) -> None:
        first = self.context("sleep", "1.0", repository_id="first")
        second = self.context("sleep", "0.8", repository_id="second")
        outcomes = {}

        def invoke(context: ParticipantContext) -> None:
            try:
                outcomes[context.repository_id] = self.runner.run(context, timeout=2.0)
            except BaseException as exc:
                outcomes[context.repository_id] = exc

        first_thread = threading.Thread(target=invoke, args=(first,))
        second_thread = threading.Thread(target=invoke, args=(second,))
        first_thread.start()
        second_thread.start()
        deadline = time.monotonic() + 1.0
        while self.runner.active_participant_ids != (
                ("workspace-run", "first"), ("workspace-run", "second")):
            if time.monotonic() >= deadline:
                self.fail("participants did not start concurrently")
            time.sleep(0.01)

        with mock.patch(
                "adw_modules.participant.quiesce_process_group",
                wraps=quiesce_process_group) as quiesce:
            self.assertTrue(
                self.runner.cancel("workspace-run", "first", time.monotonic() + 1.0))

        first_thread.join(timeout=3.0)
        second_thread.join(timeout=3.0)
        self.assertIsInstance(outcomes["first"], ParticipantCancelled)
        self.assertEqual(outcomes["second"].outcome, "accepted")
        self.assertEqual(quiesce.call_count, 1)
        self.assertEqual(quiesce.call_args[0][0], mock.ANY)


if __name__ == "__main__":
    unittest.main()
