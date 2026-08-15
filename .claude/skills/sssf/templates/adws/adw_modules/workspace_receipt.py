"""Signed, replay-safe authorization receipts for Maestro workspaces."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Tuple, Union

import fcntl

from . import receipt_crypto as crypto
from .finalization import Receipt as PlanReceipt
from .finalization import Verdict
from .workspace_canonical import canonicalize_workspace
from .workspace_digest import digest_of
from .workspace_model import RepositoryMode, WorkspacePlan

SCHEMA_V1 = "maestro-workspace-receipt.v1"
_HEX_40_OR_64 = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_PLAN_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AuthorizationError(RuntimeError):
    """A proposed receipt does not authorize the requested workspace."""


class ReceiptStoreLocationError(RuntimeError):
    """The receipt root is writable by a workspace participant."""


class ReceiptExists(RuntimeError):
    """A receipt is immutable once it has been signed and written."""


class ReceiptInvalid(RuntimeError):
    """Receipt bytes are malformed or do not describe this receipt schema."""


class SignatureMissing(RuntimeError):
    """A receipt exists without its detached signature."""


class SignatureInvalid(RuntimeError):
    """No configured public key verifies a receipt signature."""


class SigningKeyUnavailable(RuntimeError):
    """This receipt store can verify but has no key to mint receipts."""


def _require_created_at_epoch(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptInvalid("created_at_epoch must be a finite numeric value")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ReceiptInvalid(
            "created_at_epoch must be a finite numeric value") from exc
    if not finite:
        raise ReceiptInvalid("created_at_epoch must be a finite numeric value")


def _mode_value(mode: Union[RepositoryMode, str]) -> str:
    return mode.value if isinstance(mode, RepositoryMode) else str(mode)


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise ReceiptInvalid("{0} must be a 64-character lowercase hex digest".format(label))


def _require_commit(value: str) -> None:
    if not isinstance(value, str) or not _HEX_40_OR_64.fullmatch(value):
        raise ReceiptInvalid("base_commit must be a 40- or 64-character lowercase hex commit")


@dataclass(frozen=True)
class ParticipantAuthorization:
    """The participant vector element a workspace receipt authorizes."""

    repository_id: str
    mode: Union[RepositoryMode, str]
    base_commit: str
    plan_digest: Optional[str]
    target_branch: Optional[str]

    def __post_init__(self) -> None:
        if (not isinstance(self.repository_id, str) or
                not _REPOSITORY_ID.fullmatch(self.repository_id)):
            raise ReceiptInvalid("repository_id is not admitted by the workspace schema")
        mode = _mode_value(self.mode)
        if mode not in tuple(item.value for item in RepositoryMode):
            raise ReceiptInvalid("repository mode is not admitted")
        _require_commit(self.base_commit)
        if mode == RepositoryMode.WRITE.value:
            if self.plan_digest is None or self.target_branch is None:
                raise ReceiptInvalid("a writable participant requires plan_digest and target_branch")
            if (not isinstance(self.plan_digest, str) or
                    not _HEX_PLAN_DIGEST.fullmatch(self.plan_digest)):
                raise ReceiptInvalid("plan_digest must be a 64-character hex digest")
            if not isinstance(self.target_branch, str) or not self.target_branch:
                raise ReceiptInvalid("target_branch must be a nonempty string")
        elif self.plan_digest is not None or self.target_branch is not None:
            raise ReceiptInvalid("a read-only participant cannot carry a plan or target branch")

    def to_mapping(self) -> dict:
        return {"repository_id": self.repository_id, "mode": _mode_value(self.mode),
                "base_commit": self.base_commit, "plan_digest": self.plan_digest,
                "target_branch": self.target_branch}

    @classmethod
    def from_mapping(cls, mapping: object) -> "ParticipantAuthorization":
        if not isinstance(mapping, dict):
            raise ReceiptInvalid("participant must be an object")
        expected = {"repository_id", "mode", "base_commit", "plan_digest", "target_branch"}
        if set(mapping) != expected:
            raise ReceiptInvalid("participant fields do not match the frozen receipt schema")
        return cls(repository_id=mapping["repository_id"], mode=mapping["mode"],
                   base_commit=mapping["base_commit"], plan_digest=mapping["plan_digest"],
                   target_branch=mapping["target_branch"])


@dataclass(frozen=True)
class WorkspaceReceipt:
    """A signed authorization statement for exactly one workspace digest."""

    workspace_digest: str
    participants: Tuple[ParticipantAuthorization, ...]
    created_at_epoch: float = 0.0

    def __post_init__(self) -> None:
        _require_digest(self.workspace_digest, "workspace_digest")
        if not isinstance(self.participants, tuple) or not self.participants:
            raise ReceiptInvalid("participants must be a nonempty ordered tuple")
        if not all(isinstance(item, ParticipantAuthorization) for item in self.participants):
            raise ReceiptInvalid("participants must be ParticipantAuthorization values")
        participant_ids = tuple(item.repository_id for item in self.participants)
        if len(set(participant_ids)) != len(participant_ids):
            raise ReceiptInvalid("participants cannot repeat a repository_id")
        _require_created_at_epoch(self.created_at_epoch)

    def to_bytes(self) -> bytes:
        _require_created_at_epoch(self.created_at_epoch)
        payload = {"schema": SCHEMA_V1, "workspace_digest": self.workspace_digest,
                   "participants": [item.to_mapping() for item in self.participants],
                   "created_at_epoch": self.created_at_epoch}
        return (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "WorkspaceReceipt":
        if not isinstance(data, bytes):
            raise ReceiptInvalid("receipt bytes must be bytes")

        def object_without_duplicates(pairs):
            payload = {}
            for key, value in pairs:
                if key in payload:
                    raise ReceiptInvalid(
                        "receipt JSON contains a duplicate object field")
                payload[key] = value
            return payload

        try:
            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=object_without_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptInvalid("receipt bytes are not UTF-8 JSON") from exc
        expected = {"schema", "workspace_digest", "participants", "created_at_epoch"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ReceiptInvalid("receipt fields do not match the frozen receipt schema")
        if payload["schema"] != SCHEMA_V1:
            raise ReceiptInvalid("receipt schema is not admitted")
        if not isinstance(payload["participants"], list):
            raise ReceiptInvalid("participants must be an array")
        return cls(workspace_digest=payload["workspace_digest"],
                   participants=tuple(ParticipantAuthorization.from_mapping(item)
                                      for item in payload["participants"]),
                   created_at_epoch=payload["created_at_epoch"])

    def authorizes(self, workspace_digest: str,
                   participants: Sequence[ParticipantAuthorization]) -> bool:
        return self.workspace_digest == workspace_digest and self.participants == tuple(participants)


class WorkspaceReceiptStore:
    """Create-once signed receipts outside every participant boundary."""

    def __init__(self, root: Union[str, Path], *,
                 participant_repos: Sequence[Union[str, Path]],
                 data_dir: Union[str, Path], verify_keys: Sequence[bytes],
                 signing_seed: Optional[bytes] = None,
                 create: bool = True) -> None:
        self.root = Path(root).resolve()
        boundaries = tuple(Path(path).resolve() for path in participant_repos)
        boundaries += (Path(data_dir).resolve(),)
        for boundary in boundaries:
            if _is_inside(self.root, boundary):
                raise ReceiptStoreLocationError(
                    "receipt root must be outside every participant repository and SSSF data directory")
        self._verify_keys = tuple(verify_keys)
        self._signing_seed = signing_seed
        if (signing_seed is not None and
                crypto.seed_to_public_key(signing_seed) not in self._verify_keys):
            raise SigningKeyUnavailable(
                "the signing key's public key is absent from this store's "
                "verification keys")
        self._create = create
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self._replace = os.replace

    def _confined_path(self, filename: str) -> Path:
        candidate = self.root / filename
        if candidate.is_symlink() or (
                candidate.exists() and
                not _is_inside(candidate.resolve(), self.root)):
            raise ReceiptInvalid("receipt path resolves outside the receipt store")
        return candidate

    def path_for(self, workspace_digest: str) -> Path:
        _require_digest(workspace_digest, "workspace_digest")
        return self._confined_path(workspace_digest + ".json")

    def signature_path_for(self, workspace_digest: str) -> Path:
        _require_digest(workspace_digest, "workspace_digest")
        return self._confined_path(workspace_digest + ".json.sig")

    @contextlib.contextmanager
    def _locked(self, workspace_digest: str) -> Iterator[None]:
        lock_path = self._confined_path(workspace_digest + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def has(self, workspace_digest: str) -> bool:
        _require_digest(workspace_digest, "workspace_digest")
        if not self.root.is_dir():
            return False
        return (self.path_for(workspace_digest).is_file() and
                self.signature_path_for(workspace_digest).is_file())

    def _stage(self, destination: Path, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace-receipt-",
                                                 dir=str(self.root))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._replace(str(temporary_path), str(destination))
            directory = os.open(str(self.root), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _repair_or_refuse(self, receipt: WorkspaceReceipt,
                          data: bytes, signature: bytes) -> None:
        path = self.path_for(receipt.workspace_digest)
        signature_path = self.signature_path_for(receipt.workspace_digest)
        receipt_exists = path.is_file()
        signature_exists = signature_path.is_file()
        if receipt_exists and signature_exists:
            raise ReceiptExists("a workspace receipt already exists for {0}".format(
                receipt.workspace_digest))
        if receipt_exists and path.read_bytes() != data:
            raise ReceiptExists("a conflicting partial workspace receipt exists")
        expected_signature = signature.hex() + "\n"
        if not receipt_exists:
            self._stage(signature_path, expected_signature.encode("ascii"))
            self._stage(path, data)
        elif not signature_exists:
            self._stage(signature_path, expected_signature.encode("ascii"))

    def write(self, receipt: WorkspaceReceipt) -> Path:
        if self._signing_seed is None:
            raise SigningKeyUnavailable("a signing seed is required to create a workspace receipt")
        if not self._create:
            raise FileNotFoundError("workspace receipt store is opened read-only")
        data = receipt.to_bytes()
        signature = crypto.sign(self._signing_seed, data)
        with self._locked(receipt.workspace_digest):
            self._repair_or_refuse(receipt, data, signature)
        return self.path_for(receipt.workspace_digest)

    def load(self, workspace_digest: str) -> WorkspaceReceipt:
        _require_digest(workspace_digest, "workspace_digest")
        path = self.path_for(workspace_digest)
        signature_path = self.signature_path_for(workspace_digest)
        if not path.is_file():
            raise FileNotFoundError("no workspace receipt for {0}".format(workspace_digest))
        if not signature_path.is_file():
            raise SignatureMissing("workspace receipt has no detached signature")
        data = path.read_bytes()
        try:
            signature_text = signature_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise SignatureInvalid("workspace receipt signature is not UTF-8") from exc
        signature_hex = signature_text[:-1] if signature_text.endswith("\n") else signature_text
        if not re.fullmatch(r"[0-9a-fA-F]{128}", signature_hex):
            raise SignatureInvalid("workspace receipt signature is not hexadecimal")
        signature = bytes.fromhex(signature_hex)
        if not any(crypto.verify(key, data, signature) for key in self._verify_keys):
            raise SignatureInvalid("workspace receipt verifies under none of the configured keys")
        receipt = WorkspaceReceipt.from_bytes(data)
        if receipt.workspace_digest != workspace_digest:
            raise ReceiptInvalid("receipt file name and stored workspace digest differ")
        return receipt


def _participants(plan: WorkspacePlan) -> Tuple[ParticipantAuthorization, ...]:
    return tuple(ParticipantAuthorization(
        repository_id=spec.repository_id, mode=spec.mode, base_commit=spec.base_commit,
        plan_digest=spec.plan_digest, target_branch=spec.target_branch)
        for spec in plan.repositories)


def finalize(workspace_digest: str, plan: WorkspacePlan,
             load_plan_receipt: Callable[[str], object], store: WorkspaceReceiptStore,
             now: Callable[[], float] = time.time) -> WorkspaceReceipt:
    """Mint or safely replay an authorization receipt for canonical plan bytes."""
    expected_digest = digest_of(canonicalize_workspace(plan))
    if workspace_digest != expected_digest:
        raise AuthorizationError("supplied workspace digest does not identify this workspace plan")
    participants = _participants(plan)
    if store.has(workspace_digest):
        existing = store.load(workspace_digest)
        if existing.authorizes(workspace_digest, participants):
            return existing
        raise AuthorizationError("an existing workspace receipt binds a different participant vector")
    for participant in participants:
        if _mode_value(participant.mode) != RepositoryMode.WRITE.value:
            continue
        try:
            plan_receipt = load_plan_receipt(participant.plan_digest or "")
        except Exception as exc:
            raise AuthorizationError("the participant plan receipt cannot be loaded") from exc
        if not isinstance(plan_receipt, PlanReceipt):
            raise AuthorizationError("the participant plan receipt is not a finalization receipt")
        if plan_receipt.verdict is not Verdict.PASS:
            raise AuthorizationError("the participant plan receipt did not pass finalization")
        if plan_receipt.plan_digest != participant.plan_digest:
            raise AuthorizationError("the participant receipt authorizes different plan bytes")
    receipt = WorkspaceReceipt(workspace_digest=workspace_digest,
                               participants=participants, created_at_epoch=now())
    try:
        store.write(receipt)
    except ReceiptExists:
        existing = store.load(workspace_digest)
        if existing.authorizes(workspace_digest, participants):
            return existing
        raise AuthorizationError("an existing workspace receipt binds a different participant vector")
    return receipt


def _is_inside(candidate: Path, boundary: Path) -> bool:
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


__all__ = ["SCHEMA_V1", "AuthorizationError", "ReceiptStoreLocationError",
           "ReceiptExists", "ReceiptInvalid", "SignatureMissing", "SignatureInvalid",
           "SigningKeyUnavailable", "ParticipantAuthorization", "WorkspaceReceipt",
           "WorkspaceReceiptStore", "finalize"]
