"""Authenticated approval evidence for an executable plan.

Two records, one primitive. Both use planctl's receipt format exactly:
HMAC-SHA256, keyed by the deployment's reviewer key
(`<runtime_state_root>/keys/reviewer-hmac.key`, the value `run attend` already
hands planctl), over the canonical JSON of the record without its `signature`
(UTF-8, sorted keys, compact separators, no NaN). `reviewer_key_id` is the
SHA-256 of the key. `tests/test_plan_approval.py` pins this against planctl's
own `receipt_signature` so the two cannot drift.

- `verify_receipt` authenticates a `plan-contract-review.v1` receipt: the
  signature, algorithm and key id, the verdict, the IR (and rendered) digests,
  and the two-implementations pass (`findings_sha256`,
  `question_surface_sha256`). A field's presence is author-controlled data; only
  the signature makes it evidence.
- `approval_record` / `verify_approval` bind a projected plan's digest to that
  receipt. The trusted projection writes the record, and `run start` refuses
  a plan without a valid one. Nothing without the key can produce it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Optional

RECEIPT_VERSION = "plan-contract-review.v1"
SIGNATURE_ALGORITHM = "HMAC-SHA256"
APPROVAL_VERSION = "maestro-plan-approval.v1"
_SHA256_HEX = frozenset("0123456789abcdef")


class ApprovalRefused(ValueError):
    """The approval evidence does not authenticate. `code` names the check."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__("{0}:{1}".format(code, detail) if detail else code)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def key_bytes(material: str) -> bytes:
    """The key as planctl reads it: the stored text, UTF-8 encoded."""
    return material.strip().encode("utf-8")


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def signature(record: Mapping[str, Any], key: bytes) -> str:
    payload = {name: value for name, value in record.items() if name != "signature"}
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA256_HEX


def _authenticated(record: Mapping[str, Any], key: bytes, prefix: str) -> None:
    if record.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        raise ApprovalRefused(prefix + "_SIGNATURE_ALGORITHM")
    if record.get("reviewer_key_id") != key_id(key):
        raise ApprovalRefused(prefix + "_KEY_ID")
    signed = record.get("signature")
    if not isinstance(signed, str) or not hmac.compare_digest(
        signed, signature(record, key)
    ):
        raise ApprovalRefused(prefix + "_SIGNATURE")


def verify_receipt(
    ir_bytes: bytes,
    receipt: Mapping[str, Any],
    rendered: Optional[bytes],
    key: bytes,
) -> None:
    """Refuse unless `receipt` is planctl's signed PASS over these IR bytes."""
    if not isinstance(receipt, Mapping):
        raise ApprovalRefused("RECEIPT_SCHEMA")
    if receipt.get("schema_version") != RECEIPT_VERSION:
        raise ApprovalRefused("RECEIPT_SCHEMA")
    if receipt.get("verdict") != "PASS":
        raise ApprovalRefused("RECEIPT_NOT_PASS")
    _authenticated(receipt, key, "RECEIPT")
    if receipt.get("ir_sha256") != hashlib.sha256(ir_bytes).hexdigest():
        raise ApprovalRefused("RECEIPT_IR_MISMATCH")
    if not _sha256_hex(receipt.get("rendered_sha256")):
        raise ApprovalRefused("RECEIPT_RENDERED_MISMATCH")
    if rendered is not None and (
        receipt["rendered_sha256"] != hashlib.sha256(rendered).hexdigest()
    ):
        raise ApprovalRefused("RECEIPT_RENDERED_MISMATCH")
    # planctl records the two-implementations pass it signed over; the
    # signature is what makes these two values evidence rather than claims.
    if not (
        _sha256_hex(receipt.get("findings_sha256"))
        and _sha256_hex(receipt.get("question_surface_sha256"))
    ):
        raise ApprovalRefused("RECEIPT_WITHOUT_FINDINGS")
    reviewer = receipt.get("reviewer")
    if not (
        isinstance(reviewer, Mapping)
        and isinstance(reviewer.get("id"), str)
        and isinstance(reviewer.get("vendor"), str)
    ):
        raise ApprovalRefused("RECEIPT_REVIEWER")


def approval_record(
    plan_digest: str, receipt: Mapping[str, Any], key: bytes
) -> dict:
    """What the trusted projection writes into the plan it authored."""
    record = {
        "schema_version": APPROVAL_VERSION,
        "plan_digest": plan_digest,
        "receipt": dict(receipt),
        "signature_algorithm": SIGNATURE_ALGORITHM,
        "reviewer_key_id": key_id(key),
    }
    record["signature"] = signature(record, key)
    return record


def verify_approval(
    approval: Any, plan_digest: str, key: bytes
) -> None:
    """Refuse unless `approval` binds exactly this plan digest to a signed receipt."""
    if not isinstance(approval, Mapping):
        raise ApprovalRefused("PLAN_UNAPPROVED", "the plan carries no approval record")
    if approval.get("schema_version") != APPROVAL_VERSION:
        raise ApprovalRefused("PLAN_UNAPPROVED", "unknown approval schema")
    _authenticated(approval, key, "PLAN_APPROVAL")
    if approval.get("plan_digest") != plan_digest:
        raise ApprovalRefused("PLAN_APPROVAL_DIGEST")
    receipt = approval.get("receipt")
    if not isinstance(receipt, Mapping) or receipt.get("verdict") != "PASS":
        raise ApprovalRefused("PLAN_APPROVAL_RECEIPT")
    _authenticated(receipt, key, "RECEIPT")
    if not (
        _sha256_hex(receipt.get("findings_sha256"))
        and _sha256_hex(receipt.get("question_surface_sha256"))
    ):
        raise ApprovalRefused("RECEIPT_WITHOUT_FINDINGS")
