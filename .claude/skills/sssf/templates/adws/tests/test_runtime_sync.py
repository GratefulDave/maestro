"""The properties that make `tools/runtime_sync.py` worth having.

Each test below stands for one production loss, and the module is only useful
insofar as the test that names that loss stays green:

* A file present in one copy and absent from the other is reported in its own
  field, never folded into "these files differ". The 6,009-line deletion was an
  absence, and an absence that reads as an edit is a loss nobody looks for.
* `maestro.config.yaml` names one installation's lane vendors, models and
  concurrency, so it never crosses a deployment boundary in either direction —
  and, because an exclusion that is too broad is its own defect, it *is* still
  compared between two template checkouts.
* A destination that looks ahead of the source is refused by name rather than
  overwritten — on either of two independent signals, mtime and line count,
  because the mtime signal alone fired on nothing when it was measured against
  two real deployments. All three halves are asserted: each signal, and the fact
  that the named override actually overrides and says what it discarded.
* Nothing is ever deleted. A file present only in the destination survives a
  mirror and is reported.
* A dry run writes nothing. Destroying a copy must never be the shorter command.

Run:  uv run adw_test.py -k runtime_sync
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TOOLS = ADWS / "tools"
for _path in (str(ADWS), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import runtime_sync as rs                                   # noqa: E402


MAESTRO_LAYOUT = Path(".claude/skills/sssf/templates/adws")
LIBRARY_LAYOUT = Path("skills/sssf/templates/adws")
DEPLOYMENT_LAYOUT = Path("adws")


def _write(root: Path, relative: str, body: str, mtime: float | None = None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class RuntimeSyncFixture(unittest.TestCase):
    """A temporary world holding two template checkouts and one deployment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        world = Path(self._tmp.name)
        self.template = world / "maestro" / MAESTRO_LAYOUT
        self.peer = world / "the-library" / LIBRARY_LAYOUT
        self.deployment = world / "lexgenius" / DEPLOYMENT_LAYOUT
        for root in (self.template, self.peer, self.deployment):
            root.mkdir(parents=True)

    def copies(self, source: Path, destination: Path):
        return rs.describe_copy(source), rs.describe_copy(destination)


class ClassificationTests(RuntimeSyncFixture):
    def test_a_template_layout_is_a_template_and_anything_else_is_a_deployment(self):
        self.assertEqual(rs.TEMPLATE, rs.classify(self.template))
        self.assertEqual(rs.TEMPLATE, rs.classify(self.peer))
        self.assertEqual(rs.DEPLOYMENT, rs.classify(self.deployment))

    def test_a_copy_is_named_for_the_repository_that_holds_it(self):
        self.assertEqual("maestro", rs.describe_copy(self.template).name)
        self.assertEqual("the-library", rs.describe_copy(self.peer).name)
        self.assertEqual("lexgenius", rs.describe_copy(self.deployment).name)


class AbsenceIsNotADifferenceTests(RuntimeSyncFixture):
    """The 6,009-line loss mode, and the reason it has its own field."""

    def setUp(self) -> None:
        super().setUp()
        _write(self.template, "adw_modules/shared.py", "same\n")
        _write(self.peer, "adw_modules/shared.py", "same\n")
        # Present in the template, deleted from the peer: the loss mode.
        _write(self.template, "adw_modules/deleted_from_peer.py", "runtime\n")
        # Present in the peer, never in the template: the other direction.
        _write(self.peer, "tests/test_only_in_peer.py", "runtime\n")
        # Present in both, different bytes: an ordinary edit.
        _write(self.template, "maestro.py", "a\nb\nc\n")
        _write(self.peer, "maestro.py", "a\n")

    def test_an_absent_file_is_reported_as_absent_and_never_as_differing(self):
        report = rs.compare(*self.copies(self.template, self.peer))

        self.assertEqual(("adw_modules/deleted_from_peer.py",),
                         report.absent_from_destination)
        self.assertEqual(("tests/test_only_in_peer.py",),
                         report.absent_from_source)
        self.assertEqual(["maestro.py"],
                         [item.relative_path for item in report.differing])
        self.assertFalse(report.is_level)

    def test_the_two_kinds_of_drift_do_not_overlap(self):
        report = rs.compare(*self.copies(self.template, self.peer))
        differing = {item.relative_path for item in report.differing}
        self.assertEqual(set(), differing & set(report.missing_files))
        self.assertEqual(
            {"adw_modules/deleted_from_peer.py", "tests/test_only_in_peer.py"},
            set(report.missing_files),
        )

    def test_the_report_names_the_direction_of_a_content_difference(self):
        report = rs.compare(*self.copies(self.template, self.peer))
        difference, = report.differing
        self.assertEqual(3, difference.source_lines)
        self.assertEqual(1, difference.destination_lines)
        self.assertTrue(difference.source_is_longer)
        self.assertIn("maestro is ahead by 2 lines",
                      difference.describe("maestro", "the-library"))

    def test_identical_copies_report_level(self):
        shutil.rmtree(self.peer)
        shutil.copytree(self.template, self.peer)
        report = rs.compare(*self.copies(self.template, self.peer))
        self.assertTrue(report.is_level)
        self.assertIn("are level", report.describe())


class DeploymentConfigTests(RuntimeSyncFixture):
    """`maestro.config.yaml` names one installation and never crosses."""

    def setUp(self) -> None:
        super().setUp()
        _write(self.template, "maestro.py", "runtime\n")
        _write(self.deployment, "maestro.py", "runtime\n")
        _write(self.template, "maestro.config.yaml", "lanes: template-shaped\n")
        _write(self.deployment, "maestro.config.yaml", "lanes: this-installation\n")

    def test_the_config_is_excluded_from_a_comparison_with_a_deployment(self):
        source, destination = self.copies(self.template, self.deployment)
        report = rs.compare(source, destination)

        self.assertEqual(("maestro.config.yaml",), report.excluded)
        self.assertTrue(
            report.is_level,
            "the only difference is the deployment's own config, which is not drift:\n"
            + report.describe(),
        )

    def test_a_mirror_into_a_deployment_leaves_the_config_alone(self):
        source, destination = self.copies(self.template, self.deployment)
        _write(self.template, "adw_modules/new.py", "runtime\n")

        result = rs.mirror(source, destination, apply=True)

        self.assertEqual(("maestro.config.yaml",), result.excluded)
        self.assertNotIn("maestro.config.yaml", result.copied)
        self.assertEqual(
            "lanes: this-installation\n",
            (self.deployment / "maestro.config.yaml").read_text(encoding="utf-8"),
        )

    def test_the_config_is_still_compared_between_two_template_checkouts(self):
        """An exclusion that is too broad is its own defect."""
        _write(self.peer, "maestro.py", "runtime\n")
        _write(self.peer, "maestro.config.yaml", "lanes: stale\n")

        report = rs.compare(*self.copies(self.template, self.peer))

        self.assertEqual((), report.excluded)
        self.assertEqual(["maestro.config.yaml"],
                         [item.relative_path for item in report.differing])


class DestinationAheadTests(RuntimeSyncFixture):
    """A mirror that silently discards newer work is the failure being fixed."""

    def setUp(self) -> None:
        super().setUp()
        _write(self.template, "maestro.py", "template version\n", mtime=1_000_000)
        _write(self.peer, "maestro.py", "peer has newer work\n", mtime=2_000_000)

    def test_a_newer_destination_is_refused_by_name(self):
        source, destination = self.copies(self.template, self.peer)
        result = rs.mirror(source, destination, apply=True)

        refusal, = result.refused
        self.assertEqual("maestro.py", refusal.relative_path)
        self.assertIn(rs.DESTINATION_NEWER, refusal.reasons)
        self.assertFalse(result.is_clean)
        self.assertEqual((), result.copied)

    def test_a_refused_file_is_not_written(self):
        source, destination = self.copies(self.template, self.peer)
        rs.mirror(source, destination, apply=True)
        self.assertEqual(
            "peer has newer work\n",
            (self.peer / "maestro.py").read_text(encoding="utf-8"),
        )

    def test_a_longer_destination_is_refused_even_when_the_source_is_newer(self):
        """The signal that survives a fresh checkout of the source tree.

        A git worktree checkout stamps every source file with the checkout time,
        so mtime says the source is newer than everything and refuses nothing.
        Measured against two real deployments, mtime refused 0 files while six
        held content the template did not — one of them 91 lines longer than the
        file about to replace it.
        """
        _write(self.template, "maestro.py", "one line\n", mtime=9_000_000)
        _write(self.peer, "maestro.py", "one\ntwo\nthree\n", mtime=1_000)
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination, apply=True)

        refusal, = result.refused
        self.assertEqual((rs.DESTINATION_LONGER,), refusal.reasons)
        self.assertEqual(3, refusal.destination_lines)
        self.assertEqual(1, refusal.source_lines)
        self.assertEqual(
            "one\ntwo\nthree\n",
            (self.peer / "maestro.py").read_text(encoding="utf-8"),
        )

    def test_both_signals_are_reported_when_both_fire(self):
        _write(self.peer, "maestro.py", "peer\nhas\nmore\n", mtime=2_000_000)
        source, destination = self.copies(self.template, self.peer)

        refusal, = rs.mirror(source, destination).refused

        self.assertEqual((rs.DESTINATION_NEWER, rs.DESTINATION_LONGER),
                         refusal.reasons)
        self.assertIn("DESTINATION_NEWER+DESTINATION_LONGER", refusal.describe())

    def test_overwriting_a_destination_that_is_ahead_is_an_explicit_choice(self):
        source, destination = self.copies(self.template, self.peer)
        result = rs.mirror(source, destination, apply=True, overwrite_ahead=True)

        self.assertEqual((), result.refused)
        self.assertEqual(("maestro.py",), result.copied)
        overridden, = result.overridden
        self.assertIn(rs.DESTINATION_NEWER, overridden.reasons)
        self.assertIn("OVERWROTE a destination that looked ahead",
                      result.describe())
        self.assertEqual(
            "template version\n",
            (self.peer / "maestro.py").read_text(encoding="utf-8"),
        )

    def test_a_destination_that_is_behind_on_both_signals_is_copied(self):
        _write(self.template, "maestro.py", "new\nruntime\n", mtime=2_000_000)
        _write(self.peer, "maestro.py", "old\n", mtime=500_000)
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination, apply=True)

        self.assertEqual((), result.refused)
        self.assertEqual(("maestro.py",), result.copied)

    def test_an_identical_destination_is_never_refused_however_new_it_is(self):
        """`copy2` preserves mtime, but a touched-but-unchanged file must pass."""
        _write(self.peer, "maestro.py", "template version\n", mtime=9_000_000)
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination, apply=True)

        self.assertEqual((), result.refused)
        self.assertEqual((), result.copied)
        self.assertEqual(("maestro.py",), result.unchanged)


class DryRunTests(RuntimeSyncFixture):
    def test_a_mirror_writes_nothing_unless_apply_is_passed(self):
        _write(self.template, "adw_modules/new.py", "runtime\n")
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination)

        self.assertFalse(result.applied)
        self.assertEqual(("adw_modules/new.py",), result.copied)
        self.assertIn("would mirror", result.describe())
        self.assertFalse((self.peer / "adw_modules/new.py").exists())

    def test_the_cli_defaults_to_a_plan(self):
        _write(self.template, "adw_modules/new.py", "runtime\n")
        rs.main(["mirror", str(self.template), str(self.peer)])
        self.assertFalse((self.peer / "adw_modules/new.py").exists())

    def test_the_cli_check_verb_exits_nonzero_on_drift_and_zero_when_level(self):
        _write(self.template, "adw_modules/new.py", "runtime\n")
        self.assertEqual(1, rs.main(["check", str(self.template), str(self.peer)]))
        _write(self.peer, "adw_modules/new.py", "runtime\n")
        self.assertEqual(0, rs.main(["check", str(self.template), str(self.peer)]))


class NeverDeleteTests(RuntimeSyncFixture):
    def test_a_file_only_in_the_destination_survives_and_is_reported(self):
        _write(self.template, "maestro.py", "runtime\n")
        _write(self.peer, "maestro.py", "runtime\n")
        _write(self.peer, "adw_modules/local_only.py", "do not delete me\n")
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination, apply=True)

        self.assertEqual(("adw_modules/local_only.py",), result.left_in_destination)
        self.assertTrue((self.peer / "adw_modules/local_only.py").exists())


class CopyProofTests(RuntimeSyncFixture):
    def test_a_copy_reads_both_files_back_and_returns_the_shared_digest(self):
        src = _write(self.template, "maestro.py", "runtime\n")
        dst = self.peer / "maestro.py"

        digest = rs.copy_verified(src, dst)

        self.assertEqual(digest, rs.sha256_of(src))
        self.assertEqual(digest, rs.sha256_of(dst))

    def test_a_copy_that_did_not_arrive_raises_rather_than_reporting_success(self):
        src = _write(self.template, "maestro.py", "runtime\n")
        dst = self.peer / "maestro.py"

        def lying_copy(source, destination, *args, **kwargs):
            Path(destination).write_text("truncated", encoding="utf-8")

        original = shutil.copy2
        shutil.copy2 = lying_copy
        try:
            with self.assertRaises(rs.VerificationError) as caught:
                rs.copy_verified(src, dst)
        finally:
            shutil.copy2 = original
        self.assertIn("COPY_VERIFICATION_FAILED", str(caught.exception))

    def test_an_applied_mirror_records_a_digest_for_every_file_it_wrote(self):
        _write(self.template, "maestro.py", "runtime\n")
        _write(self.template, "adw_modules/mod.py", "more runtime\n")
        source, destination = self.copies(self.template, self.peer)

        result = rs.mirror(source, destination, apply=True)

        self.assertEqual(set(result.copied), set(result.digests))
        for relative, digest in result.digests.items():
            self.assertEqual(digest, rs.sha256_of(self.peer / relative))


class IgnoredPathTests(RuntimeSyncFixture):
    def test_caches_and_per_instance_state_are_not_runtime(self):
        _write(self.template, "maestro.py", "runtime\n")
        _write(self.template, "__pycache__/maestro.cpython-312.pyc", "junk\n")
        _write(self.template, "adw_data/sessions/x.json", "{}\n")
        _write(self.template, "adw_modules/x.pyc", "junk\n")

        found = rs.scan_runtime(self.template)

        self.assertEqual({"maestro.py"}, set(found))


if __name__ == "__main__":
    unittest.main()
