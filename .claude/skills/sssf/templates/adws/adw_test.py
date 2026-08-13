#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""The factory's own test suite — stdlib unittest, no network, no API keys.

Every claim the architecture makes about concurrency, isolation, and
verification is settled here by execution rather than by reading. A claim
with no test in this suite is not a property of the system; it is a
sentence in a document.

Usage:
    uv run adws/adw_test.py              # the whole suite
    uv run adws/adw_test.py -v           # per-test names
    uv run adws/adw_test.py -k identity  # only tests matching a substring

Dependencies mirror the ADW scripts because the suite imports adw_modules,
which imports pydantic and rich at module scope.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parent
TESTS = ADWS / "tests"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each test name as it runs")
    parser.add_argument("-k", "--match", default="",
                        help="run only tests whose name contains this substring")
    args = parser.parse_args()

    if not TESTS.is_dir():
        print(f"no test directory at {TESTS}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(ADWS))
    loader = unittest.TestLoader()
    if args.match:
        loader.testNamePatterns = [f"*{args.match}*"]
    suite = loader.discover(start_dir=str(TESTS), top_level_dir=str(TESTS))
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
