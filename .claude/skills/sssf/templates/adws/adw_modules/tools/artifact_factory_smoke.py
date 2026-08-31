#!/usr/bin/env python3
"""Isolated two-lane artifact-factory smoke. Proof only. Fail closed."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

ADWS_ROOT = Path(__file__).resolve().parents[2]
if str(ADWS_ROOT) not in sys.path:
    sys.path.insert(0, str(ADWS_ROOT))

from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import route_admission as admission  # noqa: E402
from adw_modules.hidden_vault import vault_path  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.runtime_state import LAYOUT_CHILDREN, RuntimeStateRoot  # noqa: E402
from adw_modules.scheduler import classify_executing_runtime  # noqa: E402
from adw_modules.scheduler_types import (  # noqa: E402
    FORBIDDEN_PRIVATE_KEYS,
    ArtifactKind,
    integration_ref,
)

SMOKE_NAME = "maestro-artifact-factory-smoke"
SMOKE_EMAIL = "maestro-artifact-factory-smoke@example.test"
PLAN_RELATIVE = Path(".maestro/plans/two-lane.v1.json")
SEED_RELATIVE = Path("public/seed.txt")
CONFIG_RELATIVE = Path("adws") / "maestro.config.yaml"
CONFIG_SCHEMA = "maestro-config.v1"
SSSF_ROOT = ADWS_ROOT.parents[1]
RUNTIME_MODE = 0o700

PRODUCT_ALLOWED_PREFIXES = (
    ".git/",
    "adws/",
    ".maestro/",
    "public/",
)


class SmokeRefused(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def emit(self) -> int:
        print(json.dumps({"detail": self.detail, "outcome": self.code}, sort_keys=True))
        return 3


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SmokeRefused(
            "GIT_FAILED",
            "{0}: {1}".format(" ".join(args), (result.stderr or result.stdout).strip()),
        )
    return (result.stdout or "").strip()


def load_runtime_sync(path: Path) -> Any:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SmokeRefused("RUNTIME_SYNC_MISSING", str(resolved))
    name = "_maestro_smoke_runtime_sync_{0}".format(
        hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:16]
    )
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise SmokeRefused("RUNTIME_SYNC_UNIMPORTABLE", str(resolved))
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def require_executable(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise SmokeRefused("DEPENDENCY_MISSING", name)
    return Path(found)


def init_product(product: Path) -> Path:
    if product.exists():
        raise SmokeRefused("PRODUCT_EXISTS", str(product))
    product.mkdir(parents=True)
    _git(product, "init", "-b", "main")
    _git(product, "config", "user.email", SMOKE_EMAIL)
    _git(product, "config", "user.name", SMOKE_NAME)
    _git(product, "config", "commit.gpgsign", "false")
    return product.resolve()


def init_state(state: Path) -> Path:
    if state.exists():
        raise SmokeRefused("STATE_EXISTS", str(state))
    state.mkdir(mode=RUNTIME_MODE, parents=True)
    os.chmod(state, RUNTIME_MODE)
    mode = stat.S_IMODE(state.stat().st_mode)
    if mode != RUNTIME_MODE:
        raise SmokeRefused("STATE_MODE", oct(mode))
    if state.is_symlink():
        raise SmokeRefused("STATE_SYMLINK", str(state))
    return state.resolve()


def install_adws(
    runtime_sync_path: Path, template_adws: Path, dest_adws: Path
) -> Mapping[str, Any]:
    rs = load_runtime_sync(runtime_sync_path)
    dest_adws.mkdir(parents=True, exist_ok=True)
    source = rs.describe_copy(template_adws, kind=rs.TEMPLATE)
    destination = rs.describe_copy(dest_adws, kind=rs.DEPLOYMENT)
    result = rs.mirror(source, destination, apply=True, commit=False)
    if result.refused:
        raise SmokeRefused(
            "MIRROR_REFUSED",
            "; ".join(item.relative_path for item in result.refused),
        )
    if not result.applied:
        raise SmokeRefused("MIRROR_NOT_APPLIED", "runtime_sync.mirror apply=False")
    maestro_file = dest_adws / "maestro.py"
    if not maestro_file.is_file():
        raise SmokeRefused("DEPLOYMENT_MAESTRO_MISSING", str(maestro_file))
    return {
        "copied": list(result.copied),
        "unchanged": list(result.unchanged),
        "maestro": str(maestro_file),
    }


def write_deployment_config(
    product: Path,
    state: Path,
    *,
    executables: Mapping[str, str],
    runner_profile: str,
    route_receipts: Mapping[str, str],
    route_verify_keys: Sequence[str],
) -> Path:
    if not state.is_absolute():
        raise SmokeRefused("RUNTIME_STATE_NOT_ABSOLUTE", str(state))
    if not runner_profile.strip():
        raise SmokeRefused("RUNNER_PROFILE", runner_profile)
    if not route_receipts or not route_verify_keys:
        raise SmokeRefused("ROUTE_RECEIPTS_REQUIRED", "captured receipt missing")
    payload: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "runtime_state_root": str(state),
        "role_routes": {
            role: {"route": "omp", "profile": runner_profile}
            for role in (
                "tester",
                "test-reviewer",
                "builder",
                "code-reviewer",
                "integration-reviewer",
            )
        },
        "executables": dict(executables),
        "route_receipts": dict(route_receipts),
        "route_verify_keys": list(route_verify_keys),
        "dashboard": {
            "enabled": True,
            "launcher": str(
                (SSSF_ROOT / "apps" / "dashboard" / "bin" / "maestro-dashboard").resolve()
            ),
            "api_port": 4600,
            "ui_port": 4317,
            "open": True,
        },
    }
    path = product / CONFIG_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def admit_omp_route(
    state: Path,
    product: Path,
    profile: str,
    omp: Path,
    herdr: Path,
) -> tuple[Path, Path]:
    keys = admission.provision_keys(state / "keys")
    session = state / "admission" / "session"
    session.mkdir(mode=RUNTIME_MODE, parents=True, exist_ok=True)
    os.chmod(session.parent, RUNTIME_MODE)
    receipt = state / "receipts" / "omp.json"
    receipt.parent.mkdir(mode=RUNTIME_MODE, parents=True, exist_ok=True)
    spec = admission.RouteCaptureSpec(
        route="omp",
        cwd=product,
        herdr=herdr,
        binary=omp,
        model="xai-oauth/grok-4.6",
        effort="high",
        profile=profile,
        session_dir=session,
        timeout_s=120.0,
    )
    try:
        written = admission.admit_routes(
            (spec,),
            {"omp": receipt},
            route_seed=keys.route_seed,
        )
    except admission.AdmissionError as exc:
        raise SmokeRefused("ROUTE_ADMISSION_FAILED", str(exc)) from exc
    if not written or written[0].route != "omp" or not written[0].path.is_file():
        raise SmokeRefused("ROUTE_ADMISSION_FAILED", "omp receipt was not captured")
    pubkey = keys.keys_dir / "route.pub"
    if not pubkey.is_file():
        raise SmokeRefused("ROUTE_ADMISSION_FAILED", "route.pub missing")
    return written[0].path, pubkey


def write_public_fixtures(product: Path, fixture_root: Path) -> tuple[Path, Path]:
    plan_src = fixture_root / "two-lane.v1.json"
    seed_src = fixture_root / "public" / "seed.txt"
    if not plan_src.is_file() or not seed_src.is_file():
        raise SmokeRefused("FIXTURE_MISSING", str(fixture_root))
    plan = product / PLAN_RELATIVE
    seed = product / SEED_RELATIVE
    plan.parent.mkdir(parents=True, exist_ok=True)
    seed.parent.mkdir(parents=True, exist_ok=True)
    plan.write_bytes(plan_src.read_bytes())
    seed.write_bytes(seed_src.read_bytes())
    return plan, seed


def commit_declared(product: Path, paths: Sequence[Path]) -> str:
    relatives = []
    for path in paths:
        rel = path.resolve().relative_to(product.resolve()).as_posix()
        relatives.append(rel)
        _git(product, "add", "--", rel)
    _git(
        product,
        "-c",
        "user.name=" + SMOKE_NAME,
        "-c",
        "user.email=" + SMOKE_EMAIL,
        "commit",
        "-m",
        "smoke: declared product seed",
        "--",
        *relatives,
    )
    identity = _git(product, "log", "-1", "--format=%an <%ae>")
    if identity != "{0} <{1}>".format(SMOKE_NAME, SMOKE_EMAIL):
        raise SmokeRefused("GIT_IDENTITY", identity)
    return _git(product, "rev-parse", "HEAD")


def require_deployment_layout(product: Path) -> str:
    maestro_file = product / "adws" / "maestro.py"
    kind = classify_executing_runtime(maestro_file)
    if kind != "deployment":
        raise SmokeRefused("RUN_REPOSITORY_MISMATCH", kind)
    return kind


def require_real_omp_route(profile: str) -> dict[str, str]:
    if profile != "grok-maestro":
        raise SmokeRefused("RUNNER_PROFILE", profile)
    omp = require_executable("omp")
    herdr = require_executable("herdr")
    probe = subprocess.run(
        [str(omp), "--profile", profile, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise SmokeRefused(
            "OMP_ROUTE_UNUSABLE",
            (probe.stderr or probe.stdout or "omp --help failed").strip(),
        )
    return {"omp": str(omp), "herdr": str(herdr), "profile": profile}


def _parse_cli_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise SmokeRefused("CLI_EMPTY_OUTPUT", "deployed CLI wrote no JSON")
    last = text.splitlines()[-1]
    try:
        payload = json.loads(last)
    except json.JSONDecodeError as exc:
        raise SmokeRefused("CLI_INVALID_JSON", last) from exc
    if not isinstance(payload, dict) or "outcome" not in payload:
        raise SmokeRefused("CLI_MALFORMED", last)
    return payload


def invoke_deployed_cli(
    product: Path,
    argv: Sequence[str],
    *,
    uv_project: Path,
) -> dict[str, Any]:
    uv = require_executable("uv")
    command = [
        str(uv),
        "run",
        "--project",
        str(uv_project),
        "python",
        "adws/maestro.py",
        *argv,
    ]
    result = subprocess.run(
        command,
        cwd=str(product),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "cli failed").strip()
        try:
            payload = _parse_cli_json(result.stdout or "")
        except SmokeRefused:
            raise SmokeRefused("CLI_FAILED", detail) from None
        raise SmokeRefused(
            str(payload.get("outcome") or "CLI_FAILED"),
            str(payload.get("detail") or detail),
        )
    return _parse_cli_json(result.stdout)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_git(product: Path) -> dict[str, str]:
    refs = {}
    raw = _git(
        product, "for-each-ref", "--format=%(refname) %(objectname)", check=False
    )
    for line in raw.splitlines():
        if not line.strip():
            continue
        name, sha = line.split(" ", 1)
        refs[name] = sha
    refs["HEAD"] = _git(product, "rev-parse", "HEAD")
    return refs


def _load_json(text: str) -> Any:
    return json.loads(text)


def collect_evidence(
    product: Path,
    state: Path,
    run_id: str,
) -> dict[str, Any]:
    runtime = RuntimeStateRoot(state, overlap_paths=(product,))
    try:
        store = ArtifactStore(runtime.ledger_path())
    except Exception as exc:
        raise SmokeRefused("LEDGER_UNREADABLE", str(exc)) from exc
    try:
        initial_row = store.conn.execute(
            "SELECT integration_initial_sha FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if initial_row is None:
            raise SmokeRefused("RUN_MISSING", run_id)
        integration_initial_sha = str(initial_row["integration_initial_sha"])
        stages = {
            lane.lane_id: store.lane_stage(run_id, lane.lane_id).value
            for lane in store.active_projection(run_id)
        }
        transitions = [
            {
                "artifact_id": row["artifact_id"],
                "from_stage": row["from_stage"],
                "lane_id": row["lane_id"],
                "reason": row["reason"],
                "to_stage": row["to_stage"],
            }
            for row in store.conn.execute(
                "SELECT lane_id, from_stage, to_stage, artifact_id, reason "
                "FROM transitions WHERE run_id=? ORDER BY id",
                (run_id,),
            )
        ]
        lane_artifacts = []
        for row in store.conn.execute(
            "SELECT lane_id, artifact_kind, artifact_id, input_digest, output_digest, "
            "payload_json FROM lane_artifacts WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ):
            payload = _load_json(row["payload_json"])
            lane_artifacts.append(
                {
                    "artifact_id": row["artifact_id"],
                    "artifact_kind": row["artifact_kind"],
                    "input_digest": row["input_digest"],
                    "lane_id": row["lane_id"],
                    "output_digest": row["output_digest"],
                    "payload": payload,
                }
            )
        run_artifacts = []
        for row in store.conn.execute(
            "SELECT artifact_kind, artifact_id, input_digest, output_digest, payload_json "
            "FROM run_artifacts WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ):
            run_artifacts.append(
                {
                    "artifact_id": row["artifact_id"],
                    "artifact_kind": row["artifact_kind"],
                    "input_digest": row["input_digest"],
                    "output_digest": row["output_digest"],
                    "payload": _load_json(row["payload_json"]),
                }
            )
    finally:
        store.close()
        runtime.close()

    merges = [
        item
        for item in lane_artifacts
        if item["artifact_kind"] == ArtifactKind.INTEGRATION_MERGE.value
    ]
    merge_counts: dict[str, int] = {}
    merge_shas: dict[str, str] = {}
    for item in merges:
        lane_id = str(item["lane_id"])
        merge_counts[lane_id] = merge_counts.get(lane_id, 0) + 1
        merge_shas[lane_id] = str(
            item["payload"].get("after_sha")
            or item["payload"].get("integration_head")
            or ""
        )
    if merge_counts.get("lane-a") != 1 or merge_counts.get("lane-b") != 1:
        raise SmokeRefused("MERGE_NOT_ONCE", json.dumps(merge_counts, sort_keys=True))

    builders = [
        item
        for item in lane_artifacts
        if item["lane_id"] == "lane-b"
        and item["artifact_kind"] == ArtifactKind.BUILDER_OUTPUT.value
    ]
    if not builders:
        raise SmokeRefused("LANE_B_BUILDER_MISSING", run_id)
    lane_b_base = str(builders[0]["payload"].get("builder_base_sha") or "")
    if lane_b_base != merge_shas.get("lane-a"):
        raise SmokeRefused(
            "LANE_B_BASE_MISMATCH",
            "{0}!={1}".format(lane_b_base, merge_shas.get("lane-a")),
        )

    reviews = [
        item
        for item in run_artifacts
        if item["artifact_kind"] == ArtifactKind.FINAL_INTEGRATION_REVIEW.value
    ]
    publications = [
        item
        for item in run_artifacts
        if item["artifact_kind"] == ArtifactKind.MAIN_PUBLICATION.value
    ]
    if len(reviews) != 1:
        raise SmokeRefused("FINAL_REVIEW_COUNT", str(len(reviews)))
    if len(publications) != 1:
        raise SmokeRefused("PUBLICATION_COUNT", str(len(publications)))
    fingerprint = str(reviews[0]["input_digest"])
    if publications[0]["input_digest"] != fingerprint:
        raise SmokeRefused("PUBLICATION_FINGERPRINT", publications[0]["input_digest"])
    published_sha = str(publications[0]["payload"].get("published_sha") or "")
    main_sha = _git(product, "rev-parse", "refs/heads/main")
    if published_sha != main_sha:
        raise SmokeRefused(
            "MAIN_RECEIPT_MISMATCH", "{0}!={1}".format(published_sha, main_sha)
        )
    integration_sha = _git(product, "rev-parse", integration_ref(run_id))
    if published_sha != integration_sha:
        raise SmokeRefused(
            "PUBLICATION_INTEGRATION_MISMATCH",
            "{0}!={1}".format(published_sha, integration_sha),
        )

    leak = private_leak(product, integration_initial_sha)
    if leak:
        raise SmokeRefused("PRIVATE_TEST_LEAK", json.dumps(leak, sort_keys=True))

    vault = vault_path(state, run_id)
    return {
        "final_review_fingerprint": fingerprint,
        "integration_ref": integration_ref(run_id),
        "integration_sha": integration_sha,
        "lane_b_base_sha": lane_b_base,
        "main_sha": main_sha,
        "merge_counts": merge_counts,
        "merge_shas": merge_shas,
        "private_leak": leak,
        "publication_count": len(publications),
        "published_sha": published_sha,
        "stages": stages,
        "transitions": transitions,
        "vault": str(vault) if vault.exists() else "",
    }


def private_leak(product: Path, integration_initial_sha: str) -> list[str]:
    found: list[str] = []
    product_vault = product / "vaults"
    if product_vault.exists():
        found.append(str(product_vault))
    needles = tuple(FORBIDDEN_PRIVATE_KEYS)
    listed = _git(
        product,
        "rev-list",
        "--objects",
        "--all",
        "--not",
        integration_initial_sha,
        check=False,
    )
    for line in listed.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, name = parts
        lowered = name.lower()
        if any(token in lowered for token in needles):
            found.append(name)
            continue
        blob = subprocess.run(
            ["git", "-C", str(product), "cat-file", "-p", sha],
            capture_output=True,
            check=False,
        )
        text = blob.stdout.decode("utf-8", "replace")
        if any(token in text for token in needles):
            found.append(name or sha)
    return found


def containment(product: Path, state: Path) -> dict[str, Any]:
    violations = []
    forbidden_names = set(LAYOUT_CHILDREN) | {
        "lifecycle.sqlite3",
        "vaults",
        "worktrees",
        "receipts",
        "locks",
        "artifacts",
    }
    for path in product.rglob("*"):
        if not path.exists():
            continue
        rel = path.relative_to(product).as_posix()
        if rel.startswith(".git/"):
            continue
        if any(
            rel == prefix.rstrip("/") or rel.startswith(prefix)
            for prefix in PRODUCT_ALLOWED_PREFIXES
        ):
            continue
        if path.name in forbidden_names:
            violations.append(rel)
    if not str(state.resolve()).startswith(str(product.parent.resolve())):
        violations.append("state-not-sibling")
    mode = stat.S_IMODE(state.stat().st_mode)
    return {
        "ok": not violations,
        "state_mode": oct(mode),
        "violations": violations,
    }


def resume_noop(
    product: Path,
    state: Path,
    run_id: str,
    *,
    uv_project: Path,
) -> bool:
    before_refs = snapshot_git(product)
    ledger = state / "lifecycle.sqlite3"
    before_ledger = _sha256_file(ledger) if ledger.is_file() else ""
    payload = invoke_deployed_cli(
        product,
        ["run", "resume", run_id],
        uv_project=uv_project,
    )
    after_refs = snapshot_git(product)
    after_ledger = _sha256_file(ledger) if ledger.is_file() else ""
    if before_refs != after_refs:
        raise SmokeRefused(
            "RESUME_CHANGED_REFS",
            json.dumps({"before": before_refs, "after": after_refs}),
        )
    if before_ledger != after_ledger:
        raise SmokeRefused("RESUME_CHANGED_LEDGER", payload.get("outcome", ""))
    return True


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    product = Path(args.product_root).resolve()
    smoke_root = product.parent
    state = smoke_root / "state"
    fixture = Path(args.fixture).resolve()
    template = Path(args.template_adws).resolve()
    runtime_sync = Path(args.runtime_sync).resolve()
    if args.uv_project:
        uv_project = Path(args.uv_project).resolve()
    else:
        uv_project = None
        for parent in template.parents:
            if (parent / "pyproject.toml").is_file():
                uv_project = parent
                break
        if uv_project is None:
            raise SmokeRefused("UV_PROJECT_MISSING", str(template))
    profile = args.runner_profile

    product = init_product(product)
    state = init_state(state)
    mirror_info = install_adws(runtime_sync, template, product / "adws")
    omp_route = require_real_omp_route(profile)
    executables = {
        "herdr": omp_route["herdr"],
        "omp": omp_route["omp"],
        "claude": str(shutil.which("claude") or "claude"),
    }
    receipt_path, pubkey_path = admit_omp_route(
        state,
        product,
        profile,
        Path(omp_route["omp"]),
        Path(omp_route["herdr"]),
    )
    config = write_deployment_config(
        product,
        state,
        executables=executables,
        runner_profile=profile,
        route_receipts={"omp": str(receipt_path)},
        route_verify_keys=(str(pubkey_path),),
    )
    plan, seed = write_public_fixtures(product, fixture)
    commit_sha = commit_declared(product, (product / "adws", config, plan, seed))
    classification = require_deployment_layout(product)
    started = invoke_deployed_cli(
        product,
        [
            "run",
            "start",
            PLAN_RELATIVE.as_posix(),
            "--repo",
            str(product),
            "--main-ref",
            "refs/heads/main",
        ],
        uv_project=uv_project,
    )
    run_id = str(started.get("run_id") or "")
    if not run_id:
        raise SmokeRefused("RUN_ID_MISSING", json.dumps(started, sort_keys=True))
    if started.get("outcome") not in {"STARTED", "STATUS", "RESUMED"}:
        invoke_deployed_cli(product, ["run", "resume", run_id], uv_project=uv_project)
    evidence = collect_evidence(product, state, run_id)
    noop = resume_noop(product, state, run_id, uv_project=uv_project)
    contained = containment(product, state)
    if not contained["ok"]:
        raise SmokeRefused("CONTAINMENT", json.dumps(contained["violations"]))
    return {
        "classification": classification,
        "commit_sha": commit_sha,
        "containment": contained,
        "evidence": evidence,
        "mirror": mirror_info,
        "omp_route": omp_route,
        "outcome": "SMOKE_PASS",
        "product": str(product),
        "resume_noop": noop,
        "run_id": run_id,
        "start": started,
        "state": str(state),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artifact_factory_smoke")
    parser.add_argument("--runtime-sync", required=True)
    parser.add_argument("--template-adws", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--product-root", required=True)
    parser.add_argument("--runner-profile", default="grok-maestro")
    parser.add_argument("--uv-project")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = run_smoke(args)
    except SmokeRefused as exc:
        return exc.emit()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
