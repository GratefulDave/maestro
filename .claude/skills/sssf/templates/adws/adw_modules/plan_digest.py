"""The one identity function (§6.3).

`digest_of(bytes) -> sha256` is the only identity function used by replay
lookup, publication, and the scheduler. It is a separate module from
`canonicalize` on purpose, and the separation is the mechanism rather than a
filing convention:

This module imports no model, no parser, and no serializer. It cannot take a
digest over a re-serialisation of a parsed model, because it has no way to
reach one. §6.3's rule — *the digest input is the stored bytes, never a
re-serialisation of a parsed model* — is therefore enforced by what is
absent here rather than by everyone remembering it, and the absence is
asserted in `tests/test_step2_plan_model.py`.

That rule is what stops the plan layer reintroducing the append-only trap at
a different level: with no load-time recomputation anywhere, a model change
can never move a published digest.
"""

from __future__ import annotations

import hashlib


def digest_of(stored: bytes) -> str:
    """The sha256 of the plan's *stored* bytes, as lowercase hex.

    Refuses anything but bytes. A `str` would have to be encoded, and the
    choice of encoding would be a second canonicalization performed here,
    which is exactly the thing this module exists to make unreachable.
    """
    if not isinstance(stored, (bytes, bytearray)):
        raise TypeError(
            "digest_of takes the stored bytes of a plan file (§6.3); a parsed "
            f"model or a str would mean re-serialising to get here, got "
            f"{type(stored).__name__}")
    return hashlib.sha256(bytes(stored)).hexdigest()
