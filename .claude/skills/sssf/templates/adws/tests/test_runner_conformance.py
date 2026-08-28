"""One contract, executed against every real binary `_RUNNERS` can name.

Three bugs shipped on the `CaseRunner` seam in one week, each costing a run,
each a fact about an external binary rather than about this code:

* `run-8a200af7f9044ce7a11a51b6908f37e3` -- `_prove_tests_red_at_parent`
  collected with pytest whatever the gate declared, and pytest collects
  nothing from a `.test.ts` file. Zero cases, `TESTS_NO_NEW_CASES`, ten
  attempts (`407d7d3`).
* `run-6b8f607d89744eeb94a79713b3b5d234` -- `vitest list --json <paths>`.
  vitest's `--json` takes an *optional value*, so the path was read as "write
  the listing here": collection OVERWROTE the tester's committed test file
  with 47KB of vitest's own JSON, printed nothing, exited 0 (`5273342`).
* `run-9f20c17ffc22497b957bd5be95dc1ddf` -- `vitest list --json` names a case
  `Suite > title`; `--reporter=json` ships `fullName` joined with a plain
  space. `run()` keeps the outcomes whose id is in the collected set, so that
  intersection was empty by construction for every vitest node ever run. Nine
  collected, nine reported, zero kept (`c087469`).

Which binary is invoked, how it parses argv, whether its two output surfaces
agree: a stubbed `subprocess.run` replays the argv you handed it and the
stdout you scripted, so it can observe none of the three. That is why 2943
green tests said nothing and why `VitestCaseRunner` had no coverage at all
until the third bug.

So the assertions live in `RunnerContract` and every runner in `tc._RUNNERS`
inherits them by supplying a probe project, not by someone remembering to
write a second test file. `RunnerRegistryIsFullyCovered` is the part that
makes that binding rather than aspirational: it reads `tc._RUNNERS` at
runtime and fails naming any key with no fixture here. cargo is coming, and
it will be a third adapter on this same unguarded seam.

C5 records a divergence rather than hiding it. Under a probe that cannot
import, pytest reports the requested case `errored` (the file-level ERROR
line is bound to every case in that file by `parse_pytest_outcomes`), while
vitest reports the file failed with an empty `assertionResults` and the case
simply does not appear. Both refuse -- `CONTROL_IMPORT_CRASH` one side,
`CONTROL_NOT_SELECTED` the other -- and neither reads as green, which is the
property asserted here. The shared contract is "a tree that cannot import is
never green", not "both runners name the crash the same way".
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple, Tuple

from adw_modules import tests_chain as tc


class Probe(NamedTuple):
    """One probe project state: what to collect, and what it declares.

    `suffixes` are the `file::case` tails a collected id must end with. They
    are tails rather than whole ids because pytest reports a case relative to
    the tree and vitest reports it under an absolute resolved path, and the
    contract is about the *shape after the file*, which is what bug 3 broke.

    `ids` are whole ids, needed only by C5: a probe that cannot import
    collects nothing, so the ids it would have declared have to be supplied
    by the fixture in order to ask the runner to run them.
    """

    paths: Tuple[str, ...]
    suffixes: Tuple[str, ...]
    ids: Tuple[str, ...]


class RunnerContract:
    """Every `CaseRunner` must satisfy these, executed against its binary.

    Not a `unittest.TestCase`: it carries no fixture of its own and must not
    be collected. A concrete runner is added by subclassing this beside
    `unittest.TestCase`, setting `runner_name` to its `tc._RUNNERS` key, and
    implementing `make_runner` and `install`. Nothing else.
    """

    #: The key this fixture covers in `tc._RUNNERS`.
    runner_name = ""

    #: Cold vitest start is slow; the contract is about correctness, and a
    #: timeout here would be a flake rather than a finding.
    timeout_s = 300.0

    # ── supplied by the concrete fixture ────────────────────────────────

    def make_runner(self) -> tc.CaseRunner:
        raise NotImplementedError

    def probe_root(self) -> Path:
        raise NotImplementedError

    def install(self, kind: str) -> Probe:
        """Write the `green`, `red`, or `broken` probe, alone in the tree.

        Alone matters: `VitestCaseRunner.run` executes the whole suite and
        filters the report afterwards, so a leftover probe from another case
        is a second file in the same run.
        """
        raise NotImplementedError

    # ── helpers ─────────────────────────────────────────────────────────

    def _collect(self, probe: Probe) -> Tuple[str, ...]:
        return self.make_runner().collect(
            self.probe_root(), probe.paths, timeout_s=self.timeout_s)

    def _tails(self, collected: Tuple[str, ...],
               probe: Probe) -> Tuple[str, ...]:
        """Each collected id matched to the declared suffix it ends with."""
        matched = []
        for nodeid in collected:
            hit = [s for s in probe.suffixes if nodeid.endswith(s)]
            matched.append(hit[0] if len(hit) == 1 else nodeid)
        return tuple(sorted(matched))

    # ── C1: exact case count, never ">= 1" ──────────────────────────────

    def test_collect_returns_exactly_the_declared_cases(self) -> None:
        probe = self.install("green")
        collected = self._collect(probe)
        self.assertEqual(
            len(probe.suffixes), len(collected),
            "{0}.collect found {1} case(s) in a probe declaring {2}: {3!r}. "
            "A runner pointed at a file it cannot read returns 0 here and "
            "the node is refused TESTS_NO_NEW_CASES forever, blaming the "
            "tester for a measurement that never ran.".format(
                self.runner_name, len(collected), len(probe.suffixes),
                collected))
        self.assertEqual(
            tuple(sorted(probe.suffixes)), self._tails(collected, probe),
            "the collected ids are not the declared cases; a case id whose "
            "shape drifts cannot be matched against a report id, which is "
            "the bug that adjudicated nine red cases as `collected 0`")

    # ── C2: a measurement must not mutate its subject ───────────────────

    def test_collect_leaves_every_input_file_byte_identical(self) -> None:
        probe = self.install("green")
        root = self.probe_root()
        before = {p: (root / p).read_bytes() for p in probe.paths}
        self._collect(probe)
        for path, original in before.items():
            self.assertEqual(
                original, (root / path).read_bytes(),
                "{0}.collect rewrote {1}, the file it was measuring. vitest "
                "did exactly this -- `--json <path>` writes the listing into "
                "<path> -- destroying the tester's committed cases inside "
                "the attempt worktree and then refusing the node for their "
                "absence.".format(self.runner_name, path))

    # ── C3: the two surfaces name the same case ─────────────────────────

    def test_every_collected_id_comes_back_from_the_report(self) -> None:
        probe = self.install("green")
        collected = self._collect(probe)
        self.assertTrue(collected, "nothing collected; C1 explains why")
        run = self.make_runner().run(
            self.probe_root(), collected, timeout_s=self.timeout_s)
        self.assertFalse(
            run.collection_failed,
            "{0}.run produced no parseable report: {1!r}".format(
                self.runner_name, run.tail))
        self.assertEqual(
            set(collected), set(o.nodeid for o in run.outcomes),
            "the ids collection produced and the ids the report produced are "
            "not the same set. run() keeps outcomes whose id is in the "
            "collected set, so a disagreement here empties that set for "
            "every node this runner ever measures -- and it is invisible in "
            "a count, because both sides are individually correct.\n"
            "  collected: {0!r}\n  reported:  {1!r}".format(
                tuple(sorted(collected)),
                tuple(sorted(o.nodeid for o in run.outcomes))))

    # ── C4: status and reason are the runner's own evidence ─────────────

    def test_a_green_probe_reports_every_case_passed(self) -> None:
        probe = self.install("green")
        collected = self._collect(probe)
        run = self.make_runner().run(
            self.probe_root(), collected, timeout_s=self.timeout_s)
        self.assertEqual(
            len(collected), run.passed,
            "{0} passed {1} of {2} green case(s): {3!r}".format(
                self.runner_name, run.passed, len(collected),
                tuple((o.nodeid, o.status) for o in run.outcomes)))

    def test_a_red_probe_reports_every_case_failed_with_a_reason(self) -> None:
        probe = self.install("red")
        collected = self._collect(probe)
        self.assertEqual(len(probe.suffixes), len(collected))
        run = self.make_runner().run(
            self.probe_root(), collected, timeout_s=self.timeout_s)
        self.assertEqual(
            len(collected), run.failed,
            "{0} failed {1} of {2} red case(s); a case that errored or was "
            "skipped is not the red a negative control proves.".format(
                self.runner_name, run.failed, len(collected)))
        for outcome in run.outcomes:
            self.assertTrue(
                outcome.reason.strip(),
                "{0} reported {1} failed with no reason. The reason is the "
                "runner's own text and the whole evidence of "
                "`expected_reason_pattern`; without it a control can only "
                "prove that something went wrong, never that the declared "
                "defect is what did.".format(self.runner_name,
                                             outcome.nodeid))

    # ── C5: zero because broken is not zero because empty ───────────────

    def test_a_probe_that_cannot_import_is_never_green(self) -> None:
        probe = self.install("broken")
        collected = self._collect(probe)
        self.assertEqual(
            (), collected,
            "{0} collected {1!r} from a file whose import target does not "
            "exist".format(self.runner_name, collected))
        run = self.make_runner().run(
            self.probe_root(), probe.ids, timeout_s=self.timeout_s)
        self.assertEqual(
            0, run.passed,
            "{0} reported {1} case(s) of a tree that cannot import as "
            "passed. An unreported case defaults to passed, so a crash that "
            "is only named at file level becomes a set of silently-green "
            "cases -- which is how a suite that never executed is "
            "adjudicated as covering its obligations.".format(
                self.runner_name, run.passed))
        self.assertNotEqual(
            0, run.exit_code,
            "{0} exited 0 on a tree that cannot import; an exit code that "
            "cannot tell a broken tree from an empty one lets a collection "
            "crash be read as a suite with nothing to say".format(
                self.runner_name))
        for outcome in run.outcomes:
            self.assertIn(
                outcome.status, ("errored", "failed"),
                "{0} is {1} under a broken import".format(
                    outcome.nodeid, outcome.status))


# ── pytest ──────────────────────────────────────────────────────────────

PY_GREEN = '''def test_alpha():
    assert 1 == 1


class TestGroup:
    def test_beta(self):
        assert 2 == 2
'''

PY_RED = '''def test_gamma():
    assert 1 == 2, "gamma is not green"


def test_delta():
    assert "a" == "b", "delta is not green"
'''

PY_BROKEN = '''from definitely_not_a_module import thing


def test_epsilon():
    assert thing
'''


class PytestRunnerConformance(RunnerContract, unittest.TestCase):
    """`PytestCaseRunner` against the pytest `_pytest_prefix()` resolves.

    The probe lives in its own tree with no `pytest.ini`, because a
    repository whose `addopts = -v` cancels `-q` turns `--collect-only -q`
    into a tree of `<Function x>` lines that parse as zero cases. The runner
    already passes `-o addopts=`; the isolation keeps this fixture honest
    about which of the two is doing the work.
    """

    runner_name = "pytest"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name).resolve()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def make_runner(self) -> tc.CaseRunner:
        return tc.PytestCaseRunner()

    def probe_root(self) -> Path:
        return self.root

    def install(self, kind: str) -> Probe:
        for stale in self.root.glob("test_*.py"):
            stale.unlink()
        shutil.rmtree(self.root / "__pycache__", ignore_errors=True)
        if kind == "green":
            name, body = "test_green.py", PY_GREEN
            suffixes = ("test_green.py::test_alpha",
                        "test_green.py::TestGroup::test_beta")
            ids = suffixes
        elif kind == "red":
            name, body = "test_red.py", PY_RED
            suffixes = ("test_red.py::test_gamma", "test_red.py::test_delta")
            ids = suffixes
        else:
            name, body = "test_broken.py", PY_BROKEN
            suffixes = ()
            ids = ("test_broken.py::test_epsilon",)
        (self.root / name).write_text(body, encoding="utf-8")
        return Probe(paths=(name,), suffixes=suffixes, ids=ids)


# ── vitest ──────────────────────────────────────────────────────────────

TS_CONFIG = '''import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["**/*.test.ts"] },
});
'''

TS_GREEN = '''import { describe, it, expect } from "vitest";

describe("outer", () => {
  describe("inner", () => {
    it("alpha", () => { expect(1).toBe(1); });
  });
  it("beta", () => { expect(2).toBe(2); });
});
'''

TS_RED = '''import { describe, it, expect } from "vitest";

describe("red", () => {
  it("gamma", () => { expect(1).toBe(2); });
  it("delta", () => { expect("a").toBe("b"); });
});
'''

TS_BROKEN = '''import { describe, it, expect } from "vitest";
import { thing } from "./definitely-not-here.js";

describe("broken", () => {
  it("epsilon", () => { expect(thing).toBe(1); });
});
'''


def _install_vitest(root: Path) -> bool:
    """Install a minimal vitest project under `root`. False when offline."""
    (root / "package.json").write_text(
        json.dumps({"name": "probe", "private": True, "type": "module",
                    "devDependencies": {"vitest": "^3"}}),
        encoding="utf-8")
    (root / "vitest.config.ts").write_text(TS_CONFIG, encoding="utf-8")
    installed = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=str(root), capture_output=True, text=True)
    return installed.returncode == 0 and (root / "node_modules").is_dir()


@unittest.skipIf(shutil.which("npm") is None, "npm is not installed")
@unittest.skipIf(os.environ.get("ADW_SKIP_NETWORK_TESTS") == "1",
                 "ADW_SKIP_NETWORK_TESTS=1")
class VitestRunnerConformance(RunnerContract, unittest.TestCase):
    """`VitestCaseRunner` against a real vitest.

    vitest reports a case under the absolute resolved path of its file, so
    the whole ids C5 needs are built from the probe root rather than written
    down; `suffixes` carry the `file::Suite > title` shape, which is the part
    that has to agree between `list --json` and `run --reporter=json`.
    """

    runner_name = "vitest"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name).resolve()
        if not _install_vitest(cls.root):
            cls._tmp.cleanup()
            raise unittest.SkipTest("could not install vitest (offline?)")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def make_runner(self) -> tc.CaseRunner:
        return tc.VitestCaseRunner()

    def probe_root(self) -> Path:
        return self.root

    def install(self, kind: str) -> Probe:
        for stale in self.root.glob("*.test.ts"):
            stale.unlink()
        if kind == "green":
            name, body = "green.test.ts", TS_GREEN
            tails = ("outer > inner > alpha", "outer > beta")
        elif kind == "red":
            name, body = "red.test.ts", TS_RED
            tails = ("red > gamma", "red > delta")
        else:
            name, body = "broken.test.ts", TS_BROKEN
            tails = ("broken > epsilon",)
        (self.root / name).write_text(body, encoding="utf-8")
        suffixes = tuple("{0}::{1}".format(name, tail) for tail in tails)
        ids = tuple("{0}::{1}".format(self.root / name, tail)
                    for tail in tails)
        if kind == "broken":
            suffixes = ()
        return Probe(paths=(name,), suffixes=suffixes, ids=ids)


# ── the keystone ────────────────────────────────────────────────────────


def _fixtures() -> Tuple[type, ...]:
    """Every concrete `RunnerContract` fixture defined in this module."""
    found = []
    for value in list(globals().values()):
        if not isinstance(value, type) or value is RunnerContract:
            continue
        if issubclass(value, RunnerContract) and value.runner_name:
            found.append(value)
    return tuple(found)


class RunnerRegistryIsFullyCovered(unittest.TestCase):
    """No runner reaches `_RUNNERS` without a probe project in this file.

    This is the case that makes the contract binding instead of aspirational,
    and the reason it reads `tc._RUNNERS` at runtime rather than a list
    written here. Every bug this file exists for was a property of one
    adapter's binary that no other adapter shared, and each was found by a
    production run rather than by the suite. Adding cargo without a fixture
    would put a third such adapter into service unmeasured; this fails
    first.

    It must never skip. The vitest fixture skips when npm is missing, but it
    is still defined, so coverage is still asserted.
    """

    def test_every_registered_runner_has_a_conformance_fixture(self) -> None:
        covered = {cls.runner_name: cls for cls in _fixtures()}
        registered = set(tc._RUNNERS)
        missing = sorted(registered - set(covered))
        self.assertEqual(
            [], missing,
            "tests_chain._RUNNERS registers {0} with no conformance fixture "
            "in {1}. Add `class {2}RunnerConformance(RunnerContract, "
            "unittest.TestCase)` with `runner_name = {3!r}`, implementing "
            "make_runner/probe_root/install to build a green, a red, and a "
            "broken probe project for that binary. Until then the contract "
            "every other runner is held to -- exact collection, collection "
            "that does not rewrite its subject, collected ids that equal "
            "reported ids, and a broken tree that is never green -- is "
            "unmeasured for {0}, which is the state each of the three "
            "shipped bugs was found in.".format(
                ", ".join(missing) or "(none)", Path(__file__).name,
                (missing[0] if missing else "New").capitalize(),
                missing[0] if missing else ""))
        stale = sorted(set(covered) - registered)
        self.assertEqual(
            [], stale,
            "{0} has a conformance fixture but is not in "
            "tests_chain._RUNNERS; a fixture for a runner nothing can "
            "select measures nothing".format(", ".join(stale)))

    def test_each_fixture_actually_supplies_a_probe(self) -> None:
        """A fixture that inherits the stubs covers nothing."""
        for cls in _fixtures():
            for hook in ("make_runner", "probe_root", "install"):
                self.assertIsNot(
                    getattr(cls, hook), getattr(RunnerContract, hook),
                    "{0} does not implement {1}; it would satisfy the "
                    "registry check while running no binary".format(
                        cls.__name__, hook))


if __name__ == "__main__":
    unittest.main()
