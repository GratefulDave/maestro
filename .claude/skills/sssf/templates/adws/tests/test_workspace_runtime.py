"""Executable contract for the multi-repository workspace runtime.

The runtime operates only on real, disposable Git repositories.  Its checks
must therefore reject filesystem and ref races against Git's actual state,
rather than accepting a mock's declaration of that state.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
import shutil
import sys
from unittest import mock
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Tuple

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_digest as pd  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import workspace_model as wm  # noqa: E402
from adw_modules import workspace_runtime as wr  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from adw_modules.launcher import HarnessQuiescenceError  # noqa: E402


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if check and result.returncode:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(" ".join(args), result.returncode,
                                           result.stderr.strip()))
    return result.stdout.strip()


def _make_repo(path: Path, name: str) -> Tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "harness@example.invalid")
    _git(path, "config", "user.name", "Harness")
    _git(path, "config", "core.hooksPath", str(path.parent / "no-such-hooks"))
    (path / "README.md").write_text(name + "\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")

def _commit_gate_suite(repository: Path, expected_cwd_name: str) -> str:
    (repository / "test_gate.py").write_text(
        "from pathlib import Path\n\n"
        "def test_gate_runs_from_declared_checkout():\n"
        "    assert Path.cwd().name == {0!r}\n\n"
        "def test_gate_reports_a_second_case():\n"
        "    assert True\n".format(expected_cwd_name),
        encoding="utf-8")
    _git(repository, "add", "test_gate.py")
    _git(repository, "commit", "-qm", "add gate suite")
    return _git(repository, "rev-parse", "HEAD")

def _make_tree_writable(path: Path) -> None:
    for current, directories, files in os.walk(str(path), topdown=False):
        for name in files + directories:
            item = Path(current) / name
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | stat.S_IWUSR)
    if not path.is_symlink():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)



def _plan_mapping(repository: str, base_commit: str) -> dict:
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": "child-plan",
        "repo": repository,
        "base_commit": base_commit,
        "intent": "change the child repository",
        "evidence": [
            {"kind": "observed", "evidence_id": "readme",
             "path": "README.md", "sha256": "a" * 64},
        ],
        "nodes": [
            {"kind": "code", "node_id": "work", "needs": [],
             "reads": ["readme"], "outputs": [],
             "command": ["true"], "cwd": ".", "expects_changes": False},
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "argv": ["true"],
                                 "cwd": ".", "min_cases": 1},
        },
        "supersedes": None,
    }


class WorkspaceRuntimeTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._before_environ = dict(os.environ)
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.manifest = self.root / "manifest"
        self.manifest.mkdir()
        self.api, self.api_base = _make_repo(self.manifest / "services" / "api", "api")
        self.docs, self.docs_base = _make_repo(self.manifest / "tools" / "docs", "docs")
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before_environ)
        self._tmp.cleanup()

    def _writer(self, repository_id: str = "api", path: str = "services/api",
                base_commit: str = None, target_branch: str = "main",
                repository: Path = None) -> wm.RepositorySpec:
        base = base_commit or self.api_base
        repo = repository or self.api
        stored = pc.canonicalize(pm.parse_mapping(_plan_mapping(path, base)))
        plan_path = repo / "plans" / "child.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_bytes(stored)
        return wm.RepositorySpec(
            repository_id=repository_id,
            mode=wm.RepositoryMode.WRITE,
            path=path,
            base_commit=base,
            plan_path="plans/child.json",
            plan_digest=pd.digest_of(stored),
            target_branch=target_branch,
            run_argv=("maestro", "run"),
        )

    def _read_only(self, repository_id: str = "docs", path: str = "tools/docs",
                   base_commit: str = None) -> wm.RepositorySpec:
        return wm.RepositorySpec(
            repository_id=repository_id,
            mode=wm.RepositoryMode.READ_ONLY,
            path=path,
            base_commit=base_commit or self.docs_base,
        )

    def _workspace(self, repositories: Iterable[wm.RepositorySpec],
                   gates: Tuple[pm.Gate, ...] = ()) -> wm.WorkspacePlan:
        return wm.WorkspacePlan(
            schema_version=wm.SCHEMA_V1,
            workspace_id="release-2026-08",
            repositories=tuple(repositories),
            integration_gates=gates,
        )

    def _candidate(self) -> Tuple[wm.RepositorySpec, dict, wr.CandidateRepository]:
        api = self._writer()
        workspace = self._workspace((api, self._read_only()))
        paths = wr.resolve_repository_paths(self.manifest, workspace)
        candidate = wr.prepare_candidate("run-1", api, paths["api"], self.state_root)
        return api, paths, candidate

    def _accepted_candidate(self) -> Tuple[wm.RepositorySpec, dict, wr.CandidateRepository, str]:
        api, paths, candidate = self._candidate()
        (candidate.candidate_worktree / "accepted.txt").write_text("accepted\n",
                                                                       encoding="utf-8")
        _git(candidate.candidate_worktree, "add", "accepted.txt")
        _git(candidate.candidate_worktree, "commit", "-qm", "accepted")
        accepted = _git(candidate.candidate_worktree, "rev-parse", "HEAD")
        return api, paths, candidate, accepted

    def test_resolves_manifest_relative_repository_paths(self) -> None:
        api = self._writer()
        workspace = self._workspace((api, self._read_only()))

        paths = wr.resolve_repository_paths(self.manifest, workspace)

        self.assertEqual(paths, {"api": self.api.resolve(), "docs": self.docs.resolve()})

    def test_refuses_alias_collisions_and_parent_child_overlap(self) -> None:
        alias = self.manifest / "services" / "api-alias"
        alias.symlink_to(self.api, target_is_directory=True)
        alias_spec = self._read_only("alias", "services/api-alias", self.api_base)
        api_spec = self._read_only("api", "services/api", self.api_base)
        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, self._workspace((api_spec, alias_spec)))

        child, child_base = _make_repo(self.api / "child", "nested")
        child_spec = self._read_only("child", "services/api/child", child_base)
        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, self._workspace((api_spec, child_spec)))

    def test_refuses_traversal_missing_non_git_and_unknown_base(self) -> None:
        traversal = self._read_only("traversal", "tools/docs", self.docs_base).model_copy(
            update={"path": "../outside"})
        forged_workspace = wm.WorkspacePlan.model_construct(
            schema_version=wm.SCHEMA_V1, workspace_id="forged", repositories=(traversal,),
            publication_mode=wm.PublicationMode.NONE, integration_gates=())
        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, forged_workspace)

        missing = self._read_only("missing", "missing", self.docs_base)
        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, self._workspace((missing,)))

        non_git = self.manifest / "plain"
        non_git.mkdir()
        plain = self._read_only("plain", "plain", self.docs_base)
        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, self._workspace((plain,)))

        unknown = self._read_only("unknown", "tools/docs", "f" * 40)
        with self.assertRaises(wr.RepositoryBaseError):
            wr.resolve_repository_paths(self.manifest, self._workspace((unknown,)))

    def test_git_lock_or_io_nonzero_is_environmental_not_repository_absence(self) -> None:
        lock_failure = subprocess.CompletedProcess(
            ("git", "rev-parse", "--is-inside-work-tree"), 128, "",
            "fatal: could not lock config file .git/config: I/O error")
        with mock.patch.object(wr, "_git", return_value=lock_failure):
            with self.assertRaises(wr.GitEnvironmentalError):
                wr._require_repository(self.api)

    def test_refuses_duplicate_repository_identity_in_forged_workspace(self) -> None:
        first = self._read_only("same", "tools/docs", self.docs_base)
        second = self._read_only("same", "services/api", self.api_base)
        forged_workspace = wm.WorkspacePlan.model_construct(
            schema_version=wm.SCHEMA_V1, workspace_id="forged",
            repositories=(first, second), publication_mode=wm.PublicationMode.NONE,
            integration_gates=())

        with self.assertRaises(wr.RepositoryPathError):
            wr.resolve_repository_paths(self.manifest, forged_workspace)

    def test_fresh_candidate_binds_plan_and_refuses_target_ref_move(self) -> None:
        api = self._writer()
        workspace = self._workspace((api,))
        paths = wr.resolve_repository_paths(self.manifest, workspace)
        _git(self.api, "commit", "--allow-empty", "-qm", "target moved")

        with self.assertRaises(wr.CandidatePreparationError):
            wr.prepare_candidate("run-1", api, paths["api"], self.state_root)

    def test_fresh_candidate_refuses_branch_collision_and_resume_requires_exact_identity(self) -> None:
        api, paths, candidate = self._candidate()
        self.assertEqual(candidate.candidate_branch,
                         "maestro/workspace/run-1/api/candidate")
        self.assertEqual(candidate.candidate_worktree,
                         self.state_root / "candidates" / "run-1" / "api" / "candidate")
        self.assertEqual(candidate.base_commit, self.api_base)

        with self.assertRaises(wr.CandidatePreparationError):
            wr.prepare_candidate("run-1", api, paths["api"], self.state_root)

        resumed = wr.prepare_candidate("run-1", api, paths["api"], self.state_root,
                                       resume=candidate)
        self.assertEqual(resumed, candidate)
        with self.assertRaises(wr.CandidatePreparationError):
            wr.prepare_candidate("run-1", api, paths["api"], self.state_root,
                                 resume=replace(candidate,
                                                candidate_branch="maestro/workspace/run-1/api/other"))
        with self.assertRaises(wr.CandidatePreparationError):
            wr.prepare_candidate("run-1", api, paths["api"], self.state_root,
                                 resume=replace(candidate,
                                                candidate_worktree=self.root / "other"))

    def test_fresh_candidate_refuses_symlinked_state_component_before_mutation(self) -> None:
        api = self._writer()
        workspace = self._workspace((api,))
        paths = wr.resolve_repository_paths(self.manifest, workspace)
        outside = self.root / "outside"
        outside.mkdir()
        candidate_parent = self.state_root / "candidates" / "run-1"
        candidate_parent.mkdir(parents=True)
        (candidate_parent / "api").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(wr.CandidatePreparationError):
            wr.prepare_candidate("run-1", api, paths["api"], self.state_root)
        self.assertFalse((outside / "candidate").exists())
        self.assertNotEqual(subprocess.run(
            ["git", "show-ref", "--verify", "--quiet",
             "refs/heads/maestro/workspace/run-1/api/candidate"],
            cwd=str(self.api), capture_output=True, text=True).returncode, 0)

    def test_writer_plan_binding_refuses_digest_base_and_repository_mismatch(self) -> None:
        api = self._writer()
        plan_file = self.api / "plans" / "child.json"

        plan_file.write_bytes(b"{}")
        with self.assertRaises(wr.PlanBindingError):
            wr.validate_writer_plan(self.api, api)

        wrong_base = self.api_base[:-1] + ("0" if self.api_base[-1] != "0" else "1")
        bad_base_bytes = pc.canonicalize(pm.parse_mapping(
            _plan_mapping("services/api", wrong_base)))
        plan_file.write_bytes(bad_base_bytes)
        base_bound = api.model_copy(update={"plan_digest": pd.digest_of(bad_base_bytes)})
        with self.assertRaises(wr.PlanBindingError):
            wr.validate_writer_plan(self.api, base_bound)

        wrong_repo_bytes = pc.canonicalize(pm.parse_mapping(
            _plan_mapping("services/not-api", self.api_base)))
        plan_file.write_bytes(wrong_repo_bytes)
        repo_bound = api.model_copy(update={"plan_digest": pd.digest_of(wrong_repo_bytes)})
        with self.assertRaises(wr.PlanBindingError):
            wr.validate_writer_plan(self.api, repo_bound)

    def test_validated_writer_plan_bytes_bind_the_single_read_snapshot(self) -> None:
        api = self._writer()
        plan_file = self.api / "plans" / "child.json"
        original = plan_file.read_bytes()

        self.assertEqual(wr.validate_writer_plan(self.api, api).base_commit,
                         self.api_base)
        self.assertEqual(wr.validated_writer_plan_bytes(self.api, api), original)

        plan_file.write_bytes(original + b"\n")
        with self.assertRaises(wr.PlanBindingError):
            wr.validated_writer_plan_bytes(self.api, api)

    def test_noncanonical_plan_bytes_refuse_when_declared_digest_is_canonical(self) -> None:
        api = self._writer()
        noncanonical = json.dumps(_plan_mapping("services/api", self.api_base), indent=2).encode("utf-8")
        (self.api / "plans" / "child.json").write_bytes(noncanonical)

        with self.assertRaises(wr.PlanBindingError):
            wr.validate_writer_plan(self.api, api)

    def test_accepted_sha_must_be_candidate_head_and_descend_from_base(self) -> None:
        _, _, candidate, accepted = self._accepted_candidate()
        self.assertEqual(wr.verify_accepted(candidate, {"outcome": "accepted",
                                                         "accepted_sha": accepted}),
                         accepted)
        with self.assertRaises(wr.AcceptedResultError):
            wr.verify_accepted(candidate, {"outcome": "accepted",
                                            "accepted_sha": self.api_base})

        _git(candidate.candidate_worktree, "checkout", "-q", "--orphan", "unrelated")
        _git(candidate.candidate_worktree, "rm", "-q", "-rf", ".")
        (candidate.candidate_worktree / "unrelated.txt").write_text("no ancestor\n",
                                                                        encoding="utf-8")
        _git(candidate.candidate_worktree, "add", "unrelated.txt")
        _git(candidate.candidate_worktree, "commit", "-qm", "unrelated")
        _git(candidate.candidate_worktree, "branch", "-f", candidate.candidate_branch, "HEAD")
        _git(candidate.candidate_worktree, "checkout", "-q", candidate.candidate_branch)
        unrelated = _git(candidate.candidate_worktree, "rev-parse", "HEAD")
        with self.assertRaises(wr.AcceptedResultError):
            wr.verify_accepted(candidate, {"outcome": "accepted",
                                            "accepted_sha": unrelated})

    def test_accepted_verification_refuses_branch_move_after_ancestry_check(self) -> None:
        _, _, candidate, accepted = self._accepted_candidate()
        original_git = wr._git
        moved = [False]

        def race_after_ancestry(cwd: Path, *args: str):
            result = original_git(cwd, *args)
            if (not moved[0] and args[:2] == ("merge-base", "--is-ancestor")):
                moved[0] = True
                _git(candidate.candidate_worktree, "reset", "--hard", self.api_base)
            return result

        with mock.patch.object(wr, "_git", side_effect=race_after_ancestry):
            with self.assertRaises(wr.AcceptedResultError):
                wr.verify_accepted(candidate, {"outcome": "accepted",
                                                "accepted_sha": accepted})

    def test_assembles_exact_writable_and_read_only_shas_and_canonical_manifest(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        self.assertEqual(wr.verify_accepted(candidate, {"outcome": "accepted",
                                                         "accepted_sha": accepted}),
                         accepted)

        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)

        self.assertEqual(acceptance.repository_shas,
                         {"api": accepted, "docs": self.docs_base})
        self.assertEqual(_git(acceptance.repository_paths["api"], "rev-parse", "HEAD"), accepted)
        self.assertEqual(_git(acceptance.repository_paths["docs"], "rev-parse", "HEAD"),
                         self.docs_base)
        self.assertFalse(
            acceptance.repository_paths["api"].stat().st_mode & stat.S_IWUSR)
        self.assertFalse(
            acceptance.repository_paths["docs"].stat().st_mode & stat.S_IWUSR)
        self.assertNotEqual(subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=str(acceptance.repository_paths["api"]),
            capture_output=True, text=True).returncode, 0)
        manifest_bytes = acceptance.manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest, {
            "repositories": {
                "api": {"path": "repositories/api", "sha": accepted},
                "docs": {"path": "repositories/docs", "sha": self.docs_base},
            },
            "schema_version": "maestro-acceptance.v1",
        })
        self.assertEqual(manifest_bytes, json.dumps(
            manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")

    def test_global_gates_refuse_checkout_symlink_escaping_acceptance_root(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)
        docs_checkout = acceptance.repository_paths["docs"]
        _make_tree_writable(docs_checkout)
        _git(self.docs, "worktree", "remove", "--force", str(docs_checkout))
        docs_checkout.symlink_to(candidate.candidate_worktree, target_is_directory=True)
        gate = pm.Gate(runner="pytest", cwd="repositories/api",
                       argv=("-q", "test_gate.py"), min_cases=1)

        with self.assertRaises(wr.GateConfigurationError):
            wr.run_global_gates(acceptance, (gate,), lambda: False)

    def test_global_gates_refuse_committed_symlink_escaping_acceptance_root(self) -> None:
        api, paths, candidate, _ = self._accepted_candidate()
        external = self.root / "external"
        external.mkdir()
        (candidate.candidate_worktree / "external-link").symlink_to(
            external, target_is_directory=True)
        _git(candidate.candidate_worktree, "add", "external-link")
        _git(candidate.candidate_worktree, "commit", "-qm", "add external link")
        accepted = _git(candidate.candidate_worktree, "rev-parse", "HEAD")
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)
        self.assertTrue((acceptance.repository_paths["api"] / "external-link").is_symlink())
        gate = pm.Gate(runner="pytest", cwd="repositories/api",
                       argv=("-q", "test_gate.py"), min_cases=1)

        with self.assertRaises(wr.GateConfigurationError):
            wr.run_global_gates(acceptance, (gate,), lambda: False)

    def test_runs_global_gates_in_declaration_order_from_acceptance_relative_cwd(self) -> None:
        api, _, candidate, _ = self._accepted_candidate()
        accepted = _commit_gate_suite(candidate.candidate_worktree, "api")
        docs_base = _commit_gate_suite(self.docs, "docs")
        docs = self._read_only(base_commit=docs_base)
        gates = (
            pm.Gate(runner="pytest", cwd="repositories/api",
                    argv=("-q", "test_gate.py"), min_cases=2),
            pm.Gate(runner="pytest", cwd="repositories/docs",
                    argv=("-q", "test_gate.py"), min_cases=2),
        )
        workspace = self._workspace((api, docs), gates)
        paths = wr.resolve_repository_paths(self.manifest, workspace)
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)

        results = wr.run_global_gates(acceptance, gates, lambda: False)

        self.assertEqual(tuple(result.label for result in results),
                         ("global-gate-0", "global-gate-1"))
        self.assertEqual(tuple(result.command for result in results),
                         (("pytest", "-q", "test_gate.py"),
                          ("pytest", "-q", "test_gate.py")))
        self.assertEqual(tuple(result.counts["passed"] for result in results), (2, 2))


    def test_global_gate_refuses_insufficient_count_or_nonzero_exit(self) -> None:
        api, paths, candidate, _ = self._accepted_candidate()
        accepted = _commit_gate_suite(candidate.candidate_worktree, "api")
        insufficient = pm.Gate(runner="pytest", cwd="repositories/api",
                               argv=("-q", "test_gate.py"), min_cases=3)
        workspace = self._workspace((api, self._read_only()), (insufficient,))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)
        with self.assertRaises(wr.GateFailure) as failure:
            wr.run_global_gates(acceptance, (insufficient,), lambda: False)
        self.assertEqual(failure.exception.gate_index, 0)
        self.assertEqual(failure.exception.result.command,
                         ("pytest", "-q", "test_gate.py"))
        self.assertEqual(failure.exception.result.counts["passed"], 2)

        exit_failure = pm.Gate(runner="pytest", cwd="repositories/api",
                               argv=("-q", "does-not-exist.py"), min_cases=1)
        with self.assertRaises(wr.GateFailure) as failure:
            wr.run_global_gates(acceptance, (exit_failure,), lambda: False)
        self.assertEqual(failure.exception.result.command,
                         ("pytest", "-q", "does-not-exist.py"))
        self.assertNotEqual(failure.exception.result.exit_code, 0)

    def test_gate_quiescence_error_propagates_as_a_typed_failure(self) -> None:
        scratch = self.root / "quiescence-scratch"
        with mock.patch.object(
                wt, "run_harness_process",
                side_effect=HarnessQuiescenceError(
                    "HARNESS_CONTEXT_QUIESCENCE_UNPROVEN")):
            with self.assertRaises(HarnessQuiescenceError):
                wt.run_integration_gate(
                    self.root, ("gate-command",), scratch, lambda: False,
                    label="quiescence-gate")

    def test_gate_environment_preserves_pytest_plugin_autoload_default(self) -> None:
        scratch = self.root / "plugin-scratch"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn(
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD", wt.launch_env(scratch, {}))
        self.assertEqual(
            wt.launch_env(
                scratch / "inherited",
                {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "operator-choice"},
            )["PYTEST_DISABLE_PLUGIN_AUTOLOAD"],
            "operator-choice",
        )

    def test_global_gate_cancellation_quiesces_the_owned_process_group(self) -> None:
        api, paths, candidate, _ = self._accepted_candidate()
        marker = self.root / "global-gate.pid"
        gate_test = candidate.candidate_worktree / "test_cancel_gate.py"
        gate_test.write_text(
            "import os, subprocess, sys, time\n"
            "\n"
            "def test_blocking_gate():\n"
            "    open({0!r}, 'w').write(str(os.getpgrp()))\n"
            "    subprocess.Popen([sys.executable, '-c', "
            "'import time;time.sleep(60)'])\n"
            "    time.sleep(60)\n".format(str(marker)),
            encoding="utf-8")
        _git(candidate.candidate_worktree, "add", gate_test.name)
        _git(candidate.candidate_worktree, "commit", "-qm", "add blocking gate")
        accepted = _git(candidate.candidate_worktree, "rev-parse", "HEAD")
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance(
            "run-1", workspace, paths, {"api": accepted}, self.state_root)
        gate = pm.Gate(
            runner="pytest", cwd="repositories/api",
            argv=("-q", gate_test.name), min_cases=1)
        cancelled = threading.Event()
        failures = []

        def execute() -> None:
            try:
                wr.run_global_gates(
                    acceptance, (gate,), cancel_requested=cancelled.is_set)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        # Reaching the gate is a precondition, not an assertion: the gate is a
        # real pytest process started inside a git worktree, and under full
        # suite load it does not reach its first line within three seconds.
        # Bounding a precondition at roughly its own duration is what made
        # this fail in the suite while passing 8/8 alone. What the test is
        # actually for — the cancellation, and the process group being gone
        # afterwards — is asserted below and deliberately unchanged.
        #
        # The `finally` is the more important half. The gate spawns a child
        # that sleeps 60s, and on the old path an expired precondition
        # returned without ever setting `cancelled`, leaving that child and
        # its parent alive for a minute. That is cross-test damage: every test
        # after it in the same run competes with a spinning process group,
        # which is a plausible reason failures clustered rather than scattered.
        try:
            deadline = time.monotonic() + 30.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            process_group = int(marker.read_text())
            cancelled.set()
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], wt.GateCancelled)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process_group, 0)
        finally:
            cancelled.set()
            thread.join(timeout=10.0)
        wr.cleanup_acceptance(acceptance)

    def test_cleanup_removes_only_acceptance_worktrees(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)

        wr.cleanup_acceptance(acceptance)

        self.assertFalse(acceptance.root.exists())
        self.assertTrue(candidate.candidate_worktree.is_dir())
        self.assertEqual(_git(self.api, "rev-parse", candidate.candidate_branch), accepted)


    def test_failed_assembly_cleanup_retains_root_for_reclaim(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        root = self.state_root / "acceptance" / "run-1"
        original_git = wr._git
        raced = [False]

        def fail_cleanup_after_second_checkout_collision(cwd: Path, *args: str):
            if (Path(cwd).resolve() == self.api and
                    args[:3] == ("worktree", "remove", "--force")):
                return subprocess.CompletedProcess(["git", *args], 1, "", "injected failure")
            result = original_git(cwd, *args)
            if (not raced[0] and Path(cwd).resolve() == self.api and
                    args[:3] == ("worktree", "add", "--detach")):
                raced[0] = True
                (root / "repositories" / "docs").mkdir(parents=True)
                (root / "repositories" / "docs" / "collision").write_text(
                    "force second worktree add failure\n", encoding="utf-8")
            return result

        with mock.patch.object(wr, "_git",
                               side_effect=fail_cleanup_after_second_checkout_collision):
            with self.assertRaises(wr.CleanupError):
                wr.assemble_acceptance("run-1", workspace, paths,
                                       {"api": accepted}, self.state_root)

        self.assertTrue(root.is_dir())
        self.assertTrue(candidate.candidate_worktree.is_dir())
        wr.reclaim_acceptance("run-1", workspace, paths, self.state_root)
        rebuilt = wr.assemble_acceptance("run-1", workspace, paths,
                                         {"api": accepted}, self.state_root)
        self.assertEqual(_git(rebuilt.repository_paths["api"], "rev-parse", "HEAD"),
                         accepted)

    def test_reclaim_removes_partial_acceptance_and_allows_exact_rebuild(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        partial = self.state_root / "acceptance" / "run-1" / "repositories" / "api"
        partial.parent.mkdir(parents=True)
        _git(self.api, "worktree", "add", "--detach", str(partial), accepted)

        wr.reclaim_acceptance("run-1", workspace, paths, self.state_root)

        self.assertFalse((self.state_root / "acceptance" / "run-1").exists())
        self.assertTrue(candidate.candidate_worktree.is_dir())
        rebuilt = wr.assemble_acceptance("run-1", workspace, paths,
                                         {"api": accepted}, self.state_root)
        self.assertEqual(rebuilt.repository_shas,
                         {"api": accepted, "docs": self.docs_base})
        self.assertEqual(_git(rebuilt.repository_paths["api"], "rev-parse", "HEAD"),
                         accepted)
        self.assertEqual(_git(rebuilt.repository_paths["docs"], "rev-parse", "HEAD"),
                         self.docs_base)

    def test_reclaim_removes_stale_full_registration_when_checkout_is_missing(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)
        missing = acceptance.repository_paths["docs"]
        for current, directories, files in os.walk(str(missing), topdown=False):
            for name in files + directories:
                item = Path(current) / name
                if not item.is_symlink():
                    item.chmod(item.stat().st_mode | stat.S_IWUSR)
        missing.chmod(missing.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(missing)

        wr.reclaim_acceptance("run-1", workspace, paths, self.state_root)

        self.assertFalse(acceptance.root.exists())
        self.assertTrue(candidate.candidate_worktree.is_dir())
        rebuilt = wr.assemble_acceptance("run-1", workspace, paths,
                                         {"api": accepted}, self.state_root)
        self.assertEqual(_git(rebuilt.repository_paths["api"], "rev-parse", "HEAD"),
                         accepted)
        self.assertEqual(_git(rebuilt.repository_paths["docs"], "rev-parse", "HEAD"),
                         self.docs_base)

    def test_reclaim_reconciles_stale_registrations_when_acceptance_root_is_absent(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        acceptance = wr.assemble_acceptance("run-1", workspace, paths,
                                             {"api": accepted}, self.state_root)
        _make_tree_writable(acceptance.repository_paths["api"])
        _make_tree_writable(acceptance.repository_paths["docs"])
        shutil.rmtree(acceptance.root)

        wr.reclaim_acceptance("run-1", workspace, paths, self.state_root)

        self.assertTrue(candidate.candidate_worktree.is_dir())
        self.assertNotIn(str(acceptance.root),
                         _git(self.api, "worktree", "list", "--porcelain"))
        self.assertNotIn(str(acceptance.root),
                         _git(self.docs, "worktree", "list", "--porcelain"))
        rebuilt = wr.assemble_acceptance("run-1", workspace, paths,
                                         {"api": accepted}, self.state_root)
        self.assertEqual(rebuilt.repository_shas,
                         {"api": accepted, "docs": self.docs_base})

    def test_reclaim_refuses_acceptance_root_or_repository_identity_escape(self) -> None:
        api, paths, candidate, accepted = self._accepted_candidate()
        workspace = self._workspace((api, self._read_only()))
        root = self.state_root / "acceptance" / "run-1"
        root.parent.mkdir(parents=True)
        root.symlink_to(candidate.candidate_worktree, target_is_directory=True)

        with self.assertRaises(wr.CleanupError):
            wr.reclaim_acceptance("run-1", workspace, paths, self.state_root)
        self.assertTrue(candidate.candidate_worktree.is_dir())

        root.unlink()
        escaped = api.model_copy(update={"repository_id": "../candidate"})
        forged_workspace = wm.WorkspacePlan.model_construct(
            schema_version=wm.SCHEMA_V1, workspace_id="forged", repositories=(escaped,),
            publication_mode=wm.PublicationMode.NONE, integration_gates=())
        with self.assertRaises(wr.CleanupError):
            wr.reclaim_acceptance("run-1", forged_workspace,
                                  {"../candidate": self.api}, self.state_root)
        self.assertEqual(_git(self.api, "rev-parse", candidate.candidate_branch), accepted)

if __name__ == "__main__":
    unittest.main()
