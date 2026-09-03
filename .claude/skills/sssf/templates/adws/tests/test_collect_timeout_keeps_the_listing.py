"""A runner that enumerated and then would not exit has answered.

`vitest list` under an Astro `getViteConfig` prints its complete listing and
then never exits: the Astro/Cloudflare pipeline holds the Vite server open and
`list` has no force-exit path the way `vitest run` does. Measured 2026-09-03
against FDAdb `lane-wp7-cookie-tests` (vitest 3.2.7): collection refused at
120s and twice more at 600s with all six case ids already on stdout, and the
probe processes were still alive twenty-eight minutes later.

`subprocess.run(timeout=…)` raises and leaves that output on the exception, so
the old handler discarded a measurement that had already succeeded and blamed
the draft for it. It also killed one pid, not the process tree.

These use a real child that prints and then sleeps -- a stubbed
`subprocess.run` replays scripted stdout and can neither hang nor leak.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from adw_modules import runner_resolution as rr

LISTS_THEN_HANGS = textwrap.dedent(
    """
    import sys, time
    print("tests/a.test.ts > suite > case one")
    print("tests/a.test.ts > suite > case two")
    sys.stdout.flush()
    open(sys.argv[1], "w").write(str(__import__("os").getpid()))
    time.sleep(600)
    """
).strip()

HANGS_SILENTLY = textwrap.dedent(
    """
    import time
    time.sleep(600)
    """
).strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class CollectTimeout(unittest.TestCase):
    def _resolved(self, script: Path, marker: Path):
        return SimpleNamespace(
            runner="vitest",
            argv_prefix=(sys.executable, str(script), str(marker)),
            collect_argv=lambda gate: (
                sys.executable,
                str(script),
                str(marker),
            ),
        )

    def test_a_listing_survives_a_runner_that_will_not_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            tree = Path(tmp)
            script = tree / "lists_then_hangs.py"
            script.write_text(LISTS_THEN_HANGS, encoding="utf-8")
            marker = tree / "pid"
            gate = SimpleNamespace(runner="vitest", argv=(), cwd=".", min_cases=1)
            ids = rr.collect_cases(
                self._resolved(script, marker), gate, tree, timeout_s=3.0
            )
            self.assertEqual(len(ids), 2)

    def test_the_process_tree_is_dead_afterwards(self) -> None:
        with TemporaryDirectory() as tmp:
            tree = Path(tmp)
            script = tree / "lists_then_hangs.py"
            script.write_text(LISTS_THEN_HANGS, encoding="utf-8")
            marker = tree / "pid"
            gate = SimpleNamespace(runner="vitest", argv=(), cwd=".", min_cases=1)
            rr.collect_cases(self._resolved(script, marker), gate, tree, timeout_s=3.0)
            pid = int(marker.read_text(encoding="utf-8"))
            deadline = time.time() + 5.0
            while _alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(pid), "collect leaked the runner process")

    def test_a_silent_hang_still_refuses(self) -> None:
        with TemporaryDirectory() as tmp:
            tree = Path(tmp)
            script = tree / "hangs.py"
            script.write_text(HANGS_SILENTLY, encoding="utf-8")
            marker = tree / "pid"
            gate = SimpleNamespace(runner="vitest", argv=(), cwd=".", min_cases=1)
            with self.assertRaises(rr.CollectFailed) as caught:
                rr.collect_cases(
                    self._resolved(script, marker), gate, tree, timeout_s=3.0
                )
            self.assertIn("did not finish collecting", str(caught.exception))


class RunBounded(unittest.TestCase):
    def test_a_clean_exit_reports_its_code_and_not_a_timeout(self) -> None:
        result = rr.run_bounded(
            (sys.executable, "-c", "import sys; print('ok'); sys.exit(3)"),
            cwd=Path.cwd(),
            env=dict(os.environ),
            timeout_s=30.0,
        )
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ok", result.stdout)

    def test_a_timeout_kills_the_group_not_just_the_child(self) -> None:
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child"
            code = textwrap.dedent(
                """
                import subprocess, sys, time
                kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
                open(sys.argv[1], "w").write(str(kid.pid))
                time.sleep(600)
                """
            ).strip()
            result = rr.run_bounded(
                (sys.executable, "-c", code, str(marker)),
                cwd=Path(tmp),
                env=dict(os.environ),
                timeout_s=3.0,
            )
            self.assertTrue(result.timed_out)
            pid = int(marker.read_text(encoding="utf-8"))
            deadline = time.time() + 5.0
            while _alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(pid), "the grandchild outlived the timeout")


if __name__ == "__main__":
    del signal
    unittest.main()
