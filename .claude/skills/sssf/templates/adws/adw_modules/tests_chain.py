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

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import scheduler_types as st
from . import verification as vf
from . import worktree as wt

class _RemediedRefusal(str, Enum):
    """A refusal code that cannot exist without stating its remedy.

    A verdict is two halves: what was measured wrong, and what would satisfy
    the measure. The first half was always typed; the second used to be
    whatever the detail string happened to imply, and on
    `run-d3bd665ce838456f989a15143f196710` it implied nothing — the tester
    received the same `TEST_STRENGTH_CONTROL_WRONG_REASON` verdict three
    times, byte-identical, and spent its turns grepping the harness for the
    gate implementation instead of changing its cases. This is B15 one level
    up: a verdict with no remedy is a field with no reader on the only side
    that can act.

    So the remedy is a **required constructor argument of the code itself**.
    A member declared as a bare string does not import — `__new__` has no
    default for `remedy`, and the TypeError is a build failure, which is the
    same enforcement posture B8 gives located findings ("a field added later
    is optional forever"). `tests/test_refusal_remedies.py` enumerates every
    member as the executable statement of the obligation.

    The remedy is deterministic text keyed on the typed code — never a
    model's opinion — and nothing transitions on it (§1.2): it is rendered
    into the retry prompt beside the verdict and read nowhere else.
    """

    remedy: str

    def __new__(cls, value: str, remedy: str) -> "_RemediedRefusal":
        member = str.__new__(cls, value)
        member._value_ = value
        member.remedy = remedy
        return member


class TestsRefusal(_RemediedRefusal):
    """Named, typed reasons a tests node's evidence chain refuses an attempt.

    These are not BlockReason members: a tests agent can rewrite the files,
    so the failures are SEMANTIC (or ENVIRONMENTAL when the runner produced
    no report). The name is the thing a test asserts and a retry prompt
    carries, not a terminal vocabulary entry. Each member's second element is
    the remedy the retry prompt renders beside the verdict — what would
    satisfy the clause, stated by the module that measures it.
    """

    DIFF_NOT_TESTS_ONLY = (
        "TESTS_DIFF_NOT_TESTS_ONLY",
        "Confine the diff to test files: revert or delete every non-test "
        "path it touches, and put any fixture or helper the cases need "
        "inside the test files themselves. This node's acceptance reads "
        "test paths only; source edits belong to the paired implementation "
        "node and can never satisfy this one.")
    NO_NEW_CASES = (
        "TESTS_NO_NEW_CASES",
        "Write at least one genuinely new test case — a function the runner "
        "collects as a nodeid that did not exist at the parent commit. "
        "Editing an existing case's body, adding a helper, or adding an "
        "assertion to an old case creates no new case. Check before "
        "finishing with `--collect-only -q -o addopts=` that the new "
        "nodeids appear.")
    PARENT_RUN_UNACCOUNTED = (
        "TESTS_PARENT_RUN_UNACCOUNTED",
        "This names the harness, not your tests: collection found the new "
        "cases, and the parent run's report accounted for fewer of them "
        "than were collected, so some or all of them were never judged at "
        "all. No edit to the tests can change that count. The verdict "
        "carries both numbers and the command that produced them; compare "
        "the collected ids against the ids the runner's own report prints "
        "back — an empty intersection means the two surfaces disagree "
        "about how a case is named, which is a defect in the measurement "
        "and needs an operator, not another attempt.")
    HOLLOW_AT_PARENT = (
        "TESTS_HOLLOW_AT_PARENT",
        "Strengthen every new case that passed at the parent commit so it "
        "asserts on behaviour the parent does not have. A case that is "
        "green before the implementation exists proves nothing and will "
        "never gate anything; each new case must FAIL when run against the "
        "parent tree.")
    COLLECTION_FAILED = (
        "TESTS_COLLECTION_FAILED",
        "This names the harness, not your tests: the runner produced no "
        "parseable report at the parent tree, so nothing about the cases "
        "was judged. The retry re-runs collection unchanged. If it recurs, "
        "the runner or its environment needs an operator's attention — "
        "different test content cannot satisfy this check.")
    IMPORT_CRASH = (
        "TESTS_IMPORT_CRASH",
        "Make every new case collectable and runnable at the parent tree: "
        "move imports of modules the parent does not have from module scope "
        "into the test body, catch the ImportError there, and turn it into "
        "a failing assertion. A case must fail by asserting; an import or "
        "collection crash is a broken tree, not a red case.")
    NOT_RED_AT_PARENT = (
        "TESTS_NOT_RED_AT_PARENT",
        "Run the new cases against the parent tree and strengthen each one "
        "that does not fail there, so every new case asserts on behaviour "
        "the parent lacks. Acceptance requires every new case red at the "
        "parent — failed by assertion, not passed, skipped, or errored.")


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


#: pytest truncates each short-summary reason to the terminal width, and the
#: negative control's whole evidence is that reason. At an 80-column default a
#: `ModuleNotFoundError: No module named 'refunds'` arrives as
#: `ModuleNotFoundErr...`, which matches no declared pattern and reads as
#: "failed for the wrong reason" for every candidate. Pinned wide, and pinned
#: rather than read from the environment so the same candidate is adjudicated
#: identically on a laptop and in CI.
_REPORT_COLUMNS = "1000"


def _report_env() -> dict:
    """The environment every runner's report is produced under."""
    env = dict(os.environ)
    env["COLUMNS"] = _REPORT_COLUMNS
    return env


def _pytest_env(tree: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree) + os.pathsep + env.get("PYTHONPATH", "")
    env["COLUMNS"] = _REPORT_COLUMNS
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
                           timeout_s: float = 120.0,
                           runner: Optional["CaseRunner"] = None
                           ) -> Tuple[str, ...]:
    """Cases collected from `paths` as they existed at `commit`.

    A path absent from that tree contributes nothing, so a newly created
    test file has no parent nodeids. A modified line in an existing file
    keeps the parent nodeids, which is how "new case" is distinguished
    from "edited line".

    `runner` is the gate's own case runner. Omitting it collects with pytest,
    which is only correct for a pytest gate: `RunnerUnsupported` already
    states why measuring a vitest node with pytest is refused rather than
    defaulted, and this is the same measurement one commit earlier.
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
        if runner is not None:
            return runner.collect(root, present, timeout_s=timeout_s)
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

#: How much of the collected-id list a refusal detail may carry. Long enough
#: for a handful of vitest `file::Suite > title` ids — the shape that has to
#: be *seen* to be recognised as disagreeing with the report's — short enough
#: that one refusal cannot flood `transitions.detail_json`.
_DETAIL_ID_BUDGET = 240


def _elided(text: str, budget: int = _DETAIL_ID_BUDGET) -> str:
    """`text`, cut to `budget` with the number of dropped characters stated.

    Stated rather than silently truncated: a detail that ends mid-id would
    read as an id that ends there, which is the same class of mistake as the
    disagreement these details exist to expose.
    """
    if len(text) <= budget:
        return text
    return "{0}… (+{1} more characters)".format(
        text[:budget], len(text) - budget)


def adjudicate_parent_red(result: "wt.GateResult",
                          new_case_count: int) -> vf.VerificationVerdict:
    """Clause 4 of the tests chain: every new case is red at parent.

    `adjudicate_gate` expects green. This is the missing opposite: a
    parseable report whose new cases all failed, with collection/import
    crashes refused by name rather than counted as the red we wanted.

    Two arithmetically different facts used to share `TESTS_NO_NEW_CASES`.
    `new_case_count < 1` is the tester's: nothing new was written.
    `counts.collected < new_case_count` is the *harness's*: collection found
    N ids, the run was asked for those N ids, and the report accounted for
    fewer than N — which no edit to the tests can change. In production that
    second branch was never once the tester's doing. It fired three times on
    `lane-wp6-tests`, each time from a different measurement defect:
    collecting a `.test.ts` file with pytest (`407d7d3`),
    `vitest list --json <path>` overwriting the file it was measuring
    (`5273342`), and `vitest list` naming a case `Suite > title` while
    `--reporter=json` joined the same parts with a plain space, so
    `run()`'s `set(collected) & set(reported)` was empty by construction
    (`c087469`). Nine correct cases, every one red at the parent for exactly
    the reason a TDD case is red, refused with a string that said the tester
    had written nothing — and the third bug cost a whole run to find because
    the refusal named the agent instead of the measurement.

    So the branch is `TESTS_PARENT_RUN_UNACCOUNTED`, it carries both counts
    and the command that produced them, and it is ENVIRONMENTAL: re-running
    the agent cannot repair a report that did not account for the cases it
    was handed, and classifying it SEMANTIC spends the fix loop's budget
    (§7.5) proving that twice. The observed ids are not reachable here — a
    `GateResult` carries the ids that were *asked for* (`selector`) and the
    five counts, not the per-case outcomes — so the detail states the side it
    has and names the command that printed the other.
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
        unaccounted = new_case_count - counts.collected
        return _refused(
            TestsRefusal.PARENT_RUN_UNACCOUNTED,
            "collection found {0} new case(s) at the parent tree; the run "
            "reported an outcome for {1} of them, {2} unaccounted{3}. "
            "ran: `{4}`; collected ids: {5}".format(
                new_case_count, counts.collected, unaccounted,
                " — the report and the collected ids intersect nowhere, so "
                "not one case was judged" if counts.collected == 0 else "",
                " ".join(result.command) or "(no command recorded)",
                _elided(result.selector) or "(none recorded)"),
            retry_class=st.RetryClass.ENVIRONMENTAL)
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
    # `refusal_code` is the same member the reason's prefix spells, carried as
    # a typed field because these refusals are deterministic adjudications:
    # two byte-identical records mean the loop reproduced the same fact, and
    # `retry_policy.refusal_repetition` may only conclude that from a typed
    # code, never from a prose prefix (§7.5).
    return vf.VerificationVerdict(
        verified=False, failed_clause=failed_clause,
        reason="{0}: {1}".format(code.value, detail),
        retry_class=retry_class,
        offending_paths=offending_paths,
        refusal_code=code.value,
        remedy=code.remedy)


# ── the executable test-strength contract (§TS) ─────────────────────────────
#
# The clauses above answer "are these cases new, and are they red where the
# implementation is absent". Run-8d1a71f463e4430f92a125a8f8b3731d showed that
# is not discrimination proof: `lane-acquisition-manifest-tests` satisfied all
# four of them on four non-skipped cases and reached MERGED, and every one of
# the four implementation candidates its tests existed to gate was
# independently rejected. Red-at-parent proves the cases *reach* absent code.
# It does not prove they cover the contract, and it does not prove they fail
# for the reason the contract names.
#
# What follows is measured, never claimed. §1.2 forbids keying a transition on
# an agent's account of its own work, so nothing here asks the tester whether
# it covered a requirement: the plan declares which case ids would prove it
# did, and this module counts them.


#: `GateStrengthEvidence.gate_min_cases` when the caller stated no threshold.
#: Zero rather than `None` so the field stays an `int` and lands in the ledger
#: row as a number a query can filter on: `gate_min_cases = 0` is the durable,
#: greppable evidence that a call site never passed the node's threshold.
UNSTATED_GATE_FLOOR = 0


class StrengthRefusal(_RemediedRefusal):
    """Typed reasons a test candidate fails its declared strength contract.

    Separate from `TestsRefusal` because they refuse different things. A
    `TestsRefusal` says the candidate is not a tests-node diff at all — it
    wrote source, or it added no case. A `StrengthRefusal` says the candidate
    *is* a tests diff and does not discriminate. Each member's second element
    is the remedy the retry prompt renders beside the verdict; the coverage
    members are worded to hold under both measurement standards, because
    `measure_coverage` raises them at test acceptance (EXECUTED, the tester's
    diff is judged) and again at implementation acceptance (PASSED, the
    builder's diff is judged) and the counting rule is the same both times.
    """

    CONTRACT_ABSENT = (
        "TEST_STRENGTH_CONTRACT_ABSENT",
        "No test edit can satisfy this check: the node declares no "
        "test-strength contract, and a candidate accepted without one is "
        "accepted on its case count, which does not discriminate. The plan "
        "must be re-shipped with a `test_strength` block for this node — "
        "coverage obligations mapping case selectors to requirements, plus "
        "an executable falsifiability control. That is an operator's edit, "
        "not this lane's.")
    REQUIREMENT_UNCOVERED = (
        "TEST_STRENGTH_REQUIREMENT_UNCOVERED",
        "Write cases whose nodeids the obligation's declared case_selector "
        "actually selects — name new tests so the selector quoted in the "
        "verdict matches them. The counter matches collected nodeids against "
        "that selector verbatim; a case that covers the requirement under a "
        "name the selector does not match counts for nothing.")
    OBLIGATION_UNMET = (
        "TEST_STRENGTH_OBLIGATION_UNMET",
        "Bring the obligation's selected-case count up to its min_cases, "
        "counted from cases that run to a verdict — passed or failed — in "
        "one suite run. Add cases the declared selector matches, and fix "
        "any selected case that errors or fails to collect: a crash reaches "
        "no verdict and never counts.")
    OBLIGATION_ONLY_SKIPPED = (
        "TEST_STRENGTH_OBLIGATION_ONLY_SKIPPED",
        "Make the selected cases execute: remove the skip/skipif/xfail "
        "marks, or make their conditions false in this environment, so "
        "every case the obligation selects runs to a verdict. A skipped "
        "case is not executed behavioural evidence and can never satisfy "
        "an obligation, whatever it would assert if it ran.")
    POSITIVE_GATE_NOT_GREEN = (
        "TEST_STRENGTH_POSITIVE_GATE_NOT_GREEN",
        "Change the implementation until every case the obligation selects "
        "passes against this candidate — run the selector locally, read "
        "each failure, and fix the code it names. Do not edit the test "
        "files: they are the accepted, reviewed bytes this node is gated "
        "by, and a diff that touches them is refused before it is judged.")
    CONTROL_NOT_SELECTED = (
        "TEST_STRENGTH_CONTROL_SELECTED_NO_CASE",
        "Make the contract's expected_failing_selector select at least one "
        "case this candidate collects: write cases whose nodeids match the "
        "selector quoted in the verdict. A control that selects nothing is "
        "unfalsifiable rather than falsified, and no run of it can prove "
        "the cases discriminate.")
    CONTROL_NOT_RED = (
        "TEST_STRENGTH_CONTROL_NOT_RED",
        "Strengthen every selected case the verdict names as surviving so "
        "it fails when the code under test is absent or reverted: assert on "
        "behaviour the control removes, not on facts that hold either way. "
        "The control passes only when every selected case goes red under "
        "the declared defect.")
    CONTROL_WRONG_REASON = (
        "TEST_STRENGTH_CONTROL_WRONG_REASON",
        "The selected cases must fail with text the declared "
        "expected_reason_pattern matches. If the observed reasons are "
        "crashes — ModuleNotFoundError, ImportError, AttributeError — the "
        "cases DO fail at the parent, but a missing import raises before "
        "any assertion runs, so the failure cannot carry the declared "
        "reason. Import the absent module inside the test body, catch the "
        "ImportError there, and assert on the result with a message the "
        "pattern matches — keep the import inside the body, because at "
        "module scope it makes the file uncollectable, which is not a "
        "satisfying red:\n"
        "    def test_<name>():\n"
        "        try:\n"
        "            from <module> import <symbol>\n"
        "        except ModuleNotFoundError:\n"
        "            <symbol> = None\n"
        "        assert <symbol> is not None, \"<text matching the "
        "declared reason>\"\n"
        "        # ... then the real behavioural assertions\n"
        "If the cases already fail by asserting but with other text, align "
        "the assertion messages with the declared pattern.")
    CONTROL_COLLECTION_FAILED = (
        "TEST_STRENGTH_CONTROL_COLLECTION_FAILED",
        "This names the harness, not the tests: the negative control run "
        "produced no parseable report, so nothing was proven either way "
        "about the cases. The retry re-executes the control unchanged; if "
        "it recurs, the runner or its environment needs an operator's "
        "attention — different test content cannot satisfy this check.")
    CONTROL_IMPORT_CRASH = (
        "TEST_STRENGTH_CONTROL_IMPORT_CRASH",
        "Make the selected cases survive collection in the control tree, "
        "where the code under test is absent or reverted: move imports of "
        "that code from module scope into the test body and turn the "
        "ImportError into a failing assertion. A case must go red by "
        "asserting; one that errors before reaching an assertion proves "
        "nothing.")
    CONTROL_NOT_ISOLATED = (
        "TEST_STRENGTH_CONTROL_NOT_ISOLATED",
        "Make the cases side-effect free with respect to the worktree: "
        "create files only under the runner's temp locations (tmp_path, "
        "tempfile) outside the repository, and remove anything a test "
        "writes inside it, so `git status --porcelain` reads identically "
        "before and after the control. A control that dirties the attempt "
        "worktree is refused whatever it proved.")
    CONTROL_UNEXECUTABLE = (
        "TEST_STRENGTH_CONTROL_UNEXECUTABLE",
        "No declared control could be run, so nothing proves these cases "
        "can fail — a contract or environment fact, not a test fact. The "
        "plan must declare an executable falsifiability strategy for this "
        "node (`baseline_absent`, or `controlled_mutation` with revert "
        "paths and a plan base commit), or an operator must fix the fault "
        "named in the verdict. Rewriting the tests cannot satisfy this "
        "check.")
    RUNNER_UNSUPPORTED = (
        "TEST_STRENGTH_RUNNER_UNSUPPORTED",
        "This factory can only count cases for the runners it names — the "
        "gate must declare one of them (pytest or vitest). A plan/config "
        "fact: re-ship the plan with a supported `gate` runner; no edit to "
        "the tests can satisfy it.")
    #: The candidate discriminates and still cannot supply the threshold its
    #: paired builder will be judged on. Not a statement about the tests —
    #: they may be excellent — but about the pair: accepting these bytes
    #: freezes the collectable count below `min_cases`, and every builder
    #: dispatched afterwards faces a gate no correct attempt can pass.
    GATE_FLOOR_UNREACHABLE = (
        "TEST_STRENGTH_GATE_FLOOR_UNREACHABLE",
        "Add enough collected cases to these files to reach the gate's "
        "declared min_cases, or have the plan re-shipped with a lower "
        "min_cases. The count is taken by collection over these exact "
        "bytes, and the paired builder must carry them verbatim, so the "
        "floor must be reachable now — a builder can never add cases "
        "later.")


#: The four statuses a single executed case can land in. `errored` is kept
#: apart from `failed` for the reason `adjudicate_parent_red` keeps them
#: apart: an import or collection crash is not a case that discriminated.
CASE_STATUSES: Tuple[str, ...] = ("passed", "failed", "errored", "skipped")


@dataclass(frozen=True)
class CaseOutcome:
    """One executed case, its status, and the reason it is not green.

    `reason` is the runner's own text for the failure — an assertion message,
    an exception type. It is evidence read out of a machine-produced report,
    not prose an agent wrote, which is why a transition may key on it (§1.2).
    """

    nodeid: str
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in CASE_STATUSES:
            raise ValueError(
                "{0}: {1!r} is not one of {2}".format(
                    self.nodeid, self.status, ", ".join(CASE_STATUSES)))


@dataclass(frozen=True)
class CaseRun:
    """One execution of a set of cases, resolved to per-case outcomes.

    `collection_failed` is a first-class field rather than an inference from
    an empty outcome list: "the runner could not collect" and "the runner
    collected nothing" are different facts, and only the second one could ever
    be a candidate's own doing.
    """

    outcomes: Tuple[CaseOutcome, ...]
    exit_code: int
    collection_failed: bool = False
    command: Tuple[str, ...] = ()
    tail: Tuple[str, ...] = ()

    def with_status(self, status: str) -> Tuple[CaseOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == status)

    @property
    def passed(self) -> int:
        return len(self.with_status("passed"))

    @property
    def failed(self) -> int:
        return len(self.with_status("failed"))

    @property
    def errored(self) -> int:
        return len(self.with_status("errored"))

    @property
    def skipped(self) -> int:
        return len(self.with_status("skipped"))

    def selecting(self, selector: str) -> Tuple[CaseOutcome, ...]:
        """Outcomes whose case id contains `selector`.

        Containment rather than a runner expression, because the selector has
        to mean the same thing under pytest and under vitest and the two
        disagree about `-k` and `--testNamePattern`. A plan that declares
        `rejects_unknown_source` selects every case whose id carries those
        characters under either runner.
        """
        return tuple(o for o in self.outcomes if selector in o.nodeid)


# ── the two runners, behind one interface (verification requirement 13) ─────
#
# The invariant is the same under both: the adjudicators below are pure
# functions over `CaseRun`, so pytest and vitest cannot drift into two
# different definitions of "covered" or "failed for the expected reason".
# What differs is only how a `CaseRun` is produced, which is this section.


#: `FAILED tests/x.py::test_y - AssertionError: message`. The reason is
#: everything after the first ` - `, which pytest emits under `-rf` and which
#: is the runner's own text rather than anything a model wrote.
_PYTEST_SHORT_SUMMARY = re.compile(
    r"^(FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+?)(?:\s+-\s+(.*))?$")

#: `1 failed, 2 passed in 0.10s` — only used to detect that a report was
#: produced at all. Per-case status comes from the short summary above.
_PYTEST_TOTALS = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed)")


class CaseRunner:
    """Collect and execute cases for one gate runner.

    Not a `Protocol`: the two implementations share `_argv_prefix` resolution
    and the sandbox environment, and a protocol would duplicate both.
    """

    name = ""

    def collect(self, tree: Path, paths: Sequence[str],
                timeout_s: float = 120.0) -> Tuple[str, ...]:
        raise NotImplementedError

    def run(self, tree: Path, nodeids: Sequence[str],
            timeout_s: float = 300.0) -> CaseRun:
        raise NotImplementedError


class PytestCaseRunner(CaseRunner):
    name = "pytest"

    def collect(self, tree: Path, paths: Sequence[str],
                timeout_s: float = 120.0) -> Tuple[str, ...]:
        return collect_nodeids(tree, paths, timeout_s=timeout_s)

    def run(self, tree: Path, nodeids: Sequence[str],
            timeout_s: float = 300.0) -> CaseRun:
        prefix = _pytest_prefix()
        command = prefix + _RUN_FLAGS + ("--tb=no", "-rfEs") + tuple(nodeids)
        if not nodeids:
            return CaseRun(outcomes=(), exit_code=5, collection_failed=False,
                           command=command)
        result = subprocess.run(
            list(command), cwd=str(tree), env=_pytest_env(tree),
            capture_output=True, text=True, timeout=timeout_s, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return CaseRun(
            outcomes=parse_pytest_outcomes(output, nodeids),
            exit_code=result.returncode,
            collection_failed=not _PYTEST_TOTALS.search(output),
            command=command,
            tail=tuple(output.strip().splitlines()[-8:]))


class VitestCaseRunner(CaseRunner):
    """vitest, driven through its machine-readable JSON reporter.

    Collection is `vitest list --json`, which every vitest that supports the
    `list` verb prints as an array of case ids. Execution is
    `vitest run --reporter=json`, whose `testResults[].assertionResults[]`
    carry a status and, on failure, `failureMessages`. Both are the runner's
    own structured output; nothing here parses human-facing text, which is
    what makes the vitest arm hold the same evidence standard as the pytest
    one rather than a looser variant of it.
    """

    name = "vitest"

    def _prefix(self) -> Tuple[str, ...]:
        found = shutil.which("vitest")
        if found:
            return (found,)
        return ("npx", "--no-install", "vitest")

    def collect(self, tree: Path, paths: Sequence[str],
                timeout_s: float = 120.0) -> Tuple[str, ...]:
        existing = [p for p in paths if (Path(tree) / p).exists()]
        if not existing:
            return ()
        # The filters go BEFORE `--json`, and this order is load-bearing:
        # vitest's `--json` takes an *optional value*, so `--json <path>` is
        # parsed as "write the listing to <path>". Emitting the paths after
        # the flag therefore made vitest OVERWRITE the first test file with
        # its own JSON output, print nothing to stdout, and exit 0. On
        # `run-6b8f607d89744eeb94a79713b3b5d234` that destroyed the tester's
        # committed cases inside its attempt worktree, returned zero node
        # ids, and refused `TESTS_NO_NEW_CASES` -- forever, because every
        # retry rebuilt the file and every collection ate it again.
        command = self._prefix() + ("list", *existing, "--json")
        result = subprocess.run(
            list(command), cwd=str(tree), env=_report_env(),
            capture_output=True, text=True, timeout=timeout_s, check=False)
        return parse_vitest_list((result.stdout or "") + (result.stderr or ""))

    def run(self, tree: Path, nodeids: Sequence[str],
            timeout_s: float = 300.0) -> CaseRun:
        """Execute and return the outcomes for `nodeids`.

        The run itself is not narrowed to those ids, and the outcomes are
        filtered after. vitest selects by file and by name pattern, and a
        `full name` carries spaces and regex metacharacters, so building a
        `--testNamePattern` from a list of them is a quoting problem with a
        silent failure mode: a pattern that matches nothing looks exactly like
        a suite where nothing ran. Filtering a complete report is slower and
        cannot be wrong about which case is which.
        """
        command = self._prefix() + ("run", "--reporter=json")
        if not nodeids:
            return CaseRun(outcomes=(), exit_code=1, collection_failed=False,
                           command=command)
        result = subprocess.run(
            list(command), cwd=str(tree), env=_report_env(),
            capture_output=True, text=True, timeout=timeout_s, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        outcomes, parsed = parse_vitest_report(output)
        wanted = set(nodeids)
        return CaseRun(
            outcomes=tuple(o for o in outcomes if o.nodeid in wanted),
            exit_code=result.returncode,
            collection_failed=not parsed,
            command=command,
            tail=tuple(output.strip().splitlines()[-8:]))


_RUNNERS: dict = {"pytest": PytestCaseRunner, "vitest": VitestCaseRunner}


class RunnerUnsupported(ValueError):
    """A gate names a runner this module cannot resolve to cases.

    Refused rather than defaulted to pytest. Silently measuring a vitest
    node's coverage with pytest would report zero cases for every obligation,
    which reads as "the tester covered nothing" instead of "this runtime
    cannot measure this node".
    """


def case_runner(name: str) -> CaseRunner:
    try:
        return _RUNNERS[name]()
    except KeyError:
        raise RunnerUnsupported(
            "{0}: {1} cannot resolve cases for this runner; supported: "
            "{2}".format(StrengthRefusal.RUNNER_UNSUPPORTED.value, name,
                         ", ".join(sorted(_RUNNERS)))) from None


def run_cases_for(runner: CaseRunner, tree: Path, nodeids: Sequence[str],
                  timeout_s: float = 300.0) -> "wt.GateResult":
    """`run_cases`, but under the gate's own runner.

    Clause 4 of the tests chain adjudicates a `GateResult`, and `run_cases`
    can only produce one by driving pytest. That made the whole clause-3/4
    chain pytest-only while the strength contract beside it was already
    runner-dispatched, and on `run-8a200af7f9044ce7a11a51b6908f37e3` a vitest
    tests node was collected with pytest, yielded zero nodeids, and refused
    `TESTS_NO_NEW_CASES` on every attempt — the exact silent-zero failure
    `RunnerUnsupported` documents, reached through the one path that had no
    runner to refuse with. The pytest arm still runs `run_cases` verbatim so
    its evidence is unchanged; every other runner is resolved through its
    `CaseRun`, whose per-case outcomes carry the same five counts.

    `collection_failed` maps to empty counts, which `GateCounts.parse` reads
    as "no report" rather than "zero cases" — the distinction clause 4 needs
    to call a collection failure environmental instead of red.
    """
    if isinstance(runner, PytestCaseRunner):
        return run_cases(tree, nodeids, timeout_s=timeout_s)
    executed = runner.run(tree, nodeids, timeout_s=timeout_s)
    counts: dict = {}
    if not executed.collection_failed:
        counts = {
            "collected": len(executed.outcomes),
            "passed": executed.passed,
            "failed": executed.failed,
            "skipped": executed.skipped,
            "errored": executed.errored,
        }
    return wt.GateResult(
        label="parent-red", scope="node", selector=" ".join(nodeids),
        command=tuple(executed.command),
        exit_code=executed.exit_code, green=executed.exit_code == 0,
        counts=counts, tail=tuple(executed.tail))


def parse_pytest_outcomes(output: str,
                          nodeids: Sequence[str]) -> Tuple[CaseOutcome, ...]:
    """Per-case outcomes from a `-rfEs --tb=no` run.

    Cases the short summary does not name passed: pytest's `-rf` reports the
    exceptional ones and stays silent about green. That inversion is why the
    executed set is passed in — a green case is proven by having been asked
    for and not reported, never by an absent line alone.
    """
    reported: dict = {}
    for line in output.splitlines():
        match = _PYTEST_SHORT_SUMMARY.match(line.strip())
        if match is None:
            continue
        kind, nodeid, reason = match.group(1), match.group(2), match.group(3)
        status = {"FAILED": "failed", "ERROR": "errored",
                  "SKIPPED": "skipped", "XFAIL": "skipped",
                  "XPASS": "failed"}[kind]
        # An ERROR line names the file, not always the case. Bind it to every
        # requested case in that file so an import crash cannot read as a set
        # of silently-green cases.
        reported[nodeid] = (status, (reason or "").strip())
    outcomes = []
    for nodeid in nodeids:
        if nodeid in reported:
            status, reason = reported[nodeid]
            outcomes.append(CaseOutcome(nodeid, status, reason))
            continue
        file_part = nodeid.split("::", 1)[0]
        if file_part in reported and reported[file_part][0] == "errored":
            outcomes.append(CaseOutcome(nodeid, "errored",
                                        reported[file_part][1]))
            continue
        outcomes.append(CaseOutcome(nodeid, "passed"))
    return tuple(outcomes)


def parse_vitest_list(output: str) -> Tuple[str, ...]:
    """Case ids from `vitest list --json`."""
    payload = _first_json(output)
    if payload is None:
        return ()
    if isinstance(payload, list):
        found = []
        for item in payload:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("fullName")
                file_name = item.get("file") or item.get("filepath")
                if isinstance(name, str):
                    found.append("{0}::{1}".format(file_name, name)
                                 if isinstance(file_name, str) else name)
        return tuple(found)
    return ()


def _vitest_case_name(case: dict) -> str:
    """The case name in the *same shape* `vitest list --json` prints.

    The two vitest surfaces disagree, and the disagreement is silent. `vitest
    list --json` joins a case's ancestor suites and its title with `" > "`;
    `--reporter=json` also ships `fullName`, which joins them with a plain
    space. Building a node id from `fullName` therefore produces an id that
    can never equal the id collection produced for the very same case.

    `VitestCaseRunner.run` runs the whole suite and keeps the outcomes whose
    id is in the collected set, so an id shape that cannot match means the
    kept set is empty for every vitest node — a report with nine failing
    cases adjudicated as `parent run collected 0`, on every attempt, which no
    edit to the tests could satisfy. Reconstructing the name from
    `ancestorTitles + title` is the only form that agrees with collection;
    `fullName` is the fallback for a report that omits the parts.
    """
    ancestors = case.get("ancestorTitles")
    title = case.get("title")
    if isinstance(ancestors, list) and isinstance(title, str) and title:
        parts = [part for part in ancestors if isinstance(part, str) and part]
        return " > ".join(parts + [title])
    full = case.get("fullName") or title or ""
    return full if isinstance(full, str) else ""


def parse_vitest_report(output: str) -> Tuple[Tuple[CaseOutcome, ...], bool]:
    """Per-case outcomes from `vitest run --reporter=json`.

    Returns the outcomes and whether a report was parsed at all, so a run that
    produced no JSON is `collection_failed` rather than a green empty set.

    Case ids are built to agree with `parse_vitest_list`; see
    `_vitest_case_name` for why `fullName` is not that shape.
    """
    payload = _first_json(output)
    if not isinstance(payload, dict):
        return (), False
    files = payload.get("testResults")
    if not isinstance(files, list):
        return (), False
    outcomes = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        file_name = entry.get("name") or entry.get("file") or ""
        cases = entry.get("assertionResults")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict):
                continue
            title = _vitest_case_name(case)
            status = {"passed": "passed", "failed": "failed",
                      "skipped": "skipped", "pending": "skipped",
                      "todo": "skipped"}.get(str(case.get("status")), "errored")
            messages = case.get("failureMessages")
            reason = ""
            if isinstance(messages, list) and messages:
                reason = str(messages[0]).strip().splitlines()[0]
            outcomes.append(CaseOutcome(
                "{0}::{1}".format(file_name, title) if file_name else title,
                status, reason))
    return tuple(outcomes), True


def _first_json(output: str):
    """The first complete JSON value in `output`, or None.

    A runner writes its report onto a stream that may already carry a banner
    or a warning. Scanning for the first decodable value is what makes the
    parse independent of that noise, without the parse ever guessing.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(output[index:])
        except ValueError:
            continue
        return value
    return None


# ── the adjudicators: pure, and shared by both runners ──────────────────────


#: The two things a coverage obligation can be measured for, and the only
#: two. `EXECUTED` is the standard at test acceptance: the implementation
#: these cases gate does not exist yet, so every one of them is red, and what
#: the obligation has to prove there is that a real case ran and reached a
#: verdict. `PASSED` is the standard at implementation acceptance: the same
#: cases, the same selectors, now green against the candidate they gate.
#:
#: One enum rather than two functions, because the refusal vocabulary, the
#: skip handling and the ledger shape are identical and a second copy would
#: be two answers to "is this requirement covered".
EXECUTED = "executed"
PASSED = "passed"


@dataclass(frozen=True)
class ObligationMeasurement:
    """What one coverage obligation actually got, measured not claimed."""

    requirement_id: str
    aspect: str
    case_selector: str
    min_cases: int
    standard: str = EXECUTED
    selected: Tuple[str, ...] = ()
    executed: Tuple[str, ...] = ()
    passed: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()

    @property
    def counted(self) -> Tuple[str, ...]:
        return self.passed if self.standard == PASSED else self.executed

    @property
    def met(self) -> bool:
        return len(self.counted) >= self.min_cases


@dataclass(frozen=True)
class CoverageMeasurement:
    """Every obligation's measurement, and the refusal if one is unmet."""

    obligations: Tuple[ObligationMeasurement, ...] = ()
    refusal: Optional[str] = None
    reason: str = ""
    standard: str = EXECUTED

    @property
    def covered(self) -> bool:
        return self.refusal is None

    def as_mapping(self) -> Dict[str, Any]:
        """The durable form. Ordered, so the ledger row is deterministic."""
        return {
            "standard": self.standard,
            "obligations": [
                {"requirement_id": item.requirement_id,
                 "aspect": item.aspect,
                 "case_selector": item.case_selector,
                 "min_cases": item.min_cases,
                 "standard": item.standard,
                 "selected": list(item.selected),
                 "executed": list(item.executed),
                 "passed": list(item.passed),
                 "skipped": list(item.skipped),
                 "met": item.met}
                for item in self.obligations],
            "refusal": self.refusal,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FalsifiabilityResult:
    """The negative control's execution and what it proved."""

    strategy: str = ""
    executed: bool = False
    proven: bool = False
    refusal: Optional[str] = None
    reason: str = ""
    selected: Tuple[str, ...] = ()
    failed_for_expected_reason: Tuple[str, ...] = ()
    observed_reasons: Tuple[str, ...] = ()
    mutation_paths: Tuple[str, ...] = ()
    control_command: Tuple[str, ...] = ()

    def as_mapping(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "executed": self.executed,
            "proven": self.proven,
            "refusal": self.refusal,
            "reason": self.reason,
            "selected": list(self.selected),
            "failed_for_expected_reason":
                list(self.failed_for_expected_reason),
            "observed_reasons": list(self.observed_reasons),
            "mutation_paths": list(self.mutation_paths),
            "control_command": list(self.control_command),
        }


def measure_coverage(obligations: Sequence[Any], run: "CaseRun",
                     standard: str = EXECUTED) -> CoverageMeasurement:
    """Count each obligation's cases in one executed run, to one standard.

    Under `EXECUTED` a selected case counts when it ran and reached a verdict
    — passed or failed. Under `PASSED` only green counts. Neither standard
    ever counts a skip, an xfail, or an errored case: the requirement it was
    declared against has no executed behavioural evidence in any of those
    states, which is what obligation 1 asks for. An obligation whose only
    selected cases were skipped is refused by its own name so the reason
    reaches the tester instead of a bare count.
    """
    if standard not in (EXECUTED, PASSED):
        raise ValueError(
            "{0!r} is not a coverage standard; expected {1} or {2}".format(
                standard, EXECUTED, PASSED))
    measured: List[ObligationMeasurement] = []
    for obligation in obligations:
        selector = obligation.case_selector
        selected = run.selecting(selector)
        measured.append(ObligationMeasurement(
            requirement_id=obligation.requirement_id,
            aspect=obligation.aspect,
            case_selector=selector,
            min_cases=int(obligation.min_cases),
            standard=standard,
            selected=tuple(o.nodeid for o in selected),
            executed=tuple(o.nodeid for o in selected
                           if o.status in ("passed", "failed")),
            passed=tuple(o.nodeid for o in selected if o.status == "passed"),
            skipped=tuple(o.nodeid for o in selected if o.status == "skipped"),
        ))
    frozen = tuple(measured)
    for item in frozen:
        if item.met:
            continue
        if not item.selected:
            return CoverageMeasurement(
                frozen, StrengthRefusal.REQUIREMENT_UNCOVERED.value,
                "{0} declares a {1} obligation selected by {2!r} and the "
                "candidate collected no such case".format(
                    item.requirement_id, item.aspect, item.case_selector),
                standard)
        if item.skipped and not item.executed:
            return CoverageMeasurement(
                frozen, StrengthRefusal.OBLIGATION_ONLY_SKIPPED.value,
                "{0}'s {1} obligation selected only skipped cases ({2}); a "
                "skip is not executed behavioural evidence".format(
                    item.requirement_id, item.aspect,
                    ", ".join(item.skipped)),
                standard)
        if standard == PASSED and item.executed:
            return CoverageMeasurement(
                frozen, StrengthRefusal.POSITIVE_GATE_NOT_GREEN.value,
                "{0}'s {1} obligation needs {2} passing case(s) selected by "
                "{3!r}; {4} of {5} executed case(s) passed".format(
                    item.requirement_id, item.aspect, item.min_cases,
                    item.case_selector, len(item.passed), len(item.executed)),
                standard)
        return CoverageMeasurement(
            frozen, StrengthRefusal.OBLIGATION_UNMET.value,
            "{0}'s {1} obligation needs {2} executed case(s) selected by "
            "{3!r}; {4} of {5} selected case(s) reached a verdict".format(
                item.requirement_id, item.aspect, item.min_cases,
                item.case_selector, len(item.executed), len(item.selected)),
            standard)
    return CoverageMeasurement(frozen, standard=standard)


def adjudicate_negative_control(falsifiability: Any,
                                run: "CaseRun") -> FalsifiabilityResult:
    """Whether the negative control's run is discrimination proof.

    Four ways it is not, each named rather than folded into "red":

    * the runner produced no report — the control could not be executed, so
      it proved nothing about the cases;
    * a case errored — an import or collection crash is a broken tree, and
      counting it as the red we wanted is how a test that does not parse
      becomes evidence of missing behaviour;
    * the declared selector matched no case — the contract names cases the
      candidate does not have, which is unfalsifiable rather than falsified;
    * every selected case failed, but none for the declared reason — the tests
      are red about something else, and a random exception is not proof.
    """
    strategy = str(getattr(falsifiability, "strategy", ""))
    selector = falsifiability.expected_failing_selector
    pattern = falsifiability.expected_reason_pattern
    selected = run.selecting(selector)
    observed = tuple(o.reason for o in selected if o.reason)
    base = dict(strategy=strategy, executed=True, selected=tuple(
        o.nodeid for o in selected), observed_reasons=observed,
        control_command=tuple(run.command))
    if run.collection_failed:
        return FalsifiabilityResult(
            refusal=StrengthRefusal.CONTROL_COLLECTION_FAILED.value,
            reason=("the negative control produced no parseable report; a run "
                    "that did not execute proves nothing about these cases"),
            **base)
    errored = [o.nodeid for o in selected if o.status == "errored"]
    if errored:
        return FalsifiabilityResult(
            refusal=StrengthRefusal.CONTROL_IMPORT_CRASH.value,
            reason=("{0} errored under the negative control; an import or "
                    "collection crash is not the red this proves".format(
                        ", ".join(errored))),
            **base)
    if not selected:
        return FalsifiabilityResult(
            refusal=StrengthRefusal.CONTROL_NOT_SELECTED.value,
            reason=("the contract's expected_failing_selector {0!r} matched "
                    "no executed case; the control is unfalsifiable rather "
                    "than falsified".format(selector)),
            **base)
    surviving = [o.nodeid for o in selected if o.status != "failed"]
    if surviving:
        return FalsifiabilityResult(
            refusal=StrengthRefusal.CONTROL_NOT_RED.value,
            reason=("{0} did not fail under the negative control; a case that "
                    "survives the declared defect does not discriminate".format(
                        ", ".join(surviving))),
            **base)
    expression = re.compile(pattern)
    matched = tuple(o.nodeid for o in selected
                    if expression.search(o.reason or ""))
    if not matched:
        return FalsifiabilityResult(
            refusal=StrengthRefusal.CONTROL_WRONG_REASON.value,
            reason=("every selected case failed, but none for the declared "
                    "reason {0!r}; observed: {1}".format(
                        pattern,
                        "; ".join(observed) if observed else "(no reason "
                        "reported)")),
            **base)
    return FalsifiabilityResult(
        proven=True, failed_for_expected_reason=matched, **base)


# ── executing the negative control, in isolation, reversibly ────────────────


class NegativeControlUnexecutable(RuntimeError):
    """The declared control could not be set up. The machine, not the tests.

    Raised rather than returned as a refusal for the reason `TestsGitReadFailed`
    is: "git could not read this object" and "these tests do not discriminate"
    are different facts, and reporting the first as the second sends a tester
    to rewrite tests that were never the problem (§7.5).
    """


def _porcelain(tree: Path) -> str:
    """The working tree's dirt, as git states it. Compared before and after
    the control so 'nothing was mutated' is proven rather than asserted."""
    result = subprocess.run(
        ["git", "-C", str(tree), "status", "--porcelain"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise NegativeControlUnexecutable(
            "git status failed in {0}: {1}".format(tree, result.stderr.strip()))
    return result.stdout


def _materialize(repo: Path, commit: str, destination: Path) -> None:
    """Write `commit`'s tree into `destination`. Never touches `repo`."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        capture_output=True, check=False)
    if archive.returncode != 0:
        raise NegativeControlUnexecutable(
            "git archive {0} failed: {1}".format(
                commit, archive.stderr.decode("utf-8", "replace").strip()))
    extract = subprocess.run(
        ["tar", "-x", "-C", str(destination)],
        input=archive.stdout, capture_output=True, check=False)
    if extract.returncode != 0:
        raise NegativeControlUnexecutable(
            "extracting {0} failed: {1}".format(
                commit, extract.stderr.decode("utf-8", "replace").strip()))


def _revert_into(scratch: Path, repo: Path, base_commit: str,
                 paths: Sequence[str]) -> Tuple[str, ...]:
    """Restore `paths` in `scratch` to their `base_commit` content.

    A path absent at the base is deleted rather than left alone: "this code
    did not exist yet" is the behaviour the control is reverting to, and
    leaving the file in place would run the control against the very code it
    was supposed to remove.
    """
    reverted: List[str] = []
    for path in paths:
        blob = _blob_at(repo, base_commit, path)
        target = scratch / path
        if blob is None:
            if target.exists():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            reverted.append(path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        reverted.append(path)
    return tuple(reverted)


def execute_negative_control(
        *,
        falsifiability: Any,
        runner: CaseRunner,
        repo: Path,
        tree: Path,
        candidate_sha: str,
        base_commit: str,
        nodeids: Sequence[str],
        scratch_root: Optional[Path] = None,
        timeout_s: float = 300.0,
        already_executed: Optional["CaseRun"] = None,
) -> FalsifiabilityResult:
    """Run the declared negative control and adjudicate what it proved.

    Two strategies, one adjudicator.

    `baseline_absent` executes the candidate's own cases in the tests node's
    worktree. That tree *is* the baseline for this purpose and the property is
    structural rather than incidental: a tests node's evidence chain refuses
    any diff touching a non-test path, so everything in that worktree except
    the new test files is exactly its parent commit, and the implementation
    these cases were written to gate is absent from it by construction.

    `controlled_mutation` materialises the candidate commit into a scratch
    directory outside every worktree, reverts the declared paths to the plan's
    base commit there, and runs the cases against that. The user's checkout,
    the attempt worktree, and the shared integration worktree are never
    written — the mutation happens in a directory that did not exist a moment
    earlier and does not exist a moment later, which is what makes it
    reversible without a restore step that could itself fail.

    Cleanliness is proven either way by comparing `git status --porcelain` in
    the attempt worktree across the whole operation. A control that leaves
    dirt behind is refused by name rather than merged with a note.
    """
    strategy = str(getattr(falsifiability, "strategy", ""))
    before = _porcelain(tree)
    if strategy == "baseline_absent":
        # Reuse the caller's run when it has one. Under this strategy the
        # control *is* the same command over the same immutable tree that
        # coverage was measured from, so executing it twice would be a second
        # answer to one question as well as a second suite run -- and if the
        # two ever disagreed, nothing here could say which was the evidence.
        run = (already_executed if already_executed is not None
               else runner.run(tree, nodeids, timeout_s=timeout_s))
        after = _porcelain(tree)
        result = adjudicate_negative_control(falsifiability, run)
    elif strategy == "controlled_mutation":
        mutation = falsifiability.mutation
        holder = tempfile.mkdtemp(prefix="maestro-negative-control-",
                                  dir=str(scratch_root) if scratch_root else None)
        scratch = Path(holder) / "tree"
        try:
            _materialize(repo, candidate_sha, scratch)
            reverted = _revert_into(scratch, repo, base_commit,
                                    tuple(mutation.paths))
            run = runner.run(scratch, nodeids, timeout_s=timeout_s)
        finally:
            shutil.rmtree(holder, ignore_errors=True)
        after = _porcelain(tree)
        result = adjudicate_negative_control(falsifiability, run)
        result = replace(result, mutation_paths=reverted)
    else:
        return FalsifiabilityResult(
            strategy=strategy,
            refusal=StrengthRefusal.CONTROL_UNEXECUTABLE.value,
            reason=("no executable falsifiability strategy is declared for "
                    "this node; there is no default and none is inferred"))
    if after != before:
        return replace(
            result,
            proven=False,
            refusal=StrengthRefusal.CONTROL_NOT_ISOLATED.value,
            reason=("the negative control left the attempt worktree dirty; "
                    "before={0!r} after={1!r}".format(before, after)))
    return result


# ── the durable evidence, and the verdict computed from it ──────────────────


#: Refusals that name the machine rather than the tests. A tester handed one
#: of these would be asked to fix something it did not break, so they retry as
#: ENVIRONMENTAL and never spend the semantic budget.
_ENVIRONMENTAL_REFUSALS: Tuple[str, ...] = (
    StrengthRefusal.CONTROL_COLLECTION_FAILED.value,
    StrengthRefusal.CONTROL_UNEXECUTABLE.value,
    StrengthRefusal.RUNNER_UNSUPPORTED.value,
)


@dataclass(frozen=True)
class GateStrengthEvidence:
    """One test candidate's measured gate strength — the whole durable record.

    Bound to `candidate_sha` rather than to the node, because the acceptance
    it supports is an acceptance of *those bytes*. A later or substituted test
    tree has a different sha, finds no row, and cannot inherit this evidence
    (obligation 9).
    """

    tests_node_id: str
    candidate_sha: str
    runner: str
    selector: str
    contract_declared: bool
    executed_nodeids: Tuple[str, ...] = ()
    new_nodeids: Tuple[str, ...] = ()
    coverage: CoverageMeasurement = field(default_factory=CoverageMeasurement)
    falsifiability: FalsifiabilityResult = field(
        default_factory=FalsifiabilityResult)
    #: The threshold the paired builder will be judged on, from the node's own
    #: `gate_min_cases`. Compared against the count these bytes actually
    #: collect, which is the whole point: accepting this candidate **freezes**
    #: that count, because `compare_test_bytes` makes the builder carry these
    #: bytes verbatim. A gate above it is unsatisfiable by construction from
    #: this moment on, and every builder dispatched against it afterwards is
    #: dispatched against a gate no correct attempt can pass.
    #:
    #: `run-8d1a71f463e4430f92a125a8f8b3731d` is the recorded cost: a merged
    #: two-case file under a five-case gate, four builders, all four forging
    #: the gate rather than failing.
    #:
    #: `UNSTATED` is the default so that adding this field could not, by
    #: itself, break a construction that predates it. It is deliberately not
    #: a silent pass: it lands in `as_mapping()` and therefore in the ledger
    #: row, where a `gate_min_cases` of zero is visible evidence that a caller
    #: never stated the threshold, and
    #: `tests/test_gate_floor_supplied.py` fails if either scheduler call site
    #: stops passing it. §3.6 B8 is why that guard exists: a field added later
    #: is optional forever unless something refuses the absence.
    gate_min_cases: int = UNSTATED_GATE_FLOOR

    @property
    def collected_case_count(self) -> int:
        """Distinct cases these bytes collect — the frozen supply.

        Distinct, because a node id is unique per run under both supported
        runners, so a repeat can only be a measurement artefact and counting
        it twice would report supply that does not exist.
        """
        return len(set(self.executed_nodeids))

    @property
    def gate_floor_reachable(self) -> bool:
        """Whether the frozen supply reaches the threshold, or is unasked.

        An `UNSTATED` threshold answers True. That is the compatibility arm
        and not a judgment: nothing was compared, so nothing was refused, and
        the zero in the ledger row is what says so.
        """
        if self.gate_min_cases <= UNSTATED_GATE_FLOOR:
            return True
        return self.collected_case_count >= self.gate_min_cases

    @property
    def refusal(self) -> Optional[str]:
        """The one named reason this candidate is not strong, or None.

        Total by construction: strength and its refusal are one fact, and a
        candidate that is not strong for a reason nobody named is exactly the
        state the ledger's `strong XOR refusal` CHECK exists to make
        unrepresentable. An unproven control with no refusal of its own is
        `CONTROL_UNEXECUTABLE` -- the control did not run.
        """
        if not self.contract_declared:
            return StrengthRefusal.CONTRACT_ABSENT.value
        named = self.coverage.refusal or self.falsifiability.refusal
        if named is not None:
            return named
        if not self.falsifiability.proven:
            return StrengthRefusal.CONTROL_UNEXECUTABLE.value
        # Last, and deliberately so. This candidate's tests are strong by
        # every measure above; what is wrong is the *pair*. Reporting it
        # ahead of a coverage or control refusal would bury the more
        # actionable defect under one about a sibling's threshold.
        if not self.gate_floor_reachable:
            return StrengthRefusal.GATE_FLOOR_UNREACHABLE.value
        return None

    @property
    def strong(self) -> bool:
        return self.refusal is None

    def as_mapping(self) -> Dict[str, Any]:
        return {
            "tests_node_id": self.tests_node_id,
            "candidate_sha": self.candidate_sha,
            "runner": self.runner,
            "selector": self.selector,
            "contract_declared": self.contract_declared,
            "executed_nodeids": list(self.executed_nodeids),
            "new_nodeids": list(self.new_nodeids),
            "coverage": self.coverage.as_mapping(),
            "falsifiability": self.falsifiability.as_mapping(),
            "gate_min_cases": self.gate_min_cases,
            "collected_case_count": self.collected_case_count,
            "gate_floor_reachable": self.gate_floor_reachable,
            "refusal": self.refusal,
            "strong": self.strong,
        }


def verify_test_strength(evidence: GateStrengthEvidence
                         ) -> vf.VerificationVerdict:
    """The tests-node acceptance predicate, computed from measured evidence.

    Never from an envelope field, a report the tester wrote, or a pass count.
    Every input is either a git object or a runner's own machine-readable
    output, which is what lets a lifecycle transition key on it (§1.2).
    """
    if not evidence.contract_declared:
        return _strength_refused(
            StrengthRefusal.CONTRACT_ABSENT.value,
            "{0} declares no test-strength contract; a tests node accepted "
            "without one is accepted on its case count, which is what "
            "run-8d1a71f463e4430f92a125a8f8b3731d shows does not "
            "discriminate".format(evidence.tests_node_id))
    if evidence.coverage.refusal:
        return _strength_refused(evidence.coverage.refusal,
                                 evidence.coverage.reason)
    if evidence.falsifiability.refusal:
        return _strength_refused(evidence.falsifiability.refusal,
                                 evidence.falsifiability.reason)
    if not evidence.falsifiability.proven:
        return _strength_refused(
            StrengthRefusal.CONTROL_UNEXECUTABLE.value,
            "the declared negative control was not executed, so nothing "
            "proves these cases can fail")
    if not evidence.gate_floor_reachable:
        return _strength_refused(
            StrengthRefusal.GATE_FLOOR_UNREACHABLE.value,
            "these bytes collect {0} case(s) and the gate this pair is judged "
            "on declares min_cases={1}. Accepting them freezes the count at "
            "{0}, because the build node must carry them verbatim, so no "
            "correct implementation could pass that gate. Add {2} more "
            "case(s) to {3}, or re-ship the plan with a lower min_cases; do "
            "not accept and let a builder discover it".format(
                evidence.collected_case_count, evidence.gate_min_cases,
                evidence.gate_min_cases - evidence.collected_case_count,
                evidence.tests_node_id))
    return vf.VerificationVerdict(verified=True)


def _strength_refused(code: str, detail: str) -> vf.VerificationVerdict:
    retry_class = (st.RetryClass.ENVIRONMENTAL
                   if code in _ENVIRONMENTAL_REFUSALS
                   else st.RetryClass.SEMANTIC)
    # Typed for the same reason `_refused` stamps its member: strength
    # refusals are measured, so an identical repeat is a convergence fact
    # `refusal_repetition` is allowed to act on. An ENVIRONMENTAL strength
    # refusal still carries its code, but never reaches the repetition check —
    # only SEMANTIC rows enter its history (§7.5).
    #
    # `code` arrives as the durable string (`CoverageMeasurement.refusal` and
    # `FalsifiabilityResult.refusal` store values, not members); resolving it
    # back through the enum is what makes an unknown code a loud ValueError
    # here rather than a verdict that silently ships without its remedy.
    member = StrengthRefusal(code)
    return vf.VerificationVerdict(
        verified=False, failed_clause=3,
        reason="{0}: {1}".format(code, detail),
        retry_class=retry_class,
        refusal_code=member.value,
        remedy=member.remedy)


def blob_id_at(tree: Path, commit: str, path: str) -> Optional[str]:
    """The git object id of `path` at `commit`, or None when absent.

    An object id rather than the bytes, because the question this answers is
    identity: are the test files in the implementation candidate's tree the
    *exact* bytes the accepted test candidate was reviewed as. Comparing ids
    is that question asked directly, and it does not read a blob into memory
    to answer it.

    Absence is an empty successful ls-tree, never a failed exit (§7.5).
    """
    listed = subprocess.run(
        ["git", "-C", str(tree), "ls-tree", "-z", "--full-tree", commit,
         "--", path],
        capture_output=True)
    if listed.returncode != 0:
        raise TestsGitReadFailed(
            "GIT_READ_FAILED:ls-tree {0} -- {1}".format(commit, path))
    records = [record for record in listed.stdout.split(b"\x00") if record]
    if len(records) != 1:
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
    return object_id.decode("ascii", "replace")


class PairingRefusal(_RemediedRefusal):
    """Why an implementation candidate is not bound to accepted test bytes.

    The audience for these remedies is the *builder*, not the tester: a
    pairing refusal lands on the implementation lane's retry prompt. Where
    one code covers several measured sub-causes (`GATE_NOT_GREEN` has
    three), the member carries the remedy for the ordinary one and the
    raising site overrides with the specific one — the override is still
    deterministic text declared at a raise site, never inferred later.
    """

    NO_ACCEPTED_CANDIDATE = (
        "TEST_PAIRING_NO_ACCEPTED_CANDIDATE",
        "Nothing in this diff can satisfy the check yet: the tests node "
        "this lane is gated by has no accepted test candidate, so there is "
        "nothing to be gated against. The pairing is re-checked once a test "
        "candidate is accepted; if none ever is, the tests lane — not this "
        "one — is where the work is.")
    BYTES_SUBSTITUTED = (
        "TEST_PAIRING_TEST_BYTES_SUBSTITUTED",
        "Restore every differing path the verdict names to the accepted "
        "test candidate's exact bytes. This node may not edit, delete, "
        "regenerate, or reformat reviewed test files — the acceptance those "
        "tests carry is an acceptance of those bytes. Make the "
        "implementation satisfy the tests as reviewed, never the tests fit "
        "the implementation.")
    GATE_NOT_GREEN = (
        "TEST_PAIRING_ACCEPTED_TESTS_NOT_GREEN",
        "Make every case the accepted test candidate's obligations select "
        "pass against this candidate: run the accepted selector locally, "
        "read each failure, and change the implementation until the "
        "selected cases are green — without touching the test files, which "
        "must stay byte-identical to the accepted candidate.")
    UNREADABLE = (
        "TEST_PAIRING_TEST_TREE_UNREADABLE",
        "This names the machine, not the diff: a git read or "
        "case-collection step failed while checking the pairing, so "
        "nothing about the implementation was judged. The retry re-runs "
        "the check unchanged; if it recurs, an operator must inspect the "
        "repository or runner fault named in the verdict.")


def compare_test_bytes(tree: Path, accepted_sha: str, candidate_sha: str,
                       paths: Sequence[str]) -> Tuple[str, ...]:
    """Paths whose bytes differ between the two commits, or are absent now.

    The returned tuple is the refusal's evidence: an implementation candidate
    that edited, deleted, or replaced a file of the accepted test candidate
    is not being gated by the tests that were reviewed, and inheriting that
    review would be exactly obligation 9's substitution.
    """
    differing: List[str] = []
    for path in paths:
        accepted = blob_id_at(tree, accepted_sha, path)
        current = blob_id_at(tree, candidate_sha, path)
        if accepted != current:
            differing.append(path)
    return tuple(differing)
