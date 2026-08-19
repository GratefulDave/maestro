"""The install path is a copy path, and it has to obey the same rules.

`tools/runtime_sync.py` makes a mirror between checkouts provable, but a sync
mechanism that leaves a second, uninstrumented copy path in place has not fixed
the defect class it was built for. `skills/sssf/scripts/install.py` is that
second path: it stamps the template into a consuming repository, and until this
test existed it did so with a bare `shutil.copy2` whose exit was never checked,
and `--force` overwrote whatever was there.

That is the same shape as the hand-mirror. Re-stamping an installed repository
with `--force` silently discarded any locally patched runtime — and every
installed instance carries at least one file that exists nowhere else, its
`maestro.config.yaml`, naming that installation's lane vendors, models and
concurrency.

So the two properties asserted below are the ones the mirror already has:

* every stamped file is proved to have arrived, by sha256, not by exit status;
* a destination that differs from the template and is *newer* than it is
  refused by name rather than overwritten, and discarding it is the explicitly
  named `--overwrite-newer`.

This skips in a deployed instance, where `scripts/install.py` is not present:
an installed repository holds the runtime, not the installer.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TOOLS = ADWS / "tools"
for _path in (str(ADWS), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import runtime_sync as rs                                    # noqa: E402

INSTALL_SCRIPT = ADWS.parents[1] / "scripts" / "install.py"


def _load_installer():
    if not INSTALL_SCRIPT.is_file():
        raise unittest.SkipTest(
            "this ADW runtime is a deployed instance; the installer at "
            "{path} ships with the skill, not with the runtime".format(
                path=INSTALL_SCRIPT
            )
        )
    spec = importlib.util.spec_from_file_location("sssf_install", INSTALL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallCopyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = _load_installer()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        world = Path(self._tmp.name)
        self.source = world / "template"
        self.dest = world / "installed"
        self.source.mkdir()
        self.dest.mkdir()

    def _write(self, root: Path, relative: str, body: str, mtime: float) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_the_installer_uses_the_verified_copy_primitive(self):
        """One definition of "the copy arrived", shared with the mirror.

        The installer loads the module from its path rather than by name, so it
        holds a distinct module object; what must be true is that it is the same
        *file*, and therefore the same definition.
        """
        self.assertEqual(
            Path(rs.__file__).resolve(),
            Path(self.installer.runtime_sync.__file__).resolve(),
        )
        self.assertTrue(callable(self.installer.runtime_sync.copy_verified))

    def test_a_stamped_file_is_proved_to_have_arrived(self):
        src = self._write(self.source, "maestro.py", "runtime\n", 1_000_000)
        stamped, skipped, refused = [], [], []

        self.installer.stamp(self.source, self.dest, False, stamped, skipped, refused)

        self.assertEqual([], refused)
        self.assertEqual(1, len(stamped))
        self.assertEqual(rs.sha256_of(src), rs.sha256_of(self.dest / "maestro.py"))

    def test_force_refuses_a_destination_that_is_newer_than_the_template(self):
        self._write(self.source, "maestro.config.yaml", "lanes: template\n", 1_000_000)
        self._write(self.dest, "maestro.config.yaml", "lanes: mine\n", 2_000_000)
        stamped, skipped, refused = [], [], []

        self.installer.stamp(self.source, self.dest, True, stamped, skipped, refused)

        self.assertEqual([str(self.dest / "maestro.config.yaml")], refused)
        self.assertEqual([], stamped)
        self.assertEqual(
            "lanes: mine\n",
            (self.dest / "maestro.config.yaml").read_text(encoding="utf-8"),
        )

    def test_overwriting_a_newer_destination_is_an_explicitly_named_choice(self):
        self._write(self.source, "maestro.config.yaml", "lanes: template\n", 1_000_000)
        self._write(self.dest, "maestro.config.yaml", "lanes: mine\n", 2_000_000)
        stamped, skipped, refused = [], [], []

        self.installer.stamp(self.source, self.dest, True, stamped, skipped, refused,
                             True)

        self.assertEqual([], refused)
        self.assertEqual([str(self.dest / "maestro.config.yaml")], stamped)
        self.assertEqual(
            "lanes: template\n",
            (self.dest / "maestro.config.yaml").read_text(encoding="utf-8"),
        )

    def test_force_still_overwrites_a_destination_that_is_merely_stale(self):
        """The refusal must be about *newer*, not about *different*."""
        self._write(self.source, "maestro.py", "new runtime\n", 2_000_000)
        self._write(self.dest, "maestro.py", "old runtime\n", 1_000_000)
        stamped, skipped, refused = [], [], []

        self.installer.stamp(self.source, self.dest, True, stamped, skipped, refused)

        self.assertEqual([], refused)
        self.assertEqual(
            "new runtime\n", (self.dest / "maestro.py").read_text(encoding="utf-8")
        )

    def test_without_force_an_existing_file_is_skipped_as_before(self):
        self._write(self.source, "maestro.py", "new runtime\n", 2_000_000)
        self._write(self.dest, "maestro.py", "old runtime\n", 1_000_000)
        stamped, skipped, refused = [], [], []

        self.installer.stamp(self.source, self.dest, False, stamped, skipped, refused)

        self.assertEqual([str(self.dest / "maestro.py")], skipped)
        self.assertEqual([], stamped)
        self.assertEqual([], refused)


if __name__ == "__main__":
    unittest.main()
