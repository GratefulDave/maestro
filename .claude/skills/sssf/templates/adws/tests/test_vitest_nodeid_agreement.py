"""A collected vitest case id must be the id its report prints back.

`run-9f20c17ffc22497b957bd5be95dc1ddf` refused `lane-wp6-tests` with

    TESTS_NO_NEW_CASES: parent run collected 0, fewer than 9 new case(s)

after the two earlier bugs on this path were fixed. Collection worked: nine
cases. The parent run worked: the same nine cases, every one of them red, for
exactly the reason a TDD test is red at its parent. `adjudicate_parent_red`
still saw zero, because the two ids never met.

`vitest list --json` prints a case as `Suite > title`. `--reporter=json`
ships a `fullName` that joins the same parts with a plain space.
`VitestCaseRunner.run` runs the whole suite and keeps the outcomes whose id
is in the collected set, so the kept set was empty for every vitest node
that ever ran -- `set(collected) & set(reported) == {}` by construction, with
both sides individually correct and neither one printing a complaint.

Three bugs, one lane, one byte-identical family of refusal, each sufficient
on its own and each hiding the next. The rule earned twice already holds a
third time: the first sufficient explanation is not the explanation.

`test_the_report_name_matches_the_list_name` fails on the old `fullName`
build with nothing installed. `RealVitestNodeIdsAgree` executes vitest and
intersects the two real outputs, because the disagreement is a fact about
two of vitest's own surfaces -- the hand-written report in
`test_test_gate_strength.py` asserts an id it also invented, so it agreed
with itself while production agreed with nothing.
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

describe("outer", () => {
  describe("inner", () => {
    it("alpha", () => { expect(1).toBe(1); });
  });
  it("beta", () => { expect(2).toBe(2); });
});
"""

CONFIG = """import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["**/*.test.ts"] },
});
"""


class VitestReportNameShape(unittest.TestCase):
    """The id shape, asserted without running anything."""

    def test_the_report_name_matches_the_list_name(self) -> None:
        """`ancestorTitles + title`, joined the way `list` joins them."""
        report = json.dumps({
            "testResults": [{
                "name": "/repo/src/geo.test.ts",
                "assertionResults": [{
                    "ancestorTitles": ["WP6 entity FAQ block"],
                    "title": "carries at most five FAQ pairs",
                    # vitest ships this too, joined with a plain space. It is
                    # the shape that cannot match a collected id.
                    "fullName":
                        "WP6 entity FAQ block carries at most five FAQ pairs",
                    "status": "failed",
                    "failureMessages": ["AssertionError: nope"],
                }],
            }]
        })
        parsed, ok = tc.parse_vitest_report(report)
        self.assertTrue(ok)
        self.assertEqual(
            ("/repo/src/geo.test.ts::WP6 entity FAQ block > carries at most "
             "five FAQ pairs",),
            tuple(item.nodeid for item in parsed),
            "the report id must be the id `vitest list --json` prints, or "
            "run() keeps nothing and every vitest node collects 0",
        )

    def test_nested_suites_keep_every_ancestor(self) -> None:
        report = json.dumps({
            "testResults": [{
                "name": "/repo/p.test.ts",
                "assertionResults": [{
                    "ancestorTitles": ["outer", "inner"],
                    "title": "alpha",
                    "status": "passed",
                }],
            }]
        })
        parsed, _ok = tc.parse_vitest_report(report)
        self.assertEqual(
            ("/repo/p.test.ts::outer > inner > alpha",),
            tuple(item.nodeid for item in parsed))

    def test_a_report_without_the_parts_falls_back_to_fullname(self) -> None:
        """An older reporter that omits `ancestorTitles` still parses."""
        report = json.dumps({
            "testResults": [{
                "name": "/repo/p.test.ts",
                "assertionResults": [
                    {"fullName": "legacy shape", "status": "passed"},
                ],
            }]
        })
        parsed, ok = tc.parse_vitest_report(report)
        self.assertTrue(ok)
        self.assertEqual(("/repo/p.test.ts::legacy shape",),
                         tuple(item.nodeid for item in parsed))


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
class RealVitestNodeIdsAgree(unittest.TestCase):
    """Executed against vitest. Two of its surfaces, intersected."""

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

    def test_every_collected_id_appears_in_the_report(self) -> None:
        runner = tc.VitestCaseRunner()
        collected = runner.collect(
            self.root, ("probe.test.ts",), timeout_s=300.0)
        self.assertEqual(2, len(collected),
                         "vitest collected {0!r}".format(collected))
        run = runner.run(self.root, collected, timeout_s=300.0)
        self.assertFalse(run.collection_failed)
        self.assertEqual(
            len(collected), len(run.outcomes),
            "run() keeps outcomes whose id is in the collected set; it kept "
            "{0} of {1}. collected={2!r} outcomes={3!r}".format(
                len(run.outcomes), len(collected), collected,
                tuple(o.nodeid for o in run.outcomes)),
        )

    def test_the_parent_red_count_sees_the_cases_that_ran(self) -> None:
        """The adjudicated shape, end to end, under the real runner."""
        runner = tc.VitestCaseRunner()
        collected = runner.collect(
            self.root, ("probe.test.ts",), timeout_s=300.0)
        result = tc.run_cases_for(runner, self.root, collected)
        counts = tc.vf.GateCounts.parse(result.counts)
        self.assertIsNotNone(
            counts, "no parseable report from run_cases_for")
        assert counts is not None
        self.assertEqual(
            len(collected), counts.collected,
            "a green suite is adjudicated `collected {0}` against {1} new "
            "case(s); this is the shape that refused nine red cases as "
            "`parent run collected 0`".format(counts.collected,
                                              len(collected)),
        )


if __name__ == "__main__":
    unittest.main()
