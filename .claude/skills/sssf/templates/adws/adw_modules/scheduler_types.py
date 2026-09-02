"""Artifact-factory kernel DTOs. Lane stage is the only durable workflow authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple


CANONICAL_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION_V1 = "artifact-factory.v1"
LEDGER_SCHEMA_VERSION = "artifact-factory.v2"

NO_TEST_REVIEW = "NO_TEST_REVIEW"
NO_PRIOR_BUILDER = "NO_PRIOR_BUILDER"
NO_CODE_REVIEW = "NO_CODE_REVIEW"
NO_BASE_INVALIDATION = "NO_BASE_INVALIDATION"
NO_TEST_INVALIDATION = "NO_TEST_INVALIDATION"
NO_CODE_REVIEW_REVISE = "NO_CODE_REVIEW_REVISE"
NO_FINAL_REVIEW = "NO_FINAL_REVIEW"
NO_PREDECESSOR = "NO_PREDECESSOR"


FORBIDDEN_PRIVATE_KEYS = frozenset(
    {
        "expected_literals",
        "fixture",
        "fixtures",
        "private_bytes",
        "private_source",
        "selector",
        "selectors",
        "test_source",
        "vault_path",
        "vault_paths",
    }
)

REVISE_FINDING_KEYS = (
    "implementation_area",
    "observed_behavior",
    "required_behavior",
    "violated_requirement",
)


class LaneStage(str, Enum):
    PLANNED = "PLANNED"
    WRITING_TESTS = "WRITING_TESTS"
    REVIEWING_TESTS = "REVIEWING_TESTS"
    TESTS_SEALED = "TESTS_SEALED"
    BUILDING = "BUILDING"
    REVIEWING_CODE = "REVIEWING_CODE"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    WAITING_FOR_USER = "WAITING_FOR_USER"


class ReviewerVerdict(str, Enum):
    PASS = "PASS"
    REVISE = "REVISE"


class ArtifactKind(str, Enum):
    LANE_PLAN = "LANE_PLAN"
    TEST_DRAFT = "TEST_DRAFT"
    TEST_REVIEW = "TEST_REVIEW"
    SEALED_TEST_BUNDLE = "SEALED_TEST_BUNDLE"
    BUILDER_OUTPUT = "BUILDER_OUTPUT"
    CODE_REVIEW = "CODE_REVIEW"
    INTEGRATION_MERGE = "INTEGRATION_MERGE"
    BASE_INVALIDATION = "BASE_INVALIDATION"
    TEST_INVALIDATION = "TEST_INVALIDATION"
    USER_WAIT = "USER_WAIT"
    USER_DECISION = "USER_DECISION"
    FINAL_INTEGRATION_REVIEW = "FINAL_INTEGRATION_REVIEW"
    MAIN_PUBLICATION = "MAIN_PUBLICATION"
    PLAN_AMENDMENT = "PLAN_AMENDMENT"


class WaitReason(str, Enum):
    PAUSE = "PAUSE"
    AMENDMENT_REQUIRED = "AMENDMENT_REQUIRED"
    NO_PROGRESS = "NO_PROGRESS"


# A lane blocked for NO_PROGRESS resumes exactly like a paused one: the operator
# is the only thing that clears it, and clearing it grants another window.
RESUMABLE_WAIT_REASONS: Tuple[WaitReason, ...] = (
    WaitReason.PAUSE,
    WaitReason.NO_PROGRESS,
)

# Rounds of slack a build lane gets before its sealed pass count has to climb.
# Under it the lane may oscillate; past it, any round that fails to beat the
# best count seen so far blocks the lane.
NO_PROGRESS_GRACE_ROUNDS = 3


class BuildingEntryKind(str, Enum):
    INITIAL = "INITIAL"
    CODE_REVISE = "CODE_REVISE"
    BASE_INVALIDATION = "BASE_INVALIDATION"


class RunStatus(str, Enum):
    COMPLETE = "complete"
    WAITING = "waiting"
    EXECUTING = "executing"
    INTEGRATION_REVIEW_PENDING = "integration_review_pending"
    PUBLISHABLE = "publishable"


LANE_STAGES: Tuple[LaneStage, ...] = tuple(LaneStage)
PAUSEABLE_STAGES: Tuple[LaneStage, ...] = (
    LaneStage.PLANNED,
    LaneStage.WRITING_TESTS,
    LaneStage.REVIEWING_TESTS,
    LaneStage.TESTS_SEALED,
    LaneStage.BUILDING,
    LaneStage.REVIEWING_CODE,
    LaneStage.READY_TO_MERGE,
)
UNSTARTED_STAGES: Tuple[LaneStage, ...] = (
    LaneStage.PLANNED,
    LaneStage.WRITING_TESTS,
    LaneStage.REVIEWING_TESTS,
    LaneStage.TESTS_SEALED,
)
STARTED_IMPLEMENTATION_STAGES: Tuple[LaneStage, ...] = (
    LaneStage.BUILDING,
    LaneStage.REVIEWING_CODE,
    LaneStage.READY_TO_MERGE,
)
LANE_ARTIFACT_KINDS: Tuple[ArtifactKind, ...] = (
    ArtifactKind.LANE_PLAN,
    ArtifactKind.TEST_DRAFT,
    ArtifactKind.TEST_REVIEW,
    ArtifactKind.SEALED_TEST_BUNDLE,
    ArtifactKind.BUILDER_OUTPUT,
    ArtifactKind.CODE_REVIEW,
    ArtifactKind.INTEGRATION_MERGE,
    ArtifactKind.BASE_INVALIDATION,
    ArtifactKind.TEST_INVALIDATION,
    ArtifactKind.USER_WAIT,
    ArtifactKind.USER_DECISION,
)

RUN_ARTIFACT_KINDS: Tuple[ArtifactKind, ...] = (
    ArtifactKind.FINAL_INTEGRATION_REVIEW,
    ArtifactKind.MAIN_PUBLICATION,
    ArtifactKind.PLAN_AMENDMENT,
)


class KernelError(RuntimeError):
    code = "KERNEL_ERROR"

    def __init__(self, detail: str = "") -> None:
        suffix = "" if not detail else f":{detail}"
        super().__init__(f"{self.code}{suffix}")


class IllegalStageEdge(KernelError):
    code = "ILLEGAL_STAGE_EDGE"


class CanonicalIdentityError(KernelError):
    code = "CANONICAL_IDENTITY_INVALID"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_canonical(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def json_ready(value: Any) -> Any:
    return json.loads(canonical_bytes(value).decode("utf-8"))


def require_hex_digest(value: str, *, name: str, length: int = 64) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise CanonicalIdentityError(f"{name} must be {length} lowercase hex")
    return value


def require_git_sha(value: str, *, name: str) -> str:
    if not isinstance(value, str) or len(value) not in (40, 64):
        raise CanonicalIdentityError(f"{name} must be a 40- or 64-hex SHA")
    lowered = value.lower()
    if any(ch not in "0123456789abcdef" for ch in lowered):
        raise CanonicalIdentityError(f"{name} must be a 40- or 64-hex SHA")
    return lowered


def _reject_private_keys(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            joined = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_PRIVATE_KEYS:
                raise CanonicalIdentityError(f"private field refused:{joined}")
            _reject_private_keys(inner, path=joined)
    elif isinstance(value, (list, tuple)):
        for index, inner in enumerate(value):
            _reject_private_keys(inner, path=f"{path}[{index}]")


def require_revise_findings(
    findings: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    if not findings:
        raise CanonicalIdentityError("REVISE requires actionable findings")
    normalized = []
    for finding in findings:
        if set(finding) != set(REVISE_FINDING_KEYS):
            raise CanonicalIdentityError("REVISE finding keys are not actionable")
        for key in REVISE_FINDING_KEYS:
            text = finding[key]
            if not isinstance(text, str) or not text.strip():
                raise CanonicalIdentityError(f"REVISE finding {key} is empty")
        _reject_private_keys(finding)
        normalized.append({key: finding[key] for key in REVISE_FINDING_KEYS})
    return tuple(normalized)


def lane_projection_digest(
    spec_digest: str,
    needs: Sequence[str],
    declared_outputs: Sequence[str],
    lane_kind: Optional[str] = None,
) -> str:
    payload = {
        "declared_outputs": list(declared_outputs),
        "needs": list(needs),
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "spec_digest": spec_digest,
    }
    kind = normalize_lane_kind(lane_kind)
    if kind is not None:
        payload["lane_kind"] = kind
    return digest_canonical(payload)



LANE_KIND_TESTS = "tests"
LANE_KIND_BUILD = "build"
_LANE_KINDS = frozenset({LANE_KIND_TESTS, LANE_KIND_BUILD})


def normalize_lane_kind(raw: Any) -> Optional[str]:
    if raw is None or raw == "":
        return None
    if raw in _LANE_KINDS:
        return str(raw)
    raise CanonicalIdentityError("lane_kind")


def lane_input_digest(members: Mapping[str, Any]) -> str:
    payload = json_ready(members)
    if payload.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        raise CanonicalIdentityError("input envelope schema_version")
    for key in (
        "run_id",
        "lane_id",
        "stage",
        "plan_revision",
        "plan_digest",
        "spec_digest",
        "lane_projection_digest",
        "input_artifact_ids",
    ):
        if key not in payload:
            raise CanonicalIdentityError(f"input envelope missing {key}")
    return digest_canonical(payload)


def planned_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    plan_artifact_ref: str,
    needs: Sequence[str],
    declared_outputs: Sequence[str],
) -> str:
    return lane_input_digest(
        {
            "declared_outputs": list(declared_outputs),
            "input_artifact_ids": [],
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "needs": list(needs),
            "plan_artifact_ref": plan_artifact_ref,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.PLANNED.value,
        }
    )


def writing_tests_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    lane_plan_id: str,
    test_review_id: str,
    integration_head: str,
    test_invalidation_id: str = NO_TEST_INVALIDATION,
) -> str:
    head = require_git_sha(integration_head, name="integration_head")
    invalidation = test_invalidation_id or NO_TEST_INVALIDATION
    return lane_input_digest(
        {
            "input_artifact_ids": [lane_plan_id, test_review_id, invalidation],
            "integration_head": head,
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.WRITING_TESTS.value,
            "test_invalidation": invalidation,
        }
    )


def active_test_invalidation_id(
    *,
    invalidation_id: str | None,
    invalidation_sequence: int | None,
    draft_sequence: int | None,
) -> str:
    if not invalidation_id or invalidation_sequence is None:
        return NO_TEST_INVALIDATION
    if draft_sequence is not None and invalidation_sequence <= draft_sequence:
        return NO_TEST_INVALIDATION
    return invalidation_id



def reviewing_tests_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    lane_plan_id: str,
    test_draft_id: str,
) -> str:
    return lane_input_digest(
        {
            "input_artifact_ids": [lane_plan_id, test_draft_id],
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.REVIEWING_TESTS.value,
        }
    )


def tests_sealed_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    lane_plan_id: str,
    test_draft_id: str,
    test_review_id: str,
) -> str:
    return lane_input_digest(
        {
            "input_artifact_ids": [lane_plan_id, test_draft_id, test_review_id],
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.TESTS_SEALED.value,
        }
    )


def building_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    input_artifact_ids: Sequence[str],
    entry_kind: BuildingEntryKind,
    builder_base_sha: str,
    prior_builder: str,
    code_review: str,
    base_invalidation: str,
) -> str:
    return lane_input_digest(
        {
            "base_invalidation": base_invalidation,
            "builder_base_sha": builder_base_sha,
            "code_review": code_review,
            "entry_kind": entry_kind.value,
            "input_artifact_ids": list(input_artifact_ids),
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "prior_builder": prior_builder,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.BUILDING.value,
        }
    )


def reviewing_code_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    lane_plan_id: str,
    sealed_bundle_id: str,
    builder_output_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
) -> str:
    return lane_input_digest(
        {
            "builder_base_sha": builder_base_sha,
            "candidate_ref": candidate_ref,
            "candidate_sha": candidate_sha,
            "input_artifact_ids": [lane_plan_id, sealed_bundle_id, builder_output_id],
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.REVIEWING_CODE.value,
        }
    )


def ready_to_merge_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    builder_output_id: str,
    code_review_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
    integration_head: str,
) -> str:
    return lane_input_digest(
        {
            "builder_base_sha": builder_base_sha,
            "candidate_ref": candidate_ref,
            "candidate_sha": candidate_sha,
            "input_artifact_ids": [builder_output_id, code_review_id],
            "integration_head": integration_head,
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.READY_TO_MERGE.value,
        }
    )


def base_invalidation_input_digest(
    *,
    run_id: str,
    lane_id: str,
    plan_revision: int,
    plan_digest: str,
    spec_digest: str,
    projection_digest: str,
    builder_output_id: str,
    code_review_id: str,
    stale_builder_base_sha: str,
    stale_candidate_sha: str,
    integration_head: str,
) -> str:
    return lane_input_digest(
        {
            "input_artifact_ids": [builder_output_id, code_review_id],
            "integration_head": integration_head,
            "lane_id": lane_id,
            "lane_projection_digest": projection_digest,
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "run_id": run_id,
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "spec_digest": spec_digest,
            "stage": LaneStage.READY_TO_MERGE.value,
            "stale_builder_base_sha": stale_builder_base_sha,
            "stale_candidate_sha": stale_candidate_sha,
        }
    )


def user_wait_input_digest(
    *,
    predecessor_artifact_id: str,
    predecessor_sequence: int,
    wait_reason: WaitReason,
    resume_stage: LaneStage,
    resume_input_digest: str,
    run_id: str,
    lane_id: str,
    plan_revision: int,
) -> str:
    payload = {
        "lane_id": lane_id,
        "plan_revision": plan_revision,
        "predecessor_artifact_id": predecessor_artifact_id,
        "predecessor_sequence": predecessor_sequence,
        "resume_input_digest": resume_input_digest,
        "resume_stage": resume_stage.value,
        "run_id": run_id,
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "wait_reason": wait_reason.value,
    }
    return digest_canonical(payload)


def user_decision_input_digest(
    *,
    user_wait_artifact_id: str,
    action: str,
    decision_payload: Mapping[str, Any],
) -> str:
    return digest_canonical(
        {
            "action": action,
            "decision_payload": json_ready(decision_payload),
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "user_wait_artifact_id": user_wait_artifact_id,
        }
    )


def final_review_input_fingerprint(
    *,
    integration_sha: str,
    plan_revision: int,
    plan_digest: str,
    lanes: Sequence[Mapping[str, str]],
) -> str:
    return digest_canonical(
        {
            "integration_sha": integration_sha,
            "lanes": [json_ready(row) for row in lanes],
            "plan_digest": plan_digest,
            "plan_revision": plan_revision,
            "schema_version": CANONICAL_SCHEMA_VERSION,
        }
    )


def runtime_state_fingerprint(realpath: str, device: int, inode: int) -> str:
    return digest_canonical(
        {
            "device": device,
            "inode": inode,
            "realpath": realpath,
            "schema_version": CANONICAL_SCHEMA_VERSION,
        }
    )


def topological_integration_order(lanes: Sequence["LaneProjection"]) -> Tuple[str, ...]:
    needs = {lane.lane_id: tuple(lane.needs) for lane in lanes}
    remaining = {lane.lane_id: set(lane.needs) for lane in lanes}
    ordered: list[str] = []
    while remaining:
        ready = sorted(lane_id for lane_id, deps in remaining.items() if not deps)
        if not ready:
            raise CanonicalIdentityError("plan DAG is cyclic")
        chosen = ready[0]
        ordered.append(chosen)
        del remaining[chosen]
        for deps in remaining.values():
            deps.discard(chosen)
    missing = {dep for deps in needs.values() for dep in deps if dep not in needs}
    if missing:
        raise CanonicalIdentityError("needs lane does not exist")
    return tuple(ordered)


def amendment_reset_stage(
    current: LaneStage,
    *,
    changed: bool,
    wait_reason: Optional[WaitReason],
    lane_kind: Optional[str] = None,
) -> LaneStage:
    if wait_reason is WaitReason.AMENDMENT_REQUIRED:
        if not changed:
            raise IllegalStageEdge("AMENDMENT_DOES_NOT_ADDRESS_REVIEW")
        return LaneStage.PLANNED
    if changed:
        return LaneStage.PLANNED
    if wait_reason in RESUMABLE_WAIT_REASONS:
        return LaneStage.WAITING_FOR_USER
    if current in UNSTARTED_STAGES:
        return current
    kind = normalize_lane_kind(lane_kind)
    if kind == LANE_KIND_BUILD:
        if current in STARTED_IMPLEMENTATION_STAGES or current is LaneStage.MERGED:
            return LaneStage.BUILDING
        raise IllegalStageEdge(f"no amendment reset for {current.value}")
    if current in STARTED_IMPLEMENTATION_STAGES or current is LaneStage.MERGED:
        return LaneStage.TESTS_SEALED
    raise IllegalStageEdge(f"no amendment reset for {current.value}")




def candidate_ref(run_id: str, lane_id: str, input_digest: str) -> str:
    return f"refs/maestro/candidates/{run_id}/{lane_id}/{input_digest}"


def integration_ref(run_id: str) -> str:
    return f"refs/maestro/integration/{run_id}"


def publication_ref(run_id: str, review_input_fingerprint: str) -> str:
    return f"refs/maestro/publications/{run_id}/{review_input_fingerprint}"


@dataclass(frozen=True)
class LaneProjection:
    lane_id: str
    needs: Tuple[str, ...]
    spec_digest: str
    declared_outputs: Tuple[str, ...]
    lane_projection_digest: str
    public_acceptance: Tuple[str, ...] = ()
    lane_kind: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.lane_id:
            raise CanonicalIdentityError("lane_id is empty")
        require_hex_digest(self.spec_digest, name="spec_digest")
        if tuple(self.needs) != tuple(sorted(self.needs)):
            raise CanonicalIdentityError("needs must be ordered by lane ID")
        if tuple(self.declared_outputs) != tuple(sorted(self.declared_outputs)):
            raise CanonicalIdentityError("declared outputs must be ordered by path")
        kind = normalize_lane_kind(self.lane_kind)
        object.__setattr__(self, "lane_kind", kind)
        expected = lane_projection_digest(
            self.spec_digest, self.needs, self.declared_outputs, lane_kind=kind
        )
        if self.lane_projection_digest != expected:
            raise CanonicalIdentityError("lane_projection_digest mismatch")


@dataclass(frozen=True)
class CompiledPlan:
    plan_bytes: bytes
    plan_artifact_ref: str
    plan_digest: str
    plan_revision: int
    lanes: Tuple[LaneProjection, ...]
    integration_order: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan_bytes, (bytes, bytearray)):
            raise CanonicalIdentityError("plan_bytes must be bytes")
        if digest_bytes(bytes(self.plan_bytes)) != self.plan_digest:
            raise CanonicalIdentityError("plan_digest mismatch")
        if self.plan_revision < 1:
            raise CanonicalIdentityError("plan_revision must be >= 1")
        if not self.plan_artifact_ref:
            raise CanonicalIdentityError("plan_artifact_ref is empty")
        ids = tuple(lane.lane_id for lane in self.lanes)
        if len(ids) != len(set(ids)):
            raise CanonicalIdentityError("duplicate lane_id")
        computed = topological_integration_order(self.lanes)
        if self.integration_order != computed:
            raise CanonicalIdentityError("integration_order mismatch")
        if ids and set(ids) != set(self.integration_order):
            raise CanonicalIdentityError("integration_order lane set mismatch")


@dataclass(frozen=True)
class RunBinding:
    runtime_state_root: str
    runtime_state_fingerprint: str
    integration_ref: str
    integration_initial_sha: str
    target_repository_root: str
    target_git_common_dir: str
    target_worktree_git_dir: str
    target_object_format: str
    target_repository_fingerprint: str
    target_sync_journal_fingerprint: str
    target_initial_main_sha: str
    target_main_ref: str

    def __post_init__(self) -> None:
        require_hex_digest(
            self.runtime_state_fingerprint, name="runtime_state_fingerprint"
        )
        require_hex_digest(
            self.target_repository_fingerprint, name="target_repository_fingerprint"
        )
        require_hex_digest(
            self.target_sync_journal_fingerprint,
            name="target_sync_journal_fingerprint",
        )
        require_git_sha(
            self.integration_initial_sha, name="integration_initial_sha"
        )
        require_git_sha(
            self.target_initial_main_sha, name="target_initial_main_sha"
        )
        if not self.integration_ref.startswith("refs/maestro/integration/"):
            raise CanonicalIdentityError("integration_ref name")
        if not self.target_main_ref.startswith("refs/"):
            raise CanonicalIdentityError("target_main_ref")


@dataclass(frozen=True)
class LaneReset:
    lane_id: str
    from_stage: LaneStage
    to_stage: LaneStage


@dataclass(frozen=True)
class RetainedInput:
    lane_id: str
    plan_revision: int
    lane_projection_digest: str
    stage: LaneStage
    input_digest: str
    artifact_ids: Tuple[str, ...]


@dataclass(frozen=True)
class LaneArtifact:
    kind: ArtifactKind
    plan_revision: int
    spec_digest: str
    lane_projection_digest: str
    input_digest: str
    output_digest: str
    artifact_ref: str
    payload: Mapping[str, Any]
    verdict: Optional[ReviewerVerdict] = None

    def __post_init__(self) -> None:
        if self.kind not in LANE_ARTIFACT_KINDS:
            raise CanonicalIdentityError("not a lane artifact kind")
        require_hex_digest(self.spec_digest, name="spec_digest")
        require_hex_digest(self.lane_projection_digest, name="lane_projection_digest")
        require_hex_digest(self.input_digest, name="input_digest")
        require_hex_digest(self.output_digest, name="output_digest")
        if not self.artifact_ref:
            raise CanonicalIdentityError("artifact_ref is empty")
        object.__setattr__(self, "payload", json_ready(self.payload))
        _reject_private_keys(self.payload)
        echoed = self.payload.get("input_digest")
        if echoed != self.input_digest:
            raise CanonicalIdentityError("payload input_digest mismatch")
        if self.kind in (ArtifactKind.TEST_REVIEW, ArtifactKind.CODE_REVIEW):
            if self.verdict is None:
                raise CanonicalIdentityError("review artifact requires verdict")
            if self.payload.get("verdict") != self.verdict.value:
                raise CanonicalIdentityError("payload verdict mismatch")
            if self.verdict is ReviewerVerdict.REVISE:
                require_revise_findings(self.payload.get("findings") or ())
            elif self.payload.get("findings"):
                raise CanonicalIdentityError("PASS findings must be empty")
        elif self.verdict is not None:
            raise CanonicalIdentityError("verdict only belongs on review artifacts")


@dataclass(frozen=True)
class RunArtifact:
    kind: ArtifactKind
    plan_revision: int
    input_digest: str
    output_digest: str
    artifact_ref: str
    payload: Mapping[str, Any]
    verdict: Optional[ReviewerVerdict] = None

    def __post_init__(self) -> None:
        if self.kind not in RUN_ARTIFACT_KINDS:
            raise CanonicalIdentityError("not a run artifact kind")
        require_hex_digest(self.input_digest, name="input_digest")
        require_hex_digest(self.output_digest, name="output_digest")
        object.__setattr__(self, "payload", json_ready(self.payload))
        _reject_private_keys(self.payload)
        if self.payload.get("input_digest") != self.input_digest:
            raise CanonicalIdentityError("payload input_digest mismatch")
        if self.kind is ArtifactKind.FINAL_INTEGRATION_REVIEW:
            if self.verdict is None:
                raise CanonicalIdentityError("final review requires verdict")
            if self.payload.get("verdict") != self.verdict.value:
                raise CanonicalIdentityError("payload verdict mismatch")
            if self.verdict is ReviewerVerdict.REVISE:
                require_revise_findings(self.payload.get("findings") or ())


@dataclass(frozen=True)
class StageEdge:
    current: LaneStage
    kind: ArtifactKind
    verdict: Optional[ReviewerVerdict]
    next_stage: LaneStage


COMPLETE_STAGE_EDGES: Tuple[StageEdge, ...] = (
    StageEdge(LaneStage.PLANNED, ArtifactKind.LANE_PLAN, None, LaneStage.WRITING_TESTS),
    StageEdge(
        LaneStage.WRITING_TESTS,
        ArtifactKind.TEST_DRAFT,
        None,
        LaneStage.REVIEWING_TESTS,
    ),
    StageEdge(
        LaneStage.REVIEWING_TESTS,
        ArtifactKind.TEST_REVIEW,
        ReviewerVerdict.PASS,
        LaneStage.TESTS_SEALED,
    ),
    StageEdge(
        LaneStage.REVIEWING_TESTS,
        ArtifactKind.TEST_REVIEW,
        ReviewerVerdict.REVISE,
        LaneStage.WRITING_TESTS,
    ),
    StageEdge(
        LaneStage.TESTS_SEALED,
        ArtifactKind.SEALED_TEST_BUNDLE,
        None,
        LaneStage.BUILDING,
    ),
    StageEdge(
        LaneStage.BUILDING,
        ArtifactKind.BUILDER_OUTPUT,
        None,
        LaneStage.REVIEWING_CODE,
    ),
    StageEdge(
        LaneStage.REVIEWING_CODE,
        ArtifactKind.CODE_REVIEW,
        ReviewerVerdict.PASS,
        LaneStage.READY_TO_MERGE,
    ),
    StageEdge(
        LaneStage.REVIEWING_CODE,
        ArtifactKind.CODE_REVIEW,
        ReviewerVerdict.REVISE,
        LaneStage.BUILDING,
    ),
    StageEdge(
        LaneStage.REVIEWING_CODE,
        ArtifactKind.TEST_INVALIDATION,
        None,
        LaneStage.WRITING_TESTS,
    ),
    StageEdge(
        LaneStage.READY_TO_MERGE,
        ArtifactKind.INTEGRATION_MERGE,
        None,
        LaneStage.MERGED,
    ),
    StageEdge(
        LaneStage.READY_TO_MERGE,
        ArtifactKind.BASE_INVALIDATION,
        None,
        LaneStage.BUILDING,
    ),

)


def next_stage_for(
    current: LaneStage,
    kind: ArtifactKind,
    verdict: Optional[ReviewerVerdict],
    lane_kind: Optional[str] = None,
) -> LaneStage:
    normalized = normalize_lane_kind(lane_kind)
    if (
        normalized == LANE_KIND_TESTS
        and current is LaneStage.TESTS_SEALED
        and kind is ArtifactKind.SEALED_TEST_BUNDLE
        and verdict is None
    ):
        return LaneStage.MERGED
    if (
        normalized == LANE_KIND_BUILD
        and current is LaneStage.PLANNED
        and kind is ArtifactKind.LANE_PLAN
        and verdict is None
    ):
        return LaneStage.BUILDING
    for edge in COMPLETE_STAGE_EDGES:
        if edge.current is current and edge.kind is kind and edge.verdict is verdict:
            return edge.next_stage
    raise IllegalStageEdge(f"{current.value}->{kind.value}")


def completed_stage_for(kind: ArtifactKind, payload: Mapping[str, Any]) -> LaneStage:
    if kind is ArtifactKind.LANE_PLAN:
        return LaneStage.PLANNED
    if kind is ArtifactKind.TEST_DRAFT:
        return LaneStage.WRITING_TESTS
    if kind is ArtifactKind.TEST_REVIEW:
        return LaneStage.REVIEWING_TESTS
    if kind is ArtifactKind.SEALED_TEST_BUNDLE:
        return LaneStage.TESTS_SEALED
    if kind is ArtifactKind.BUILDER_OUTPUT:
        return LaneStage.BUILDING
    if kind is ArtifactKind.CODE_REVIEW:
        return LaneStage.REVIEWING_CODE
    if kind is ArtifactKind.TEST_INVALIDATION:
        return LaneStage.REVIEWING_CODE
    if kind in (ArtifactKind.INTEGRATION_MERGE, ArtifactKind.BASE_INVALIDATION):
        return LaneStage.READY_TO_MERGE

    if kind is ArtifactKind.USER_WAIT:
        return LaneStage(payload["resume_stage"])
    if kind is ArtifactKind.USER_DECISION:
        return LaneStage.WAITING_FOR_USER
    raise CanonicalIdentityError(f"no completed stage for {kind.value}")


def derive_run_status(
    *,
    stages: Sequence[LaneStage],
    publication_for_active_fingerprint: bool,
    passing_final_review_for_active_fingerprint: bool,
) -> RunStatus:
    if publication_for_active_fingerprint:
        return RunStatus.COMPLETE
    if any(stage is LaneStage.WAITING_FOR_USER for stage in stages):
        return RunStatus.WAITING
    if any(stage is not LaneStage.MERGED for stage in stages):
        return RunStatus.EXECUTING
    if not passing_final_review_for_active_fingerprint:
        return RunStatus.INTEGRATION_REVIEW_PENDING
    return RunStatus.PUBLISHABLE
