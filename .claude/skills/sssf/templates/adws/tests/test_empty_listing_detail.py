"""An empty listing is refused with what the runner printed, never the clock.

`collect_cases` used to raise `"<runner> did not finish collecting in 120.0s"`.
That string is a stopwatch reading, and `scheduler._collect_private_draft`
forwards it verbatim as the draft author's one correction -- so the author was
told the harness took two minutes and asked to fix that. The repair it suggests
is a bigger budget, and time was never the scarce thing.

Measured 2026-09-05 against a throwaway worktree of FDAdb `integration`
(vitest 3.2.7, node 24.16.0), running `runner_resolution.run_bounded` on the
real binary with `COLLECT_ARGS["vitest"]`:

    tests/wp8-gateway/entity-route-deps.test.ts, Astro `getViteConfig`
        -> 3 ids on stdout, then never exits; killed at the 120s deadline
    module whose top-level `await` never settles, plain `defineConfig`
        -> 0 bytes on both streams; killed at the deadline
    file registering nothing (cases inside an uninvoked `describe`), plain
        -> exits 0 in 2.29s with 0 bytes; never reaches the timeout branch
    either of those two files under Astro `getViteConfig`
        -> the same 453 bytes of adapter banner; killed at the deadline

So silence and output are genuinely different states with different repairs,
and output alone does not separate a deadlocked import from an empty file --
under the config FDAdb's own gates use they are byte-identical. The refusal
text has to say that rather than guess, and it has to carry the runner's own
words either way.

These use real children that print and then sleep. A stubbed `subprocess.run`
replays scripted stdout; it can neither hang nor be killed at a deadline.
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from adw_modules import runner_resolution as rr

HANGS_SILENTLY = textwrap.dedent(
    """
    import time
    time.sleep(600)
    """
).strip()

PRINTS_NOISE_THEN_HANGS = textwrap.dedent(
    """
    import sys, time
    print("00:00:00 [@astrojs/cloudflare] Enabling sessions with Cloudflare KV.")
    print("boom: SECRETTOKEN could not be loaded from " + sys.argv[1])
    sys.stdout.flush()
    time.sleep(600)
    """
).strip()


def _resolved(script: Path, marker: Path):
    return SimpleNamespace(
        runner="vitest",
        argv_prefix=(sys.executable, str(script), str(marker)),
        collect_argv=lambda gate: (sys.executable, str(script), str(marker)),
    )


def _refuse(script_body: str, tree: Path) -> rr.CollectFailed:
    script = tree / "runner.py"
    script.write_text(script_body, encoding="utf-8")
    gate = SimpleNamespace(runner="vitest", argv=(), cwd=".", min_cases=1)
    try:
        rr.collect_cases(_resolved(script, tree / "pid"), gate, tree, timeout_s=3.0)
    except rr.CollectFailed as caught:
        return caught
    raise AssertionError("collect_cases did not refuse an empty listing")


class SilenceIsItsOwnFailure(unittest.TestCase):
    def test_it_says_nothing_loaded_rather_than_reporting_the_clock(self) -> None:
        with TemporaryDirectory() as tmp:
            detail = _refuse(HANGS_SILENTLY, Path(tmp)).detail
        self.assertIn("printed nothing at all", detail)
        self.assertIn("no test module finished loading", detail)

    def test_it_names_the_repair_and_who_owns_it(self) -> None:
        with TemporaryDirectory() as tmp:
            detail = _refuse(HANGS_SILENTLY, Path(tmp)).detail
        self.assertIn("Run the same collect command", detail)
        self.assertIn("the case count is not the problem", detail)

    def test_the_stopwatch_reading_is_not_the_whole_message(self) -> None:
        # The deadline may be cited; it may not be the answer. This is the
        # exact string the old handler raised.
        with TemporaryDirectory() as tmp:
            detail = _refuse(HANGS_SILENTLY, Path(tmp)).detail
        self.assertNotIn("did not finish collecting", detail)


class OutputWithoutCasesIsADifferentFailure(unittest.TestCase):
    def test_the_runner_s_own_words_are_forwarded(self) -> None:
        with TemporaryDirectory() as tmp:
            detail = _refuse(PRINTS_NOISE_THEN_HANGS, Path(tmp)).detail
        self.assertIn("boom: SECRETTOKEN could not be loaded", detail)
        self.assertIn("@astrojs/cloudflare", detail)

    def test_it_does_not_claim_to_know_which_of_the_two_defects_it_is(self) -> None:
        # Under FDAdb's own Astro config a deadlocked import and an empty file
        # print the same 453 bytes. A message that picked one would be wrong
        # half the time and would send the author to the wrong repair.
        with TemporaryDirectory() as tmp:
            detail = _refuse(PRINTS_NOISE_THEN_HANGS, Path(tmp)).detail
        self.assertIn("cannot tell those apart", detail)
        self.assertIn("registered no case", detail)

    def test_it_is_not_the_message_a_silent_runner_gets(self) -> None:
        with TemporaryDirectory() as tmp:
            noisy = _refuse(PRINTS_NOISE_THEN_HANGS, Path(tmp)).detail
        with TemporaryDirectory() as tmp:
            silent = _refuse(HANGS_SILENTLY, Path(tmp)).detail
        self.assertNotEqual(noisy, silent)
        self.assertNotIn("printed nothing at all", noisy)

    def test_the_tree_path_never_crosses_the_boundary(self) -> None:
        # `_collect_private_draft` redacts private tokens on top of this, but
        # the vault worktree path is this function's own to hide: the old
        # detail carried no output and so never had to.
        with TemporaryDirectory() as tmp:
            tree = Path(tmp)
            detail = _refuse(PRINTS_NOISE_THEN_HANGS, tree).detail
        self.assertNotIn(str(tree), detail)
        self.assertNotIn(str(tree.resolve()), detail)
        self.assertIn("$tree", detail)


if __name__ == "__main__":
    unittest.main()
