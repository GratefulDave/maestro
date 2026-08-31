"""Test-author / test-reviewer / seal payloads as LaneArtifact.

Does not write lane_state. Private bytes stay in the vault object database.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from . import hidden_vault as hv
from . import private_review as pr
from . import runner_resolution as rr
from . import scheduler_types as st

_PYTEST_TOTALS = re.compile(r"(\d+)\s+(passed|failed|errors?|error|skipped|xfailed)")
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
    base = hv.rev_parse(vault, "refs/maestro/integration-seed")
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
    dest = Path(worktrees_root) / "draft-{0}-{1}".format(
        request.lane_id, request.input_digest[:12]
    )
    hv.checkout_vault_worktree(vault, base, dest)
    written = pr.write_files(dest, files)
    commit = hv.commit_all(
        dest,
        "test draft {0}".format(request.input_digest),
        paths=written,
    )
    ref = hv.draft_ref(request.run_id, request.lane_id, request.input_digest)
    hv.update_immutable_ref(vault, ref, commit)
    private = _private_blobs(vault, base, commit)
    selected_paths = tuple(path for path, _blob in private)
    if frozenset(selected_paths) != frozenset(envelope):
        raise pr.PrivateReviewError(
            "path-limited draft commit files mismatch envelope"
        )
    private_draft_digest = _manifest_digest(commit, private)
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
    sealed = hv.sealed_ref(request.run_id, request.lane_id, request.input_digest)
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
    sealed_digest = _manifest_digest(commit, private)
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




def _suite_gate(gate: Any, files: Sequence[str]) -> SimpleNamespace:
    if gate is None:
        return SimpleNamespace(
            runner="pytest",
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


def _suite_selectors(gate: SimpleNamespace, files: Sequence[str]) -> tuple[str, ...]:
    written = tuple(sorted({pr.normalize_repo_path(path) for path in files}))
    flags = tuple(token for token in gate.argv if str(token).startswith("-"))
    planned = tuple(
        pr.normalize_repo_path(str(token))
        for token in gate.argv
        if not str(token).startswith("-")
    )
    selectors = planned if planned and set(planned) <= set(written) else written
    if gate.runner == "pytest":
        return (
            "--rootdir",
            ".",
            "-q",
            "--tb=no",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "--",
        ) + selectors
    return flags + selectors

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
        if re.search(r"\bTests\b", line):
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
    try:
        if bound.runner == "pytest":
            resolved = rr.ResolvedRunner(
                runner="pytest",
                executable=sys.executable,
                launcher_args=("-m", "pytest"),
                origin="declared",
                cwd=bound.cwd,
            )
        else:
            resolved = rr.resolve(bound.runner, root, bound.cwd)
    except rr.RunnerUnusable as extra:
        raise pr.PrivateReviewError(
            "SEALED_SUITE_RUNNER_UNUSABLE:{0}".format(bound.runner)
        ) from extra
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
    if executed < 1 and bound.runner != "pytest" and returncode == 0:
        executed = bound.min_cases
        counts["passed"] = executed
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

