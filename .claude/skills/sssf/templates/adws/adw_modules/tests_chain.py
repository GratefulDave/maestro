"""Test-author / test-reviewer / seal payloads as LaneArtifact.

Does not write lane_state. Private bytes stay in the vault object database.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from . import hidden_vault as hv
from . import private_review as pr
from . import runner_resolution as rr
from . import scheduler_types as st

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
#: `SEALED_SUITE_COUNTS_UNPARSEABLE` against a candidate whose sealed tests all
#: passed. Feeding stdout alone to the parser returns 15; the combined capture
#: returns 0.
#:
#: So the line must also CARRY a count. Every real totals line has at least one
#: `N passed`/`N failed`/`N skipped`; no warning does. A suite that genuinely
#: executed nothing still refuses, which is the point of the check.
_VITEST_SUMMARY = re.compile(r"^\s*Tests\s+.*?\d+\s+(?:passed|failed|skipped)\b")
PRIVATE_MANIFEST_SCHEMA = "private-manifest.v1"


def _private_blobs(vault: Path, base: str, commit: str) -> tuple[tuple[str, str], ...]:
    before = dict(hv.list_commit_blobs(vault, base))
    after = hv.list_commit_blobs(vault, commit)
    return tuple((path, blob) for path, blob in after if before.get(path) != blob)


def _manifest_digest(commit: str, files: Sequence[tuple[str, str]]) -> str:
    return st.digest_bytes(
        st.canonical_bytes(
            {
                "commit": commit,
                "files": [{"blob": blob, "path": path} for path, blob in files],
            }
        )
    )


def _declared_changed_blobs(
    vault: Path,
    base: str,
    commit: str,
    declared_outputs: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    declared = tuple(pr.normalize_repo_path(path) for path in declared_outputs)
    wanted = set(declared)
    selected = tuple(
        (path, blob)
        for path, blob in _private_blobs(vault, base, commit)
        if path in wanted
    )
    present = {path for path, _blob in selected}
    missing = [path for path in declared if path not in present]
    if missing:
        raise pr.PrivateReviewError(
            "declared outputs missing or unchanged: {0}".format(", ".join(missing))
        )
    return selected


def _select_private_blobs(
    vault: Path,
    commit: str,
    payload: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    # The private files are what the draft commit changed against the base it
    # was written on, and that base is the commit's own parent. Reading it off
    # `refs/maestro/integration-seed` instead would make the manifest a
    # function of a ref that every later draft force-updates: once integration
    # moved, an older bundle's files no longer matched its recorded digest.
    base = hv.rev_parse(vault, commit + "^")
    if payload.get("private_manifest_schema") == PRIVATE_MANIFEST_SCHEMA:
        selected = _private_blobs(vault, base, commit)
        if not selected:
            raise pr.PrivateReviewError("private manifest selected no files")
        digest = _manifest_digest(commit, selected)
        expected = payload.get("private_manifest_digest")
        if expected is None:
            expected = payload.get("private_draft_digest") or payload.get(
                "sealed_digest"
            )
        if digest != expected:
            raise pr.PrivateReviewError("private manifest digest mismatch")
        return selected
    contract = payload["public_contract"]
    if not isinstance(contract, Mapping):
        raise pr.PrivateReviewError("public_contract must be a mapping")
    declared = pr.as_str_tuple(contract["declared_outputs"], "declared_outputs")
    return _declared_changed_blobs(vault, base, commit, declared)



def write_test_draft(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    run_repo: Path,
    integration_ref: str,
    files: Mapping[str, str],
    public_contract: Mapping[str, object],
    worktrees_root: Path,
) -> st.LaneArtifact:
    contract = pr.public_contract(
        acceptance_criteria=pr.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=pr.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
    )
    envelope = tuple(
        sorted({pr.normalize_repo_path(path) for path in files})
    )
    if not envelope:
        raise pr.PrivateReviewError("test draft files are empty")
    vault = hv.ensure_vault(state_root, request.run_id)
    base = hv.seed(vault, run_repo, integration_ref)
    dest = hv.scratch_worktree_path(
        worktrees_root, "draft-{0}".format(request.lane_id)
    )
    hv.checkout_vault_worktree(vault, base, dest)
    # The worktree stays registered until the draft ref is pinned. Its HEAD
    # is the only thing anchoring the commit before that ref exists, so
    # removing it earlier would leave the commit unreferenced across every
    # check below. A draft those checks refuse still ends with the worktree
    # gone and no ref, which is the intended outcome for a refused draft.
    try:
        written = pr.write_files(dest, files)
        commit = hv.commit_all(
            dest,
            "test draft {0}".format(request.input_digest),
            paths=written,
        )
        private = _private_blobs(vault, base, commit)
        selected_paths = tuple(path for path, _blob in private)
        if frozenset(selected_paths) != frozenset(envelope):
            raise pr.PrivateReviewError(
                "path-limited draft commit files mismatch envelope"
            )
        private_draft_digest = _manifest_digest(commit, private)
        ref = hv.draft_ref(request.run_id, request.lane_id, private_draft_digest)
        payload = {
            "input_artifact_ids": list(request.input_artifact_ids),
            "input_digest": request.input_digest,
            "private_draft_digest": private_draft_digest,
            "private_draft_ref": ref,
            "private_manifest_digest": private_draft_digest,
            "private_manifest_schema": PRIVATE_MANIFEST_SCHEMA,
            "public_contract": contract,
        }

        private_files = {
            path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private
        }
        tokens = pr.collect_private_tokens(
            files=private_files,
            vault_path=vault,
            vault_refs=(ref,),
            blob_ids=tuple(blob for _path, blob in private) + (commit,),
        )
        public_tokens = pr.public_contract_allow(
            contract,
            extra=(
                request.input_digest,
                private_draft_digest,
                ref,
                PRIVATE_MANIFEST_SCHEMA,
            ),
        )
        pr.refuse_private_leak(payload, tokens, allow=public_tokens)
        artifact = pr.make_lane_artifact(
            kind=st.ArtifactKind.TEST_DRAFT,
            request=request,
            payload=payload,
            artifact_ref=ref,
        )

        pr.refuse_private_leak(
            st.canonical_bytes(artifact.payload),
            tokens,
            allow=public_tokens,
        )
        # Pinned last so a draft the checks above refused leaves no ref behind.
        # The name is a digest over the commit, so this either creates the ref
        # or finds it already pinned to this same commit; it cannot collide.
        hv.update_immutable_ref(vault, ref, commit)
    finally:
        hv.remove_vault_worktree(vault, dest)
    return artifact


def review_test_draft(
    *,
    request: pr.VaultLaneRequest,
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]] = (),
    test_draft: st.LaneArtifact,
    private_tokens: Sequence[str] = (),
) -> st.LaneArtifact:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise pr.PrivateReviewError("test review requires TEST_DRAFT")
    findings_out = pr.actionable_findings(verdict, findings, private_tokens)
    payload = {
        "findings": [dict(item) for item in findings_out],
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "verdict": verdict.value,
    }
    if private_tokens:
        pr.refuse_private_leak(payload, private_tokens)
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.TEST_REVIEW,
        request=request,
        payload=payload,
        artifact_ref="test-review:{0}".format(request.input_digest),
        verdict=verdict,
    )
    if private_tokens:
        pr.refuse_private_leak(st.canonical_bytes(artifact.payload), private_tokens)
    return artifact


def seal_accepted_tests(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    run_repo: Path,
    builder_worktree: Path | None,
    test_draft: st.LaneArtifact,
    test_review: st.LaneArtifact,
) -> st.LaneArtifact:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise pr.PrivateReviewError("seal requires TEST_DRAFT")
    if test_review.kind is not st.ArtifactKind.TEST_REVIEW:
        raise pr.PrivateReviewError("seal requires TEST_REVIEW")
    if test_review.verdict is not st.ReviewerVerdict.PASS:
        raise pr.PrivateReviewError("seal requires TEST_REVIEW PASS")
    vault = hv.ensure_vault(state_root, request.run_id)
    commit = hv.rev_parse(vault, test_draft.artifact_ref)
    contract = test_draft.payload["public_contract"]
    private = _select_private_blobs(vault, commit, test_draft.payload)
    # Sealing accepts the draft commit as it is; nothing is re-committed. The
    # sealed digest is therefore the draft's manifest digest recomputed over
    # the same commit and the same blobs, and equals `private_draft_digest`
    # by construction. `_select_private_blobs` has already refused a draft
    # whose recorded digest does not match that recomputation, so the two
    # names are one identity carried under two ref namespaces, not a
    # coincidence.
    sealed_digest = _manifest_digest(commit, private)
    sealed = hv.sealed_ref(request.run_id, request.lane_id, sealed_digest)
    object_ids = (commit,) + tuple(blob for _path, blob in private)
    hv.prove_absent((run_repo,), object_ids)
    if builder_worktree is not None:
        hv.prove_absent((builder_worktree,), object_ids)
        if commit in hv.advertised_object_ids(run_repo):
            raise pr.IsolationError("run repository advertises the sealed commit")
        builder_objects = hv.batch_object_ids(builder_worktree)
        if any(object_id in builder_objects for object_id in object_ids):
            raise pr.IsolationError("builder object database holds sealed tests")
        reachable = hv.rev_list_objects(builder_worktree)
        for object_id in object_ids:
            if object_id in reachable:
                raise pr.IsolationError("builder rev-list reaches private object")
        hv.prove_unfetchable(run_repo, builder_worktree, commit)
        vault_path = hv.vault_path(state_root, request.run_id).resolve()
        builder_root = builder_worktree.resolve()
        if vault_path in builder_root.parents:
            raise pr.IsolationError("vault is inside the builder worktree")
        if str(vault_path).startswith(str(builder_root) + os.sep):
            raise pr.IsolationError("vault is under the builder worktree")
    payload = {
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "public_contract": contract,
        "sealed_digest": sealed_digest,
    }
    if test_draft.payload.get("private_manifest_schema") == PRIVATE_MANIFEST_SCHEMA:
        payload["private_manifest_digest"] = sealed_digest
        payload["private_manifest_schema"] = PRIVATE_MANIFEST_SCHEMA

    files = {path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private}
    tokens = pr.collect_private_tokens(
        files=files,
        vault_path=hv.vault_path(state_root, request.run_id),
        vault_refs=(test_draft.artifact_ref, sealed),
        blob_ids=object_ids,
    )
    public_tokens = pr.public_contract_allow(
        contract,
        extra=(
            request.input_digest,
            sealed_digest,
            *(
                (PRIVATE_MANIFEST_SCHEMA,)
                if payload.get("private_manifest_schema") == PRIVATE_MANIFEST_SCHEMA
                else ()
            ),
        ),
    )

    pr.refuse_private_leak(payload, tokens, allow=public_tokens)
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.SEALED_TEST_BUNDLE,
        request=request,
        payload=payload,
        artifact_ref=sealed,
    )
    pr.refuse_private_leak(
        st.canonical_bytes(artifact.payload),
        tokens,
        allow=public_tokens,
    )
    # Pinned last, for the same reason as the draft ref; it cannot collide.
    hv.update_immutable_ref(vault, sealed, commit)
    return artifact


def sealed_private_files(
    vault: Path, sealed_artifact: st.LaneArtifact
) -> dict[str, str]:
    commit = hv.rev_parse(vault, sealed_artifact.artifact_ref)
    return {
        path: blob
        for path, blob in _select_private_blobs(
            vault, commit, sealed_artifact.payload
        )
    }

def private_draft_overlay_paths(
    vault: Path, test_draft: st.LaneArtifact
) -> tuple[str, ...]:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise pr.PrivateReviewError("overlay listing requires TEST_DRAFT")
    commit = hv.rev_parse(vault, test_draft.artifact_ref)
    return tuple(
        path
        for path, _blob in _select_private_blobs(
            vault, commit, test_draft.payload
        )
    )




#: Which runner a sealed file's name names. `.py` is pytest outright; a
#: JavaScript or TypeScript file only names vitest when it is a test file, so a
#: `.ts` helper sealed beside a `.test.ts` suite does not get a vote of its own.
#: Anything else votes for nothing.
_PYTEST_SUFFIX = ".py"
_VITEST_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_VITEST_STEMS = (".test", ".spec")


def _file_runner(path: str) -> str | None:
    """The runner a single sealed file names, or `None` when it names none."""
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
    """The one runner a sealed file set names, or a refusal.

    Nothing in the artifact-factory plan schema can supply a gate —
    `plan_model.LANE_KEYS` has no `gate` key and `plan_validate` refuses a lane
    carrying one — so every sealed suite arrives here with `gate=None`. This
    used to answer `pytest` unconditionally, which ran a vitest suite under
    pytest: `found no collectors for …/paid-dpa.test.ts`, exit 4, zero cases
    executed, which `code_review` reads as a failed sealed suite and turns a
    reviewer PASS into REVISE. Deriving is the fix; guessing is what broke it,
    so an ambiguous or unreadable file set refuses instead of picking a side.
    """
    named = {runner for runner in map(_file_runner, files) if runner is not None}
    if len(named) == 1:
        return named.pop()
    # Only the distinct suffixes go in the message: the sealed file names are
    # private and this error travels back through public review payloads.
    suffixes = sorted(
        {"." + str(path).rsplit(".", 1)[-1] for path in files if "." in str(path)}
    )
    if not named:
        raise pr.SealedEnvironmentError(
            "SEALED_SUITE_RUNNER_UNDERIVABLE: no sealed file names a runner "
            "(suffixes: {0})".format(", ".join(suffixes) or "none")
        )
    raise pr.SealedEnvironmentError(
        "SEALED_SUITE_RUNNER_AMBIGUOUS: sealed files name more than one runner "
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
        raise pr.PrivateReviewError("sealed suite gate is not a mapping")
    runner = gate.get("runner")
    if runner not in rr.EXECUTE_ARGS:
        raise pr.PrivateReviewError("unsupported sealed suite runner")
    min_cases = gate.get("min_cases")
    if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:
        raise pr.PrivateReviewError("min_cases")
    argv = gate.get("argv") or ()
    if not isinstance(argv, (list, tuple)):
        raise pr.PrivateReviewError("argv")
    cwd = gate.get("cwd") or "."
    if not isinstance(cwd, str) or not cwd:
        raise pr.PrivateReviewError("cwd")
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
# as "sealed private tests failed, errored, or did not execute" and reports
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

    The gate's own `cwd` and each sealed file's directory are both walked
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
            relative = pr.normalize_repo_path(str(path))
        except pr.PrivateReviewError:
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
        raise pr.SealedEnvironmentError(
            "SEALED_SUITE_PYTHON_UNSUPPORTED: the sealed suite resolved pytest to "
            "{0}, running Python {1}, which does not satisfy requires-python "
            "{2!r} declared in {3}. This is a harness environment fault: the "
            "candidate under test was never executed, and no change to it can "
            "fix this.".format(" ".join(resolved.argv_prefix), found, specifier, project)
        )


def _suite_selectors(gate: SimpleNamespace, files: Sequence[str]) -> tuple[str, ...]:
    argv, selectors = pr.substituted_gate_argv(gate.argv, files)
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
            # nothing but pytest's short summary, whose only content is
            # `path::case_name` -- private, dropped, and therefore a lane that
            # reported zero forwardable failures on every round forever.
            # Verified against the real binary: this yields
            # `path:LINE: AttributeError: ...` and no test source.
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


def run_private_suite(
    tree: Path,
    paths: Sequence[str],
    *,
    gate: Any = None,
    runtime_root: Path | None = None,
    timeout_s: float = 120.0,
) -> dict:
    files = tuple(paths)
    bound = _suite_gate(gate, files)
    root = Path(runtime_root or tree)
    # Where the runner is resolved FROM is not the same question for the two
    # runners, because only one of them has its environment bridged into the
    # tree. `rr.COLLECT_RUNTIME_DIRS` is `("node_modules",)`: before executing,
    # `prepare_collect_tree` symlinks the runtime root's `node_modules` into
    # the tree, so a vitest resolved against the runtime root and the modules
    # it imports are the same installation, and resolving vitest there is
    # correct.
    #
    # Nothing bridges a Python environment. A pytest resolved against the
    # runtime root is the REAL repository's interpreter, while
    # `rr.execute_cases` runs it with cwd inside the review tree. Two ways that
    # goes wrong, both silent: the repository's environment lacks what the
    # candidate needs and the suite reports zero executed cases, or — worse —
    # that environment has the project installed editable, `import app.x`
    # resolves to the REAL repository's source, and the sealed suite greenly
    # certifies code that is not the candidate's.
    #
    # It currently works only because the repository happens to have no
    # `.venv`, so `_rank_candidates` falls through to `uv run pytest`, and uv
    # discovers its environment from cwd — which is the tree. Creating a
    # `.venv` in the repository would silently take resolution away from the
    # tree. So pytest resolves against the tree it will execute in. If the tree
    # has no usable environment, `rr.resolve` refuses, and an explicit
    # `SEALED_SUITE_RUNNER_UNUSABLE:pytest` is the right answer — better than
    # running the wrong interpreter against the wrong source.
    resolution_root = Path(tree) if bound.runner == "pytest" else root
    try:
        # Both runners resolve through `rr.resolve`. pytest used to be pinned
        # to `sys.executable -m pytest`, the interpreter the *scheduler* runs
        # under — a 3.9 scheduler could never import a
        # `requires-python = ">=3.12"` project, so the sealed suite failed on
        # import forever. `rr.resolve` probes a project-local environment first
        # and refuses rather than guessing.
        resolved = rr.resolve(bound.runner, resolution_root, bound.cwd)
    except rr.RunnerUnusable as extra:
        raise pr.SealedEnvironmentError(
            "SEALED_SUITE_RUNNER_UNUSABLE:{0}".format(bound.runner)
        ) from extra
    _assert_declared_python(resolved, resolution_root, Path(tree), bound.cwd, files)
    exec_gate = SimpleNamespace(
        runner=bound.runner,
        argv=_suite_selectors(bound, files),
        cwd=bound.cwd,
        min_cases=bound.min_cases,
    )
    raw = rr.execute_cases(
        resolved,
        exec_gate,
        tree,
        timeout_s=timeout_s,
        runtime_root=root if Path(root).resolve() != Path(tree).resolve() else None,
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
        # fabrication turned "vitest executed nothing" into a green sealed suite
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
        raise pr.SealedEnvironmentError(
            "SEALED_SUITE_COUNTS_UNPARSEABLE:{0}: the sealed suite's runner "
            "exited 0 but reported no executed cases, so how many cases ran "
            "could not be measured. An unreadable measurement is not a pass "
            "and is not a defect in the candidate under test.".format(bound.runner)
        )
    evaluated = counts["passed"] + counts["failed"] + counts["errored"]
    if returncode == 0 and executed > 0 and evaluated == 0:
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
        # what makes it worth a refusal rather than a count adjustment. The
        # `returncode == 0` guard keeps a genuinely failing run on the failure
        # path, where blaming the candidate is correct.
        raise pr.SealedEnvironmentError(
            "SEALED_SUITE_ALL_CASES_SKIPPED:{0}: every one of the {1} counted "
            "cases was skipped, so the sealed suite evaluated nothing. A suite "
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


def run_private_pytest(
    tree: Path, paths: Sequence[str], timeout_s: float = 120.0
) -> dict:
    return run_private_suite(tree, paths, timeout_s=timeout_s)



def draft_private_tokens(
    *,
    state_root: Path,
    run_id: str,
    draft: st.LaneArtifact,
) -> tuple[str, ...]:
    vault = hv.ensure_vault(state_root, run_id)
    commit = hv.rev_parse(vault, draft.artifact_ref)
    private = _select_private_blobs(vault, commit, draft.payload)
    files = {path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private}
    return pr.collect_private_tokens(
        files=files,
        vault_path=vault,
        vault_refs=(draft.artifact_ref,),
        blob_ids=(commit,) + tuple(blob for _path, blob in private),
    )

