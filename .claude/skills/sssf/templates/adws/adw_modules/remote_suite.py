"""Run every test process on the GitHub Actions spot fleet, never on this host.

WP3 run 6461818 ran its lanes' suites on the operator's machine and pushed it out
of memory. Each place maestro used to start a runner -- the run preflight, a
draft's collection, the accepted suite, a reviewer's probe, an agent checking its
own work -- now calls `call`, which:

1. names the subject: the commit the tree was materialized from (or the
   checkout's HEAD), plus the tree's uncommitted files as an overlay;
2. pushes that commit and a job commit (this runtime's `adw_modules`, the job,
   the overlay) to throwaway refs under `refs/maestro-suite/<id>/` in the
   target repository;
3. dispatches the target repository's `maestro-suite.yml` workflow
   (`runs-on: orbstack`, the AWS spot pool) with those refs;
4. waits for it, downloads its result artifact, and deletes the refs.

The workflow runs `remote_job`, which provisions the subject and executes the
same body the call site used to run locally. Its result, or the typed refusal it
raised, is decoded here and returned or raised at the call site.

There is no local fallback. A run that cannot be pushed, dispatched, found,
finished or read refuses `REMOTE_SUITE_REFUSED:<reason>`; nothing then runs on
this host instead. The only other executor is `in_process`, which the test
suite installs (`tests/conftest.py`) and production never does.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import remote_job as rj
from . import scheduler_types as st
from . import tree_materialize as tm

#: The workflow file `call` dispatches. The template ships at
#: `adws/workflows/maestro-suite.yml` and is installed into the target
#: repository's `.github/workflows/` on its default branch.
WORKFLOW = "maestro-suite.yml"
WORKFLOW_TEMPLATE = Path(__file__).resolve().parent.parent / "workflows" / WORKFLOW

#: The ref namespace a run's subject and job are pushed under.
REF_PREFIX = "refs/maestro-suite"

#: How long a dispatched run may take to appear in the run list.
START_TIMEOUT_S = 300.0
#: Spot capacity, runner boot and checkout, on top of the op's own bounds.
QUEUE_ALLOWANCE_S = 1800.0
POLL_S = 20.0
#: An overlay is the call site's handful of written files, never a tree.
OVERLAY_MAX_BYTES = 20 * 1024 * 1024


class RemoteSuiteRefused(st.KernelError):
    """The remote run could not be started, finished or read. Nothing ran here."""

    code = "REMOTE_SUITE_REFUSED"

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__("{0}: {1}".format(reason, detail) if detail else reason)


Executor = Callable[[str, Path, Mapping[str, Any], Mapping[str, Any], float], Mapping[str, Any]]


def call(
    op: str,
    tree: Path,
    args: Mapping[str, Any],
    *,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: Optional[float] = None,
    timeout_s: Optional[float] = None,
) -> Any:
    """Run `op` against `tree` remotely; return its result or raise its refusal."""
    from . import provisioning as prov

    provision = {
        "argv": [str(item) for item in provision_argv if str(item)],
        "timeout_s": provision_timeout_s,
        "packages": list(prov.tree_packages()),
        "requirements": [dict(item) for item in prov.suite_requirements()],
    }
    budget = float(timeout_s or 0) + float(provision_timeout_s or prov.PROVISION_TIMEOUT_S)
    outcome = _EXECUTOR(op, Path(tree), rj.encode(dict(args)), provision, budget)
    if "error" in outcome:
        raise rj.decode(outcome["error"])
    return rj.decode(outcome.get("ok"))


# -- executors ---------------------------------------------------------------


def in_process(
    op: str,
    tree: Path,
    args: Mapping[str, Any],
    provision: Mapping[str, Any],
    budget_s: float,
) -> Mapping[str, Any]:
    """The test suite's executor: `remote_job` against the local tree.

    The result goes through the same JSON round trip as a remote one; an
    exception propagates as itself. Never installed by production.
    """
    del budget_s
    result = rj.run_op(op, tree, json.loads(json.dumps(args)), provision)
    return {"ok": json.loads(json.dumps(rj.encode(result)))}


def github(
    op: str,
    tree: Path,
    args: Mapping[str, Any],
    provision: Mapping[str, Any],
    budget_s: float,
) -> Mapping[str, Any]:
    """Push, dispatch, wait, download. Refuses rather than running locally."""
    request = uuid.uuid4().hex
    source, sha, overlay, deleted = subject_of(Path(tree))
    url = _git(source, "remote", "get-url", "origin", reason="NO_ORIGIN")
    slug = repo_slug(url)
    job = {"op": op, "args": args, "provision": provision, "deleted": deleted}
    job_sha = _job_commit(source, request, job, overlay)
    refs = ("{0}/{1}/subject".format(REF_PREFIX, request), "{0}/{1}/job".format(REF_PREFIX, request))
    _git(source, "push", "--quiet", url, "{0}:{1}".format(sha, refs[0]),
         "{0}:{1}".format(job_sha, refs[1]), reason="PUSH_FAILED")
    try:
        minutes = int(math.ceil((budget_s + QUEUE_ALLOWANCE_S) / 60.0))
        _gh("workflow", "run", WORKFLOW, "-R", slug,
            "-f", "request_id={0}".format(request),
            "-f", "subject_ref={0}".format(refs[0]),
            "-f", "job_ref={0}".format(refs[1]),
            "-f", "timeout_minutes={0}".format(minutes),
            reason="DISPATCH_FAILED",
            remedy="install {0} at .github/workflows/{1} on the default branch of {2}".format(
                WORKFLOW_TEMPLATE, WORKFLOW, slug))
        run_id, run_url = _find_run(slug, request)
        _await_run(slug, run_id, run_url, budget_s + QUEUE_ALLOWANCE_S)
        return _download_result(slug, run_id, run_url, request)
    finally:
        _run(["git", "-C", str(source), "push", "--quiet", url, "--delete", *refs])


_EXECUTOR: Executor = github


def use_executor(executor: Executor) -> Executor:
    """Install `executor`; returns the previous one. Test suites only."""
    global _EXECUTOR
    previous, _EXECUTOR = _EXECUTOR, executor
    return previous


# -- the subject -------------------------------------------------------------


def subject_of(tree: Path) -> tuple[Path, str, dict[str, Path], list[str]]:
    """`(objects repo, commit, overlay files, deleted paths)` for `tree`.

    A materialized tree names the commit it was extracted from; any other tree
    is a checkout whose HEAD is the commit. The overlay is whatever the call site
    wrote on top -- a draft, a probe, an agent's edits -- read from `git status`,
    so ignored files (an installed `.venv`, `node_modules`) never travel.
    """
    tree = Path(tree).resolve()
    record = tree / ".git" / tm.SOURCE_RECORD
    if record.is_file():
        data = json.loads(record.read_text(encoding="utf-8"))
        source, sha = Path(data["repo"]), str(data["sha"])
    else:
        source = tree
        sha = _git(tree, "rev-parse", "HEAD", reason="NO_SUBJECT")
    status = _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                  "--no-renames", reason="NO_SUBJECT", strip=False)
    overlay: dict[str, Path] = {}
    deleted: list[str] = []
    size = 0
    for entry in status.split("\0"):
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "D" in code:
            deleted.append(path)
            continue
        local = tree / path
        if not local.is_file():
            continue
        size += local.stat().st_size
        overlay[path] = local
    if size > OVERLAY_MAX_BYTES:
        raise RemoteSuiteRefused(
            "OVERLAY_TOO_LARGE",
            "{0} bytes of uncommitted files in {1}; commit or ignore them".format(size, tree),
        )
    return source, sha, overlay, deleted


def repo_slug(url: str) -> str:
    match = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    if match is None:
        raise RemoteSuiteRefused("NOT_GITHUB", "origin is {0}".format(url.strip()))
    return match.group(1)


def _runtime_files() -> dict[str, Path]:
    root = Path(rj.__file__).resolve().parent
    return {
        "runtime/adw_modules/{0}".format(path.relative_to(root).as_posix()): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _job_commit(source: Path, request: str, job: Mapping[str, Any], overlay: Mapping[str, Path]) -> str:
    """One parentless commit: the runtime, `job.json`, and `overlay/`."""
    with tempfile.TemporaryDirectory(prefix="maestro-remote-job-") as scratch:
        job_file = Path(scratch) / "job.json"
        job_file.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
        files = dict(_runtime_files())
        files["job.json"] = job_file
        files.update({"overlay/{0}".format(path): local for path, local in overlay.items()})
        names = sorted(files)
        blobs = _git(source, "hash-object", "-w", "--stdin-paths", reason="JOB_COMMIT",
                     stdin="".join(str(files[name]) + "\n" for name in names)).split()
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"),
                   GIT_AUTHOR_NAME="maestro", GIT_AUTHOR_EMAIL="maestro@localhost",
                   GIT_COMMITTER_NAME="maestro", GIT_COMMITTER_EMAIL="maestro@localhost")
        _git(source, "update-index", "--add", "--index-info", reason="JOB_COMMIT", env=env,
             stdin="".join("100644 {0}\t{1}\n".format(blob, name) for blob, name in zip(blobs, names)))
        tree = _git(source, "write-tree", reason="JOB_COMMIT", env=env)
        return _git(source, "commit-tree", tree, "-m", "maestro remote suite job {0}".format(request),
                    reason="JOB_COMMIT", env=env)


# -- the run -----------------------------------------------------------------


def _find_run(slug: str, request: str) -> tuple[str, str]:
    title = "maestro-suite {0}".format(request)
    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        listed = _gh("run", "list", "-R", slug, "--workflow", WORKFLOW, "--event",
                     "workflow_dispatch", "--limit", "50", "--json",
                     "databaseId,displayTitle,url", reason="RUN_NOT_FOUND")
        for row in json.loads(listed or "[]"):
            if row.get("displayTitle") == title:
                return str(row["databaseId"]), str(row.get("url") or "")
        if time.monotonic() >= deadline:
            raise RemoteSuiteRefused(
                "RUN_NOT_FOUND", "no {0} run titled {1!r} appeared in {2}".format(WORKFLOW, title, slug)
            )
        time.sleep(POLL_S)


def _await_run(slug: str, run_id: str, run_url: str, budget_s: float) -> None:
    deadline = time.monotonic() + budget_s
    while True:
        view = json.loads(_gh("run", "view", run_id, "-R", slug, "--json", "status,conclusion",
                              reason="RUN_UNREADABLE") or "{}")
        if view.get("status") == "completed":
            return
        if time.monotonic() >= deadline:
            _run(["gh", "run", "cancel", run_id, "-R", slug])
            raise RemoteSuiteRefused(
                "RUN_TIMEOUT", "{0} did not complete within {1:g}s; cancelled".format(run_url, budget_s)
            )
        time.sleep(POLL_S)


def _download_result(slug: str, run_id: str, run_url: str, request: str) -> Mapping[str, Any]:
    with tempfile.TemporaryDirectory(prefix="maestro-remote-result-") as scratch:
        proc = _run(["gh", "run", "download", run_id, "-R", slug, "-n",
                     "maestro-suite-{0}".format(request), "-D", scratch])
        result = Path(scratch) / "result.json"
        if proc.returncode != 0 or not result.is_file():
            raise RemoteSuiteRefused(
                "NO_RESULT", "{0} left no result artifact: {1}".format(
                    run_url, (proc.stderr or proc.stdout or "").strip()[-400:])
            )
        try:
            return dict(json.loads(result.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise RemoteSuiteRefused("NO_RESULT", "{0}: unreadable result: {1}".format(run_url, exc)) from exc


# -- processes ---------------------------------------------------------------


def _run(
    argv: Sequence[str],
    *,
    stdin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(argv), input=stdin, capture_output=True, text=True,
            env=None if env is None else dict(env),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _git(repo: Path, *argv: str, reason: str, stdin: Optional[str] = None,
         env: Optional[Mapping[str, str]] = None, strip: bool = True) -> str:
    proc = _run(["git", "-C", str(repo), *argv], stdin=stdin, env=env)
    if proc.returncode != 0:
        raise RemoteSuiteRefused(
            reason, "git {0} in {1} exited {2}: {3}".format(
                argv[0], repo, proc.returncode, (proc.stderr or "").strip()[-400:])
        )
    return proc.stdout.strip() if strip else proc.stdout


def _gh(*argv: str, reason: str, remedy: str = "") -> str:
    proc = _run(["gh", *argv])
    if proc.returncode != 0:
        detail = "gh {0} {1} exited {2}: {3}".format(
            argv[0], argv[1], proc.returncode, (proc.stderr or proc.stdout or "").strip()[-400:])
        raise RemoteSuiteRefused(reason, detail + ("; " + remedy if remedy else ""))
    return proc.stdout.strip()
