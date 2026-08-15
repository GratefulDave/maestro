"""Executable contract for durable multi-repository publication."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
import time
import sys
import tempfile
from typing import (Callable, Dict, Iterable, List, Optional, Sequence, Tuple)
import unittest

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import coordinator_store as cs  # noqa: E402
from adw_modules import publication  # noqa: E402
from adw_modules import workspace_model as wm  # noqa: E402
from adw_modules import workspace_runtime as wr  # noqa: E402


RUN_ID = "workspace-run"
PLAN_DIGEST = "d" * 64
ACCEPTED_BRANCH = "maestro/workspace"


def git(repository: Path, *argv: str) -> str:
    """Run one test-only Git command and return its stripped standard output."""
    result = subprocess.run(
        ("git", *argv), cwd=str(repository), capture_output=True, text=True,
        shell=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} failed: {1}".format(" ".join(argv), result.stderr))
    return result.stdout.strip()


def real_runner(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        tuple(argv), cwd=str(cwd), capture_output=True, text=True, shell=False,
    )


@dataclass(frozen=True)
class RepositoryFixture:
    repository_id: str
    path: Path
    base_sha: str
    accepted_sha: str
    moved_sha: str


def make_repository(root: Path, repository_id: str) -> RepositoryFixture:
    path = root / repository_id
    path.mkdir()
    git(path, "init")
    git(path, "checkout", "-b", "main")
    git(path, "config", "user.name", "Maestro Test")
    git(path, "config", "user.email", "maestro@example.test")

    source = path / "published.txt"
    source.write_text("base\n", encoding="utf-8")
    git(path, "add", "published.txt")
    git(path, "commit", "-m", "base")
    base_sha = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "-b", "accepted")
    source.write_text("accepted\n", encoding="utf-8")
    git(path, "add", "published.txt")
    git(path, "commit", "-m", "accepted")
    accepted_sha = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "main")
    git(path, "checkout", "-b", "moved")
    source.write_text("moved\n", encoding="utf-8")
    git(path, "add", "published.txt")
    git(path, "commit", "-m", "moved")
    moved_sha = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "main")

    return RepositoryFixture(
        repository_id, path.resolve(), base_sha, accepted_sha, moved_sha)



def ensure_git_worktree(path: Path) -> None:
    if path.is_dir():
        return
    path.mkdir(parents=True)
    git(path, "init")
    git(path, "config", "user.name", "Publication Binding")
    git(path, "config", "user.email", "binding@example.invalid")
    (path / "README").write_text("binding\n", encoding="utf-8")
    git(path, "add", "README")
    git(path, "commit", "-m", "binding")


def create_accepted_run(store: cs.CoordinatorStore, *, mode: wm.PublicationMode,
                        repositories: Iterable[RepositoryFixture],
                        remote: str = "origin", bind_paths: bool = True
                        ) -> wm.WorkspacePlan:
    fixtures = tuple(repositories)
    plan = wm.WorkspacePlan(
        schema_version=wm.SCHEMA_V1,
        workspace_id="publication-contract",
        repositories=tuple(
            wm.RepositorySpec(
                repository_id=fixture.repository_id,
                mode=wm.RepositoryMode.WRITE,
                path=fixture.repository_id,
                base_commit=fixture.base_sha,
                remote=remote if mode is wm.PublicationMode.PULL_REQUESTS else None,
                plan_path="plans/{0}.json".format(fixture.repository_id),
                plan_digest=("a" if fixture.repository_id == "api" else "b") * 64,
                target_branch="main",
                run_argv=("maestro", "run"),
            )
            for fixture in fixtures
        ),
        publication_mode=mode,
    )
    store.create_run(RUN_ID, PLAN_DIGEST, plan)
    lease_owner = "publication-fixture"
    if not store.acquire_lease(RUN_ID, lease_owner, 0.0, 60.0):
        raise AssertionError("publication fixture could not acquire coordinator lease")
    if bind_paths:
        for fixture in fixtures:
            ensure_git_worktree(fixture.path)
        store.bind_repository_paths(
            RUN_ID,
            {fixture.repository_id: wr.repository_binding(fixture.path)
             for fixture in fixtures},
            lease_owner=lease_owner,
        )
    for fixture in fixtures:
        store.claim_repository(
            RUN_ID, fixture.repository_id,
            "child-{0}".format(fixture.repository_id),
            "{0}/{1}".format(ACCEPTED_BRANCH, fixture.repository_id),
            lease_owner=lease_owner,
        )
        store.transition_repository(
            RUN_ID, fixture.repository_id, wm.RepositoryState.ACCEPTED,
            accepted_sha=fixture.accepted_sha, lease_owner=lease_owner,
        )
    store.declare_outcome(
        RUN_ID, wm.WorkspaceOutcome.ACCEPTED, lease_owner=lease_owner)
    if not store.release_lease(RUN_ID, lease_owner):
        raise AssertionError("publication fixture could not release coordinator lease")
    return plan


class RecordingRunner:
    def __init__(self, before: Callable[[Tuple[str, ...], Path], None] = None,
                 result_for: Callable[[Tuple[str, ...], Path],
                                      Optional[subprocess.CompletedProcess]] = None):
        self.calls: List[Tuple[Tuple[str, ...], Path]] = []
        self._before = before
        self._result_for = result_for

    def __call__(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
        command = tuple(argv)
        directory = Path(cwd)
        self.calls.append((command, directory))
        if self._before is not None:
            self._before(command, directory)
        if self._result_for is not None:
            replacement = self._result_for(command, directory)
            if replacement is not None:
                return replacement
        return real_runner(command, directory)


class RemoteRunner:
    def __init__(self, *, base_sha: str, accepted_sha: str,
                 pr_result: subprocess.CompletedProcess = None,
                 candidate_sha: Optional[str] = None,
                 auth_result: subprocess.CompletedProcess = None,
                 pr_list_output: Optional[str] = None,
                 pr_list_outputs: Optional[Iterable[str]] = None,
                 remote_url: str = "https://github.com/acme/api.git"):
        self.base_sha = base_sha
        self.accepted_sha = accepted_sha
        self.pr_result = pr_result
        self.candidate_sha = candidate_sha
        self.auth_result = auth_result
        self.pr_list_output = pr_list_output
        self.pr_list_outputs = list(pr_list_outputs or ())
        self.remote_url = remote_url
        self.calls: List[Tuple[Tuple[str, ...], Path]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
        command = tuple(argv)
        self.calls.append((command, Path(cwd)))
        candidate_ref = "refs/heads/{0}/api".format(ACCEPTED_BRANCH)
        if command == ("git", "remote", "get-url", "--", "origin"):
            return subprocess.CompletedProcess(command, 0, self.remote_url + "\n", "")
        if command == (
                "git", "ls-remote", "--", self.remote_url, "refs/heads/main"):
            return subprocess.CompletedProcess(
                command, 0, "{0}\trefs/heads/main\n".format(self.base_sha), "")
        if command == ("git", "ls-remote", "--", self.remote_url, candidate_ref):
            output = ("" if self.candidate_sha is None else
                      "{0}\t{1}\n".format(self.candidate_sha, candidate_ref))
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == ("gh", "auth", "status"):
            if self.auth_result is not None:
                return self.auth_result
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ("git", "push"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == (
                "gh", "pr", "list", "--repo", "acme/api", "--head",
                "{0}/api".format(ACCEPTED_BRANCH), "--base", "main",
                "--state", "open", "--json", "url,headRefOid", "--limit", "2"):
            if self.pr_list_outputs:
                output = self.pr_list_outputs.pop(0)
            else:
                output = "[]\n" if self.pr_list_output is None else self.pr_list_output
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == (
                "gh", "pr", "create", "--repo", "acme/api", "--base", "main",
                "--head", "{0}/api".format(ACCEPTED_BRANCH), "--fill"):
            if self.pr_result is not None:
                return self.pr_result
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/acme/api/pull/42\n", "")
        raise AssertionError("unexpected command: {0!r}".format(command))


class LocalPublicationTests(unittest.TestCase):

    def test_prepare_preflights_every_head_and_a_moved_ref_creates_no_intent_or_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web),
            )
            git(web.path, "update-ref", "refs/heads/main", web.moved_sha, web.base_sha)
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={"api": api.path, "web": web.path},
                command_runner=runner,
            )

            with self.assertRaises(publication.PublicationError):
                publisher.prepare(RUN_ID)

            probes = tuple(
                (command, cwd) for command, cwd in runner.calls
                if command[:3] == ("git", "rev-parse", "--verify"))
            self.assertEqual(
                probes,
                ((("git", "rev-parse", "--verify", "--quiet",
                   "refs/heads/main^{commit}"), api.path),
                 (("git", "rev-parse", "--verify", "--quiet",
                   "refs/heads/main^{commit}"), web.path)),
            )
            self.assertEqual(git(api.path, "rev-parse", "refs/heads/main"), api.base_sha)
            self.assertEqual(git(web.path, "rev-parse", "refs/heads/main"), web.moved_sha)
            self.assertFalse(any(command[1:3] == ("update-ref", "refs/heads/main")
                                 for command, _ in runner.calls))
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)
            store.close()

    def test_publish_updates_targets_in_persisted_order_and_declares_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web),
            )
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={"api": api.path, "web": web.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.PUBLISHED)
            self.assertEqual(result.intent.state, wm.PublicationState.PUBLISHED)
            self.assertEqual(git(api.path, "rev-parse", "refs/heads/main"), api.accepted_sha)
            self.assertEqual(git(web.path, "rev-parse", "refs/heads/main"), web.accepted_sha)
            updates = tuple(command for command, _ in runner.calls
                            if command[:2] == ("git", "update-ref"))
            self.assertEqual(
                updates,
                (("git", "update-ref", "refs/heads/main", api.accepted_sha, api.base_sha),
                 ("git", "update-ref", "refs/heads/main", web.accepted_sha, web.base_sha)),
            )
            self.assertEqual(
                tuple(step.repository_id for step in store.list_publication_steps(RUN_ID)),
                ("api", "web"),
            )
            self.assertEqual(
                tuple(step.repository_id for step in result.steps), ("api", "web"))
            self.assertEqual(store.get_run(RUN_ID).outcome, wm.WorkspaceOutcome.PUBLISHED)
            store.close()

    def test_live_foreign_lease_refuses_publish_without_any_git_side_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS, repositories=(api,),
            )
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=runner, actor="publication-operator",
            )
            self.assertTrue(store.acquire_lease(
                RUN_ID, "active-coordinator", time.time(), 60.0))

            with self.assertRaisesRegex(publication.PublicationError, "live lease"):
                publisher.publish(RUN_ID)

            self.assertEqual(runner.calls, [])
            self.assertEqual(
                git(api.path, "rev-parse", "refs/heads/main"), api.base_sha)
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)

            self.assertTrue(store.release_lease(RUN_ID, "active-coordinator"))
            recovered = publisher.publish(RUN_ID)

            self.assertEqual(recovered.outcome, wm.WorkspaceOutcome.PUBLISHED)
            self.assertEqual(
                git(api.path, "rev-parse", "refs/heads/main"), api.accepted_sha)
            store.close()

    def test_invalid_target_branch_refuses_before_command_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store = cs.CoordinatorStore(root / "coordinator.db")
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=runner,
            )

            with self.assertRaises(publication.PublicationError):
                publisher._local_head(api.path, "release/..")

            self.assertEqual(runner.calls, [])
            store.close()

    def test_concurrent_publishers_share_one_cas_and_replay_durable_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS, repositories=(api,),
            )
            update_entered = threading.Event()
            allow_update = threading.Event()
            first_update = True

            def hold_first_update(command: Tuple[str, ...], cwd: Path) -> None:
                nonlocal first_update
                if (first_update and cwd == api.path and command == (
                        "git", "update-ref", "refs/heads/main", api.accepted_sha,
                        api.base_sha)):
                    first_update = False
                    update_entered.set()
                    self.assertTrue(allow_update.wait(5))

            runner = RecordingRunner(before=hold_first_update)
            first = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=runner,
            )
            second = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=runner,
            )
            second_started = threading.Event()

            def second_publish() -> publication.PublicationResult:
                second_started.set()
                return second.publish(RUN_ID)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_result = pool.submit(first.publish, RUN_ID)
                self.assertTrue(update_entered.wait(5))
                second_result = pool.submit(second_publish)
                self.assertTrue(second_started.wait(5))
                allow_update.set()
                self.assertEqual(
                    first_result.result(timeout=5).outcome,
                    wm.WorkspaceOutcome.PUBLISHED,
                )
                self.assertEqual(
                    second_result.result(timeout=5).outcome,
                    wm.WorkspaceOutcome.PUBLISHED,
                )

            updates = tuple(
                command for command, _ in runner.calls
                if command[:2] == ("git", "update-ref"))
            self.assertEqual(
                updates,
                (("git", "update-ref", "refs/heads/main",
                  api.accepted_sha, api.base_sha),),
            )
            self.assertEqual(store.get_run(RUN_ID).outcome, wm.WorkspaceOutcome.PUBLISHED)
            store.close()

    def test_mid_flight_failure_records_failure_and_cas_rolls_back_prior_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web),
            )
            def refuse_web_update(command: Tuple[str, ...], cwd: Path
                                  ) -> Optional[subprocess.CompletedProcess]:
                if cwd == web.path and command == (
                        "git", "update-ref", "refs/heads/main", web.accepted_sha,
                        web.base_sha):
                    return subprocess.CompletedProcess(
                        command, 1, "", "simulated update refusal")
                return None

            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={"api": api.path, "web": web.path},
                command_runner=RecordingRunner(result_for=refuse_web_update),
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.ACCEPTED)
            self.assertEqual(git(api.path, "rev-parse", "refs/heads/main"), api.base_sha)
            self.assertEqual(git(web.path, "rev-parse", "refs/heads/main"), web.base_sha)
            self.assertEqual(
                tuple((step.repository_id, step.to_state)
                      for step in store.list_publication_steps(RUN_ID)),
                (("api", wm.PublicationState.PUBLISHED),
                 ("web", wm.PublicationState.FAILED),
                 ("web", wm.PublicationState.ROLLED_BACK),
                 ("api", wm.PublicationState.ROLLED_BACK)),
            )
            self.assertEqual(
                store.get_publication_intent(RUN_ID).state,
                wm.PublicationState.ROLLED_BACK,
            )
            self.assertEqual(store.get_run(RUN_ID).outcome, wm.WorkspaceOutcome.ACCEPTED)
            store.close()

    def test_rollback_cas_refusal_requires_manual_recovery_without_overwriting_moved_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web),
            )
            moved_api = False

            def move_before_unsafe_rollback(command: Tuple[str, ...], cwd: Path) -> None:
                nonlocal moved_api
                if (not moved_api and cwd == api.path and command == (
                        "git", "update-ref", "refs/heads/main", api.base_sha,
                        api.accepted_sha)):
                    git(api.path, "update-ref", "refs/heads/main",
                        api.moved_sha, api.accepted_sha)
                    moved_api = True

            def refuse_web_update(command: Tuple[str, ...], cwd: Path
                                  ) -> Optional[subprocess.CompletedProcess]:
                if cwd == web.path and command == (
                        "git", "update-ref", "refs/heads/main", web.accepted_sha,
                        web.base_sha):
                    return subprocess.CompletedProcess(
                        command, 1, "", "simulated update refusal")
                return None

            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={"api": api.path, "web": web.path},
                command_runner=RecordingRunner(
                    move_before_unsafe_rollback, refuse_web_update),
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(git(api.path, "rev-parse", "refs/heads/main"), api.moved_sha)
            self.assertEqual(git(web.path, "rev-parse", "refs/heads/main"), web.base_sha)
            self.assertEqual(
                store.get_run(RUN_ID).outcome,
                wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED,
            )
            self.assertEqual(
                tuple((step.repository_id, step.to_state)
                      for step in store.list_publication_steps(RUN_ID)),
                (("api", wm.PublicationState.PUBLISHED),
                 ("web", wm.PublicationState.FAILED),
                 ("web", wm.PublicationState.ROLLED_BACK)),
            )
            store.close()

    def test_middle_failure_rolls_back_later_pending_target_before_finalizing_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            worker = make_repository(root, "worker")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web, worker),
            )

            def refuse_web_update(command: Tuple[str, ...], cwd: Path
                                  ) -> Optional[subprocess.CompletedProcess]:
                if cwd == web.path and command == (
                        "git", "update-ref", "refs/heads/main", web.accepted_sha,
                        web.base_sha):
                    return subprocess.CompletedProcess(
                        command, 1, "", "simulated update refusal")
                return None

            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={
                    "api": api.path, "web": web.path, "worker": worker.path,
                },
                command_runner=RecordingRunner(result_for=refuse_web_update),
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.ACCEPTED)
            self.assertEqual(git(api.path, "rev-parse", "refs/heads/main"), api.base_sha)
            self.assertEqual(git(web.path, "rev-parse", "refs/heads/main"), web.base_sha)
            self.assertEqual(
                git(worker.path, "rev-parse", "refs/heads/main"), worker.base_sha)
            self.assertEqual(
                tuple((step.repository_id, step.to_state)
                      for step in store.list_publication_steps(RUN_ID)),
                (("api", wm.PublicationState.PUBLISHED),
                 ("web", wm.PublicationState.FAILED),
                 ("worker", wm.PublicationState.ROLLED_BACK),
                 ("web", wm.PublicationState.ROLLED_BACK),
                 ("api", wm.PublicationState.ROLLED_BACK)),
            )
            self.assertEqual(
                store.get_publication_intent(RUN_ID).state,
                wm.PublicationState.ROLLED_BACK,
            )
            store.close()

    def test_moved_later_pending_target_requires_manual_recovery_without_cas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            web = make_repository(root, "web")
            worker = make_repository(root, "worker")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS,
                repositories=(api, web, worker),
            )
            moved_worker = False

            def move_pending_worker(command: Tuple[str, ...], cwd: Path) -> None:
                nonlocal moved_worker
                if (not moved_worker and cwd == web.path and command == (
                        "git", "update-ref", "refs/heads/main", web.accepted_sha,
                        web.base_sha)):
                    git(worker.path, "update-ref", "refs/heads/main",
                        worker.accepted_sha, worker.base_sha)
                    moved_worker = True

            def refuse_web_update(command: Tuple[str, ...], cwd: Path
                                  ) -> Optional[subprocess.CompletedProcess]:
                if cwd == web.path and command == (
                        "git", "update-ref", "refs/heads/main", web.accepted_sha,
                        web.base_sha):
                    return subprocess.CompletedProcess(
                        command, 1, "", "simulated update refusal")
                return None

            runner = RecordingRunner(move_pending_worker, refuse_web_update)
            publisher = publication.WorkspacePublisher(
                store=store,
                repository_paths={
                    "api": api.path, "web": web.path, "worker": worker.path,
                },
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(
                git(worker.path, "rev-parse", "refs/heads/main"), worker.accepted_sha)
            self.assertFalse(any(
                cwd == worker.path and command == (
                    "git", "update-ref", "refs/heads/main",
                    worker.base_sha, worker.accepted_sha)
                for command, cwd in runner.calls))
            self.assertEqual(
                tuple((step.repository_id, step.to_state)
                      for step in store.list_publication_steps(RUN_ID)),
                (("api", wm.PublicationState.PUBLISHED),
                 ("web", wm.PublicationState.FAILED)),
            )
            self.assertEqual(
                store.get_run(RUN_ID).outcome,
                wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED,
            )
            store.close()

    def test_reopened_publisher_returns_existing_durable_intent_without_repreflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store_path = root / "coordinator.db"
            store = cs.CoordinatorStore(store_path)
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS, repositories=(api,),
            )
            first = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=RecordingRunner(),
            )
            prepared = first.prepare(RUN_ID)
            store.close()

            reopened_store = cs.CoordinatorStore(store_path)
            runner = RecordingRunner()
            reopened = publication.WorkspacePublisher(
                store=reopened_store, repository_paths={"api": api.path},
                command_runner=runner,
            )
            recovered = reopened.prepare(RUN_ID)

            self.assertEqual(recovered, prepared)
            self.assertEqual(recovered.targets[0].expected_base_sha, api.base_sha)
            self.assertEqual(runner.calls, [])
            reopened_store.close()


    def test_path_mismatch_refuses_before_any_publication_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS, repositories=(api,))
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": root / "other"},
                command_runner=runner)

            with self.assertRaises(cs.RepositoryPathMismatch):
                publisher.prepare(RUN_ID)

            self.assertEqual(runner.calls, [])
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)
            store.close()

    def test_unbound_legacy_run_refuses_before_any_publication_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = make_repository(root, "api")
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.LOCAL_REFS, repositories=(api,),
                bind_paths=False)
            runner = RecordingRunner()
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": api.path},
                command_runner=runner)

            with self.assertRaises(cs.RepositoryPathMismatch):
                publisher.prepare(RUN_ID)

            self.assertEqual(runner.calls, [])
            store.close()

class PullRequestPublicationTests(unittest.TestCase):

    def test_remote_compare_create_only_push_and_pr_url_are_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            runner = RemoteRunner(base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha)
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            candidate_ref = "refs/heads/{0}/api".format(ACCEPTED_BRANCH)
            self.assertEqual(result.outcome, wm.WorkspaceOutcome.PUBLISHED)
            self.assertEqual(result.intent.state, wm.PublicationState.PUBLISHED)
            self.assertEqual(
                runner.calls,
                [
                    (("git", "remote", "get-url", "--", "origin"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      "refs/heads/main"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      candidate_ref), fixture.path),
                    (("gh", "auth", "status"), fixture.path),
                    (("git", "remote", "get-url", "--", "origin"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      "refs/heads/main"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      candidate_ref), fixture.path),
                    (("git", "push", "--force-with-lease={0}:".format(candidate_ref),
                      runner.remote_url,
                      "{0}:{1}".format(fixture.accepted_sha, candidate_ref)),
                     fixture.path),
                    (("gh", "pr", "list", "--repo", "acme/api", "--head",
                      "{0}/api".format(ACCEPTED_BRANCH), "--base", "main",
                      "--state", "open", "--json", "url,headRefOid",
                      "--limit", "2"), fixture.path),
                    (("gh", "pr", "create", "--repo", "acme/api", "--base", "main",
                      "--head", "{0}/api".format(ACCEPTED_BRANCH), "--fill"),
                     fixture.path),
                ],
            )
            step = store.list_publication_steps(RUN_ID)[0]
            self.assertEqual(step.detail["url"], "https://github.com/acme/api/pull/42")
            store.close()

    def test_remote_retarget_after_prepare_refuses_before_push_or_pull_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha)
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            prepared = publisher.prepare(RUN_ID)
            self.assertEqual(
                prepared.targets[0].remote_url,
                "https://github.com/acme/api.git")
            self.assertEqual(prepared.targets[0].remote_repository, "acme/api")
            runner.remote_url = "https://github.com/other/repository.git"

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome,
                             wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(result.intent.targets[0].state,
                             wm.PublicationState.FAILED)
            self.assertFalse(any(
                command[:2] == ("git", "push") or command[:3] == ("gh", "pr", "create")
                for command, _ in runner.calls))
            store.close()

    def test_prepare_requires_gh_auth_before_creating_durable_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            refused_auth = subprocess.CompletedProcess(
                ("gh", "auth", "status"), 1, "", "not authenticated")
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                auth_result=refused_auth,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            with self.assertRaises(publication.PublicationError):
                publisher.prepare(RUN_ID)

            candidate_ref = "refs/heads/{0}/api".format(ACCEPTED_BRANCH)
            self.assertEqual(
                runner.calls,
                [
                    (("git", "remote", "get-url", "--", "origin"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      "refs/heads/main"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      candidate_ref), fixture.path),
                    (("gh", "auth", "status"), fixture.path),
                ],
            )
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)
            store.close()

    def test_prepare_refuses_a_foreign_candidate_without_intent_or_push(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                candidate_sha=fixture.moved_sha,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            with self.assertRaises(publication.PublicationError):
                publisher.prepare(RUN_ID)

            candidate_ref = "refs/heads/{0}/api".format(ACCEPTED_BRANCH)
            self.assertEqual(
                runner.calls,
                [
                    (("git", "remote", "get-url", "--", "origin"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      "refs/heads/main"), fixture.path),
                    (("git", "ls-remote", "--", runner.remote_url,
                      candidate_ref), fixture.path),
                ],
            )
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)
            store.close()

    def test_resume_recovers_a_unique_matching_open_pull_request_without_recreate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            existing = (
                '[{"url":"https://github.com/acme/api/pull/42",'
                '"headRefOid":"%s"}]\n' % fixture.accepted_sha)
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                candidate_sha=fixture.accepted_sha, pr_list_output=existing,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.PUBLISHED)
            step = store.list_publication_steps(RUN_ID)[0]
            self.assertTrue(step.detail["recovered"])
            self.assertEqual(step.detail["url"], "https://github.com/acme/api/pull/42")
            self.assertFalse(any(
                command[:2] == ("git", "push") or command[:3] == ("gh", "pr", "create")
                for command, _ in runner.calls))
            store.close()

    def test_pr_failure_after_candidate_push_is_manual_and_never_deletes_the_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            failed_pr = subprocess.CompletedProcess(
                ("gh", "pr", "create"), 1, "", "pull request refused")
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                pr_result=failed_pr,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(
                store.get_run(RUN_ID).outcome,
                wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED,
            )
            self.assertEqual(
                store.get_publication_intent(RUN_ID).targets[0].state,
                wm.PublicationState.FAILED,
            )
            self.assertFalse(any(
                command[:2] == ("git", "push")
                and command[-1] == ":refs/heads/{0}/api".format(ACCEPTED_BRANCH)
                for command, _ in runner.calls))
            store.close()

    def test_ambiguous_pr_create_output_requires_manual_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            ambiguous = subprocess.CompletedProcess(
                ("gh", "pr", "create"), 0,
                ("https://github.com/acme/api/pull/42\n"
                 "https://github.com/acme/api/pull/43\n"), "")
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                pr_result=ambiguous,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(
                store.get_publication_intent(RUN_ID).targets[0].state,
                wm.PublicationState.FAILED,
            )
            store.close()


    def test_remote_binding_accepts_standard_github_forms_and_refuses_non_github(self):
        self.assertEqual(
            publication.WorkspacePublisher._github_repository_from_url(
                "https://github.com/acme/api.git"),
            "acme/api",
        )
        self.assertEqual(
            publication.WorkspacePublisher._github_repository_from_url(
                "ssh://git@github.com/acme/api.git"),
            "acme/api",
        )
        self.assertEqual(
            publication.WorkspacePublisher._github_repository_from_url(
                "git@github.com:acme/api.git"),
            "acme/api",
        )
        with self.assertRaises(publication.PublicationError):
            publication.WorkspacePublisher._github_repository_from_url(
                "https://example.test/acme/api.git")

    def test_preflight_rejects_non_github_remote_before_creating_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                remote_url="https://example.test/acme/api.git",
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            with self.assertRaises(publication.PublicationError):
                publisher.prepare(RUN_ID)

            self.assertEqual(
                runner.calls,
                [(("git", "remote", "get-url", "--", "origin"), fixture.path)],
            )
            with self.assertRaises(cs.PublicationRefused):
                store.get_publication_intent(RUN_ID)
            store.close()

    def test_create_ignores_unrelated_urls_and_records_bound_pull_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            create_output = subprocess.CompletedProcess(
                ("gh", "pr", "create"), 0,
                ("notice: https://example.test/release-notes\n"
                 "https://github.com/acme/api/pull/42\n"), "")
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                pr_result=create_output,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.PUBLISHED)
            self.assertEqual(
                store.list_publication_steps(RUN_ID)[0].detail["url"],
                "https://github.com/acme/api/pull/42",
            )
            self.assertEqual(
                sum(command[:3] == ("gh", "pr", "list")
                    for command, _ in runner.calls),
                1,
            )
            store.close()

    def test_ambiguous_create_output_recovers_through_bound_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            ambiguous = subprocess.CompletedProcess(
                ("gh", "pr", "create"), 0,
                ("https://github.com/acme/api/pull/42\n"
                 "https://github.com/acme/api/pull/43\n"), "")
            matching = (
                '[{"url":"https://github.com/acme/api/pull/42",'
                '"headRefOid":"%s"}]\n' % fixture.accepted_sha)
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
                pr_result=ambiguous, pr_list_outputs=("[]\n", matching),
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )

            result = publisher.publish(RUN_ID)

            self.assertEqual(result.outcome, wm.WorkspaceOutcome.PUBLISHED)
            step = store.list_publication_steps(RUN_ID)[0]
            self.assertTrue(step.detail["recovered"])
            self.assertEqual(step.detail["url"], "https://github.com/acme/api/pull/42")
            self.assertEqual(
                sum(command[:3] == ("gh", "pr", "list")
                    for command, _ in runner.calls),
                2,
            )
            store.close()

    def test_unexpected_prepared_target_records_failure_before_manual_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = RepositoryFixture(
                "api", (root / "api").resolve(), "a" * 40, "c" * 40, "b" * 40)
            store = cs.CoordinatorStore(root / "coordinator.db")
            create_accepted_run(
                store, mode=wm.PublicationMode.PULL_REQUESTS, repositories=(fixture,),
            )
            runner = RemoteRunner(
                base_sha=fixture.base_sha, accepted_sha=fixture.accepted_sha,
            )
            publisher = publication.WorkspacePublisher(
                store=store, repository_paths={"api": fixture.path},
                command_runner=runner,
            )
            publisher.prepare(RUN_ID)
            store.conn.execute(
                "UPDATE publication_targets SET state=?"
                " WHERE run_id=? AND repository_id=?",
                (wm.PublicationState.PREPARED.value, RUN_ID, fixture.repository_id),
            )
            store.conn.commit()

            result = publisher.publish(RUN_ID)

            self.assertEqual(
                result.outcome, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED)
            self.assertEqual(result.intent.state, wm.PublicationState.FAILED)
            self.assertEqual(len(result.steps), 1)
            self.assertEqual(result.steps[0].from_state, wm.PublicationState.PREPARED)
            self.assertEqual(result.steps[0].to_state, wm.PublicationState.FAILED)
            self.assertEqual(
                result.steps[0].detail["reason"],
                "unexpected pull-request target state PREPARED",
            )
            store.close()

if __name__ == "__main__":
    unittest.main()
