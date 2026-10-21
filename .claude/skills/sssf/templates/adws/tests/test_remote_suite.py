"""Every test process runs on the GitHub Actions spot fleet, never on this host.

Drives the production executor (`remote_suite.github`) through a real call site
(`code_review._run_suite`) against a real git repository and a real materialized
tree. Only `gh` and `git push` are faked, at `subprocess.run`: the fake plays the
`maestro-suite.yml` job by extracting the pushed subject and job commits and
running `remote_job.main` on them, so what was pushed is what is measured.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock

import maestro
from adw_modules import code_review as cr
from adw_modules import provisioning as prov
from adw_modules import remote_job as rj
from adw_modules import remote_suite as rs
from adw_modules import review_contract as rc
from adw_modules import runner_resolution as rr
from adw_modules import tree_materialize as tm

from tests import probe_actor_bench as pb

ADWS = Path(__file__).resolve().parents[1]
_REAL_RUN = subprocess.run
SUITE = {"tests/test_one.py": "def test_one():\n    assert 1 + 1 == 2\n"}
#: What `gh` printed when one poll ended a whole run.
TLS_TIMEOUT = ('failed to get run: Get "https://api.github.com/repos/acme/widgets/actions/runs/7'
               '?exclude_pull_requests=true": net/http: TLS handshake timeout')


def _git(repo: Path, *argv: str) -> str:
    return _REAL_RUN(
        ["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True
    ).stdout.strip()


def _extract(repo: Path, sha: str, dest: Path) -> None:
    archive = _REAL_RUN(["git", "-C", str(repo), "archive", "--format=tar", sha],
                        check=True, capture_output=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as tar:
        tar.extractall(dest)


class FakeGitHub:
    """`gh` and `git push`, answering as GitHub and the spot runner would."""

    def __init__(self, repo: Path, scratch: Path, *, dispatch_rc: int = 0, download_rc: int = 0,
                 view_failures: int = 0, push_failures: int = 0) -> None:
        self.repo = repo
        self.scratch = scratch
        self.dispatch_rc = dispatch_rc
        self.download_rc = download_rc
        #: Polls of `gh run view` / subject pushes that fail in transit first.
        self.view_failures = view_failures
        self.push_failures = push_failures
        self.calls: list[list[str]] = []
        self.refs: dict[str, str] = {}
        self.deleted: list[str] = []
        self.inputs: dict[str, str] = {}

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        if argv[0] == "git" and "push" in argv:
            return self._push(argv)
        if argv[0] == "gh":
            return self._gh(argv)
        return _REAL_RUN(argv, *args, **kwargs)

    def _done(self, argv: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, code, out, err)

    def _push(self, argv: list[str]) -> subprocess.CompletedProcess:
        tail = argv[argv.index("push") + 1:]
        if "--delete" in tail:
            self.deleted.extend(item for item in tail if item.startswith("refs/"))
            return self._done(argv)
        if self.push_failures:
            self.push_failures -= 1
            return self._done(argv, 128, err="fatal: unable to access 'https://github.com/acme/widgets.git/': "
                                             "Connection reset by peer")
        for spec in tail:
            if ":" in spec and not spec.startswith("http"):
                sha, ref = spec.split(":", 1)
                self.refs[ref] = sha
        return self._done(argv)

    def _gh(self, argv: list[str]) -> subprocess.CompletedProcess:
        verb = argv[1:3]
        if verb == ["workflow", "run"]:
            for index, item in enumerate(argv):
                if item == "-f":
                    key, value = argv[index + 1].split("=", 1)
                    self.inputs[key] = value
            if self.dispatch_rc:
                return self._done(argv, self.dispatch_rc, err="could not find any workflows named maestro-suite.yml")
            return self._done(argv)
        if verb == ["run", "list"]:
            title = "maestro-suite {0}".format(self.inputs["request_id"])
            rows = [{"databaseId": 7, "displayTitle": title, "url": "https://github.com/acme/widgets/actions/runs/7"}]
            return self._done(argv, out=json.dumps(rows))
        if verb == ["run", "view"]:
            if self.view_failures:
                self.view_failures -= 1
                return self._done(argv, 1, err=TLS_TIMEOUT)
            return self._done(argv, out=json.dumps({"status": "completed", "conclusion": "success"}))
        if verb == ["run", "download"]:
            if self.download_rc:
                return self._done(argv, self.download_rc, err="no valid artifacts found to download")
            self._play_the_job(Path(argv[argv.index("-D") + 1]))
            return self._done(argv)
        return self._done(argv)

    def _play_the_job(self, out: Path) -> None:
        run = Path(tempfile.mkdtemp(dir=self.scratch))
        _extract(self.repo, self.refs[self.inputs["subject_ref"]], run / "subject")
        _extract(self.repo, self.refs[self.inputs["job_ref"]], run / "job")
        rj.main(["remote_job", str(run / "job" / "job.json"), str(run / "subject"), str(out / "result.json")])


class RemoteSuiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "--initial-branch=main")
        _git(self.repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
        # A project-local pytest, so discovery resolves the same binary on the
        # runner as the suite's own interpreter provides.
        script = self.repo / ".venv" / "bin" / "pytest"
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\nexec "{0}" -m pytest "$@"\n'.format(sys.executable))
        script.chmod(0o755)
        (self.repo / "README.md").write_text("widgets\n")
        _git(self.repo, "add", "-A", "--force")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.state = self.root / "state"
        self.tree = tm.materialize_commit(
            self.repo, self.head, self.state / "worktrees" / "review", state_root=self.state
        )
        # The suite exists only as the call site's uncommitted file: it reaches
        # the runner through the overlay or not at all.
        rc.write_files(self.tree, SUITE)
        previous = rs.use_executor(rs.github)
        self.addCleanup(rs.use_executor, previous)

    def _fake(self, **kwargs: Any) -> FakeGitHub:
        fake = FakeGitHub(self.repo, self.root, **kwargs)
        patcher = mock.patch.object(rs.subprocess, "run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `time` is one module: the in-process job's own sub-second polling
        # sleeps go through this patch too. Those still sleep; only the
        # scheduler-scale waits (backoff, poll) are recorded and skipped.
        self.slept: list[float] = []
        real_sleep = rs.time.sleep

        def sleep(delay: float) -> None:
            if delay >= 1.0:
                self.slept.append(delay)
            else:
                real_sleep(delay)

        sleeper = mock.patch.object(rs.time, "sleep", sleep)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        return fake

    def test_a_transient_poll_failure_is_retried_and_the_run_is_read(self) -> None:
        fake = self._fake(view_failures=2, push_failures=1)
        run, refusal = cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIsNone(refusal)
        self.assertEqual(run["counts"]["passed"], 1)
        views = [call for call in fake.calls if call[:3] == ["gh", "run", "view"]]
        self.assertEqual(len(views), 3)
        self.assertEqual(self.slept, [rs.TRANSIENT_BACKOFF_S[0]] + list(rs.TRANSIENT_BACKOFF_S[:2]))

    def test_a_transient_failure_beyond_the_bound_refuses_typed(self) -> None:
        fake = self._fake(view_failures=len(rs.TRANSIENT_BACKOFF_S) + 1)
        with self.assertRaises(rs.RemoteSuiteRefused) as caught:
            cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertEqual(caught.exception.reason, "RUN_UNREADABLE")
        self.assertIn("TLS handshake timeout", str(caught.exception))
        views = [call for call in fake.calls if call[:3] == ["gh", "run", "view"]]
        self.assertEqual(len(views), len(rs.TRANSIENT_BACKOFF_S) + 1)
        self.assertEqual(self.slept, list(rs.TRANSIENT_BACKOFF_S))
        self.assertFalse([call for call in fake.calls if any("pytest" in item for item in call)])
        self.assertEqual(len(fake.deleted), 2)

    def test_a_call_site_measures_on_the_fleet_and_deletes_its_refs(self) -> None:
        fake = self._fake()
        run, refusal = cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIsNone(refusal)
        self.assertEqual(run["counts"]["passed"], 1)
        self.assertEqual(run["returncode"], 0)
        subject_ref, job_ref = fake.inputs["subject_ref"], fake.inputs["job_ref"]
        prefix = "refs/maestro-suite/{0}/".format(fake.inputs["request_id"])
        self.assertTrue(subject_ref.startswith(prefix) and job_ref.startswith(prefix))
        # The subject is the commit the tree was materialized from, not a copy.
        self.assertEqual(fake.refs[subject_ref], self.head)
        job_files = set(_git(self.repo, "ls-tree", "-r", "--name-only", fake.refs[job_ref]).split())
        self.assertIn("job.json", job_files)
        self.assertIn("overlay/tests/test_one.py", job_files)
        self.assertIn("runtime/adw_modules/remote_job.py", job_files)
        self.assertEqual(sorted(fake.deleted), sorted([subject_ref, job_ref]))
        dispatch = next(call for call in fake.calls if call[:3] == ["gh", "workflow", "run"])
        self.assertIn(rs.WORKFLOW, dispatch)
        self.assertEqual(dispatch[dispatch.index("-R") + 1], "acme/widgets")

    def test_a_dispatch_that_fails_refuses_typed_and_runs_nothing_here(self) -> None:
        fake = self._fake(dispatch_rc=1)
        with self.assertRaises(rs.RemoteSuiteRefused) as caught:
            cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIn("DISPATCH_FAILED", str(caught.exception))
        # Not a transient failure: refused on the first answer, never retried.
        self.assertEqual(self.slept, [])
        self.assertIn(".github/workflows/maestro-suite.yml", str(caught.exception))
        self.assertFalse(
            [call for call in fake.calls if any("pytest" in item for item in call)],
            "a refused remote run must not fall back to a local test process",
        )
        self.assertEqual(len(fake.deleted), 2)

    def test_a_run_that_leaves_no_result_refuses(self) -> None:
        self._fake(download_rc=1)
        with self.assertRaises(rs.RemoteSuiteRefused) as caught:
            cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIn("NO_RESULT", str(caught.exception))
        self.assertIn("actions/runs/7", str(caught.exception))

    def test_an_unpushable_tree_refuses(self) -> None:
        _git(self.repo, "remote", "set-url", "origin", "/srv/not-github.git")
        self._fake()
        with self.assertRaises(rs.RemoteSuiteRefused) as caught:
            cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIn("NOT_GITHUB", str(caught.exception))


    def test_a_missing_suite_requirement_refuses_typed_from_the_fleet(self) -> None:
        # The declared requirement travels in the job and is probed on the
        # runner; its failure is the environment's, typed, and no suite runs.
        requirement = {"name": "ffmpeg", "probe": ["/bin/sh", "-c", "echo no ffmpeg >&2; exit 127"]}
        prov.set_suite_requirements([requirement])
        self.addCleanup(prov.set_suite_requirements, ())
        fake = self._fake()
        run, refusal = cr._run_suite(self.tree, SUITE, gate=None, suite_timeout_s=120)
        self.assertIsInstance(refusal, prov.SuiteRequirementMissing)
        self.assertEqual(refusal.name, "ffmpeg")
        self.assertIn("SUITE_REQUIREMENT_MISSING ffmpeg", refusal.detail)
        self.assertIn("no ffmpeg", str(refusal))
        self.assertEqual(run["executed"], 0)
        job = json.loads(_git(self.repo, "show", "{0}:job.json".format(fake.refs[fake.inputs["job_ref"]])))
        self.assertEqual(job["provision"]["requirements"], [requirement])


class CodecTest(unittest.TestCase):
    def test_a_remote_refusal_is_raised_as_its_own_type(self) -> None:
        raised = rr.RunnerUnusable("vitest", rr.Reason.UNRESOLVED, ".", candidates=("npx vitest",))
        back = rj.decode(json.loads(json.dumps(rj.encode(raised))))
        self.assertIsInstance(back, rr.RunnerUnusable)
        self.assertEqual(back.detail, raised.detail)
        self.assertEqual(str(back), str(raised))
        timeout = rj.decode(rj.encode(rc.SuiteTimeoutError("SUITE_TIMEOUT:pytest: slow")))
        self.assertIsInstance(timeout, rc.SuiteTimeoutError)

    def test_a_probe_execution_survives_the_round_trip(self) -> None:
        execution = rr.ProbeExecution(1, "out", {"a::b": rr.PROBE_FAILED_STATE}, "")
        self.assertEqual(rj.decode(json.loads(json.dumps(rj.encode(execution)))), execution)

    def test_a_foreign_type_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            rj.decode({"__error__": {"module": "os", "name": "error", "args": []}})


class ProductionExecutorTest(unittest.TestCase):
    def test_production_dispatches_to_github(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(ADWS))
        out = _REAL_RUN(
            [sys.executable, "-c", "from adw_modules import remote_suite as rs; print(rs._EXECUTOR.__name__)"],
            cwd=str(ADWS), env=env, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(out, "github")


class AgentRuleTest(unittest.TestCase):
    def test_every_dispatch_forbids_a_local_runner_and_names_the_remote_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = pb.Bench(tmp, lambda spec, prompt: {"verdict": "PASS", "findings": []},
                             role_vendors=pb.DIFFERENT_VENDORS)
            bench.actor.review_code(pb.context(bench.head, probe_budget_left=0))
            text = bench.launcher.prompts[0]["instructions"]
        self.assertIn("Never run pytest, vitest", text)
        self.assertIn(str(maestro.REMOTE_TESTS_TOOL), text)

    def test_the_tester_collect_rule_names_no_local_runner(self) -> None:
        rule = maestro.HerdrStageActor._TESTER_COLLECT_RULE
        self.assertNotIn("--collect-only", rule)
        self.assertNotIn("<pytest binary>", rule)
        self.assertIn("--collect", rule)


if __name__ == "__main__":
    unittest.main()
