"""Test-author / test-reviewer / acceptance payloads as LaneArtifact.

Does not write lane_state. A test draft is an ordinary candidate: its files are
committed on the integration head in the run repository, admitted through the
same `admit_candidate` path a builder's candidate takes, and pinned at a
candidate ref. Acceptance records the candidate and the `test_binding` digest
of its files; nothing is hidden from anyone.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from . import git_publication as gitpub
from . import review_contract as rc
from . import runner_resolution as rr
from . import scheduler_types as st
from . import test_binding as tb
from . import tree_materialize as tm

_PYTEST_TOTALS = re.compile(r"(\d+)\s+(passed|failed|errors?|error|skipped|xfailed)")

#: vitest's totals line, anchored on its shape rather than on containing the
#: word `Tests`.
#:
#: vitest writes its summary to STDOUT and its failure banner to STDERR:
#:
#:     stdout:       Tests  1 failed | 1 passed (2)
#:     stderr: ⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯
#:
#: `rr.execute_cases` returns `stdout + "\n" + stderr`, so on any failing run
#: the banner lands AFTER the summary no matter what order they were printed
#: in. Scanning backwards for the last line containing `Tests` therefore found
#: the banner, which carries no `N failed` token, and every failing vitest
#: suite parsed to all zeros — `executed: 0` for a run that executed eleven
#: cases. That did not read as a pass (a banner implies failures implies a
#: non-zero exit, which `code_review` already treats as failed), but it wrote a
#: wrong `public_result_summary` into the ledger and neutered the
#: `executed < min_cases` check on exactly the runs it exists for.
#:
#: This was invisible to a shell measurement: running vitest with `2>&1`
#: interleaves the streams in real time and puts the banner FIRST, which parses
#: correctly. Only the harness's separate capture reorders them.
#:
#: The summary begins the line (leading whitespace only); the banner has
#: box-drawing characters and the word `Failed` before `Tests`. `Test Files`
#: does not match either, because the word there is `Test`.
#:
#: Shape alone was not enough. vitest also writes, to STDERR, after a suite
#: that passed:
#:
#:     Tests closed successfully but something prevents Vite server from exiting
#:
#: which begins the line with `Tests ` exactly as the summary does, and lands
#: after it in the combined capture. Measured 2026-09-03 on FDAdb
#: `lane-wp7-page-build`, whose config boots the Astro pipeline and leaves the
#: Vite server open: the suite ran 15 cases and exited 0, the reverse scan took
#: that warning, and every count parsed as zero -- refused
#: `SUITE_COUNTS_UNPARSEABLE` against a candidate whose tests all
#: passed. Feeding stdout alone to the parser returns 15; the combined capture
#: returns 0.
#:
#: So the line must also CARRY a count. Every real totals line has at least one
#: `N passed`/`N failed`/`N skipped`; no warning does. A suite that genuinely
#: executed nothing still refuses, which is the point of the check.
_VITEST_SUMMARY = re.compile(r"^\s*Tests\s+.*?\d+\s+(?:passed|failed|skipped)\b")

#: The commit message of every test draft. A draft commit is content-addressed
#: (`git_publication.commit_files_on_base` fixes author, committer and date),
#: so identical bytes on the same base reach the identical sha every time:
#: a redraft of identical files is the same candidate, and a crash between
#: committing and recording re-derives the same commit rather than a second.
DRAFT_COMMIT_MESSAGE = b"maestro test draft\n"


def suite_files(artifact: st.LaneArtifact) -> dict[str, str]:
    """The accepted suite as a path-to-blob map, off a TEST_DRAFT or ACCEPTED_TEST_SUITE."""
    if artifact.kind not in (
        st.ArtifactKind.TEST_DRAFT,
        st.ArtifactKind.ACCEPTED_TEST_SUITE,
    ):
        raise rc.ReviewContractError("suite files require TEST_DRAFT or ACCEPTED_TEST_SUITE")
    files = artifact.payload.get("files")
    if not isinstance(files, Mapping) or not files:
        raise rc.ReviewContractError("test artifact carries no files")
    return {str(path): str(blob) for path, blob in files.items()}


def read_suite(repo: Path, files: Mapping[str, str]) -> dict[str, str]:
    """The suite's bodies, read out of the run repository's object database."""
    return {
        path: tm.cat_blob(Path(repo), blob).decode("utf-8", errors="replace")
        for path, blob in files.items()
    }


def write_test_draft(
    *,
    request: rc.LaneRequest,
    binding: gitpub.TargetBinding,
    integration_head: str,
    files: Mapping[str, str],
    public_contract: Mapping[str, object],
    declared_outputs: Sequence[str] | None = None,
) -> st.LaneArtifact:
    """Commit the tester's files on the integration head and admit the candidate.

    `declared_outputs` is what the draft may own. A typed tests lane's is its
    lane's declared outputs, which `FactoryScheduler._require_typed_test_outputs`
    has already proved equal to the written set. An untyped lane's tests sit at
    paths of the tester's choosing, so its draft owns exactly what it wrote.
    """
    contract = rc.public_contract(
        acceptance_criteria=rc.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=rc.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
    )
    written = {rc.normalize_repo_path(path): body for path, body in files.items()}
    if not written:
        raise rc.ReviewContractError("test draft files are empty")
    owned = tuple(
        sorted(
            {rc.normalize_repo_path(item) for item in declared_outputs}
            if declared_outputs is not None
            else set(written)
        )
    )
    st.require_git_sha(integration_head, name="integration_head")
    candidate = gitpub.commit_files_on_base(
        binding,
        base_sha=integration_head,
        files={path: body.encode("utf-8") for path, body in written.items()},
        message=DRAFT_COMMIT_MESSAGE,
    )
    git = binding.git()
    # A tests lane merges its accepted suite, so a lane re-drafted after an
    # amendment can write bytes the integration head already carries. That
    # draft changes nothing: it is the head itself, admitted `changed=false`,
    # and it takes the same zero-delta merge edge an unchanged build
    # candidate does. Committing it anyway would be an empty commit, which
    # admission refuses as `changed=true empty delta`.
    changed = git.commit_tree_oid(candidate) != git.commit_tree_oid(integration_head)
    if not changed:
        candidate = integration_head
    admitted = gitpub.admit_candidate(
        binding,
        run_id=request.run_id,
        lane_id=request.lane_id,
        input_digest=request.input_digest,
        builder_base_sha=integration_head,
        candidate_sha=candidate,
        changed=changed,
        declared_outputs=owned,
    )
    blobs: dict[str, str] = {}
    for path in sorted(written):
        blob = git.tree_blob(candidate, path)
        if blob is None:
            raise rc.ReviewContractError(
                "draft commit does not carry {0}".format(path)
            )
        blobs[path] = blob
    payload = {
        "builder_base_sha": admitted["builder_base_sha"],
        "candidate_ref": admitted["candidate_ref"],
        "candidate_sha": admitted["candidate_sha"],
        "changed": admitted["changed"],
        "files": blobs,
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "public_contract": contract,
        "test_suite_digest": tb.suite_digest(blobs),
    }
    return rc.make_lane_artifact(
        kind=st.ArtifactKind.TEST_DRAFT,
        request=request,
        payload=payload,
        artifact_ref=admitted["candidate_ref"],
    )


def review_test_draft(
    *,
    request: rc.LaneRequest,
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]] = (),
    test_draft: st.LaneArtifact,
) -> st.LaneArtifact:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise rc.ReviewContractError("test review requires TEST_DRAFT")
    findings_out = rc.actionable_findings(verdict, findings)
    payload = {
        "findings": [dict(item) for item in findings_out],
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "verdict": verdict.value,
    }
    return rc.make_lane_artifact(
        kind=st.ArtifactKind.TEST_REVIEW,
        request=request,
        payload=payload,
        artifact_ref="test-review:{0}".format(request.input_digest),
        verdict=verdict,
    )


def accept_tests(
    *,
    request: rc.LaneRequest,
    test_draft: st.LaneArtifact,
    test_review: st.LaneArtifact,
) -> st.LaneArtifact:
    """Pin the accepted draft as the suite every later stage is bound to.

    Nothing is re-committed and nothing moves: the candidate the reviewer
    read is the candidate that is accepted, and `test_suite_digest` is the
    `test_binding` digest of exactly its files. Every site that runs the suite
    verifies the tree it runs in against these blobs first (`TEST_SUITE_TAMPERED`),
    and every candidate a build lane submits is refused at admission if its
    delta names one of these paths (`CANDIDATE_TEST_PATH_REFUSED`).
    """
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise rc.ReviewContractError("acceptance requires TEST_DRAFT")
    if test_review.kind is not st.ArtifactKind.TEST_REVIEW:
        raise rc.ReviewContractError("acceptance requires TEST_REVIEW")
    if test_review.verdict is not st.ReviewerVerdict.PASS:
        raise rc.ReviewContractError("acceptance requires TEST_REVIEW PASS")
    files = suite_files(test_draft)
    digest = tb.suite_digest(files)
    if test_draft.payload.get("test_suite_digest") != digest:
        raise rc.ReviewContractError("test draft digest does not match its files")
    payload = {
        "builder_base_sha": test_draft.payload["builder_base_sha"],
        "candidate_ref": test_draft.payload["candidate_ref"],
        "candidate_sha": test_draft.payload["candidate_sha"],
        "changed": bool(test_draft.payload.get("changed", True)),
        "files": files,
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "public_contract": test_draft.payload["public_contract"],
        "test_suite_digest": digest,
    }
    return rc.make_lane_artifact(
        kind=st.ArtifactKind.ACCEPTED_TEST_SUITE,
        request=request,
        payload=payload,
        artifact_ref=str(test_draft.payload["candidate_ref"]),
    )


#: Which runner a test file's name names. `.py` is pytest outright; a
#: JavaScript or TypeScript file only names vitest when it is a test file, so a
#: `.ts` helper accepted beside a `.test.ts` suite does not get a vote of its own.
#: Anything else votes for nothing.
_PYTEST_SUFFIX = ".py"
_VITEST_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_VITEST_STEMS = (".test", ".spec")


def _file_runner(path: str) -> str | None:
    """The runner a single test file names, or `None` when it names none."""
    name = str(path).rsplit("/", 1)[-1]
    if name.endswith(_PYTEST_SUFFIX):
        return "pytest"
    for extension in _VITEST_EXTENSIONS:
        if name.endswith(extension):
            stem = name[: -len(extension)]
            if stem.endswith(_VITEST_STEMS):
                return "vitest"
            return None
    return None


def _derive_runner(files: Sequence[str]) -> str:
    """The one runner a test file set names, or a refusal.

    Nothing in the artifact-factory plan schema can supply a gate —
    `plan_model.LANE_KEYS` has no `gate` key and `plan_validate` refuses a lane
    carrying one — so every suite arrives here with `gate=None`. This
    used to answer `pytest` unconditionally, which ran a vitest suite under
    pytest: `found no collectors for …/paid-dpa.test.ts`, exit 4, zero cases
    executed, which `code_review` reads as a failed suite and turns a
    reviewer PASS into REVISE. Deriving is the fix; guessing is what broke it,
    so an ambiguous or unreadable file set refuses instead of picking a side.
    """
    named = {runner for runner in map(_file_runner, files) if runner is not None}
    if len(named) == 1:
        return named.pop()
    suffixes = sorted(
        {"." + str(path).rsplit(".", 1)[-1] for path in files if "." in str(path)}
    )
    if not named:
        raise rc.SuiteEnvironmentError(
            "SUITE_RUNNER_UNDERIVABLE: no test file names a runner "
            "(suffixes: {0})".format(", ".join(suffixes) or "none")
        )
    raise rc.SuiteEnvironmentError(
        "SUITE_RUNNER_AMBIGUOUS: test files name more than one runner "
        "({0}; suffixes: {1})".format(", ".join(sorted(named)), ", ".join(suffixes))
    )


def _suite_gate(gate: Any, files: Sequence[str]) -> SimpleNamespace:
    if gate is None:
        return SimpleNamespace(
            runner=_derive_runner(files),
            argv=tuple(files),
            cwd=".",
            min_cases=1,
        )
    if isinstance(gate, SimpleNamespace):
        return gate
    if not isinstance(gate, Mapping):
        raise rc.ReviewContractError("suite gate is not a mapping")
    runner = gate.get("runner")
    if runner not in rr.EXECUTE_ARGS:
        raise rc.ReviewContractError("unsupported suite runner")
    min_cases = gate.get("min_cases")
    if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:
        raise rc.ReviewContractError("min_cases")
    argv = gate.get("argv") or ()
    if not isinstance(argv, (list, tuple)):
        raise rc.ReviewContractError("argv")
    cwd = gate.get("cwd") or "."
    if not isinstance(cwd, str) or not cwd:
        raise rc.ReviewContractError("cwd")
    return SimpleNamespace(
        runner=str(runner),
        argv=tuple(str(item) for item in argv),
        cwd=cwd,
        min_cases=int(min_cases),
    )


# ── the interpreter the project declares ────────────────────────────────────
#
# Resolving pytest through `rr.resolve` finds a project-local binary, which is
# most of the answer. It is not all of it: a project may declare
# `requires-python = ">=3.12"` in its `pyproject.toml` while the only pytest on
# the machine runs 3.9. That combination cannot import the project at all, and
# the way it fails is the problem — `executed == 0`, which `code_review` reads
# as "suite failed, errored, or did not execute" and reports
# against the BUILDER. No builder can raise the harness's Python version, so
# this refuses with its own typed error naming the environment instead.
#
# `tomllib` is stdlib from 3.11. On an older harness the version assertion is
# skipped rather than crashing, and no third-party parser is introduced for it.
try:  # pragma: no cover - exercised by whichever interpreter runs the suite
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

_VERSION_PROBE = "import sys;print('%d.%d.%d' % sys.version_info[:3])"
_VERSION_TIMEOUT_S = 30.0
_SPECIFIER_CLAUSE = re.compile(r"^(===|==|!=|~=|>=|<=|>|<)\s*([0-9A-Za-z_.*+!-]+)$")


def _release(text: str) -> tuple[int, ...] | None:
    """The leading numeric release segments of a version, or `None`."""
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(text))
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _pad(release: Sequence[int], length: int) -> tuple[int, ...]:
    parts = tuple(release)
    if len(parts) >= length:
        return parts[:length]
    return parts + (0,) * (length - len(parts))


def _compare(left: Sequence[int], right: Sequence[int]) -> int:
    width = max(len(tuple(left)), len(tuple(right)))
    one, two = _pad(left, width), _pad(right, width)
    return (one > two) - (one < two)


def _prefix_equal(version: Sequence[int], want: Sequence[int]) -> bool:
    return _pad(version, len(tuple(want))) == tuple(want)


def _satisfies(version: Sequence[int], specifier: str) -> bool | None:
    """Whether `version` meets a PEP 440 specifier set, or `None` if unreadable.

    Deliberately small: Python's own version is always a numeric release, so
    the epoch, pre/post/dev, and local-version rules have nothing to bite on
    here. Anything this does not understand — including `===` arbitrary
    equality — returns `None`, and the caller skips the assertion rather than
    refusing a run over a specifier it cannot read.
    """
    for clause in str(specifier).split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = _SPECIFIER_CLAUSE.match(clause)
        if match is None:
            return None
        operator, raw = match.group(1), match.group(2)
        if operator == "===":
            return None
        wildcard = raw.endswith(".*")
        want = _release(raw[:-2] if wildcard else raw)
        if want is None or (wildcard and operator not in ("==", "!=")):
            return None
        if operator == "~=":
            if len(want) < 2:
                return None
            ok = _compare(version, want) >= 0 and _prefix_equal(version, want[:-1])
        elif operator == "==":
            ok = (
                _prefix_equal(version, want)
                if wildcard
                else _compare(version, want) == 0
            )
        elif operator == "!=":
            ok = not (
                _prefix_equal(version, want)
                if wildcard
                else _compare(version, want) == 0
            )
        elif operator == ">=":
            ok = _compare(version, want) >= 0
        elif operator == "<=":
            ok = _compare(version, want) <= 0
        elif operator == ">":
            ok = _compare(version, want) > 0
        else:
            ok = _compare(version, want) < 0
        if not ok:
            return False
    return True


def _nearest_pyproject(start: Path, stop: Path) -> Path | None:
    """The closest `pyproject.toml` at or above `start`, bounded by `stop`."""
    current = Path(start).resolve()
    boundary = Path(stop).resolve()
    if current != boundary and boundary not in current.parents:
        return None
    while True:
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if current == boundary or current.parent == current:
            return None
        current = current.parent


def _requires_python(project: Path) -> str | None:
    if tomllib is None:
        return None
    try:
        with Path(project).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    table = data.get("project")
    if not isinstance(table, Mapping):
        return None
    value = table.get("requires-python")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _python_requirements(
    base: Path, cwd: str, files: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """`(pyproject path, requires-python)` for every project the suite touches.

    The gate's own `cwd` and each test file's directory are both walked
    upward, because a monorepo declares `requires-python` beside the service
    under test (`services/api-gateway/pyproject.toml`) rather than at the root
    the gate happens to run from.
    """
    if tomllib is None:
        return ()
    base = Path(base)
    starts = [base / (cwd or ".")]
    for path in files:
        try:
            relative = rc.normalize_repo_path(str(path))
        except rc.ReviewContractError:
            continue
        starts.append((base / relative).parent)
    found: dict[str, str] = {}
    for start in starts:
        project = _nearest_pyproject(start, base)
        if project is None:
            continue
        specifier = _requires_python(project)
        if specifier:
            found.setdefault(str(project), specifier)
    return tuple(sorted(found.items()))


def _interpreter_argv(resolved: rr.ResolvedRunner) -> tuple[str, ...] | None:
    """How to invoke the interpreter `resolved` runs under, or `None`.

    Three shapes, in falling order of certainty: an environment launcher
    (`uv run pytest` -> `uv run python`), the interpreter sitting beside the
    console script (`.venv/bin/pytest` -> `.venv/bin/python`), and the absolute
    path in the console script's own shebang. `None` means the interpreter
    could not be identified, and the caller then skips the version assertion
    rather than refusing a runner it has not measured.
    """
    prefix = tuple(resolved.argv_prefix)
    if not prefix:
        return None
    if len(prefix) > 1:
        if prefix[-1] != resolved.runner:
            return None
        return prefix[:-1] + ("python",)
    executable = Path(prefix[0])
    for name in ("python", "python3"):
        sibling = executable.parent / name
        if sibling.is_file() and os.access(str(sibling), os.X_OK):
            return (str(sibling),)
    try:
        with executable.open("rb") as handle:
            first = handle.readline()
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    tokens = first[2:].decode("utf-8", "replace").strip().split()
    if not tokens:
        return None
    head = Path(tokens[0])
    if head.is_absolute() and head.is_file() and os.access(str(head), os.X_OK):
        return (str(head),)
    return None


def _interpreter_release(argv: Sequence[str], cwd: Path) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(
            list(argv) + ["-c", _VERSION_PROBE],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _release((result.stdout or "").strip())


def _assert_declared_python(
    resolved: rr.ResolvedRunner,
    root: Path,
    tree: Path,
    cwd: str,
    files: Sequence[str],
) -> None:
    """Refuse a pytest whose interpreter the project's own metadata rejects."""
    if resolved.runner != "pytest":
        return
    requirements = _python_requirements(root, cwd, files)
    if not requirements:
        requirements = _python_requirements(tree, cwd, files)
    if not requirements:
        return
    argv = _interpreter_argv(resolved)
    if argv is None:
        return
    working = Path(root) / (cwd or ".")
    version = _interpreter_release(argv, working if working.is_dir() else Path(root))
    if version is None:
        return
    found = ".".join(str(part) for part in version)
    for project, specifier in requirements:
        if _satisfies(version, specifier) is not False:
            continue
        raise rc.SuiteEnvironmentError(
            "SUITE_PYTHON_UNSUPPORTED: the suite resolved pytest to "
            "{0}, running Python {1}, which does not satisfy requires-python "
            "{2!r} declared in {3}. This is a harness environment fault: the "
            "candidate under test was never executed, and no change to it can "
            "fix this.".format(" ".join(resolved.argv_prefix), found, specifier, project)
        )


def _suite_selectors(
    gate: SimpleNamespace, files: Sequence[str], tree: Path
) -> tuple[str, ...]:
    argv, selectors = rc.substituted_gate_argv(gate.argv, files, tree)
    if gate.runner == "pytest":
        return (
            "--rootdir",
            ".",
            # Verbosity 2, not `-q`. Below it pytest truncates a dict
            # comparison to `Omitting N identical items, use -vv to show` and
            # renders both sides identically, so the only forwardable line
            # said a dict differed from itself. Verified against the real
            # binary that `_parse_suite_counts` still reads the summary line
            # at this verbosity.
            "-vv",
            # One location-and-exception line per failure. `--tb=no` printed
            # nothing but pytest's short summary, which names the case and
            # not the assertion; the builder needs the assertion. Verified
            # against the real binary: this yields
            # `path:LINE: AttributeError: ...`.
            "--tb=line",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--",
        ) + selectors
    return argv

def _parse_suite_counts(runner: str, output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    if runner == "pytest":
        summary = ""
        for line in reversed(output.splitlines()):
            if " in " in line and _PYTEST_TOTALS.search(line):
                summary = line
                break
        if summary:
            for count, label in _PYTEST_TOTALS.findall(summary):
                key = "errored" if label.startswith("error") else label
                if key == "xfailed":
                    continue
                counts[key] = int(count)
        return counts
    tests_line = ""
    for line in reversed(output.splitlines()):
        if _VITEST_SUMMARY.match(line):
            tests_line = line
            break
    if tests_line:
        failed = re.search(r"(\d+)\s+failed", tests_line)
        passed = re.search(r"(\d+)\s+passed", tests_line)
        skipped = re.search(r"(\d+)\s+skipped", tests_line)
        if failed:
            counts["failed"] = int(failed.group(1))
        if passed:
            counts["passed"] = int(passed.group(1))
        if skipped:
            counts["skipped"] = int(skipped.group(1))
    return counts


def run_suite(
    tree: Path,
    paths: Sequence[str],
    *,
    gate: Any = None,
    timeout_s: float = 120.0,
) -> dict:
    files = tuple(paths)
    bound = _suite_gate(gate, files)
    # The tree runs with what provisioning installed in it and nothing else.
    # Nothing is bridged in from the product checkout: a dependency that is
    # missing here is missing for every actor tree too, and belongs to the
    # round-1 runner preflight, not to this measurement.
    try:
        # Resolve the runner against the materialized execution environment.
        # Only the path operands. `_suite_selectors` returns a whole
        # invocation -- `--rootdir . -vv --tb=line …` -- and those flags are
        # this suite's reporting shape, not the probe's question.
        _argv, selectors = rc.substituted_gate_argv(bound.argv, files, Path(tree))
        resolved = rr.resolve(
            bound.runner, Path(tree), bound.cwd, paths=selectors
        )
    except rr.RunnerUnusable as extra:
        # The measurement travels with the refusal. `SUITE_RUNNER_UNUSABLE:
        # vitest` alone cannot tell UNRESOLVED (the runner was never installed --
        # repair `provision_argv`) from INCAPABLE (it is installed and cannot
        # resolve this project's config -- repair the config or its deps), and
        # those have opposite repairs. `detail` names the reason, the candidates
        # tried, the resolved binary, the probe exit and the probe's own words,
        # verbatim: the suite is visible, so nothing in a probe's output is a
        # secret to anyone who reads this.
        detail = getattr(extra, "detail", "") or str(extra)
        raise rc.SuiteEnvironmentError(
            "SUITE_RUNNER_UNUSABLE:{0}: {1}".format(bound.runner, detail)
        ) from extra
    _assert_declared_python(resolved, Path(tree), Path(tree), bound.cwd, files)
    exec_gate = SimpleNamespace(
        runner=bound.runner,
        argv=_suite_selectors(bound, files, Path(tree)),
        cwd=bound.cwd,
        min_cases=bound.min_cases,
    )
    raw = rr.execute_cases(
        resolved,
        exec_gate,
        tree,
        timeout_s=timeout_s,
    )
    output = str(raw.get("output") or "")
    returncode = int(raw.get("returncode") or 0)
    counts = _parse_suite_counts(bound.runner, output)
    executed = (
        counts["passed"] + counts["failed"] + counts["errored"] + counts["skipped"]
    )
    if returncode == 0 and executed < 1:
        # A runner that exited 0 while the parser found no cases has produced a
        # measurement this code cannot read. It is not a pass.
        #
        # This used to credit `executed = min_cases; counts["passed"] = executed`
        # for every runner except pytest. No plan can declare a gate, so
        # `min_cases` is always 1, and the moment vitest became reachable that
        # fabrication turned "vitest executed nothing" into a green suite
        # binding the candidate — `code_review`'s `executed < min_cases` check
        # cannot fire against a count this function invented. A false REVISE is
        # loud and costs builder rounds; a false green is silent and ships.
        #
        # Measured before deleting it, because a fallback that covers a real
        # case must not be removed blind. vitest's default reporter prints its
        # `Tests` summary line on every terminating run — `Tests  2 passed (2)`,
        # `Tests  1 failed | 1 passed (2)`, `Tests  2 skipped (2)` — on 3.2.7
        # and 4.1.11 alike, and the two shapes that print no readable count
        # (an empty test file, a filter matching nothing) both exit non-zero and
        # never reach here. The fallback was covering nothing.
        #
        # pytest needs no exemption from the same rule: it cannot exit 0 having
        # collected nothing, because that is exit 5 (NO_TESTS_COLLECTED). Both
        # runners now fail closed identically.
        raise rc.SuiteEnvironmentError(
            "SUITE_COUNTS_UNPARSEABLE:{0}: the suite's runner "
            "exited 0 but reported no executed cases, so how many cases ran "
            "could not be measured. An unreadable measurement is not a pass "
            "and is not a defect in the candidate under test.".format(bound.runner)
        )
    evaluated = counts["passed"] + counts["failed"] + counts["errored"]
    if executed > 0 and evaluated == 0:
        # Cases were counted and not one of them ran an assertion. `it.skip` or
        # `@pytest.mark.skip` across the board proves exactly what an empty
        # suite proves, and `executed` — which counts skips — would otherwise
        # clear `code_review`'s `executed < min_cases` check and bind the
        # candidate green.
        #
        # Deliberately narrower than dropping `skipped` from `executed`: a
        # suite with one passed case and ten skipped has exercised the
        # candidate and must still pass, and redefining `executed` would move
        # `min_cases` under every existing suite. Only a suite that evaluated
        # NOTHING refuses.
        #
        # Measured, on real binaries: a fully skipped suite exits 0 in both
        # runners — pytest prints `1 skipped in 0.00s`, vitest prints
        # `Tests  2 skipped (2)` — so this is reachable and silent, which is
        # what makes it worth a refusal rather than a count adjustment.
        #
        # This used to carry a `returncode == 0` guard, which claimed to keep
        # "a genuinely failing run on the failure path, where blaming the
        # candidate is correct". That branch is not reachable: a genuinely
        # failing run has `failed` or `errored` above zero, so `evaluated > 0`
        # and this condition is already false. The guard covered nothing and
        # exempted the one shape that matters — a suite whose fixture cannot
        # start exits non-zero with every case skipped, and was billed to the
        # builder. Measured on vitest 3.2.7: a `beforeAll` that throws exits 1
        # with `Tests  2 skipped (2)`, evaluating nothing. FDAdb run
        # be064e58 `lane-wp3-adapter-build` spent three REVISE rounds and a
        # NO_PROGRESS park on exactly that, because the suite spawned
        # `python3` off PATH and the interpreter it found had no `uvicorn`.
        #
        # Whether the runner exited 0 or 1 while evaluating nothing says
        # nothing about the candidate; `evaluated == 0` is the whole question.
        raise rc.SuiteEnvironmentError(
            "SUITE_ALL_CASES_SKIPPED:{0}: every one of the {1} counted "
            "cases was skipped, so the suite evaluated nothing. A suite "
            "that asserted nothing is not a pass and is not a defect in the "
            "candidate under test.".format(bound.runner, executed)
        )
    return {
        "counts": counts,
        "executed": executed,
        "min_cases": bound.min_cases,
        "output": output,
        "returncode": returncode,
        "runner": bound.runner,
    }
