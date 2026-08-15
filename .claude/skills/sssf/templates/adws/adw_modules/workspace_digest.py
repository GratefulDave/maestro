"""Byte-only workspace identity.

This module intentionally imports neither the workspace model nor its
canonicalizer.  Runtime callers hash the bytes they loaded; they cannot
re-serialize a workspace before deriving its identity.
"""

from __future__ import annotations

import hashlib


def digest_of(stored: bytes) -> str:
    """Return the lowercase SHA-256 digest of the supplied stored bytes."""
    if not isinstance(stored, (bytes, bytearray)):
        raise TypeError(
            "digest_of takes stored workspace bytes, got {0}".format(
                type(stored).__name__))
    return hashlib.sha256(bytes(stored)).hexdigest()
