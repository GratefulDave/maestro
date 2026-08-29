"""SHA-256 identity over already-canonical bytes.

The digest input is the stored canonical bytes, never a re-serialisation of a
parsed model. This module imports no plan model.
"""

from __future__ import annotations

import hashlib


def digest_of(stored: bytes) -> str:
    """Lowercase hex SHA-256 of canonical stored bytes."""
    if not isinstance(stored, (bytes, bytearray)):
        raise TypeError(
            "digest_of takes stored bytes; got {0}".format(type(stored).__name__)
        )
    return hashlib.sha256(bytes(stored)).hexdigest()
