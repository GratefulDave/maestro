"""Genuinely signed plan-contract receipts for tests.

Signed with `plan_approval.signature`, which `tests/test_plan_approval.py` pins
byte-for-byte against planctl's own `receipt_signature`, so a receipt made
here is one planctl would have signed with the same key. Nothing is fabricated:
a receipt without a valid signature is refused by the code under test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from adw_modules import plan_approval
from adw_modules import route_admission as admission

#: 32 bytes as hex, the shape `route_admission.provision_keys` mints.
KEY_MATERIAL = "ab" * 32
KEY = plan_approval.key_bytes(KEY_MATERIAL)


def signed_receipt(
    ir_bytes: bytes,
    *,
    key: bytes = KEY,
    rendered: bytes = b"<html></html>",
    **overrides: Any,
) -> dict:
    receipt = {
        "schema_version": plan_approval.RECEIPT_VERSION,
        "verdict": "PASS",
        "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
        "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
        "source_inventory_sha256": "1" * 64,
        "record_manifest": {},
        "findings_sha256": "2" * 64,
        "question_surface_sha256": "3" * 64,
        "validator_version": "test",
        "reviewer": {"id": "independent-reviewer", "vendor": "other-vendor"},
        "signature_algorithm": plan_approval.SIGNATURE_ALGORITHM,
        "reviewer_key_id": plan_approval.key_id(key),
    }
    receipt.update(overrides)
    receipt["signature"] = plan_approval.signature(receipt, key)
    return receipt


def install_key(state_root: Path, material: str = KEY_MATERIAL) -> None:
    """Put the reviewer key where a deployment's runtime keeps it."""
    keys = Path(state_root) / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text(material + "\n", encoding="ascii")


def approve_plan_bytes(stored: bytes, *, key: bytes = KEY,
                       receipt: Optional[dict] = None) -> bytes:
    """Add the approval record the trusted projection writes."""
    import json

    from adw_modules import plan_author, plan_compiler

    document = json.loads(stored)
    document.pop("approval", None)
    digest = plan_compiler.compile_plan(plan_author.author_plan(document)).plan_digest
    document["approval"] = plan_approval.approval_record(
        digest, receipt or signed_receipt(b"{}", key=key), key
    )
    return plan_author.author_plan(document)
