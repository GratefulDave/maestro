"""Canonical plan bytes — authoring and validation only (§6.3).

`canonicalize(plan) -> bytes` and `digest_of(bytes) -> sha256` are **two
disjoint functions with disjoint call sites**. This module holds the first
and is imported by exactly two places: authoring, which writes the file, and
validation, which checks the stored file is already in canonical form.

Nothing on the runtime path may import this module. `plan_digest` is what
the scheduler, replay lookup, and publication use, and it cannot reach a
model to re-serialise one. The boundary is asserted by parsing every module
in the tree in `tests/test_step2_plan_model.py` — Step 2's shipped invariant
(§12.2): *the runtime never re-canonicalizes.*

Canonical form is UTF-8 JSON, keys sorted, no insignificant whitespace, one
trailing newline. It is a property of the bytes on disk rather than a step
performed at load: the digest is always taken over the stored bytes, and
validation's job is to refuse bytes that are not already canonical rather
than to quietly rewrite them into digest agreement.
"""

from __future__ import annotations

import json

from .plan_model import Plan, PlanParseError, parse_bytes


def canonicalize(plan: Plan) -> bytes:
    """The canonical bytes for a parsed plan.

    Used at authoring time to write the file, and at validation time to check
    the stored file already equals its own canonical form. It is never used
    to produce the input to a digest at run time — see the module docstring.
    """
    payload = plan.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def is_canonical(stored: bytes) -> bool:
    """Whether the stored bytes are already in canonical form.

    False for bytes that parse but are formatted differently. That is not
    pedantry: two byte-different files with one meaning would carry two
    digests, which is two identities for one plan — the multiple-
    representations root cause (RC1, §4) at the identity layer.
    """
    try:
        plan = parse_bytes(stored)
    except PlanParseError:
        return False
    return canonicalize(plan) == stored
