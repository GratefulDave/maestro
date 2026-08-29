"""Git-receipt sentinels.

Canonical JSON identity is ``scheduler_types.canonical_bytes`` /
``digest_bytes`` / ``digest_canonical``. This module does not reimplement
them and does not write lane stage.
"""

from __future__ import annotations

NO_BLOB = "NO_BLOB"
NO_PRIOR_REF = "NO_PRIOR_REF"

__all__ = ["NO_BLOB", "NO_PRIOR_REF"]
