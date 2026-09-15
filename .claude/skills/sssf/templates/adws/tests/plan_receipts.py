"""Genuinely signed plan-contract receipts for tests.

Signed with `plan_approval.signature`, which `tests/test_plan_approval.py` pins
byte-for-byte against planctl's own `receipt_signature`, so a receipt made
here is one planctl would have signed with the same key. Nothing is fabricated:
a receipt without a valid signature is refused by the code under test.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from adw_modules import plan_approval
from adw_modules import route_admission as admission

#: 32 bytes as hex, the shape `route_admission.provision_keys` mints.
KEY_MATERIAL = "ab" * 32
KEY = plan_approval.key_bytes(KEY_MATERIAL)


def _ir(ir_bytes: bytes) -> dict:
    try:
        ir = json.loads(ir_bytes.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    return ir if isinstance(ir, dict) else {}


#: planctl's ID_BEARING_COLLECTIONS, for `record_manifest`.
_ID_BEARING = (
    ("claims", "claim_id"), ("fixtures", "fixture_id"), ("lanes", "lane_id"),
    ("links", "link_id"), ("rendered_bindings", "binding_id"),
    ("requirements", "requirement_id"), ("seams", "seam_id"),
    ("source_artifacts", "source_id"), ("verifiers", "verifier_id"),
)


def record_manifest(ir: dict) -> dict:
    """planctl's `plan_record_manifest`, derived from the IR (parity-tested)."""
    manifest = {}
    for name, id_field in _ID_BEARING:
        digests = {
            record[id_field]: hashlib.sha256(plan_approval.canonical_json(record)).hexdigest()
            for record in ir.get(name) or []
            if isinstance(record, dict) and isinstance(record.get(id_field), str)
        }
        if digests:
            manifest[name] = dict(sorted(digests.items()))
    return manifest


def source_inventory_sha256(ir: dict) -> str:
    """planctl's `source_inventory_digest` for repository-relative sources (parity-tested)."""
    inventory = []
    for source in ir.get("source_artifacts") or []:
        if not (isinstance(source, dict) and all(
                isinstance(source.get(k), str) for k in ("source_id", "path", "sha256"))):
            continue
        item = {
            "source_id": source["source_id"],
            "canonical_path": Path(os.path.normpath(source["path"])).as_posix(),
            "sha256": source["sha256"],
            "required": source.get("required"),
        }
        if "access" in source:
            item["access"] = source["access"]
        inventory.append(item)
    inventory.sort(key=lambda item: (item["source_id"], item["canonical_path"]))
    return hashlib.sha256(plan_approval.canonical_json(inventory)).hexdigest()


def signed_receipt(
    ir_bytes: bytes,
    *,
    key: bytes = KEY,
    rendered: bytes = b"<html></html>",
    findings: bytes = b'{"findings": []}',
    **overrides: Any,
) -> dict:
    """A receipt whose every digest is derived from the IR it approves."""
    ir = _ir(ir_bytes)
    receipt = {
        "schema_version": plan_approval.RECEIPT_VERSION,
        "verdict": "PASS",
        "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
        "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
        "source_inventory_sha256": source_inventory_sha256(ir),
        "record_manifest": record_manifest(ir),
        "findings_sha256": hashlib.sha256(findings).hexdigest(),
        "question_surface_sha256": plan_approval.question_surface_sha256(ir),
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


def write_deployment_config(adws_dir: Path, state_root: Path, **extra: Any) -> Path:
    """The template's own config with this deployment's state root and extras."""
    import yaml

    template = Path(__file__).resolve().parents[1] / "maestro.config.yaml"
    config = yaml.safe_load(template.read_text(encoding="utf-8"))
    config["runtime_state_root"] = str(state_root)
    config.update({key: str(value) for key, value in extra.items()})
    path = Path(adws_dir) / "maestro.config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    return path
