"""The `tests` node evidence chain — counted facts, never a claim (§1.1 item 4).

A tests node writes tests before the implementation exists. Its VERIFIED
predicate is not the agent-node chain (pre red, post green, falsify red):
that chain cannot tell a hollow test from a real one, which is why it
missed r7. This module is the scoped replacement:

    1. the terminal envelope parses
    2. the measured delta is a subset of declared outputs (§8.3) AND every
       written path is a test file
    3. at least one NEW collected case versus the parent commit, counted
       from `--collect-only -q -o addopts=` (so a repo `-v` cannot zero it)
    4. every new case is RED at the parent tree: parseable report,
       `passed == 0`, `failed == collected`, `errored == 0`

Collection errors and import crashes of the test file are not red. A test
that does not parse is not evidence of missing behaviour. A new case that
passes at the parent is hollow and is refused by name.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from . import scheduler_types as st
from . import verification as vf
from . import worktree as wt

class TestsRefusal(str, Enum):
    """Named, typed reasons a tests node's evidence chain refuses an attempt.

    These are not BlockReason members: a tests agent can rewrite the files,
    so the failures are SEMANTIC (or ENVIRONMENTAL when the runner produced
    no report). The name is the thing a test asserts and a retry prompt
    carries, not a terminal vocabulary entry.
    """

    DIFF_NOT_TESTS_ONLY = "TESTS_DIFF_NOT_TESTS_ONLY"
    NO_NEW_CASES = "TESTS_NO_NEW_CASES"
    HOLLOW_AT_PARENT = "TESTS_HOLLOW_AT_PARENT"
    COLLECTION_FAILED = "TESTS_COLLECTION_FAILED"
    IMPORT_CRASH = "TESTS_IMPORT_CRASH"
    NOT_RED_AT_PARENT = "TESTS_NOT_RED_AT_PARENT"


class TestsGitReadFailed(RuntimeError):
    """A git read failed; this is the machine, not a missing path (§7.5)."""


#: `-o addopts=` is mandatory: a repository `addopts = -v` cancels `-q` and
#: `--collect-only` then prints a tree (`<Function x>`) that counts as zero.
_COLLECT_FLAGS = ("-p", "no:cacheprovider", "-o", "addopts=",
                  "--collect-only", "-q")
_RUN_FLAGS = ("-p", "no:cacheprovider", "-o", "addopts=", "-q")

def _pytest_prefix() -> Tuple[str, ...]:
    found = shutil.which("pytest")
    if found:
        return (found,)
    return (sys.executable, "-m", "pytest")


def _pytest_env(tree: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def is_test_path(path: str) -> bool:
    """Whether `path` is a test file the tests node is allowed to write."""
    norm = posixpath.normpath(path.replace("\\", "/"))
    name = posixpath.basename(norm)
    if norm == "tests" or norm.startswith("tests/") or "/tests/" in norm:
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith(("_test.py", ".test.ts", ".test.js", ".test.tsx",
                          ".spec.ts", ".spec.js"))


def paths_not_tests(written: Iterable[str]) -> Tuple[str, ...]:
    return tuple(path for path in written if not is_test_path(path))


def parse_collect_nodeids(stdout: str) -> Tuple[str, ...]:
    """Node ids from `pytest --collect-only -q`. One `::` identifier per case."""
    found = []
    for line in stdout.splitlines():
        token = line.strip()
        if not token or token.startswith(("=", "-", "[")):
            continue
        if "::" in token:
            found.append(token)
    return tuple(found)


def new_nodeids(parent: Sequence[str], current: Sequence[str]) -> Tuple[str, ...]:
    """Cases present now that were absent at the parent commit."""
    before = set(parent)
    return tuple(nodeid for nodeid in current if nodeid not in before)



def collect_parent_nodeids(tree: Path, commit: str, paths: Sequence[str],
                           timeout_s: float = 120.0) -> Tuple[str, ...]:
    """Cases collected from `paths` as they existed at `commit`.

    A path absent from that tree contributes nothing, so a newly created
    test file has no parent nodeids. A modified line in an existing file
    keeps the parent nodeids, which is how "new case" is distinguished
    from "edited line".
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        present = []
        for path in paths:
            blob = _blob_at(tree, commit, path)
            if blob is None:
                continue
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            present.append(path)
        return collect_nodeids(root, present, timeout_s=timeout_s)


def _blob_at(tree: Path, commit: str, path: str) -> Optional[bytes]:
    """Bytes of `path` at `commit`, or None when ls-tree proves it absent.

    Absence is an empty successful ls-tree, never a failed exit (§7.5).
    """
    listed = subprocess.run(
        ["git", "-C", str(tree), "ls-tree", "-z", "--full-tree",
         commit, "--", path],
        capture_output=True)
    if listed.returncode != 0:
        raise TestsGitReadFailed(
            "GIT_READ_FAILED:ls-tree {0} -- {1}".format(commit, path))
    records = [record for record in listed.stdout.split(b"\x00") if record]
    if not records:
        return None
    if len(records) > 1:
        return None
    meta, _, _name = records[0].partition(b"\t")
    try:
        _mode, kind, object_id = meta.split(b" ", 2)
    except ValueError as exc:
        raise TestsGitReadFailed(
            "GIT_READ_FAILED:unparseable ls-tree record for {0}@{1}".format(
                path, commit)) from exc
    if kind != b"blob":
        return None
    shown = subprocess.run(
        ["git", "-C", str(tree), "cat-file", "blob", object_id],
        capture_output=True)
    if shown.returncode != 0:
        raise TestsGitReadFailed(
            "GIT_READ_FAILED:cat-file blob {0} for {1}@{2}".format(
                object_id.decode("ascii", "replace"), path, commit))
    return shown.stdout

def adjudicate_parent_red(result: "wt.GateResult",
                          new_case_count: int) -> vf.VerificationVerdict:
    """Clause 4 of the tests chain: every new case is red at parent.

    `adjudicate_gate` expects green. This is the missing opposite: a
    parseable report whose new cases all failed, with collection/import
    crashes refused by name rather than counted as the red we wanted.
    """
    if new_case_count < 1:
        return _refused(TestsRefusal.NO_NEW_CASES,
                        "no new collected case versus the parent commit")
    counts = vf.GateCounts.parse(result.counts)
    if counts is None:
        return _refused(
            TestsRefusal.COLLECTION_FAILED,
            "no parseable report at parent — a collection error is not red",
            retry_class=st.RetryClass.ENVIRONMENTAL)
    if counts.errored:
        return _refused(
            TestsRefusal.IMPORT_CRASH,
            "{0} errored at parent; an import/collection crash is not red"
            .format(counts.errored))
    if counts.passed:
        return _refused(
            TestsRefusal.HOLLOW_AT_PARENT,
            "{0} new case(s) passed at the parent commit"
            .format(counts.passed))
    if counts.collected < new_case_count:
        return _refused(
            TestsRefusal.NO_NEW_CASES,
            "parent run collected {0}, fewer than {1} new case(s)"
            .format(counts.collected, new_case_count))
    if counts.failed < new_case_count or counts.failed != counts.collected:
        return _refused(
            TestsRefusal.NOT_RED_AT_PARENT,
            "parent run failed {0} of {1} collected; every new case must fail"
            .format(counts.failed, counts.collected))
    return vf.VerificationVerdict(verified=True)


def verify_tests_node(
        envelope_parsed: bool,
        permission: "wt.PermissionVerdict",
        written: Sequence[str],
        new_case_count: int,
        parent_red: Optional[vf.VerificationVerdict] = None,
) -> vf.VerificationVerdict:
    """The tests-node VERIFIED predicate. Not the agent-node chain.

    `parent_red` is omitted at measurement (clause 4 of the agent chain is
    evaluated before the commit). After the commit the caller re-asks with
    the parent-red verdict in hand.
    """
    if not envelope_parsed:
        return vf.VerificationVerdict(
            verified=False, failed_clause=1,
            reason="the terminal envelope did not parse as a typed envelope")
    if not permission.passes:
        paths = vf._permission_paths(permission)
        return vf.VerificationVerdict(
            verified=False, failed_clause=4,
            reason="the measured delta failed §8.3's permission check",
            retry_class=st.RetryClass.SEMANTIC,
            offending_paths=paths)
    extra = paths_not_tests(written)
    if extra:
        return _refused(
            TestsRefusal.DIFF_NOT_TESTS_ONLY,
            "diff touches non-test paths: {0}".format(", ".join(extra)),
            failed_clause=4, offending_paths=extra)
    if parent_red is None:
        # Measurement half only — parent-red is asked after the commit.
        return vf.VerificationVerdict(verified=True)
    if new_case_count < 1:
        return _refused(TestsRefusal.NO_NEW_CASES,
                        "no new collected case versus the parent commit")
    if not parent_red.verified:
        return parent_red
    return vf.VerificationVerdict(verified=True)


def collect_nodeids(tree: Path, paths: Sequence[str],
                    timeout_s: float = 120.0) -> Tuple[str, ...]:
    """Enumerate cases in `paths` under `tree`. Missing paths contribute none."""
    existing = [path for path in paths if (Path(tree) / path).exists()]
    if not existing:
        return ()
    prefix = _pytest_prefix()
    result = subprocess.run(
        [*prefix, *_COLLECT_FLAGS, *existing],
        cwd=str(tree), env=_pytest_env(tree),
        capture_output=True, text=True, timeout=timeout_s,
        check=False)
    return parse_collect_nodeids((result.stdout or "") + (result.stderr or ""))


def run_cases(tree: Path, nodeids: Sequence[str],
              timeout_s: float = 120.0) -> "wt.GateResult":
    """Execute `nodeids` under `tree` and return a GateResult with counts."""
    prefix = _pytest_prefix()
    if not nodeids:
        return wt.GateResult(
            label="parent-red", scope="node", selector="",
            command=prefix + _RUN_FLAGS, exit_code=5, green=False, counts={})
    result = subprocess.run(
        [*prefix, *_RUN_FLAGS, *nodeids],
        cwd=str(tree), env=_pytest_env(tree),
        capture_output=True, text=True, timeout=timeout_s,
        check=False)
    output = (result.stdout or "") + (result.stderr or "")
    counts = {("error" if key.startswith("error") else key): int(value)
              for value, key in wt._COUNT.findall(output)}
    return wt.GateResult(
        label="parent-red", scope="node", selector=" ".join(nodeids),
        command=prefix + _RUN_FLAGS + tuple(nodeids),
        exit_code=result.returncode, green=result.returncode == 0,
        counts=counts,
        tail=tuple(output.strip().splitlines()[-5:]))


def _refused(code: TestsRefusal, detail: str, *,
             failed_clause: int = 3,
             retry_class: Optional[st.RetryClass] = st.RetryClass.SEMANTIC,
             offending_paths: Tuple[str, ...] = ()) -> vf.VerificationVerdict:
    return vf.VerificationVerdict(
        verified=False, failed_clause=failed_clause,
        reason="{0}: {1}".format(code.value, detail),
        retry_class=retry_class,
        offending_paths=offending_paths)
