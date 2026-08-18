"""The two controls, as tests, because they are the whole value of the tool.

`tools/effect_falsifier.py` checks a lane's declared effect disposition against
the code the lane actually added. Its first form scanned whole modules and
produced three findings against an object-store client that already existed in
a pre-existing production file — code the lane neither wrote nor would write. A
falsifier that cannot tell added code from code that was already there trains
people to ignore it, which is worse than not having one.

So the pair below is not a sample of its behaviour; it is the property that
makes it worth running. One diff adds an executing client under a `planned`
disposition and must be convicted at the line that adds it. One diff touches a
module that already constructs such a client, adding nothing of the kind, and
must be acquitted. Either one alone proves nothing: a tool that convicts
everything passes the first, and a tool that convicts nothing passes the
second.

Measured against real history at adoption, with the same tool and the same
plan:

    negative, no changes in range:
      checked 0 changed module(s); 16 with no added lines in range;
      0 finding(s)                                                   exit 0

    positive, the attempt-3 branch of run-0120c32064d144c2aa55c344087e0b0a:
      FALSIFIED .../canonical_object.py:200  canonical_object_write is
        declared planned, and this line adds 'copy_object'
      FALSIFIED .../canonical_object.py:211  ... 'put_object'
      FALSIFIED .../canonical_object.py:213  ... 'put_object'
      checked 1 changed module(s); 15 with no added lines in range;
      3 finding(s)                                                   exit 1

That is the defect which cost a node its whole review budget and which three
cross-vendor reviews examined without ever naming.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TOOLS = ADWS / "tools"
for _path in (str(ADWS), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import effect_falsifier as ef                              # noqa: E402


#: A module that already constructs an object-store client, committed at base.
#: The acquittal control turns on this existing before the lane runs.
PRE_EXISTING = (
    "import boto3\n"
    "\n"
    "_CLIENT = boto3.client('s3')\n"
    "\n"
    "def fetch(key):\n"
    "    return _CLIENT.get_object(Bucket='b', Key=key)\n"
)

#: The same module with a docstring line added and nothing else. The lane
#: touched it; the lane did not add a client.
TOUCHED_ONLY = (
    "import boto3\n"
    "\n"
    "_CLIENT = boto3.client('s3')\n"
    "\n"
    "def fetch(key):\n"
    '    """Read one object."""\n'
    "    return _CLIENT.get_object(Bucket='b', Key=key)\n"
)

#: The materializer shape: a lane declaring `planned` that writes anyway.
EXECUTING = (
    "def materialize(client, key, body):\n"
    "    client.put_object(Bucket='b', Key=key, Body=body)\n"
    "    return key\n"
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if result.returncode:
        raise AssertionError("git {0}: {1}".format(" ".join(args),
                                                   result.stderr.strip()))
    return result.stdout.strip()


def _plan(disposition: str = "planned", outputs=("src/mod.py",)) -> dict:
    return {
        "extensions": {"maestro": {"outputs": {"lane-a": list(outputs)}}},
        "lanes": [{"lane_id": "lane-a", "requirement_ids": ["req-a"]}],
        "requirements": [{
            "requirement_id": "req-a",
            "effects": [
                {"effect": "canonical_object_write",
                 "disposition": disposition},
                {"effect": "source_backfill", "disposition": "none"},
            ],
        }],
    }


class EffectFalsifierControlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        _git(self.root, "init", "-q", "-b", "main", "repo")
        _git(self.repo, "config", "user.email", "maestro@example.invalid")
        _git(self.repo, "config", "user.name", "Maestro Test")
        (self.repo / "src" / "mod.py").write_text(PRE_EXISTING, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit(self, text: str, rel: str = "src/mod.py") -> str:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "lane work")
        return _git(self.repo, "rev-parse", "HEAD")

    def run_tool(self, plan: dict, head: str):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return ef.findings_for(str(plan_path), str(self.repo), self.base, head)

    # ── the conviction ─────────────────────────────────────────────────────

    def test_a_diff_that_adds_an_executing_client_is_falsified(self):
        head = self.commit(PRE_EXISTING + "\n" + EXECUTING)
        findings, checked, _untouched = self.run_tool(_plan("planned"), head)
        self.assertEqual(checked, 1)
        self.assertEqual(len(findings), 1, findings)
        rel, line, effect, disposition, hit = findings[0]
        self.assertEqual(rel, "src/mod.py")
        self.assertEqual(effect, "canonical_object_write")
        self.assertEqual(disposition, "planned")
        self.assertEqual(hit, "put_object")
        # The location is the point: a finding an operator cannot open is a
        # finding they will not act on.
        self.assertEqual(
            self.repo.joinpath(rel).read_text().splitlines()[line - 1].strip(),
            "client.put_object(Bucket='b', Key=key, Body=body)")

    def test_the_same_diff_under_none_is_falsified_too(self):
        head = self.commit(PRE_EXISTING + "\n" + EXECUTING)
        findings, _checked, _untouched = self.run_tool(_plan("none"), head)
        self.assertEqual([item[3] for item in findings], ["none"])

    # ── the acquittal ──────────────────────────────────────────────────────

    def test_a_diff_that_only_touches_a_module_holding_a_client_is_not(self):
        """The control that killed the whole-module form. The module has
        `boto3.client('s3')` at base and still does; the lane added a
        docstring. Convicting here is convicting a lane for code it did not
        write."""
        head = self.commit(TOUCHED_ONLY)
        findings, checked, _untouched = self.run_tool(_plan("planned"), head)
        self.assertEqual(checked, 1, "the module was in the diff")
        self.assertEqual(findings, [], "acquitted: it added no client")

    def test_an_untouched_output_is_counted_apart_from_a_checked_one(self):
        """A path the lane never touched and a path that was checked are
        different facts, and the summary keeps them apart."""
        head = self.commit(EXECUTING, rel="src/other.py")
        findings, checked, untouched = self.run_tool(
            _plan("planned", outputs=("src/mod.py", "src/other.py")), head)
        self.assertEqual(checked, 1)
        self.assertEqual(untouched, 1)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "src/other.py")

    # ── the stated limits, asserted so they cannot be quietly lost ─────────

    def test_a_lanes_own_tests_are_excluded(self):
        """`fake_only` means the test exercises the path against an injected
        fake, so a test that constructs a client is the declaration working."""
        head = self.commit(EXECUTING, rel="tests/test_mod.py")
        findings, checked, _untouched = self.run_tool(
            _plan("planned", outputs=("tests/test_mod.py",)), head)
        self.assertEqual((findings, checked), ([], 0))

    def test_performed_is_not_this_tools_question(self):
        """Admission already refuses a `performed` declaration against a
        prohibition. There is nothing left here to catch."""
        head = self.commit(PRE_EXISTING + "\n" + EXECUTING)
        findings, _checked, _untouched = self.run_tool(_plan("performed"), head)
        self.assertEqual(findings, [])

    def test_a_client_reached_through_a_wrapper_is_missed(self):
        """Stated in the docstring and asserted here, because a limit nobody
        can see is a limit nobody accounts for. Silence from this tool is not
        evidence of a true declaration."""
        head = self.commit(
            PRE_EXISTING + "\ndef materialize(store, key, body):\n"
            "    store.write(key, body)\n")
        findings, _checked, _untouched = self.run_tool(_plan("planned"), head)
        self.assertEqual(findings, [])


class TheToolDecidesNothingTest(unittest.TestCase):
    """§1.2: no lifecycle transition may be caused by a regex over source text.

    The tool is run by hand and informs a human. Nothing in the runtime may
    import it, and this is the reader that keeps that true.
    """

    def test_no_runtime_module_imports_the_falsifier(self):
        offenders = []
        for path in sorted((ADWS / "adw_modules").glob("*.py")):
            if "effect_falsifier" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        for path in sorted(ADWS.glob("*.py")):
            if "effect_falsifier" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(
            [], offenders,
            "the falsifier informs a human after a merge; a runtime module "
            "reading it would key a transition on a regex over source text, "
            "which is what §1.2 forbids: " + ", ".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
