"""The reviewer's probe submission is validated before any file is read, then read bounded.

`parse_envelope` takes no filesystem argument, so every assertion about it is
an assertion about a check that ran before a read could. `read_submissions`
then reads real files in a real directory, including the shapes that make a
naive reader follow a link out of the checkout or block on a FIFO.
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import probes


def _entry(path: str = "tests/test_p.py", **extra: str) -> dict:
    return {"path": path, "requirement": "negative amounts are refused", "intent": "x", **extra}


def _codes(rejections) -> list[str]:
    return [item.code for item in rejections]


class SchemaBeforeAnyRead(unittest.TestCase):
    def test_absent_probes_is_no_submission(self) -> None:
        self.assertEqual(probes.parse_envelope({}), ((), (), ()))
        self.assertEqual(probes.parse_envelope(None), ((), (), ()))

    def test_a_non_list_probes_value_is_malformed(self) -> None:
        _entries, rejected, _ = probes.parse_envelope({"probes": {"path": "x"}})
        self.assertEqual(_codes(rejected), [probes.PROBE_ENVELOPE_MALFORMED])

    def test_a_non_mapping_envelope_is_malformed(self) -> None:
        _entries, rejected, _ = probes.parse_envelope(["probes"])
        self.assertEqual(_codes(rejected), [probes.PROBE_ENVELOPE_MALFORMED])

    def test_entries_past_the_count_cap_are_rejected_not_truncated_silently(self) -> None:
        raw = {"probes": [_entry("tests/test_{0}.py".format(n)) for n in range(5)]}
        entries, rejected, _ = probes.parse_envelope(raw)
        self.assertEqual(len(entries), probes.MAX_PROBES_PER_ROUND)
        self.assertEqual(_codes(rejected), [probes.PROBE_COUNT_EXCEEDED] * 2)
        self.assertEqual([r.path for r in rejected], ["tests/test_3.py", "tests/test_4.py"])

    def test_extra_missing_or_empty_fields_are_malformed_per_entry(self) -> None:
        raw = {
            "probes": [
                _entry(note="x"),
                {"path": "tests/test_q.py", "requirement": "r"},
                _entry("tests/test_r.py", intent="   "),
            ]
        }
        entries, rejected, _ = probes.parse_envelope(raw)
        self.assertEqual(entries, ())
        self.assertEqual(_codes(rejected), [probes.PROBE_ENVELOPE_MALFORMED] * 3)
        entries, rejected, _ = probes.parse_envelope({"probes": [_entry("tests/test_ok.py")]})
        self.assertEqual([e.path for e in entries], ["tests/test_ok.py"])

    def test_non_string_fields_do_not_reach_path_normalization(self) -> None:
        _e, rejected, _c = probes.parse_envelope({"probes": [_entry() | {"path": 7}]})
        self.assertEqual(_codes(rejected), [probes.PROBE_ENVELOPE_MALFORMED])

    def test_unnormalized_paths_are_refused(self) -> None:
        for path in ("../x.py", "/abs.py", "a//b.py", "a/./b.py", "dir/"):
            with self.subTest(path=path):
                entries, rejected, _ = probes.parse_envelope({"probes": [_entry(path)]})
                self.assertEqual(entries, ())
                self.assertEqual(_codes(rejected), [probes.PROBE_PATH_REFUSED])

    def test_a_git_component_is_refused_in_any_case(self) -> None:
        for path in (".git/hooks/test_p.py", "x/.GIT/test_p.py"):
            with self.subTest(path=path):
                _e, rejected, _c = probes.parse_envelope({"probes": [_entry(path)]})
                self.assertEqual(_codes(rejected), [probes.PROBE_PATH_REFUSED])

    def test_reserved_paths_overlap_by_component_in_both_directions(self) -> None:
        reserved = ("services/api/app.py", "tests/accepted", "tests/probe_a.py")
        for path in (
            "services/api/app.py",
            "services/api/app.py/x.py",
            "tests/accepted/test_x.py",
            "tests/probe_a.py",
        ):
            with self.subTest(path=path):
                _e, rejected, _c = probes.parse_envelope(
                    {"probes": [_entry(path)]}, reserved=reserved
                )
                self.assertEqual(_codes(rejected), [probes.PROBE_PATH_REFUSED])
        entries, rejected, _ = probes.parse_envelope(
            {"probes": [_entry("services/api/app_extra.py"), _entry("tests/test_ok.py")]},
            reserved=reserved,
        )
        self.assertEqual(rejected, ())
        self.assertEqual(len(entries), 2)

    def test_two_submissions_may_not_overlap_each_other(self) -> None:
        entries, rejected, _ = probes.parse_envelope(
            {"probes": [_entry("tests/a.py"), _entry("tests/a.py"), _entry("tests/a.py/b.py")]}
        )
        self.assertEqual([e.path for e in entries], ["tests/a.py"])
        self.assertEqual(_codes(rejected), [probes.PROBE_PATH_REFUSED] * 2)

    def test_challenges_are_strict_and_deduplicated(self) -> None:
        raw = {
            "challenges": [
                {"probe_id": "p-1", "reason": "wrong"},
                {"probe_id": "p-1", "reason": "again"},
                {"probe_id": "p-2"},
            ]
        }
        _e, rejected, challenges = probes.parse_envelope(raw)
        self.assertEqual([c.probe_id for c in challenges], ["p-1"])
        self.assertEqual(_codes(rejected), [probes.PROBE_ENVELOPE_MALFORMED] * 2)


class BoundedSafeReads(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "checkout"
        (self.root / "tests").mkdir(parents=True)
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()

    def _read(self, *paths: str, limit: int = probes.MAX_PROBE_BYTES):
        entries = [probes.ProbeEntry(p, "r", "i") for p in paths]
        return probes.read_submissions(self.root, entries, limit=limit)

    def test_a_regular_utf8_file_is_read(self) -> None:
        (self.root / "tests" / "test_ok.py").write_text("def test_a():\n    pass\n")
        submissions, rejected = self._read("tests/test_ok.py")
        self.assertEqual(rejected, ())
        self.assertEqual(submissions[0].source, "def test_a():\n    pass\n")

    def test_a_mixed_batch_keeps_the_good_entry_and_types_each_bad_one(self) -> None:
        (self.root / "tests" / "test_ok.py").write_text("ok = 1\n")
        (self.outside / "secret.py").write_text("secret = 1\n")
        os.symlink(self.outside / "secret.py", self.root / "tests" / "test_link.py")
        os.symlink(self.outside, self.root / "linkdir")
        (self.root / "tests" / "test_bin.py").write_bytes(b"\xff\xfe\x00bad")
        (self.root / "tests" / "test_big.py").write_text("x" * 200)
        os.mkfifo(self.root / "tests" / "test_fifo.py")
        (self.root / "tests" / "adir").mkdir()
        (self.root / "tests" / "afile").write_text("x")
        submissions, rejected = self._read(
            "tests/test_missing.py",
            "tests/test_link.py",
            "linkdir/secret.py",
            "tests/test_bin.py",
            "tests/test_big.py",
            "tests/test_fifo.py",
            "tests/adir",
            "tests/afile/below.py",
            "tests/test_ok.py",
            limit=100,
        )
        self.assertEqual([s.path for s in submissions], ["tests/test_ok.py"])
        self.assertEqual(
            {r.path: r.code for r in rejected},
            {
                "tests/test_missing.py": probes.PROBE_FILE_MISSING,
                "tests/test_link.py": probes.PROBE_FILE_UNSAFE,
                "linkdir/secret.py": probes.PROBE_FILE_UNSAFE,
                "tests/test_bin.py": probes.PROBE_NOT_UTF8,
                "tests/test_big.py": probes.PROBE_TOO_LARGE,
                "tests/test_fifo.py": probes.PROBE_FILE_UNSAFE,
                "tests/adir": probes.PROBE_FILE_MISSING,
                "tests/afile/below.py": probes.PROBE_FILE_MISSING,
            },
        )

    def test_a_file_at_the_limit_is_read_and_one_byte_over_is_not(self) -> None:
        (self.root / "tests" / "at.py").write_text("x" * 50)
        (self.root / "tests" / "over.py").write_text("x" * 51)
        submissions, rejected = self._read("tests/at.py", "tests/over.py", limit=50)
        self.assertEqual([s.path for s in submissions], ["tests/at.py"])
        self.assertEqual(_codes(rejected), [probes.PROBE_TOO_LARGE])

    def test_the_read_is_bounded_not_drained(self) -> None:
        (self.root / "tests" / "huge.py").write_bytes(b"x" * 5_000_000)
        sizes: list[int] = []
        real = os.read

        def spy(fd, n):
            data = real(fd, n)
            sizes.append(len(data))
            return data

        with mock.patch.object(probes.os, "read", side_effect=spy):
            _s, rejected = self._read("tests/huge.py")
        self.assertEqual(_codes(rejected), [probes.PROBE_TOO_LARGE])
        self.assertLessEqual(sum(sizes), probes.MAX_PROBE_BYTES + 1)

    def test_an_infrastructure_error_propagates_instead_of_becoming_a_rejection(self) -> None:
        (self.root / "tests" / "test_ok.py").write_text("ok = 1\n")
        with mock.patch.object(probes.os, "read", side_effect=OSError(errno.EIO, "disk")):
            with self.assertRaises(OSError):
                self._read("tests/test_ok.py")

    def test_a_permission_error_on_open_is_infrastructure_too(self) -> None:
        path = self.root / "tests" / "locked.py"
        path.write_text("x = 1\n")
        real = os.open

        def deny(name, flags, *args, **kwargs):
            if name == "locked.py":
                raise PermissionError(errno.EACCES, "denied")
            return real(name, flags, *args, **kwargs)

        with mock.patch.object(probes.os, "open", side_effect=deny):
            with self.assertRaises(PermissionError):
                self._read("tests/locked.py")


if __name__ == "__main__":
    unittest.main()
