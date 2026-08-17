"""Suite-wide isolation for state Maestro writes outside the repository.

`_register_installation` records each configured installation in a registry the
dashboard reads, and its default location is the operator's home directory.
Every test that drives a configured verb would otherwise append to the real
registry, so the whole suite is pointed at a temporary file instead.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_maestro_registry():
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("MAESTRO_REGISTRY")
        os.environ["MAESTRO_REGISTRY"] = str(Path(tmp) / "registry.json")
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("MAESTRO_REGISTRY", None)
            else:
                os.environ["MAESTRO_REGISTRY"] = previous
