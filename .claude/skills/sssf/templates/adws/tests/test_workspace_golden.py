"""End-to-end golden contract for the ADWS workspace coordinator."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Dict, Tuple

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import coordinator as coordinator  # noqa: E402
from adw_modules import coordinator_store as coordinator_store  # noqa: E402
from adw_modules import participant  # noqa: E402
from adw_modules import plan_canonical  # noqa: E402
from adw_modules import plan_digest  # noqa: E402
from adw_modules import plan_model  # noqa: E402
from adw_modules import publication  # noqa: E402
from adw_modules import workspace_canonical  # noqa: E402
from adw_modules import workspace_digest  # noqa: E402
from adw_modules import workspace_model  # noqa: E402
from adw_modules import workspace_receipt  # noqa: E402
from adw_modules.plan_model import Gate  # noqa: E402


RUN_ID = "golden-three-repository-run"


def _git(cwd: Path, *args: str) -> str:
    """Run Git against a test-owned real repository and return stdout."""
    result = subprocess.run(
        ("git",) + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(
            "git {0} failed in {1}: {2}".format(
                " ".join(args), cwd, result.stderr.strip()))
    return result.stdout.strip()


def _git_succeeds(cwd: Path, *args: str) -> bool:
    return subprocess.run(
        ("git",) + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    ).returncode == 0


def _make_repository(root: Path, repository_id: str) -> Tuple[Path, str]:
    repository = root / repository_id
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "golden@example.invalid")
    _git(repository, "config", "user.name", "Golden Scenario")
    _git(repository, "config", "core.hooksPath", str(root / "no-hooks"))
    (repository / "README").write_text(repository_id + " base\n", encoding="utf-8")
    _git(repository, "add", "README")
    _git(repository, "commit", "-qm", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _child_plan_bytes(repository_path: str, base_commit: str) -> bytes:
    """Return canonical stored bytes for the child plan bound to one source."""
    return plan_canonical.canonicalize(plan_model.parse_mapping({
        "schema_version": "maestro-plan.v1",
        "plan_id": "golden-" + repository_path,
        "repo": repository_path,
        "base_commit": base_commit,
        "intent": "produce the golden repository output",
        "evidence": [{
            "kind": "observed",
            "evidence_id": "source-readme",
            "path": "README",
            "sha256": "a" * 64,
        }],
        "nodes": [{
            "kind": "code",
            "node_id": "write-output",
            "needs": [],
            "reads": ["source-readme"],
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


def _write_participant(path: Path) -> None:
    """Generate the sole child executable used by all real subprocess runs."""
    script = textwrap.dedent('''\
        #!__PYTHON__
        import hashlib
        import json
        import os
        from pathlib import Path
        import subprocess
        import time

        BARRIER_PARTIES = frozenset(("api", "worker"))
        BARRIER_TIMEOUT_S = 2.0


        def read_state(path):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}


        def update_state(path, update):
            lock = path.with_name(path.name + ".lock")
            deadline = time.monotonic() + BARRIER_TIMEOUT_S
            while True:
                try:
                    lock.mkdir()
                    break
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise SystemExit("shared-state lock timed out")
                    time.sleep(0.005)
            try:
                state = read_state(path)
                update(state)
                temporary = path.with_name(
                    path.name + ".{0}.tmp".format(os.getpid()))
                temporary.write_text(
                    json.dumps(state, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8")
                os.replace(temporary, path)
            finally:
                lock.rmdir()


        def record_start(path, repository_id):
            def update(state):
                state["event_sequence"] = state.get("event_sequence", 0) + 1
                state.setdefault("start_order", {})[repository_id] = state["event_sequence"]
                state.setdefault("launch_order", []).append(repository_id)
            update_state(path, update)


        def record_end(path, repository_id):
            def update(state):
                state["event_sequence"] = state.get("event_sequence", 0) + 1
                state.setdefault("end_order", {})[repository_id] = state["event_sequence"]
            update_state(path, update)


        def wait_for(path, key, repository_ids, description):
            deadline = time.monotonic() + BARRIER_TIMEOUT_S
            while True:
                values = read_state(path).get(key, {})
                if set(values).issuperset(repository_ids):
                    return
                if time.monotonic() >= deadline:
                    raise SystemExit(description + " timed out")
                time.sleep(0.01)


        def git(*args):
            result = subprocess.run(
                ("git",) + args, cwd=str(Path.cwd()), capture_output=True, text=True)
            if result.returncode:
                raise SystemExit("git {0}: {1}".format(
                    " ".join(args), result.stderr.strip()))
            return result.stdout.strip()


        def write_acceptance_test(path):
            path.write_text("""from pathlib import Path
        import json
        import stat


        def test_cross_repository_outputs_and_manifest():
            root = Path(__file__).resolve().parents[2]
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["schema_version"] == "maestro-acceptance.v1"
            repositories = manifest["repositories"]
            assert set(repositories) == {"api", "worker", "web"}
            for repository_id in ("api", "worker", "web"):
                entry = repositories[repository_id]
                assert entry["path"] == "repositories/" + repository_id
                assert len(entry["sha"]) in (40, 64)
                assert (root / entry["path"]).stat().st_mode & stat.S_IWUSR == 0
                output = root / entry["path"] / (repository_id + ".output")
                assert output.read_text(encoding="utf-8").startswith(
                    "repository=" + repository_id + "\\\\n")
            with (root.parents[1] / "golden-gate-runs.log").open(
                    "a", encoding="utf-8") as marker:
                marker.write("gate-ran\\\\n")
        """, encoding="utf-8")


        def main():
            environment = os.environ
            repository_id = environment["MAESTRO_REPOSITORY_ID"]
            child_run_id = environment["MAESTRO_CHILD_RUN_ID"]
            candidate_branch = environment["MAESTRO_CANDIDATE_BRANCH"]
            candidate_worktree = Path(environment["MAESTRO_CANDIDATE_WORKTREE"]).resolve()
            plan_path = Path(environment["MAESTRO_PLAN_PATH"]).resolve()
            result_path = Path(environment["MAESTRO_PARTICIPANT_RESULT_PATH"])
            state_path = result_path.parents[3] / "golden-participant-state.json"

            if Path.cwd().resolve() != candidate_worktree:
                raise SystemExit("participant cwd is not the declared candidate worktree")
            try:
                plan_path.relative_to(candidate_worktree)
            except ValueError:
                raise SystemExit("participant plan is outside the candidate worktree")
            if hashlib.sha256(plan_path.read_bytes()).hexdigest() != environment["MAESTRO_PLAN_DIGEST"]:
                raise SystemExit("participant plan digest is not the declared digest")
            if git("symbolic-ref", "--quiet", "--short", "HEAD") != candidate_branch:
                raise SystemExit("candidate branch is not the declared branch")

            record_start(state_path, repository_id)
            if repository_id in BARRIER_PARTIES:
                wait_for(state_path, "start_order", BARRIER_PARTIES, "two-party barrier")
                # Leave a material overlap window after both parties have joined.
                time.sleep(0.10)
            elif repository_id == "web":
                wait_for(state_path, "end_order", BARRIER_PARTIES,
                         "web dependency barrier")
            else:
                raise SystemExit("unexpected repository " + repository_id)

            output = Path(repository_id + ".output")
            output.write_text(
                "repository={0}\\nchild_run_id={1}\\nplan_digest={2}\\n".format(
                    repository_id, child_run_id, environment["MAESTRO_PLAN_DIGEST"]),
                encoding="utf-8")
            paths = [output.name]
            if repository_id == "web":
                acceptance_test = Path("test_cross_repository_acceptance.py")
                write_acceptance_test(acceptance_test)
                paths.append(acceptance_test.name)
            git("add", "--", *paths)
            git("commit", "-qm", "golden " + repository_id)
            accepted_sha = git("rev-parse", "HEAD")

            payload = {
                "schema": "maestro-participant-result.v1",
                "child_run_id": child_run_id,
                "outcome": "accepted",
                "accepted_sha": accepted_sha,
                "reason": "golden participant accepted",
            }
            result_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = result_path.with_name(
                result_path.name + ".{0}.tmp".format(os.getpid()))
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8")
            os.replace(temporary, result_path)
            record_end(state_path, repository_id)


        if __name__ == "__main__":
            main()
        ''').replace("__PYTHON__", sys.executable)
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _receipt(plan: workspace_model.WorkspacePlan,
             digest: str) -> workspace_receipt.WorkspaceReceipt:
    return workspace_receipt.WorkspaceReceipt(
        workspace_digest=digest,
        participants=tuple(workspace_receipt.ParticipantAuthorization(
            repository_id=spec.repository_id,
            mode=spec.mode,
            base_commit=spec.base_commit,
            plan_digest=spec.plan_digest,
            target_branch=spec.target_branch,
        ) for spec in plan.repositories),
        created_at_epoch=time.time(),
    )


class WorkspaceGoldenScenario(unittest.TestCase):
    """The one all-real composition scenario for workspace execution and publish."""

    def setUp(self) -> None:
        self._before_environ = dict(os.environ)
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest_dir = self.root / "manifest"
        self.manifest_dir.mkdir()
        self.state_root = self.root / "state"
        self.store_path = self.root / "coordinator.sqlite3"
        self.store = coordinator_store.CoordinatorStore(self.store_path)

    def tearDown(self) -> None:
        self.store.close()
        os.environ.clear()
        os.environ.update(self._before_environ)
        self.temporary.cleanup()

    def _coordinator(self, plan: workspace_model.WorkspacePlan, digest: str,
                     receipt: workspace_receipt.WorkspaceReceipt,
                     runner: participant.SubprocessParticipantRunner
                     ) -> coordinator.WorkspaceCoordinator:
        return coordinator.WorkspaceCoordinator(
            run_id=RUN_ID,
            plan=plan,
            workspace_digest=digest,
            receipt=receipt,
            store=self.store,
            manifest_dir=self.manifest_dir,
            state_root=self.state_root,
            participant_runner=runner,
            config=coordinator.CoordinatorConfig(
                max_workers=2,
                lease_owner="golden-workspace-test",
                lease_stale_after_s=10.0,
                participant_timeout_s=5.0,
                cancellation_timeout_s=1.0,
                poll_interval_s=0.01,
            ),
        )

    def test_three_repository_execution_restart_gate_and_local_publish(self) -> None:
        """Exercise real Git, subprocesses, gates, restart replay, and local CAS."""
        repositories: Dict[str, Path] = {}
        bases: Dict[str, str] = {}
        for repository_id in ("api", "worker", "web"):
            repositories[repository_id], bases[repository_id] = _make_repository(
                self.manifest_dir, repository_id)

        executable = self.root / "golden-participant.py"
        _write_participant(executable)
        specs = []
        for repository_id in ("api", "worker", "web"):
            stored = _child_plan_bytes(repository_id, bases[repository_id])
            plan_path = "plans/{0}.json".format(repository_id)
            source_path = repositories[repository_id] / plan_path
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(stored)
            needs: Tuple[str, ...] = ("api", "worker") if repository_id == "web" else ()
            specs.append(workspace_model.RepositorySpec(
                repository_id=repository_id,
                mode=workspace_model.RepositoryMode.WRITE,
                path=repository_id,
                base_commit=bases[repository_id],
                needs=needs,
                plan_path=plan_path,
                plan_digest=plan_digest.digest_of(stored),
                target_branch="main",
                run_argv=(str(executable),),
            ))

        gate = Gate(
            runner="pytest",
            argv=("repositories/web/test_cross_repository_acceptance.py",),
            cwd=".",
            min_cases=1,
        )
        plan = workspace_model.WorkspacePlan(
            schema_version="maestro-workspace.v1",
            workspace_id="golden-three-repository-workspace",
            repositories=tuple(specs),
            publication_mode=workspace_model.PublicationMode.LOCAL_REFS,
            integration_gates=(gate,),
        )
        workspace_bytes = workspace_canonical.canonicalize_workspace(plan)
        digest = workspace_digest.digest_of(workspace_bytes)
        receipt = _receipt(plan, digest)
        self.assertTrue(receipt.authorizes(digest, tuple(
            workspace_receipt.ParticipantAuthorization(
                repository_id=spec.repository_id,
                mode=spec.mode,
                base_commit=spec.base_commit,
                plan_digest=spec.plan_digest,
                target_branch=spec.target_branch,
            ) for spec in specs)))
        for spec in specs:
            stored = (repositories[spec.repository_id] / spec.plan_path).read_bytes()
            self.assertTrue(plan_canonical.is_canonical(stored))
            self.assertEqual(plan_digest.digest_of(stored), spec.plan_digest)
            child = plan_model.parse_bytes(stored)
            self.assertEqual(child.repo, spec.path)
            self.assertEqual(child.base_commit, spec.base_commit)

        first_runner = participant.SubprocessParticipantRunner(diagnostic_tail_lines=10)
        started = time.monotonic()
        outcome = self._coordinator(plan, digest, receipt, first_runner).run()
        elapsed = time.monotonic() - started

        self.assertEqual(outcome, workspace_model.WorkspaceOutcome.ACCEPTED)
        self.assertLess(elapsed, 30.0)
        self.assertEqual(first_runner.active_participant_ids, ())
        state_path = self.state_root / "golden-participant-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["launch_order"]), 3)
        self.assertEqual(set(state["launch_order"]), {"api", "worker", "web"})
        self.assertEqual(state["launch_order"].count("api"), 1)
        self.assertEqual(state["launch_order"].count("worker"), 1)
        self.assertEqual(state["launch_order"].count("web"), 1)
        self.assertEqual(set(state["launch_order"][:2]), {"api", "worker"})
        self.assertEqual(state["launch_order"][2:], ["web"])
        starts, ends = state["start_order"], state["end_order"]
        self.assertEqual(set(starts), {"api", "worker", "web"})
        self.assertEqual(set(ends), {"api", "worker", "web"})
        self.assertLess(
            max(starts["api"], starts["worker"]),
            min(ends["api"], ends["worker"]),
        )
        self.assertGreater(starts["web"], max(ends["api"], ends["worker"]))

        records = {record.repository_id: record
                   for record in self.store.list_repositories(RUN_ID)}
        self.assertEqual(set(records), {"api", "worker", "web"})
        for repository_id in ("api", "worker", "web"):
            record = records[repository_id]
            self.assertEqual(record.state, workspace_model.RepositoryState.ACCEPTED)
            self.assertEqual(record.child_run_id, RUN_ID + ":" + repository_id)
            self.assertEqual(
                record.candidate_branch,
                "maestro/workspace/{0}/{1}/candidate".format(RUN_ID, repository_id),
            )
            self.assertEqual(
                record.accepted_sha,
                _git(repositories[repository_id], "rev-parse", record.candidate_branch),
            )
            self.assertTrue(_git_succeeds(
                repositories[repository_id], "merge-base", "--is-ancestor",
                bases[repository_id], record.accepted_sha))
            self.assertEqual(
                _git(repositories[repository_id], "rev-parse", "main"),
                bases[repository_id],
            )
            result_path = (self.state_root / "participant-results" / RUN_ID /
                           repository_id / "result.json")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(set(result), {
                "schema", "child_run_id", "outcome", "accepted_sha", "reason"})
            self.assertEqual(result["schema"], "maestro-participant-result.v1")
            self.assertEqual(result["child_run_id"], record.child_run_id)
            self.assertEqual(result["accepted_sha"], record.accepted_sha)

        gates_before_reopen = self.store.list_gates(RUN_ID)
        self.assertEqual(len(gates_before_reopen), 1)
        self.assertTrue(gates_before_reopen[0].passed)
        self.assertEqual(gates_before_reopen[0].detail["command"], [
            "pytest", "repositories/web/test_cross_repository_acceptance.py"])
        gate_owner = "golden-gate-check"
        self.assertTrue(self.store.acquire_lease(
            RUN_ID, gate_owner, 0.0, 60.0))
        try:
            with self.assertRaises(coordinator_store.GateAlreadyRecorded):
                self.store.record_gate(
                    RUN_ID, 0, passed=True, detail={"must": "not replace"},
                    lease_owner=gate_owner)
        finally:
            self.assertTrue(self.store.release_lease(RUN_ID, gate_owner))
        self.assertEqual(self.store.list_gates(RUN_ID), gates_before_reopen)
        self.assertEqual(
            (self.state_root / "golden-gate-runs.log").read_text(
                encoding="utf-8").splitlines(),
            ["gate-ran"],
        )
        self.assertEqual(
            self.store.get_run(RUN_ID).outcome,
            workspace_model.WorkspaceOutcome.ACCEPTED,
        )
        audit = self.store.audit_transitions(RUN_ID)
        self.assertIn(("gate", "gate-recorded"),
                      {(entry.kind, entry.reason) for entry in audit})
        self.assertIn(("workspace", "outcome-declared"),
                      {(entry.kind, entry.reason) for entry in audit})

        state_before_reopen = state_path.read_bytes()
        gate_marker_before_reopen = (self.state_root / "golden-gate-runs.log").read_bytes()
        self.store.close()
        self.store = coordinator_store.CoordinatorStore(self.store_path)
        reopened_runner = participant.SubprocessParticipantRunner(diagnostic_tail_lines=10)
        self.assertEqual(
            self._coordinator(plan, digest, receipt, reopened_runner).run(),
            workspace_model.WorkspaceOutcome.ACCEPTED,
        )
        self.assertEqual(reopened_runner.active_participant_ids, ())
        self.assertEqual(state_path.read_bytes(), state_before_reopen)
        self.assertEqual(
            (self.state_root / "golden-gate-runs.log").read_bytes(),
            gate_marker_before_reopen,
        )
        self.assertEqual(self.store.list_gates(RUN_ID), gates_before_reopen)

        publisher = publication.WorkspacePublisher(
            store=self.store,
            repository_paths=repositories,
            actor="golden-workspace-test",
        )
        intent = publisher.prepare(RUN_ID)
        self.assertEqual(intent.state, workspace_model.PublicationState.PREPARED)
        self.assertEqual([target.repository_id for target in intent.targets],
                         ["api", "worker", "web"])
        for target in intent.targets:
            self.assertEqual(target.expected_base_sha, bases[target.repository_id])
            self.assertEqual(target.accepted_sha,
                             records[target.repository_id].accepted_sha)
            self.assertEqual(target.state, workspace_model.PublicationState.PENDING)

        published = publisher.publish(RUN_ID)
        self.assertEqual(published.outcome, workspace_model.WorkspaceOutcome.PUBLISHED)
        self.assertEqual(published.intent.state, workspace_model.PublicationState.PUBLISHED)
        for target in published.intent.targets:
            self.assertEqual(
                _git(repositories[target.repository_id], "rev-parse", target.target_branch),
                target.accepted_sha,
            )
        steps_before_replay = self.store.list_publication_steps(RUN_ID)
        self.assertEqual(len(steps_before_replay), 3)
        self.assertEqual([step.repository_id for step in steps_before_replay],
                         ["api", "worker", "web"])
        self.assertTrue(all(
            step.from_state is workspace_model.PublicationState.PENDING
            and step.to_state is workspace_model.PublicationState.PUBLISHED
            for step in steps_before_replay))

        replayed = publisher.publish(RUN_ID)
        self.assertEqual(replayed.outcome, workspace_model.WorkspaceOutcome.PUBLISHED)
        self.assertEqual(replayed.intent, published.intent)
        self.assertEqual(replayed.steps, steps_before_replay)
        self.assertEqual(self.store.list_publication_steps(RUN_ID), steps_before_replay)


if __name__ == "__main__":
    unittest.main()
