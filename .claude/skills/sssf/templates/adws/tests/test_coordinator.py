"""Executable contract for the ADWS multi-repository coordinator."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import coordinator as co  # noqa: E402
from adw_modules import coordinator_store as cs  # noqa: E402
from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_digest as pd  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import workspace_canonical as wc  # noqa: E402
from adw_modules import workspace_digest as wd  # noqa: E402
from adw_modules import workspace_model as wm  # noqa: E402
from adw_modules import workspace_receipt as wr  # noqa: E402
from adw_modules import workspace_runtime as workspace_runtime  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from adw_modules.plan_model import Gate  # noqa: E402


# How long to wait for the coordinator to *reach* a state, as opposed to how
# long it is allowed to take once there. Every wait in this module expressed in
# terms of this constant is a precondition: overrunning it means "the
# coordinator has not got here yet", never "the coordinator is wrong". Reaching
# a participant dispatch or a global gate costs real `git` subprocess work --
# repository binding, branch creation, candidate worktree creation -- and this
# suite's default is `-n auto` (see pytest.ini), so on an 18-core machine
# eighteen workers fork `git` against one disk while the operator's other work
# runs alongside. The pre-dispatch phase measures 0.735s to 1.154s on an idle
# machine, and the 3.0s bounds these replaced sat close enough to that
# distribution that `-n auto` failed one to six of these cases per run under
# load while `-n 0` passed every time (issue #50). A bound placed at roughly
# the duration it measures is a coin toss, so this one is a hang detector
# instead: generous enough that only a genuine deadlock reaches it, bounded so
# a deadlock still reports rather than hanging the suite.
#
# The one wall-clock bound this module genuinely asserts -- the 0.5s
# cancellation bound in
# test_stuck_participant_cancellation_returns_with_blocked_cleanup_evidence --
# is a property under test and is deliberately not expressed in terms of this
# constant. Do not fold it in.
ARRIVAL_TIMEOUT_S = float(os.environ.get("MAESTRO_TEST_ARRIVAL_TIMEOUT_S", "60.0"))


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode:
        raise AssertionError("git {0}: {1}".format(" ".join(args), result.stderr))
    return result.stdout.strip()


def _repo(root: Path, name: str) -> Tuple[Path, str]:
    repository = root / name
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "harness@example.invalid")
    _git(repository, "config", "user.name", "Harness")
    _git(repository, "config", "core.hooksPath", str(root / "no-hooks"))
    (repository / "README").write_text(name + "\n", encoding="utf-8")
    _git(repository, "add", "README")
    _git(repository, "commit", "-qm", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _writer(repository_id: str, base: str, *, needs: Sequence[str] = ()
            ) -> wm.RepositorySpec:
    stored = _writer_plan_bytes(repository_id, base)
    return wm.RepositorySpec(
        repository_id=repository_id,
        mode=wm.RepositoryMode.WRITE,
        path=repository_id,
        base_commit=base,
        needs=tuple(needs),
        plan_path="plans/{0}.json".format(repository_id),
        plan_digest=pd.digest_of(stored),
        target_branch="main",
        run_argv=("fixture-child",),
    )


def _reader(repository_id: str, base: str) -> wm.RepositorySpec:
    return wm.RepositorySpec(
        repository_id=repository_id,
        mode=wm.RepositoryMode.READ_ONLY,
        path=repository_id,
        base_commit=base,
    )


def _plan(repositories: Iterable[wm.RepositorySpec], *, gates: Sequence[Gate] = ()
          ) -> wm.WorkspacePlan:
    return wm.WorkspacePlan(
        schema_version="maestro-workspace.v1",
        workspace_id="release",
        repositories=tuple(repositories),
        integration_gates=tuple(gates),
    )


def _workspace_digest(plan: wm.WorkspacePlan) -> str:
    return wd.digest_of(wc.canonicalize_workspace(plan))


def _receipt(plan: wm.WorkspacePlan, digest: Optional[str] = None
             ) -> wr.WorkspaceReceipt:
    digest = _workspace_digest(plan) if digest is None else digest
    return wr.WorkspaceReceipt(
        workspace_digest=digest,
        participants=tuple(wr.ParticipantAuthorization(
            repository_id=spec.repository_id,
            mode=spec.mode,
            base_commit=spec.base_commit,
            plan_digest=spec.plan_digest,
            target_branch=spec.target_branch,
        ) for spec in plan.repositories),
    )


def _writer_plan_bytes(repository: str, base_commit: str) -> bytes:
    """Create the minimal canonical child plan bound to one declared repository."""
    return pc.canonicalize(pm.parse_mapping({
        "schema_version": "maestro-plan.v1",
        "plan_id": "child-plan",
        "repo": repository,
        "base_commit": base_commit,
        "intent": "change the child repository",
        "evidence": [{
            "kind": "observed",
            "evidence_id": "readme",
            "path": "README",
            "sha256": "a" * 64,
        }],
        "nodes": [{
            "kind": "code",
            "node_id": "work",
            "needs": [],
            "reads": ["readme"],
            "outputs": [],
            "command": ["true"],
            "cwd": ".",
            "expects_changes": False,
        }],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest",
                "argv": ["true"],
                "cwd": ".",
                "min_cases": 1,
            },
        },
        "supersedes": None,
    }))


def _write_manifest(manifest: Path, plan: wm.WorkspacePlan) -> Path:
    for spec in plan.repositories:
        if spec.mode is wm.RepositoryMode.WRITE:
            path = manifest / spec.path / spec.plan_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_writer_plan_bytes(spec.path, spec.base_commit))
    return manifest


@dataclass
class _ChildResult:
    outcome: str
    accepted_sha: Optional[str]
    reason: str


class FakeParticipantRunner:
    """A controllable participant that always uses the real candidate Git repo."""

    def __init__(self, *, outcomes: Optional[Mapping[str, str]] = None,
                 hold: Iterable[str] = ()) -> None:
        self.outcomes = dict(outcomes or {})
        self._hold = set(hold)
        self._release = {repository_id: threading.Event()
                         for repository_id in self._hold}
        self.started = {}
        self._started_condition = threading.Condition()
        self.contexts = []
        self.cancelled = []
        self._cancelled = set()
        self._active = set()
        self._lock = threading.Lock()

    @property
    def active_repository_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active))

    def wait_started(self, repository_id: str,
                     timeout: float = ARRIVAL_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout
        with self._started_condition:
            while repository_id not in self.started:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._started_condition.wait(remaining)
            return self.started[repository_id].is_set()

    def release(self, repository_id: str) -> None:
        self._release[repository_id].set()

    def run(self, context, *, timeout):
        repository_id = context.repository_id
        with self._started_condition:
            started = self.started.setdefault(repository_id, threading.Event())
            started.set()
            self._started_condition.notify_all()
        with self._lock:
            self.contexts.append(context)
            self._active.add(repository_id)
        try:
            if repository_id in self._hold:
                if not self._release[repository_id].wait(timeout):
                    raise AssertionError("test runner was not released")
            if repository_id in self._cancelled:
                return _ChildResult("cancelled", None, "cancelled by test")
            outcome = self.outcomes.get(repository_id, "accepted")
            if outcome == "accepted":
                path = context.candidate_worktree / (repository_id + ".txt")
                path.write_text(repository_id + "\n", encoding="utf-8")
                _git(context.candidate_worktree, "add", path.name)
                _git(context.candidate_worktree, "commit", "-qm", "accepted")
                return _ChildResult(
                    "accepted", _git(context.candidate_worktree, "rev-parse", "HEAD"),
                    "accepted by fixture")
            return _ChildResult(outcome, None, "fixture " + outcome)
        finally:
            with self._lock:
                self._active.discard(repository_id)

    def cancel(self, workspace_run_id: str, repository_id: str,
               deadline: float) -> bool:
        self.cancelled.append((repository_id, deadline))
        self._cancelled.add(repository_id)
        if repository_id in self._release:
            self._release[repository_id].set()
        return True


class WorkspaceCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        workspace_root = Path(self.temporary.name)
        self.root = workspace_root / "manifest"
        self.root.mkdir()
        self.store = cs.CoordinatorStore(workspace_root / "coordinator.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def coordinator(self, plan: wm.WorkspacePlan, runner: FakeParticipantRunner,
                    *, run_id: str = "workspace-run", config=None,
                    receipt: Optional[wr.WorkspaceReceipt] = None):
        return co.WorkspaceCoordinator(
            run_id=run_id,
            plan=plan,
            workspace_digest=_workspace_digest(plan),
            receipt=receipt or _receipt(plan),
            store=self.store,
            manifest_dir=_write_manifest(self.root, plan),
            state_root=self.root / "state",
            participant_runner=runner,
            config=config or co.CoordinatorConfig(max_workers=2, lease_owner="test"),
        )

    def test_runs_declared_child_in_isolated_candidate_and_records_acceptance(self):
        """A writer advances only its deterministic candidate ref, never main."""
        repository, base = _repo(self.root, "api")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner()

        outcome = self.coordinator(plan, runner).run()

        self.assertEqual(outcome, wm.WorkspaceOutcome.ACCEPTED)
        record = self.store.get_repository("workspace-run", "api")
        self.assertEqual(record.state, wm.RepositoryState.ACCEPTED)
        self.assertEqual(record.child_run_id, "workspace-run:api")
        self.assertEqual(record.candidate_branch,
                         "maestro/workspace/workspace-run/api/candidate")
        self.assertEqual(_git(repository, "rev-parse", "main"), base)
        self.assertEqual(
            _git(repository, "rev-parse", record.candidate_branch),
            record.accepted_sha,
        )
        self.assertEqual(runner.contexts[0].child_run_id, record.child_run_id)

    def test_independent_writers_overlap_but_a_dependent_writer_waits(self):
        """Claims are bounded-concurrent and persisted needs impose execution order."""
        _repo(self.root, "api")
        _, web_base = _repo(self.root, "web")
        _repo(self.root, "worker")
        api_base = _git(self.root / "api", "rev-parse", "HEAD")
        worker_base = _git(self.root / "worker", "rev-parse", "HEAD")
        plan = _plan((
            _writer("api", api_base),
            _writer("web", web_base, needs=("api",)),
            _writer("worker", worker_base),
        ))
        runner = FakeParticipantRunner(hold=("api", "worker"))
        coordinator = self.coordinator(plan, runner)
        result = []
        thread = threading.Thread(target=lambda: result.append(coordinator.run()))
        thread.start()

        self.assertTrue(runner.wait_started("api"))
        self.assertTrue(runner.wait_started("worker"))
        self.assertEqual(runner.active_repository_ids, ("api", "worker"))
        self.assertNotIn("web", runner.started)
        runner.release("api")
        self.assertTrue(runner.wait_started("web"))
        runner.release("worker")
        thread.join(timeout=ARRIVAL_TIMEOUT_S)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [wm.WorkspaceOutcome.ACCEPTED])
        launched = [context.repository_id for context in runner.contexts]
        self.assertLess(launched.index("api"), launched.index("web"))
        self.assertEqual(set(launched), {"api", "worker", "web"})

    def test_read_only_repository_is_checked_at_its_declared_sha_without_launch(self):
        """Read-only members never receive a candidate branch or participant command."""
        _, base = _repo(self.root, "audit")
        plan = _plan((_reader("audit", base),))
        runner = FakeParticipantRunner()

        self.assertEqual(self.coordinator(plan, runner).run(), wm.WorkspaceOutcome.ACCEPTED)

        record = self.store.get_repository("workspace-run", "audit")
        self.assertEqual(record.state, wm.RepositoryState.ACCEPTED)
        self.assertEqual(record.accepted_sha, base)
        self.assertIsNone(record.candidate_branch)
        self.assertEqual(runner.contexts, [])

    def test_reopen_re_adjudicates_running_read_only_claim_without_launch(self):
        _repo(self.root, "audit")
        base = _git(self.root / "audit", "rev-parse", "HEAD")
        plan = _plan((_reader("audit", base),))
        self.store.create_run("workspace-run", _workspace_digest(plan), plan)
        self.assertTrue(self.store.acquire_lease(
            "workspace-run", "fixture-owner", 0.0, 60.0))
        self.store.claim_repository(
            "workspace-run", "audit", "workspace-run:audit", None,
            lease_owner="fixture-owner")
        self.assertTrue(self.store.release_lease(
            "workspace-run", "fixture-owner"))
        runner = FakeParticipantRunner()

        self.assertEqual(self.coordinator(plan, runner).run(), wm.WorkspaceOutcome.ACCEPTED)
        record = self.store.get_repository("workspace-run", "audit")
        self.assertEqual(record.state, wm.RepositoryState.ACCEPTED)
        self.assertEqual(record.accepted_sha, base)
        self.assertEqual(runner.contexts, [])

    def test_blocked_writer_blocks_every_pending_descendant(self):
        """A refused prerequisite must prevent all downstream launches."""
        _, api_base = _repo(self.root, "api")
        _, web_base = _repo(self.root, "web")
        _, docs_base = _repo(self.root, "docs")
        plan = _plan((
            _writer("api", api_base),
            _writer("web", web_base, needs=("api",)),
            _writer("docs", docs_base, needs=("web",)),
        ))
        runner = FakeParticipantRunner(outcomes={"api": "blocked"})

        self.assertEqual(self.coordinator(plan, runner).run(), wm.WorkspaceOutcome.BLOCKED)

        records = {record.repository_id: record
                   for record in self.store.list_repositories("workspace-run")}
        self.assertEqual(records["api"].state, wm.RepositoryState.BLOCKED)
        self.assertEqual(records["web"].block_reason, "upstream-blocked:api")
        self.assertEqual(records["docs"].block_reason, "upstream-blocked:web")
        self.assertEqual([context.repository_id for context in runner.contexts], ["api"])

    def test_accepted_sha_is_bound_to_the_candidate_ref(self):
        """A child cannot claim acceptance for a SHA other than its candidate head."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner()
        original_run = runner.run

        def dishonest_run(context, *, timeout):
            accepted = original_run(context, timeout=timeout)
            return _ChildResult(accepted.outcome, base, accepted.reason)

        runner.run = dishonest_run
        self.assertEqual(self.coordinator(plan, runner).run(), wm.WorkspaceOutcome.BLOCKED)
        record = self.store.get_repository("workspace-run", "api")
        self.assertEqual(record.state, wm.RepositoryState.BLOCKED)
        self.assertIn("accepted", record.block_reason)

    def test_cancellation_polls_store_and_targets_only_running_participants(self):
        """A cancellation request cancels active children and never launches successors."""
        _, api_base = _repo(self.root, "api")
        _, web_base = _repo(self.root, "web")
        plan = _plan((
            _writer("api", api_base),
            _writer("web", web_base, needs=("api",)),
        ))
        runner = FakeParticipantRunner(hold=("api",))
        coordinator = self.coordinator(plan, runner)
        result = []
        thread = threading.Thread(target=lambda: result.append(coordinator.run()))
        thread.start()

        self.assertTrue(runner.wait_started("api"))
        self.store.request_cancellation("workspace-run")
        thread.join(timeout=ARRIVAL_TIMEOUT_S)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [wm.WorkspaceOutcome.CANCELLED])
        self.assertEqual([repository_id for repository_id, _ in runner.cancelled], ["api"])
        records = {record.repository_id: record
                   for record in self.store.list_repositories("workspace-run")}
        self.assertEqual(records["api"].state, wm.RepositoryState.CANCELLED)
        self.assertEqual(records["web"].state, wm.RepositoryState.CANCELLED)
        self.assertNotIn("web", runner.started)

    def test_lease_exclusion_then_expiry_takeover(self):
        """Another owner cannot enter a live run but may take an expired lease."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        self.store.create_run("workspace-run", _workspace_digest(plan), plan)
        self.assertTrue(self.store.acquire_lease("workspace-run", "other", 100.0, 10.0))
        blocked = self.coordinator(
            plan, FakeParticipantRunner(),
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="test", lease_stale_after_s=10.0,
                cancellation_timeout_s=1.0, clock=lambda: 105.0),
        )

        with self.assertRaises(co.LeaseUnavailable):
            blocked.run()
        self.assertEqual(self.store.get_repository("workspace-run", "api").state,
                         wm.RepositoryState.PENDING)

        recovered = self.coordinator(
            plan, FakeParticipantRunner(),
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="test", lease_stale_after_s=10.0,
                cancellation_timeout_s=1.0, clock=lambda: 110.0),
        )
        self.assertEqual(recovered.run(), wm.WorkspaceOutcome.ACCEPTED)
        self.assertIsNone(self.store.get_run("workspace-run").lease_owner)

    def test_reopen_resumes_exact_running_candidate_without_a_durable_result(self):
        """A crash may resume only the exact durable candidate identity."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        first = FakeParticipantRunner()

        def crash(context, *, timeout):
            first.contexts.append(context)
            raise KeyboardInterrupt("simulated coordinator crash")

        first.run = crash
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator(plan, first).run()
        running = self.store.get_repository("workspace-run", "api")
        self.assertEqual(running.state, wm.RepositoryState.RUNNING)

        resumed = FakeParticipantRunner()
        self.assertEqual(
            self.coordinator(plan, resumed).run(), wm.WorkspaceOutcome.ACCEPTED)
        accepted = self.store.get_repository("workspace-run", "api")
        self.assertEqual(len(resumed.contexts), 1)
        self.assertEqual(accepted.state, wm.RepositoryState.ACCEPTED)

    def test_reopen_cancellation_cannot_claim_an_unowned_child_is_quiescent(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        first = FakeParticipantRunner()

        def crash(context, *, timeout):
            raise KeyboardInterrupt("simulated crash")

        first.run = crash
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator(plan, first).run()
        self.store.request_cancellation("workspace-run")

        resumed = FakeParticipantRunner()
        self.assertEqual(
            self.coordinator(plan, resumed).run(), wm.WorkspaceOutcome.BLOCKED)
        blocked = self.store.get_repository("workspace-run", "api")
        self.assertEqual(resumed.cancelled, [])
        self.assertEqual(
            blocked.block_reason, "running-participant-liveness-unproven")

    def test_reopen_adjudicates_existing_running_receipt_without_relaunch(self):
        """A durable child receipt wins recovery; the runner is not invoked again."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        first = FakeParticipantRunner()

        def crash_after_receipt(context, *, timeout):
            path = context.candidate_worktree / "accepted.txt"
            path.write_text("accepted\n", encoding="utf-8")
            _git(context.candidate_worktree, "add", path.name)
            _git(context.candidate_worktree, "commit", "-qm", "accepted")
            payload = {
                "schema": "maestro-participant-result.v1",
                "child_run_id": context.child_run_id,
                "outcome": "accepted",
                "accepted_sha": _git(context.candidate_worktree, "rev-parse", "HEAD"),
                "reason": "durable before crash",
            }
            temporary = context.participant_result_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, context.participant_result_path)
            raise KeyboardInterrupt("simulated coordinator crash")

        first.run = crash_after_receipt
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator(plan, first).run()

        resumed = FakeParticipantRunner()
        self.assertEqual(self.coordinator(plan, resumed).run(), wm.WorkspaceOutcome.ACCEPTED)
        self.assertEqual(resumed.contexts, [])

    def test_accepted_member_is_not_relaunched_on_reopen(self):
        """An accepted repository remains authoritative across coordinator restarts."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                         wm.WorkspaceOutcome.ACCEPTED)
        second = FakeParticipantRunner()

        self.assertEqual(self.coordinator(plan, second).run(), wm.WorkspaceOutcome.ACCEPTED)

        self.assertEqual(second.contexts, [])

    def test_global_gates_are_assembled_recorded_once_and_replayed(self):
        """Gate results become durable authority and are not re-executed on reopen."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        gate = Gate(runner="pytest", argv=("tests",), cwd=".", min_cases=1)
        plan = _plan((_writer("api", base),), gates=(gate,))
        assembled = object()
        with mock.patch.object(co, "assemble_acceptance", return_value=assembled) as assemble, \
                mock.patch.object(
                    co, "run_global_gates",
                    return_value=(wt.GateResult(
                        label="global-gate-0", scope="integration", selector=None,
                        command=("pytest",), exit_code=0, green=True,
                        counts={"passed": 1}),)) as gates, \
                mock.patch.object(co, "cleanup_acceptance") as cleanup:
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.ACCEPTED)
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.ACCEPTED)

        self.assertEqual(assemble.call_count, 1)
        self.assertEqual(gates.call_count, 1)
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual([(entry.gate_index, entry.passed)
                          for entry in self.store.list_gates("workspace-run")], [(0, True)])

    def test_cleanup_failure_leaves_gate_authority_replayable_without_outcome(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        gate = Gate(runner="pytest", argv=("tests",), cwd=".", min_cases=1)
        plan = _plan((_writer("api", base),), gates=(gate,))
        green = wt.GateResult(
            label="global-gate-0", scope="integration", selector=None,
            command=("pytest",), exit_code=0, green=True, counts={"passed": 1})

        with mock.patch.object(co, "assemble_acceptance", return_value=object()), \
                mock.patch.object(co, "run_global_gates", return_value=(green,)) as gates, \
                mock.patch.object(co, "cleanup_acceptance",
                                  side_effect=OSError("cleanup failed")):
            with self.assertRaises(OSError):
                self.coordinator(plan, FakeParticipantRunner()).run()
            self.assertIsNone(self.store.get_run("workspace-run").outcome)
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.ACCEPTED)

        self.assertEqual(gates.call_count, 1)

    def test_crash_between_global_gates_replays_only_unrecorded_gate(self):
        """Each green gate is durable before a later gate may crash the process."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        first_gate = Gate(runner="pytest", argv=("first",), cwd=".", min_cases=1)
        second_gate = Gate(runner="pytest", argv=("second",), cwd=".", min_cases=1)
        plan = _plan((_writer("api", base),), gates=(first_gate, second_gate))
        calls = []
        green = wt.GateResult(
            label="global-gate", scope="integration", selector=None,
            command=("pytest",), exit_code=0, green=True, counts={"passed": 1})

        def run_gate(acceptance, gates, **kwargs):
            calls.append(gates[0].argv)
            if len(calls) == 2:
                raise KeyboardInterrupt("crash after gate zero")
            return (green,)

        with mock.patch.object(co, "assemble_acceptance", return_value=object()), \
                mock.patch.object(co, "run_global_gates", side_effect=run_gate), \
                mock.patch.object(co, "cleanup_acceptance"):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator(plan, FakeParticipantRunner()).run()
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.ACCEPTED)

        self.assertEqual(calls, [("first",), ("second",), ("second",)])
        self.assertEqual([(entry.gate_index, entry.passed)
                          for entry in self.store.list_gates("workspace-run")],
                         [(0, True), (1, True)])

    def test_global_gate_heartbeats_lease_until_completion(self):
        """The main coordinator keeps a blocking gate's lease alive."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),), gates=(
            Gate(runner="pytest", argv=("gate",), cwd=".", min_cases=1),))
        started, release, observed = threading.Event(), threading.Event(), threading.Event()
        watch = [False]
        now = [0.0]
        original = self.store.heartbeat_lease

        def heartbeat(*args, **kwargs):
            result = original(*args, **kwargs)
            if watch[0] and started.is_set():
                observed.set()
            return result

        def blocking_gate(acceptance, gates, **kwargs):
            started.set()
            self.assertTrue(release.wait(ARRIVAL_TIMEOUT_S))
            return (wt.GateResult("gate", "integration", None, ("pytest",), 0,
                                  True, {"passed": 1}),)

        self.store.heartbeat_lease = heartbeat
        coordinator = self.coordinator(
            plan, FakeParticipantRunner(),
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="primary", lease_stale_after_s=1.0,
                cancellation_timeout_s=0.25, poll_interval_s=0.01,
                clock=lambda: now[0]))
        outcome = []
        with mock.patch.object(co, "assemble_acceptance", return_value=object()), \
                mock.patch.object(co, "run_global_gates", side_effect=blocking_gate), \
                mock.patch.object(co, "cleanup_acceptance"):
            thread = threading.Thread(target=lambda: outcome.append(coordinator.run()))
            thread.start()
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            now[0] = 0.5
            watch[0] = True
            self.assertTrue(observed.wait(ARRIVAL_TIMEOUT_S))
            self.assertFalse(self.store.acquire_lease(
                "workspace-run", "other", 1.0, 1.0))
            release.set()
            thread.join(timeout=ARRIVAL_TIMEOUT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [wm.WorkspaceOutcome.ACCEPTED])

    def test_polling_does_not_write_a_lease_heartbeat_each_iteration(self):
        """Polling frequently renews only after the deterministic lease interval."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner(hold=("api",))
        now = [0.0]
        polls, heartbeated = threading.Event(), threading.Event()
        poll_count, heartbeat_calls = [0], []
        original_wait = co.wait
        original_heartbeat = self.store.heartbeat_lease

        def wait_for_poll(*args, **kwargs):
            poll_count[0] += 1
            if poll_count[0] >= 4:
                polls.set()
            return original_wait(*args, **kwargs)

        def heartbeat(*args, **kwargs):
            heartbeat_calls.append((args, kwargs))
            heartbeated.set()
            return original_heartbeat(*args, **kwargs)

        self.store.heartbeat_lease = heartbeat
        coordinator = self.coordinator(
            plan, runner,
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="primary", lease_stale_after_s=1.0,
                cancellation_timeout_s=0.25, poll_interval_s=0.01,
                clock=lambda: now[0]))
        outcome = []
        with mock.patch.object(co, "wait", side_effect=wait_for_poll):
            thread = threading.Thread(target=lambda: outcome.append(coordinator.run()))
            thread.start()
            self.assertTrue(runner.wait_started("api"))
            self.assertTrue(polls.wait(ARRIVAL_TIMEOUT_S))
            self.assertEqual(heartbeat_calls, [])
            now[0] = 1.0 / 3.0
            self.assertTrue(heartbeated.wait(ARRIVAL_TIMEOUT_S))
            self.assertEqual(len(heartbeat_calls), 1)
            self.store.request_cancellation("workspace-run")
            thread.join(timeout=ARRIVAL_TIMEOUT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [wm.WorkspaceOutcome.CANCELLED])

    def test_source_plan_mutation_after_candidate_preflight_blocks_without_launch(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner()
        original_prepare = co.prepare_candidate

        def prepare_then_mutate(*args, **kwargs):
            candidate = original_prepare(*args, **kwargs)
            (self.root / "api" / "plans" / "api.json").write_bytes(b"changed")
            return candidate

        with mock.patch.object(co, "prepare_candidate",
                               side_effect=prepare_then_mutate):
            self.assertEqual(self.coordinator(plan, runner).run(),
                             wm.WorkspaceOutcome.BLOCKED)

        self.assertEqual(runner.contexts, [])
        self.assertEqual(self.store.get_repository(
            "workspace-run", "api").state, wm.RepositoryState.BLOCKED)

    def test_cancellation_during_global_gate_cannot_declare_accepted(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),), gates=(
            Gate(runner="pytest", argv=("gate",), cwd=".", min_cases=1),))
        started = threading.Event()

        def blocking_gate(acceptance, gates, **kwargs):
            started.set()
            cancel_requested = kwargs["cancel_requested"]
            deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
            while not cancel_requested() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(cancel_requested())
            raise wt.GateCancelled("cancelled")

        outcome = []
        with mock.patch.object(co, "assemble_acceptance", return_value=object()), \
                mock.patch.object(co, "run_global_gates", side_effect=blocking_gate), \
                mock.patch.object(co, "cleanup_acceptance"):
            thread = threading.Thread(target=lambda: outcome.append(
                self.coordinator(plan, FakeParticipantRunner()).run()))
            thread.start()
            self.assertTrue(started.wait(ARRIVAL_TIMEOUT_S))
            self.store.request_cancellation("workspace-run")
            thread.join(timeout=ARRIVAL_TIMEOUT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [wm.WorkspaceOutcome.CANCELLED])

    def test_config_rejects_polling_at_or_after_lease_expiry(self):
        with self.assertRaises(ValueError):
            co.CoordinatorConfig(lease_stale_after_s=1.0, poll_interval_s=1.0)

    def test_config_rejects_non_finite_execution_and_lease_intervals(self):
        for name in ("lease_stale_after_s", "participant_timeout_s",
                     "cancellation_timeout_s", "poll_interval_s"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        co.CoordinatorConfig(**{name: value})

    def test_stuck_participant_cancellation_returns_with_blocked_cleanup_evidence(self):
        """A hostile cancel implementation cannot retain the coordinator executor."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))

        class StuckCancellationRunner:
            def __init__(self):
                self.started = threading.Event()
                self.release_worker = threading.Event()
                self.release_cancel = threading.Event()
                self.worker_finished = threading.Event()
                self.cancel_started = threading.Event()

            def run(self, context, *, timeout):
                self.started.set()
                self.release_worker.wait(timeout=ARRIVAL_TIMEOUT_S)
                self.worker_finished.set()
                return _ChildResult("blocked", None, "child did not quiesce")

            def cancel(self, workspace_run_id, repository_id, deadline):
                self.cancel_started.set()
                self.release_cancel.wait(timeout=ARRIVAL_TIMEOUT_S)
                return False

        runner = StuckCancellationRunner()
        coordinator = self.coordinator(
            plan, runner,
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="bounded-cancel",
                lease_stale_after_s=1.0, cancellation_timeout_s=0.05,
                poll_interval_s=0.01))
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(coordinator.run()))
        thread.start()
        # Reaching the first dispatch is a precondition of this test rather
        # than anything it asserts, so it takes the module's shared arrival
        # bound; the measurement behind that bound is recorded beside the
        # constant. This test was the first instance of that shape to be
        # diagnosed, and for a while the only one fixed — the six siblings that
        # kept their 1-in-a-coin-toss 3.0s bounds are what issue #50 reported.
        # What this test is actually for is the cancellation bound below, and
        # that one is deliberately unchanged.
        #
        # Released and joined in the `finally`, whatever happens here. When
        # this assertion fired mid-flight the coordinator thread was left
        # running, `tearDown` closed the store under it, and the failure was
        # reported as `sqlite3.ProgrammingError: Cannot operate on a closed
        # database` — a consequence of the timeout that read like a database
        # defect.
        try:
            self.assertTrue(runner.started.wait(timeout=ARRIVAL_TIMEOUT_S))
            self.store.request_cancellation("workspace-run")
            started = time.monotonic()
            thread.join(timeout=0.5)
            elapsed = time.monotonic() - started
            self.assertFalse(thread.is_alive())
            self.assertLess(elapsed, 0.5)
            self.assertEqual(outcomes, [wm.WorkspaceOutcome.BLOCKED])
            record = self.store.get_repository("workspace-run", "api")
            self.assertEqual(record.state, wm.RepositoryState.BLOCKED)
            self.assertIn("cleanup-unproven", record.block_reason)
            self.assertTrue(runner.cancel_started.is_set())
        finally:
            runner.release_cancel.set()
            runner.release_worker.set()
            # Still asserted — it is the leak check, not cleanup: the worker
            # must actually finish rather than be abandoned. Only the bound is
            # an arrival bound, and the join after it is what keeps `tearDown`
            # from closing the store under a thread that is still running.
            self.assertTrue(runner.worker_finished.wait(timeout=ARRIVAL_TIMEOUT_S))
            thread.join(timeout=ARRIVAL_TIMEOUT_S)


    def test_failed_participant_retries_unproven_cleanup_before_blocking(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))

        class RetainedProcessFailure:
            def __init__(self):
                self.cancel_calls = 0

            def run(self, context, *, timeout):
                return _ChildResult("blocked", None, "malformed child result")

            def cancel(self, workspace_run_id, repository_id, deadline):
                self.cancel_calls += 1
                return False

        runner = RetainedProcessFailure()
        outcome = self.coordinator(
            plan, runner,
            config=co.CoordinatorConfig(
                max_workers=1, lease_owner="cleanup-retry",
                lease_stale_after_s=1.0, cancellation_timeout_s=0.2,
                poll_interval_s=0.01)).run()

        self.assertEqual(outcome, wm.WorkspaceOutcome.BLOCKED)
        self.assertEqual(runner.cancel_calls, 2)
        blocked = self.store.get_repository("workspace-run", "api")
        self.assertEqual(blocked.state, wm.RepositoryState.BLOCKED)
        self.assertIn("cleanup-unproven; cancel-attempts=2", blocked.block_reason)
    def test_symlinked_candidate_plan_path_blocks_before_copy_or_launch(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner()
        escaped_directory = self.root / "escaped"
        original_prepare = co.prepare_candidate

        def prepare_with_plan_escape(*args, **kwargs):
            candidate = original_prepare(*args, **kwargs)
            (candidate.candidate_worktree / "plans").symlink_to(
                escaped_directory, target_is_directory=True)
            return candidate

        with mock.patch.object(co, "prepare_candidate",
                               side_effect=prepare_with_plan_escape):
            outcome = self.coordinator(plan, runner).run()

        self.assertEqual(outcome, wm.WorkspaceOutcome.BLOCKED)
        record = self.store.get_repository("workspace-run", "api")
        self.assertEqual(record.state, wm.RepositoryState.BLOCKED)
        self.assertTrue(record.block_reason.startswith(
            "CoordinatorError: bound plan escapes candidate worktree:"))
        self.assertEqual(runner.contexts, [])
        self.assertFalse(escaped_directory.exists())

    def test_config_rejects_cancellation_timeout_at_or_after_lease_expiry(self):
        for cancellation_timeout_s in (1.0, 2.0):
            with self.subTest(cancellation_timeout_s=cancellation_timeout_s):
                with self.assertRaises(ValueError):
                    co.CoordinatorConfig(
                        lease_stale_after_s=1.0,
                        cancellation_timeout_s=cancellation_timeout_s)

    def test_fresh_claim_refuses_stale_participant_result_without_launch(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        stale = (self.root / "state").resolve() / "participant-results" / "workspace-run" / "api" / "result.json"
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({
            "schema": "maestro-participant-result.v1",
            "child_run_id": "workspace-run:api",
            "outcome": "accepted",
            "accepted_sha": base,
            "reason": "stale",
        }), encoding="utf-8")
        runner = FakeParticipantRunner()

        self.assertEqual(self.coordinator(plan, runner).run(), wm.WorkspaceOutcome.BLOCKED)
        self.assertEqual(runner.contexts, [])
        self.assertIn("fresh participant claim",
                      self.store.get_repository("workspace-run", "api").block_reason)

    def test_same_config_instances_cannot_share_a_lease_owner(self):
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner(hold=("api",))
        config = co.CoordinatorConfig(max_workers=1, lease_owner="shared")
        first = self.coordinator(plan, runner, config=config)
        thread = threading.Thread(target=first.run)
        thread.start()
        self.assertTrue(runner.wait_started("api"))

        with self.assertRaises(co.LeaseUnavailable):
            self.coordinator(plan, FakeParticipantRunner(), config=config).run()

        runner.release("api")
        thread.join(timeout=ARRIVAL_TIMEOUT_S)
        self.assertFalse(thread.is_alive())

    def test_failed_global_gate_blocks_once_and_cleanup_runs(self):
        """A failed global gate makes the workspace blocked without a rerun."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        gate = Gate(runner="pytest", argv=("tests",), cwd=".", min_cases=1)
        plan = _plan((_writer("api", base),), gates=(gate,))
        with mock.patch.object(co, "assemble_acceptance", return_value=object()), \
                mock.patch.object(
                    co, "run_global_gates",
                    side_effect=co.GateFailure(
                        0, wt.GateResult(
                            label="global-gate-0", scope="integration", selector=None,
                            command=("pytest",), exit_code=1, green=False,
                            counts={"passed": 0}))) as gates, \
                mock.patch.object(co, "cleanup_acceptance") as cleanup:
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.BLOCKED)
            self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                             wm.WorkspaceOutcome.BLOCKED)

        self.assertEqual(gates.call_count, 1)
        self.assertEqual(cleanup.call_count, 1)
        self.assertFalse(self.store.list_gates("workspace-run")[0].passed)

    def test_receipt_mismatch_refuses_before_projecting_or_acquiring_lease(self):
        """Unauthorized workspace bytes have no durable coordinator side effects."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))
        runner = FakeParticipantRunner()

        with self.assertRaises(wr.AuthorizationError):
            self.coordinator(plan, runner, receipt=_receipt(plan, "e" * 64)).run()

        self.assertEqual(self.store.list_runs(), ())
        self.assertEqual(runner.contexts, [])

    def test_plan_change_outside_receipt_vector_refuses_before_projection(self):
        """The signed digest binds execution fields omitted from participant rows."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        authorized = _plan((_writer("api", base),))
        changed_spec = wm.RepositorySpec.model_validate({
            **authorized.repositories[0].model_dump(mode="json"),
            "run_argv": ["different-executable"],
        })
        changed = _plan((changed_spec,))
        runner = FakeParticipantRunner()
        coordinator = co.WorkspaceCoordinator(
            run_id="workspace-run", plan=changed,
            workspace_digest=_workspace_digest(authorized),
            receipt=_receipt(authorized), store=self.store,
            manifest_dir=self.root, state_root=self.root / "state",
            participant_runner=runner, config=co.CoordinatorConfig())

        with self.assertRaises(wr.AuthorizationError):
            coordinator.run()

        self.assertEqual(self.store.list_runs(), ())
        self.assertEqual(runner.contexts, [])

    def test_copied_manifest_clone_swap_refuses_before_launch(self):
        repository, base = _repo(self.root, "api")
        plan = _plan((_writer("api", base),))
        _write_manifest(self.root, plan)
        self.store.create_run("workspace-run", _workspace_digest(plan), plan)
        self.assertTrue(self.store.acquire_lease(
            "workspace-run", "binding-fixture", 0.0, 60.0))
        self.store.bind_repository_paths(
            "workspace-run",
            {"api": workspace_runtime.repository_binding(repository)},
            lease_owner="binding-fixture")
        self.assertTrue(self.store.release_lease("workspace-run", "binding-fixture"))

        copied_manifest = self.root.parent / "copied-manifest"
        shutil.copytree(self.root, copied_manifest)
        runner = FakeParticipantRunner()
        copied = co.WorkspaceCoordinator(
            run_id="workspace-run", plan=plan,
            workspace_digest=_workspace_digest(plan),
            receipt=_receipt(plan), store=self.store, manifest_dir=copied_manifest,
            state_root=copied_manifest / "state", participant_runner=runner,
            config=co.CoordinatorConfig(max_workers=1, lease_owner="copy-test"))

        with self.assertRaises(cs.RepositoryPathMismatch):
            copied.run()

        self.assertEqual(runner.contexts, [])
        self.assertEqual(runner.started, {})


    def test_terminal_outcome_and_audit_cover_repository_and_lease_events(self):
        """The declared outcome and append-only audit report the complete execution."""
        _repo(self.root, "api")
        base = _git(self.root / "api", "rev-parse", "HEAD")
        plan = _plan((_writer("api", base),))

        self.assertEqual(self.coordinator(plan, FakeParticipantRunner()).run(),
                         wm.WorkspaceOutcome.ACCEPTED)

        run = self.store.get_run("workspace-run")
        self.assertEqual(run.outcome, wm.WorkspaceOutcome.ACCEPTED)
        audit = self.store.audit_transitions("workspace-run")
        self.assertIn(("repository", "pending", "running"),
                      [(entry.kind, entry.from_state.value, entry.to_state.value)
                       for entry in audit if entry.kind == "repository"])
        self.assertIn(("repository", "running", "accepted"),
                      [(entry.kind, entry.from_state.value, entry.to_state.value)
                       for entry in audit if entry.kind == "repository"])
        self.assertIn("lease-acquired", [entry.reason for entry in audit])
        self.assertIn("lease-released", [entry.reason for entry in audit])
        self.assertIn("outcome-declared", [entry.reason for entry in audit])


if __name__ == "__main__":
    unittest.main()
