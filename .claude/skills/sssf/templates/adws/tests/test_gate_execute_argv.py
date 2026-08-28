"""A gate must execute its cases and terminate.

`run-9d03105407f440079f3730f1fe4c67b3` retried `lane-wp6-build` until it was
stopped by hand. Not one attempt ever launched an agent pane: every attempt
provisioned its worktree, entered the pre-gate, and stayed there until the
harness timeout reaped it.

`plan_contract_ingress._parse_verifier_command` strips the `run` sub-command
out of an authored `npx vitest run <paths>` -- correctly, because `run` is the
runner's mode and not part of the gate's argv. Collection re-supplied its own
mode through `COLLECT_ARGS`. Execution re-supplied nothing, so every vitest
gate ran as bare `vitest <paths>`, which is vitest's WATCH mode: it executes
the cases, prints the report, and then waits forever for a file to change.

The gate never returned. A gate that never terminates is indistinguishable
from a hung agent, and the node was blamed for it on every attempt.

`ExecuteArgvCarriesTheRunnerMode` fails on the old `execute_argv` with no
vitest installed at all. `RealVitestTerminates` executes vitest for real,
because "this argv terminates" is a fact about vitest's own CLI that a
stubbed `subprocess` cannot observe -- the same reason the sibling bug in
`test_vitest_collect_argv.py` shipped.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adw_modules import runner_resolution as rr


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


class ExecuteArgvCarriesTheRunnerMode(unittest.TestCase):
    """The mode, asserted without running anything."""

    def test_a_vitest_execution_opens_with_run(self) -> None:
        resolved = rr.ResolvedRunner(runner="vitest", executable="/bin/vitest")
        argv = resolved.execute_argv(("src/a.test.ts",))
        self.assertEqual(
            argv, ("/bin/vitest", "run", "src/a.test.ts"),
            "without `run` this is vitest's watch mode and the gate never "
            "returns",
        )

    def test_the_mode_is_not_doubled_when_the_argv_already_carries_it(
            self) -> None:
        resolved = rr.ResolvedRunner(runner="vitest", executable="/bin/vitest")
        self.assertEqual(
            resolved.execute_argv(("run", "src/a.test.ts")),
            ("/bin/vitest", "run", "src/a.test.ts"),
        )

    def test_pytest_has_no_mode_to_add(self) -> None:
        resolved = rr.ResolvedRunner(runner="pytest", executable="/bin/pytest")
        self.assertEqual(
            resolved.execute_argv(("-q", "tests/t.py")),
            ("/bin/pytest", "-q", "tests/t.py"),
        )

    def test_every_runner_declares_an_execution_mode(self) -> None:
        self.assertEqual(
            set(rr.EXECUTE_ARGS), set(rr.COLLECT_ARGS),
            "a runner that can be collected must also be executable; a "
            "missing key here is a KeyError at gate time",
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
class RealVitestTerminates(unittest.TestCase):
    """Executed against vitest itself. A stub cannot observe watch mode.

    The child is given a pty for stdin, which is what the scheduler's own
    child inherits. That detail is load-bearing: with stdin redirected from
    /dev/null the watching process gives up on its own after ~20s, so a test
    that closes stdin passes against the defect it is meant to catch.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        if not _vitest_project(cls.root):
            cls._tmp.cleanup()
            raise unittest.SkipTest("could not install vitest (offline?)")
        cls.runner = rr.ResolvedRunner(
            runner="vitest",
            executable=str(cls.root / "node_modules" / ".bin" / "vitest"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run(self, argv, timeout_s=90.0):
        import pty
        parent, child = pty.openpty()
        process = subprocess.Popen(
            list(argv), cwd=str(self.root), stdin=child,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        os.close(child)
        try:
            process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            os.close(parent)
            return None
        os.close(parent)
        return process.returncode

    def test_the_built_argv_terminates(self) -> None:
        code = self._run(self.runner.execute_argv(("probe.test.ts",)))
        self.assertIsNotNone(
            code,
            "the gate's own argv did not terminate: vitest was left watching "
            "for file changes and the node would be reaped as a hung agent",
        )
        self.assertEqual(code, 0)

    def test_the_argv_without_the_mode_does_not_terminate(self) -> None:
        """The defect itself, so the case above cannot pass vacuously."""
        argv = (self.runner.executable, "probe.test.ts")
        self.assertIsNone(
            self._run(argv, timeout_s=45.0),
            "bare `vitest <paths>` terminated; vitest no longer defaults to "
            "watch mode and EXECUTE_ARGS may be re-derived",
        )


if __name__ == "__main__":
    unittest.main()
