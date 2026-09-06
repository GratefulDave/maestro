"""A runner that ran and failed is not a runner that was not found.

Measured 2026-09-05 on FDAdb, `uv run adws/maestro.py` against plan
`fdadb-wp4`. The whole refusal an operator got was:

    RUNNER_PREFLIGHT_REFUSED: no usable pytest was found for .; candidates
    tried: [redacted]/.venv/bin/pytest, /opt/.../uv run pytest,
    /Users/.../bin/pytest

Every word of that except the paths is wrong about what happened.
`_rank_candidates` appends a rank-1 candidate only after `_is_executable`
returns True, and `tried` is appended to only after that -- so the redacted
`.venv/bin/pytest`, which is inside the preflight's own provisioned tree, was
present, executable, started, and answered something other than
`CAPABLE_EXIT["pytest"]`. It was found. It could not collect.

The type already draws that distinction: `Reason.INCAPABLE` exists, carries
`resolved` and `probe_exit`, and the *declared* branch of `resolve` has always
raised it for exactly this event. Discovery could not, because `probe()`
returned an exit code and threw `_run`'s captured output away, and the loop
kept no record of which candidates had started. Both halves of the answer were
measured and then discarded, and `UNRESOLVED` -- "nothing resolved to an
executable file at all" -- was raised over the top of them.

The cost is not the wrong sentence. It is that the preflight deleted its tree
in `finally`, so the only way left to learn why the probe failed was to rebuild
the environment by hand from the deployment's own `provision_argv` and re-run
the probe, which is what diagnosing this refusal actually took. A refusal about
an environment is answered by opening that environment, so a tree that refused
now stays and is named in the refusal.

Second half of the same defect, same method: `provision_tree` raises
`ReviewProvisioningError`, and `_assert_runners_usable` caught only
`RunnerUnusable`. A deployment whose `provision_argv` fails is the preflight's
own sentence -- "every runner the plan names must run, before any agent is
dispatched" -- coming out false, and it left run start as an untyped crash.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import runner_resolution as rr


def _fake_pytest(root: Path, body: str) -> Path:
    binary = root / ".venv" / "bin" / "pytest"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


class DiscoveryTellsFoundFromUnusable(unittest.TestCase):
    """`PATH` is emptied so discovery cannot reach ranks 2-4 of this machine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patched = mock.patch.dict(os.environ, {"PATH": ""}, clear=False)
        patched.start()
        self.addCleanup(patched.stop)

    def test_a_candidate_that_started_and_failed_is_incapable(self) -> None:
        binary = _fake_pytest(self.root, "echo 'ImportError: no module named app' >&2\nexit 4\n")

        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.root, ".", ())

        refusal = caught.exception
        self.assertIs(refusal.reason, rr.Reason.INCAPABLE)
        self.assertEqual(refusal.resolved, str(binary))
        self.assertEqual(refusal.probe_exit, 4)
        self.assertIn("ImportError", refusal.probe_output)
        # The sentence an operator reads must not claim the opposite of the
        # measurement that produced it.
        self.assertNotIn("no usable pytest was found", refusal.detail)
        self.assertIn("ImportError", refusal.detail)

    def test_the_payload_carries_the_probe_output_it_measured(self) -> None:
        _fake_pytest(self.root, "echo 'conftest.py: boom' >&2\nexit 3\n")

        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.root, ".", ())

        payload = caught.exception.payload()
        self.assertEqual(payload["reason"], "INCAPABLE")
        self.assertEqual(payload["probe_exit"], 3)
        self.assertIn("conftest.py: boom", payload["probe_output"])

    def test_nothing_executable_anywhere_is_still_unresolved(self) -> None:
        # The refusal this replaces is correct when it is true, and the empty
        # tree is when it is true.
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.root, ".", ())

        refusal = caught.exception
        self.assertIs(refusal.reason, rr.Reason.UNRESOLVED)
        self.assertIn("no usable pytest was found", refusal.detail)
        self.assertEqual(refusal.probe_output, "")

    def test_a_capable_candidate_still_resolves(self) -> None:
        binary = _fake_pytest(self.root, "exit 5\n")

        resolved = rr.resolve("pytest", self.root, ".", ())

        self.assertEqual(resolved.executable, str(binary))
        self.assertEqual(resolved.probe_exit, 5)
        self.assertEqual(resolved.origin, "discovered")


class ProbeReturnsWhatItMeasured(unittest.TestCase):
    def test_probe_returns_the_exit_code_and_the_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = _fake_pytest(root, "echo 'said this'\nexit 7\n")

            exit_code, output = rr.probe("pytest", (str(binary),), root, ())

        self.assertEqual(exit_code, 7)
        self.assertIn("said this", output)

    def test_a_probe_that_cannot_start_reports_minus_one_and_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, output = rr.probe(
                "pytest", (str(Path(tmp) / "absent"),), Path(tmp), ()
            )

        self.assertEqual(exit_code, -1)
        self.assertEqual(output, "")


class ProvisioningFailureIsThePreflightsOwnRefusal(unittest.TestCase):
    def test_the_preflight_catches_a_provisioning_failure(self) -> None:
        from adw_modules import scheduler as sch

        source = Path(sch.__file__).read_text(encoding="utf-8")
        body = source.split("    def _assert_runners_usable(self) -> None:", 1)[1]
        body = body.split("    def _collect_private_draft(", 1)[0]
        self.assertIn("cr.ReviewProvisioningError", body)
        self.assertIn("cr.provision_tree(", body)
        self.assertLess(body.index("cr.provision_tree("), body.index("rr.resolve("))


class TheProbeMeasuresTheEnvironmentNotTheDraft(unittest.TestCase):
    """FDAdb run 2489c772, `lane-wp4-release-tests`, 2026-09-06.

    The lane drafted `services/device-substrates/tests/release/
    test_release_artifact.py`. The repository already carried
    `services/label-batch/tests/observations/test_release_artifact.py` from a
    merged lane, and neither directory holds an `__init__.py`, so pytest
    derived the module name `test_release_artifact` twice and interrupted
    collection: `import file mismatch`, exit 2.

    `_collect_private_draft` wrote the draft and *then* called `rr.resolve`,
    whose probe is a whole-tree `--collect-only`. So the measurement of the
    environment was taken through the draft, and a filename the tester chose
    and can change in one line was reported as `RUNNER_PREFLIGHT_REFUSED` --
    which ends the run, for every lane, rather than sending that lane a
    REVISE. The run-start preflight had passed the identical probe fifteen
    minutes earlier, in a tree with no draft in it.

    Resolving before the write is what separates the two questions. A draft
    that will not collect then fails in `collect_cases`, where `CollectFailed`
    becomes `DraftCollectionRefused` and reaches its author.
    """

    def test_the_runner_is_resolved_before_the_draft_is_written(self) -> None:
        from adw_modules import scheduler as sch

        source = Path(sch.__file__).read_text(encoding="utf-8")
        body = source.split("    def _collect_private_draft(", 1)[1]
        body = body.split("    def _reviewing_tests(", 1)[0]
        provision = body.index("cr.provision_tree(")
        resolve = body.index("rr.resolve(")
        write = body.index("prv.write_files(")
        collect = body.index("rr.collect_cases(")
        self.assertLess(provision, resolve, "resolve needs the provisioned tree")
        self.assertLess(resolve, write, "the probe must not collect the draft")
        self.assertLess(write, collect, "cases are collected from the draft")


class TheTreeThatRefusedIsKept(unittest.TestCase):
    """A `finally` that always removed the tree deleted the answer with it."""

    def _method(self, name: str, until: str) -> str:
        from adw_modules import scheduler as sch

        source = Path(sch.__file__).read_text(encoding="utf-8")
        return source.split(name, 1)[1].split(until, 1)[0]

    def test_the_preflight_removes_its_tree_only_when_nothing_refused(self) -> None:
        body = self._method(
            "    def _assert_runners_usable(self) -> None:",
            "    def _collect_private_draft(",
        )
        self.assertIn("if not keep:\n                    _remove_collect_tree", body)
        self.assertIn("keep = True", body)

    def test_the_preflight_names_the_tree_it_kept(self) -> None:
        body = self._method(
            "    def _assert_runners_usable(self) -> None:",
            "    def _collect_private_draft(",
        )
        self.assertIn("which is kept", body)
        # Unredacted on this path: no private byte is ever written into the
        # preflight tree, so redacting its path hid the evidence and protected
        # nothing.
        self.assertNotIn("prv.redact_text", body)

    def test_a_mid_run_runner_fault_keeps_its_tree_too(self) -> None:
        body = self._method(
            "    def _collect_private_draft(",
            "    def _reviewing_tests(",
        )
        self.assertIn("if not keep:\n                _remove_collect_tree", body)
        self.assertIn("which is kept", body)
        # A draft that could not be collected is the author's to fix and its
        # tree holds private files; only the harness fault keeps one.
        author, harness = body.index("DraftCollectionRefused"), body.index("keep = True")
        self.assertGreater(author, harness)
        self.assertIn("prv.redact_text", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
