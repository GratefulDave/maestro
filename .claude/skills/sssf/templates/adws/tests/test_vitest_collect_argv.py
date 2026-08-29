"""Collecting a test file must never rewrite it.

`run-6b8f607d89744eeb94a79713b3b5d234` retried `lane-wp6-tests` until it was
stopped by hand. Every attempt wrote real TypeScript cases, committed them,
and was refused

    TESTS_NO_NEW_CASES: no new collected case versus the parent commit

`VitestCaseRunner.collect` built `vitest list --json <paths>`. vitest's
`--json` takes an *optional value*, so the first path was parsed as "write
the listing to this file": vitest OVERWROTE the tester's 661-line test file
with 47KB of its own JSON, printed nothing to stdout, and exited 0. Zero node
ids became zero new cases, the node never verified, never merged, and its
derived reviewer never dispatched -- and the next attempt's collection ate
the next attempt's file.

The measurement destroyed the evidence it was measuring, and the refusal it
produced named the tester. No edit to the cases could ever have satisfied it.

`test_the_argv_puts_the_filters_before_the_flag` fails on the old order with
no vitest installed at all. The two `RealVitest` cases execute vitest for
real, because the argv order is a fact about vitest's parser and a stubbed
subprocess cannot observe it -- which is exactly why the bug shipped: nothing
in the suite had ever run this runner.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adw_modules import tests_chain as tc


PROBE = """import { describe, it, expect } from "vitest";

describe("probe", () => {
  it("alpha", () => { expect(1).toBe(1); });
  it("beta", () => { expect(2).toBe(2); });
});
"""

CONFIG = """import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["**/*.test.ts"] },
});
"""


class VitestCollectArgv(unittest.TestCase):
    """The order, asserted without running anything."""

    def test_the_argv_puts_the_filters_before_the_flag(self) -> None:
        seen = {}

        def _capture(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.test.ts").write_text(PROBE, encoding="utf-8")
            (root / "b.test.ts").write_text(PROBE, encoding="utf-8")
            original = subprocess.run
            subprocess.run = _capture
            try:
                tc.VitestCaseRunner().collect(root, ("a.test.ts", "b.test.ts"))
            finally:
                subprocess.run = original

        argv = seen["argv"]
        self.assertIn("--json", argv)
        for path in ("a.test.ts", "b.test.ts"):
            self.assertIn(path, argv)
            self.assertLess(
                argv.index(path), argv.index("--json"),
                "every filter must precede --json: vitest reads the token "
                "after --json as the file to WRITE the listing into, so a "
                "path there is overwritten with vitest's own output",
            )
        self.assertEqual(
            argv[-1], "--json",
            "--json must be last so no path can be taken as its value",
        )


def _vitest_project(root: Path) -> bool:
    """Install a minimal vitest project under `root`. False when offline."""
    (root / "package.json").write_text(
        json.dumps({"name": "probe", "private": True, "type": "module",
                    "devDependencies": {"vitest": "^3"}}),
        encoding="utf-8")
    (root / "vitest.config.ts").write_text(CONFIG, encoding="utf-8")
    (root / "probe.test.ts").write_text(PROBE, encoding="utf-8")
    installed = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=str(root), capture_output=True, text=True)
    return installed.returncode == 0 and (root / "node_modules").is_dir()


@unittest.skipIf(shutil.which("npm") is None, "npm is not installed")
@unittest.skipIf(os.environ.get("ADW_SKIP_NETWORK_TESTS") == "1",
                 "ADW_SKIP_NETWORK_TESTS=1")
class RealVitestDoesNotEatTheFile(unittest.TestCase):
    """Executed against vitest itself. A stub cannot observe an argv parser."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        if not _vitest_project(cls.root):
            cls._tmp.cleanup()
            raise unittest.SkipTest("could not install vitest (offline?)")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        (self.root / "probe.test.ts").write_text(PROBE, encoding="utf-8")

    def test_collect_returns_the_cases(self) -> None:
        found = tc.VitestCaseRunner().collect(
            self.root, ("probe.test.ts",), timeout_s=300.0)
        self.assertEqual(len(found), 2, "vitest collected {0!r}".format(found))

    def test_collect_leaves_the_test_file_byte_identical(self) -> None:
        before = (self.root / "probe.test.ts").read_bytes()
        tc.VitestCaseRunner().collect(
            self.root, ("probe.test.ts",), timeout_s=300.0)
        self.assertEqual(
            (self.root / "probe.test.ts").read_bytes(), before,
            "collection rewrote the file it was measuring",
        )


if __name__ == "__main__":
    unittest.main()
