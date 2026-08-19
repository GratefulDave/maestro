#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""make_config — generate adws/adw_sssf_config/sssf.config.yaml with great defaults.

Usage:
    uv run <skill>/scripts/make_config.py [--force]
"""

import argparse
import importlib.util
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE = TEMPLATES / "sssf.config.yaml"


def _load_runtime_sync():
    """Load the template's verified-copy primitive; see install.py's copy."""
    module_path = TEMPLATES / "adws" / "tools" / "runtime_sync.py"
    spec = importlib.util.spec_from_file_location("runtime_sync", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_sync = _load_runtime_sync()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    dest = Path.cwd() / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    if dest.exists() and not args.force:
        print(f"{dest} already exists — use --force to overwrite")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = runtime_sync.copy_verified(TEMPLATE, dest)
    print(f"wrote {dest} (sha256 {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
