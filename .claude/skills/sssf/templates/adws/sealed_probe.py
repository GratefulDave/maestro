#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml", "python-dotenv"]
# ///
"""Builder-facing sealed-suite probe. Not an operator verb.

The operator surface stays frozen at `run start|resume|amend|status`, so this
is a separate entrypoint rather than a `maestro.py run` verb. It parses
nothing: the module owns the arguments and the output.

The inline script metadata is what lets it run from a builder checkout that is
not a Python project -- `uv run` resolves this script's own dependencies rather
than the checkout's, so a TypeScript repository can invoke it unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules.sealed_probe import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
