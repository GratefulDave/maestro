"""One generated-output policy, applied by every role-output collection path.

Every row of the filesystem-output matrix is exercised against real temporary
git repositories and real files, through both collection paths:

* the tester path, `HerdrStageActor._collect_uncommitted`
* the builder path, `HerdrStageActor._commit_declared`

Safety (containment, no-follow, regular file) is proven before any path may be
ignored, so a generated-looking symlink or a non-regular path still refuses.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))


import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules.scheduler import FactoryRefused

_ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}


class _CollectionHung(BaseException):
    """Raised by the watchdog when a collection call fails to return."""


@contextlib.contextmanager
def _watchdog(seconds: float = 20.0) -> Iterator[None]:
    """Interrupt a blocked syscall instead of hanging the suite."""

    def _fire(_signum: int, _frame: object) -> None:
        raise _CollectionHung()

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")
    return _git(path, "rev-parse", "HEAD")


class _SilentLauncher:
    """Never launched. `HerdrStageActor` only needs the attribute to exist."""


class _RoleOutputCase(unittest.TestCase):
    """A checkout under test plus a bound product repository."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.checkout = self.root / "checkout"
        self.base = _init_repo(self.checkout)
        product = self.root / "product"
        _init_repo(product)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, _SilentLauncher()),
            state,
            gitpub.bind_target_worktree(product, "refs/heads/main"),
            _ROLE_ROUTES,
        )

    def write(self, relative: str, text: str) -> Path:
        target = self.checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_bytes(self, relative: str, payload: bytes) -> Path:
        target = self.checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def track(self, *relatives: str) -> str:
        _git(self.checkout, "add", "--", *relatives)
        _git(self.checkout, "commit", "-m", "track")
        self.base = _git(self.checkout, "rev-parse", "HEAD")
        return self.base

    def collect(self, outputs: Sequence[str] = ()) -> dict[str, str]:
        with _watchdog():
            return self.actor._collect_uncommitted(self.checkout, outputs)

    def commit_declared(self, outputs: Sequence[str]) -> tuple[str, bool]:
        with _watchdog():
            return self.actor._commit_declared(self.checkout, outputs, self.base)

    def candidate_paths(self, sha: str) -> frozenset[str]:
        listed = _git(self.checkout, "ls-tree", "-r", "--name-only", sha)
        return frozenset(line for line in listed.splitlines() if line)

    def candidate_text(self, sha: str, relative: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.checkout), "show", f"{sha}:{relative}"],
            text=True,
        )


class SharedPolicyTest(_RoleOutputCase):
    """`_role_output_disposition` is the one policy both paths consult."""

    def test_regular_utf8_is_kept_with_its_bytes(self) -> None:
        self.write("src/keep.py", "value = 1\n")
        self.assertEqual(
            maestro._role_output_disposition(
                self.checkout, self.checkout / "src/keep.py"
            ),
            ("src/keep.py", b"value = 1\n"),
        )

    def test_generated_directories_and_suffixes_are_ignored(self) -> None:
        for relative in (
            "tests/__pycache__/t.cpython-312.pyc",
            ".pytest_cache/v/cache/nodeids",
            ".ruff_cache/content/x",
            ".mypy_cache/3.12/x.json",
            "build/module.pyo",
            "build/module.pyd",
            ".coverage",
            ".coverage.host.1234",
        ):
            with self.subTest(relative=relative):
                self.write_bytes(relative, b"\0\xff")
                self.assertEqual(
                    maestro._role_output_disposition(
                        self.checkout, self.checkout / relative
                    ),
                    (relative, None),
                )

    def test_absolute_path_outside_checkout_refuses(self) -> None:
        outside = self.root / "host-secret.txt"
        outside.write_text("VAULT_BYTES\n", encoding="utf-8")
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE:outside"):
            maestro._role_output_disposition(self.checkout, outside)

    def test_dotdot_relative_path_refuses(self) -> None:
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE:path"):
            maestro._role_output_disposition(self.checkout, Path("../escape.txt"))

    def test_directory_is_not_a_regular_file(self) -> None:
        (self.checkout / "src").mkdir()
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE:src"):
            maestro._role_output_disposition(self.checkout, self.checkout / "src")

    def test_fifo_refuses_without_blocking(self) -> None:
        os.mkfifo(self.checkout / "pipe.txt")
        with _watchdog(10.0):
            with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE:pipe.txt"):
                maestro._role_output_disposition(
                    self.checkout, self.checkout / "pipe.txt"
                )

    def test_generated_looking_symlink_refuses_before_being_ignored(self) -> None:
        secret = self.root / "host-secret.pyc"
        secret.write_bytes(b"\0secret")
        cache = self.checkout / "__pycache__"
        cache.mkdir()
        (cache / "escape.pyc").symlink_to(secret)
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_UNSAFE:__pycache__/escape.pyc"
        ):
            maestro._role_output_disposition(
                self.checkout, self.checkout / "__pycache__/escape.pyc"
            )

    def test_symlinked_generated_directory_refuses(self) -> None:
        outside = self.root / "outside-cache"
        outside.mkdir()
        (outside / "x.pyc").write_bytes(b"\0")
        (self.checkout / "__pycache__").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE"):
            maestro._role_output_disposition(
                self.checkout, self.checkout / "__pycache__/x.pyc"
            )

    def test_symlinked_generated_directory_inside_checkout_refuses(self) -> None:
        inside = self.checkout / "real-cache"
        inside.mkdir()
        (inside / "x.pyc").write_bytes(b"\0")
        (self.checkout / "__pycache__").symlink_to(inside, target_is_directory=True)
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE"):
            maestro._role_output_disposition(
                self.checkout, Path("__pycache__/x.pyc")
            )

    def test_missing_path_is_not_a_refusal(self) -> None:
        with self.assertRaises(FileNotFoundError):
            maestro._role_output_disposition(
                self.checkout, self.checkout / "never-written.py"
            )


class ScopedTesterCollectionTest(_RoleOutputCase):
    """Declared outputs scope the tester sweep, as they scope the builder.

    A tests lane must return exactly its declared outputs. An unscoped sweep
    read a toolchain byproduct the role never declared -- observed in
    production as a `bun.lock` written by an unprompted `bun install`, and as
    a stray `__pycache__` entry -- as role output, and the lane then refused
    TYPED_TEST_OUTPUTS for a file no role had authored.
    """

    def test_undeclared_byproduct_is_not_role_output(self) -> None:
        self.write("tests/architecture/test_wp7.py", "def test_x():\n    raise\n")
        self.write("bun.lock", '{"lockfileVersion": 2}\n')
        self.write("tests/architecture/__pycache__/test_wp7.cpython-312.pyc", "")
        self.assertEqual(
            self.collect(("tests/architecture/test_wp7.py",)),
            {"tests/architecture/test_wp7.py": "def test_x():\n    raise\n"},
        )

    def test_unscoped_collection_still_sweeps_everything(self) -> None:
        self.write("tests/architecture/test_wp7.py", "x = 1\n")
        self.write("bun.lock", "lock\n")
        self.assertEqual(
            self.collect(),
            {"tests/architecture/test_wp7.py": "x = 1\n", "bun.lock": "lock\n"},
        )

    def test_undeclared_output_absent_reports_as_missing(self) -> None:
        self.write("bun.lock", "lock\n")
        self.assertEqual(self.collect(("tests/architecture/test_wp7.py",)), {})

    def test_deleted_declared_output_still_refuses(self) -> None:
        self.write("tests/architecture/test_wp7.py", "x = 1\n")
        self.track("tests/architecture/test_wp7.py")
        (self.checkout / "tests/architecture/test_wp7.py").unlink()
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_DELETED:tests/architecture/test_wp7.py"
        ):
            self.collect(("tests/architecture/test_wp7.py",))


class TesterCollectionMatrixTest(_RoleOutputCase):
    """`_collect_uncommitted` applies the shared policy."""

    def test_declared_and_undeclared_utf8_are_collected(self) -> None:
        self.write("src/declared.py", "declared = 1\n")
        self.write("notes/undeclared.md", "undeclared\n")
        self.assertEqual(
            self.collect(),
            {
                "src/declared.py": "declared = 1\n",
                "notes/undeclared.md": "undeclared\n",
            },
        )

    def test_modified_tracked_output_is_collected(self) -> None:
        self.write("src/tracked.py", "value = 1\n")
        self.track("src/tracked.py")
        self.write("src/tracked.py", "value = 2\n")
        self.assertEqual(self.collect(), {"src/tracked.py": "value = 2\n"})

    def test_deleted_tracked_output_refuses_instead_of_vanishing(self) -> None:
        self.write("src/tracked.py", "value = 1\n")
        self.track("src/tracked.py")
        (self.checkout / "src/tracked.py").unlink()
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_DELETED:src/tracked.py"
        ):
            self.collect()

    def test_empty_output_is_collected_as_empty_text(self) -> None:
        self.write("src/empty.py", "")
        self.assertEqual(self.collect(), {"src/empty.py": ""})

    def test_generated_caches_are_ignored_and_siblings_kept(self) -> None:
        self.write("tests/architecture/test_wp7.py", "def test_contract():\n    pass\n")
        self.write_bytes(
            "tests/architecture/__pycache__/test_wp7.cpython-312-pytest.pyc",
            b"\0\xff",
        )
        self.write(".pytest_cache/v/cache/nodeids", "[]")
        self.write_bytes(".ruff_cache/content/abc", b"\0\xff")
        self.write(".mypy_cache/3.12/x.json", "{}")
        self.write_bytes(".coverage", b"\0coverage")
        self.write_bytes(".coverage.host.9", b"\0coverage")
        self.assertEqual(
            self.collect(),
            {
                "tests/architecture/test_wp7.py": (
                    "def test_contract():\n    pass\n"
                )
            },
        )

    def test_arbitrary_binary_refuses(self) -> None:
        self.write_bytes("role-output.bin", b"\0\xff")
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_UNSAFE:role-output.bin"
        ):
            self.collect()

    def test_generated_looking_symlink_refuses(self) -> None:
        secret = self.root / "host-secret.pyc"
        secret.write_bytes(b"\0secret")
        cache = self.checkout / "__pycache__"
        cache.mkdir()
        (cache / "escape.pyc").symlink_to(secret)
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_UNSAFE:__pycache__/escape.pyc"
        ):
            self.collect()

    def test_symlink_escape_refuses(self) -> None:
        secret = self.root / "host-secret.txt"
        secret.write_text("VAULT_BYTES\n", encoding="utf-8")
        (self.checkout / "leaked.txt").symlink_to(secret)
        with self.assertRaisesRegex(FactoryRefused, "ROLE_OUTPUT_UNSAFE:leaked.txt"):
            self.collect()

    def test_fifo_is_never_collected_and_never_hangs(self) -> None:
        # git's worktree walk lists neither a FIFO nor any other non-regular,
        # non-symlink entry, so a FIFO cannot enter the draft. The call must
        # still return rather than block on the pipe.
        os.mkfifo(self.checkout / "pipe.txt")
        self.write("src/keep.py", "keep = 1\n")
        self.assertEqual(self.collect(), {"src/keep.py": "keep = 1\n"})

    def test_role_agent_scratch_is_never_collected(self) -> None:
        self.write(f"{lch.ROLE_AGENT_DIR}/prompt-1.json", "{}")
        self.write("src/keep.py", "keep = 1\n")
        self.assertEqual(self.collect(), {"src/keep.py": "keep = 1\n"})


class BuilderCommitMatrixTest(_RoleOutputCase):
    """`_commit_declared` applies the same shared policy before staging."""

    def test_declared_utf8_output_is_committed(self) -> None:
        self.write("src/declared.py", "declared = 1\n")
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        self.assertIn("src/declared.py", self.candidate_paths(sha))
        self.assertEqual(
            self.candidate_text(sha, "src/declared.py"), "declared = 1\n"
        )

    def test_undeclared_output_is_not_committed(self) -> None:
        self.write("src/declared.py", "declared = 1\n")
        self.write("notes/undeclared.md", "undeclared\n")
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        paths = self.candidate_paths(sha)
        self.assertIn("src/declared.py", paths)
        self.assertNotIn("notes/undeclared.md", paths)

    def test_modified_tracked_output_is_committed(self) -> None:
        self.write("src/tracked.py", "value = 1\n")
        self.track("src/tracked.py")
        self.write("src/tracked.py", "value = 2\n")
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        self.assertEqual(self.candidate_text(sha, "src/tracked.py"), "value = 2\n")

    def test_deleted_tracked_output_is_recorded_as_a_deletion(self) -> None:
        self.write("src/tracked.py", "value = 1\n")
        self.write("src/kept.py", "kept = 1\n")
        self.track("src/tracked.py", "src/kept.py")
        (self.checkout / "src/tracked.py").unlink()
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        paths = self.candidate_paths(sha)
        self.assertNotIn("src/tracked.py", paths)
        self.assertIn("src/kept.py", paths)

    def test_empty_declared_output_is_committed(self) -> None:
        self.write("src/empty.py", "")
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        self.assertIn("src/empty.py", self.candidate_paths(sha))
        self.assertEqual(self.candidate_text(sha, "src/empty.py"), "")

    def test_generated_cache_under_a_declared_tree_is_not_committed(self) -> None:
        self.write("src/declared.py", "declared = 1\n")
        self.write_bytes(
            "src/__pycache__/declared.cpython-312.pyc", b"\0\xff"
        )
        self.write("src/.pytest_cache/v/cache/nodeids", "[]")
        self.write_bytes("src/.ruff_cache/content/abc", b"\0\xff")
        self.write(".mypy_cache/3.12/x.json", "{}")
        self.write_bytes("src/.coverage", b"\0coverage")
        self.write_bytes("src/.coverage.host.9", b"\0coverage")
        sha, changed = self.commit_declared(["src", ".mypy_cache"])
        self.assertTrue(changed)
        self.assertEqual(self.candidate_paths(sha) - {"seed.txt"}, {"src/declared.py"})

    def test_only_generated_output_commits_nothing(self) -> None:
        self.write_bytes("src/__pycache__/x.cpython-312.pyc", b"\0\xff")
        sha, changed = self.commit_declared(["src"])
        self.assertFalse(changed)
        self.assertEqual(sha, self.base)
        self.assertNotIn("src/__pycache__/x.cpython-312.pyc", self.candidate_paths(sha))

    def test_declared_binary_output_is_committed(self) -> None:
        # A declared binary is legitimate builder output; `git diff --binary`
        # carries it. Only the tester path refuses binary, because its private
        # draft is a path-to-text mapping.
        self.write_bytes("assets/logo.bin", b"\0\xff\x00\x01")
        sha, changed = self.commit_declared(["assets"])
        self.assertTrue(changed)
        self.assertIn("assets/logo.bin", self.candidate_paths(sha))

    def test_generated_looking_symlink_refuses_before_staging(self) -> None:
        secret = self.root / "host-secret.pyc"
        secret.write_bytes(b"\0secret")
        cache = self.checkout / "src" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "escape.pyc").symlink_to(secret)
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_UNSAFE:src/__pycache__/escape.pyc"
        ):
            self.commit_declared(["src"])
        self.assertEqual(_git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_symlink_escape_refuses_before_staging(self) -> None:
        secret = self.root / "host-secret.txt"
        secret.write_text("VAULT_BYTES\n", encoding="utf-8")
        (self.checkout / "src").mkdir()
        (self.checkout / "src" / "leaked.txt").symlink_to(secret)
        with self.assertRaisesRegex(
            FactoryRefused, "ROLE_OUTPUT_UNSAFE:src/leaked.txt"
        ):
            self.commit_declared(["src"])
        self.assertEqual(_git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_fifo_declared_output_is_never_committed_and_never_hangs(self) -> None:
        (self.checkout / "src").mkdir()
        os.mkfifo(self.checkout / "src" / "pipe.txt")
        self.write("src/declared.py", "declared = 1\n")
        sha, changed = self.commit_declared(["src"])
        self.assertTrue(changed)
        self.assertEqual(self.candidate_paths(sha) - {"seed.txt"}, {"src/declared.py"})

    def test_role_agent_scratch_is_never_committed(self) -> None:
        self.write(f"{lch.ROLE_AGENT_DIR}/prompt-1.json", "{}")
        self.write("src/declared.py", "declared = 1\n")
        sha, changed = self.commit_declared(["src", lch.ROLE_AGENT_DIR])
        self.assertTrue(changed)
        self.assertEqual(self.candidate_paths(sha) - {"seed.txt"}, {"src/declared.py"})


class EnvelopeNonRegularTest(_RoleOutputCase):
    """The envelope path is the one place a FIFO can actually reach a read."""

    def _handle(self) -> lch.LaunchHandle:
        return lch.LaunchHandle(
            correlation_token="tok-fifo",
            pane_id="p:fifo",
            agent_name=lch.agent_name_for("tok-fifo"),
            launched_cwd=self.checkout,
        )

    def test_fifo_envelope_refuses_without_blocking_on_open(self) -> None:
        envelope = self.checkout / "envelope.json"
        os.mkfifo(envelope)
        with _watchdog(15.0):
            with self.assertRaisesRegex(
                FactoryRefused, "ROLE_OUTPUT_UNSAFE:envelope.json"
            ):
                self.actor._await_envelope(self._handle(), envelope, "tester")

    def test_directory_envelope_refuses(self) -> None:
        envelope = self.checkout / "envelope.json"
        envelope.mkdir()
        with _watchdog(15.0):
            with self.assertRaisesRegex(
                FactoryRefused, "ROLE_OUTPUT_UNSAFE:envelope.json"
            ):
                self.actor._await_envelope(self._handle(), envelope, "tester")


if __name__ == "__main__":
    unittest.main()
