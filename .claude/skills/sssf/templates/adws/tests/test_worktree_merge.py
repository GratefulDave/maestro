"""Executable proof of §8 — the worktree bracket and the merge protocol.

Every test here builds a real throwaway git repository in a temporary
directory and runs real `git`. Nothing is mocked, because this design has
been wrong every time it was reasoned about rather than run: the measurement
bracket, the private-index commit, and the cleanliness comparison all turn on
what git actually does to an index and a working tree, which a mock would
simply assert back at us.

The tests are grouped by the section they settle:

  §8.1  the attempt branches from the integration head, not from base_commit
  §8.2  identity, and the branch collision guard
  §8.3  the measurement bracket, the inventory tuple, cache redirection,
        the two-conjunct permission check, and the four checks
  §8.4  the scheduler-side commit from a harness-private index
  §8.5  deterministic merge order by (depth, node_id)
  §8.6  ancestry, merging by output SHA and never by branch name
  §8.7  the conflict protocol
  §8.8  derived post-merge acceptance and cleanup

Run with:  uv run adws/adw_test.py -k worktree
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# This file ships inside adws/tests/, so the package root is its parent's parent.
ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

CALC_TEST = """import unittest
from calc import add


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
"""

STRUTIL_TEST = """import unittest
from strutil import shout


class T(unittest.TestCase):
    def test_shout(self):
        self.assertEqual(shout("hi"), "HI!")
"""

NODE_GATE = [sys.executable, "-m", "unittest", "-q"]
INTEGRATION_GATE = [sys.executable, "-m", "unittest", "discover", "-q", "-s", ".",
                    "-p", "test_*.py"]


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git in a throwaway repository and return its stdout."""
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def _make_repo(root: Path) -> Path:
    """A repository with two failing tests and neither implementation.

    The two suites are deliberately independent: one node can make its own
    suite green while the other's stays red, which is the shape §8.5's
    ordering and the F3 gate-scope finding both need.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    # The developer's own global hooks must never run inside a test repository.
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "test_calc.py").write_text(CALC_TEST)
    (repo / "test_strutil.py").write_text(STRUTIL_TEST)
    (repo / "README.md").write_text("fixture repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _attempt(repo: Path, root: Path, node_id: str = "nodeA", attempt_no: int = 1,
             head: str | None = None) -> wt.AttemptWorktree:
    return wt.create_attempt_worktree(
        repo=repo,
        run_id="run1",
        node_id=node_id,
        attempt_no=attempt_no,
        integration_head=head or _git(repo, "rev-parse", "HEAD"),
        worktrees_root=root / "worktrees",
        scratch_root=root / "scratch",
    )


class WorktreeTestCase(unittest.TestCase):
    """Base class holding the temporary directory every test needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = _make_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ── §8.1 / §8.2 ─────────────────────────────────────────────────────────────

class TwoBasesAndIdentity(WorktreeTestCase):

    def test_attempt_branches_from_the_integration_head_not_from_base_commit(self):
        """§8.1: the execution base is the integration head, deliberately not
        the authoring base. Under a base-commit rule no data crosses a
        dependency edge, so this distinction is the whole reason `needs` works."""
        base_commit = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "integration")
        (self.repo / "from_upstream.txt").write_text("merged by an earlier node\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "an earlier node merged here")
        head = wt.integration_head(self.repo, "integration")
        self.assertNotEqual(head, base_commit)

        attempt = _attempt(self.repo, self.root, head=head)
        self.assertEqual(attempt.base, head)
        # The predecessor's work is visible in the attempt's tree, which is
        # exactly what branching from base_commit would have destroyed.
        self.assertTrue((attempt.path / "from_upstream.txt").is_file())

    def test_branch_and_worktree_carry_the_run_node_attempt_tuple(self):
        """§8.2: one definition of the name, and it encodes the attempt."""
        attempt = _attempt(self.repo, self.root, node_id="nodeA", attempt_no=2)
        self.assertEqual(attempt.branch, "maestro/run1/nodeA/a2")
        self.assertIn("run1", attempt.path.name)
        self.assertIn("nodeA", attempt.path.name)
        self.assertIn("a2", attempt.path.name)
        self.assertEqual(_git(attempt.path, "symbolic-ref", "--short", "HEAD"),
                         attempt.branch)

    def test_creating_the_same_attempt_twice_collides_on_the_branch(self):
        """§8.2: `git worktree add -b` failing on an existing branch *is* the
        collision guard, so a second attempt with the same tuple must fail."""
        _attempt(self.repo, self.root)
        with self.assertRaises(wt.BranchCollision):
            _attempt(self.repo, self.root)

    def test_a_second_attempt_number_is_a_different_worktree(self):
        """§8.2: the attempt component is what lets attempt 2 exist at all."""
        first = _attempt(self.repo, self.root, attempt_no=1)
        second = _attempt(self.repo, self.root, attempt_no=2)
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.branch, second.branch)


# ── §8.3 the inventory tuple ────────────────────────────────────────────────

class InventoryTuple(WorktreeTestCase):

    def test_tuple_pins_git_mode_class_and_git_blob_object_id(self):
        """§8.3 / §17 item 95: the tuple is git's committable resolution — mode
        class and blob object id — so no measured delta can fail to stage."""
        attempt = _attempt(self.repo, self.root)
        inv = wt.inventory(attempt.path)
        mode, blob = inv["README.md"]
        self.assertEqual(mode, "100644")
        self.assertEqual(
            blob, _git(attempt.path, "hash-object", "-t", "blob", "README.md"))

    def test_a_chmod_only_change_is_a_measured_delta(self):
        """§8.3: a chmod node changes content not at all and still measures a
        one-path delta, because the mode class is part of the tuple."""
        attempt = _attempt(self.repo, self.root)
        target = attempt.path / "deploy.sh"
        target.write_text("#!/bin/sh\necho hi\n")
        before = wt.inventory(attempt.path)
        target.chmod(0o755)
        after = wt.inventory(attempt.path)
        self.assertEqual(before["deploy.sh"][1], after["deploy.sh"][1])  # same bytes
        self.assertEqual(wt.delta(before, after).changed, ("deploy.sh",))

    def test_a_symlink_hashes_its_own_target_bytes_not_the_dereferenced_file(self):
        """§8.3: a file replaced by a symlink to identical bytes is a tuple
        change, which is only true if the symlink hashes its target path."""
        attempt = _attempt(self.repo, self.root)
        (attempt.path / "real.txt").write_text("payload\n")
        link = attempt.path / "link.txt"
        link.symlink_to("real.txt")
        inv = wt.inventory(attempt.path)
        self.assertEqual(inv["link.txt"][0], "120000")
        self.assertNotEqual(inv["link.txt"][1], inv["real.txt"][1])

    def test_the_git_administrative_path_is_never_measured(self):
        """The linked worktree's `.git` file is git's own bookkeeping and can
        never be committed; measuring it would put a path in every delta that
        no commit could ever carry. Nothing else is excluded — §8.3 forbids an
        ignore list, so dotfiles the repository really owns stay measured."""
        attempt = _attempt(self.repo, self.root)
        (attempt.path / ".gitignore").write_text("*.log\n")
        inv = wt.inventory(attempt.path)
        self.assertNotIn(".git", inv)
        self.assertIn(".gitignore", inv)


# ── §8.3 cache redirection ──────────────────────────────────────────────────

class CacheRedirection(WorktreeTestCase):

    def test_scratch_env_names_every_variable_the_bracket_depends_on(self):
        attempt = _attempt(self.repo, self.root)
        env = wt.scratch_env(attempt.scratch)
        for key in ("XDG_CACHE_HOME", "TMPDIR", "PYTHONPYCACHEPREFIX", "PYTEST_ADDOPTS"):
            self.assertIn(key, env)
            self.assertIn(str(attempt.scratch), env[key])

    def test_every_redirected_variable_is_forwarded_across_the_pane_boundary(self):
        """§8.3: the same environment goes to all three bracket contexts.

        Two of them are subprocesses of this harness and inherit the mapping.
        The pane is not: it is forked by the herdr server, and reaches it only
        through `launcher.SCRATCH_ENV_KEYS`. A variable added here and not
        there is honoured by the gates and ignored by the agent — the
        asymmetry that convicted a node on 2026-08-17 — so the two sets are
        the same set, checked rather than reviewed."""
        attempt = _attempt(self.repo, self.root)
        self.assertEqual(set(wt.scratch_env(attempt.scratch)),
                         set(launcher.SCRATCH_ENV_KEYS))

    def test_the_gate_invocation_itself_carries_the_redirection(self):
        """§8.3 item 3: the environment is applied to the gate invocation, not
        only to the agent's pane. The gate reports the variables it actually
        received, so this is the invoked process's own answer rather than ours."""
        attempt = _attempt(self.repo, self.root)
        script = ("import json, os; print(json.dumps({k: os.environ.get(k) for k in "
                  "('XDG_CACHE_HOME', 'TMPDIR', 'PYTHONPYCACHEPREFIX', 'PYTEST_ADDOPTS')}))")
        gate = wt.run_node_gate(
            attempt, [sys.executable, "-c", script], selector="ignored",
            cancel_requested=lambda: False)
        observed = json.loads(gate.tail[-1])
        for key, value in observed.items():
            self.assertIsNotNone(value, key)
            self.assertIn(str(attempt.scratch), value, key)

    def test_a_redirected_byproduct_lands_in_scratch_and_not_in_the_delta(self):
        """§8.3: byproducts are redirected out of the worktree, not suppressed
        tool by tool. A cache-honouring tool writes into the attempt's scratch,
        so nothing it does appears in the measured delta.

        The probe measured this as load-bearing — 16 baseline paths without the
        redirection, 6 with it — but the obvious control for the Python half of
        it cannot be executed on this machine: Apple's `python3` ships a default
        `sys.pycache_prefix`, so this interpreter never writes `__pycache__`
        into the tree whether `PYTHONPYCACHEPREFIX` is set or not. What the
        control below proves instead is that the measurement is not vacuous:
        a byproduct with nowhere to redirect really does land in the delta."""
        attempt = _attempt(self.repo, self.root)
        script = ("import os, pathlib\n"
                  "pathlib.Path(os.environ['TMPDIR'], 'gate-temp').write_text('t')\n"
                  "pathlib.Path(os.environ['XDG_CACHE_HOME'], 'gate-cache').write_text('c')\n")
        baseline = wt.take_baseline(attempt)
        gate = wt.run_node_gate(
            attempt, [sys.executable, "-c", script], selector="ignored",
            cancel_requested=lambda: False)
        self.assertEqual(gate.exit_code, 0, gate.tail)
        self.assertEqual(wt.delta(baseline, wt.inventory(attempt.path)), wt.InventoryDelta())
        self.assertTrue((attempt.scratch / "tmp" / "gate-temp").is_file())
        self.assertTrue((attempt.scratch / "xdg" / "gate-cache").is_file())

    def test_a_byproduct_with_nowhere_to_redirect_still_convicts(self):
        """§8.3's preference order, third arm: redirect; failing that, suppress;
        failing both, the write convicts — loudly, with the path named."""
        attempt = _attempt(self.repo, self.root)
        script = "import pathlib; pathlib.Path('tool-residue.txt').write_text('beside its work')"
        baseline = wt.take_baseline(attempt)
        wt.run_node_gate(
            attempt, [sys.executable, "-c", script], selector="ignored",
            cancel_requested=lambda: False)
        measured = wt.delta(baseline, wt.inventory(attempt.path))
        self.assertEqual(measured.added, ("tool-residue.txt",))
        verdict = wt.permission_check(attempt, measured, declared=["calc.py"])
        self.assertFalse(verdict.passes)
        self.assertEqual(verdict.conjunct1_violations, ("tool-residue.txt",))


# ── §8.3 the two-conjunct permission check ──────────────────────────────────

class PermissionCheck(WorktreeTestCase):

    def _bracket(self, write) -> tuple[wt.AttemptWorktree, wt.InventoryDelta, dict]:
        attempt = _attempt(self.repo, self.root)
        (attempt.path / "provisioned.txt").write_text("written by provision\n")
        baseline = wt.take_baseline(attempt)
        write(attempt.path)
        after = wt.inventory(attempt.path)
        return attempt, wt.delta(baseline, after), after

    def test_a_declared_output_passes_both_conjuncts(self):
        attempt, d, _ = self._bracket(
            lambda p: (p / "calc.py").write_text("def add(a, b):\n    return a + b\n"))
        verdict = wt.permission_check(attempt, d, declared=["calc.py"])
        self.assertTrue(verdict.passes, verdict)

    def test_an_undeclared_write_fails_conjunct_one(self):
        attempt, d, _ = self._bracket(
            lambda p: (p / "elsewhere.py").write_text("not mine to write\n"))
        verdict = wt.permission_check(attempt, d, declared=["calc.py"])
        self.assertFalse(verdict.passes)
        self.assertEqual(verdict.conjunct1_violations, ("elsewhere.py",))

    def test_declared_outputs_may_be_globs(self):
        attempt, d, _ = self._bracket(
            lambda p: (p / "calc.py").write_text("def add(a, b):\n    return a + b\n"))
        self.assertTrue(wt.permission_check(attempt, d, declared=["*.py"]).passes)

    def test_tampering_with_provisioned_untracked_content_convicts_whatever_the_globs_say(self):
        """§8.3 conjunct (2): a glob can authorize creating a file or changing
        committed content; it can never authorize rewriting content provision
        put there. This is what turns the stubbed-dependency case into a
        theorem rather than an assumption about plan hygiene."""
        attempt, d, _ = self._bracket(
            lambda p: (p / "provisioned.txt").write_text("agent rewrote this\n"))
        verdict = wt.permission_check(attempt, d, declared=["provisioned.txt", "*"])
        self.assertFalse(verdict.passes)
        self.assertEqual(verdict.conjunct1_violations, ())
        self.assertTrue(verdict.conjunct2_violations)

    def test_a_rewritten_provisioned_file_with_restored_stat_metadata_still_convicts(self):
        """§8.3: full re-hashing is the defined semantics. A stat short-circuit
        would silently degrade this to "detected unless the agent restores
        mtime", which is the precise hole the content hash closes."""
        attempt = _attempt(self.repo, self.root)
        provisioned = attempt.path / "provisioned.txt"
        provisioned.write_text("aaaa\n")
        stat = provisioned.stat()
        baseline = wt.take_baseline(attempt)
        provisioned.write_text("bbbb\n")  # same length, restored timestamps
        os.utime(provisioned, (stat.st_atime, stat.st_mtime))
        d = wt.delta(baseline, wt.inventory(attempt.path))
        self.assertEqual(d.changed, ("provisioned.txt",))


# ── §8.4 the scheduler-side commit ──────────────────────────────────────────

class SchedulerSideCommit(WorktreeTestCase):

    def _committed(self, node_id: str = "nodeA"):
        attempt = _attempt(self.repo, self.root, node_id=node_id)
        (attempt.path / ".pre_gate_cache").write_text("byproduct of the pre-gate\n")
        baseline = wt.take_baseline(attempt)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        sha = wt.commit_measured_delta(attempt, d, after, f"{node_id}: measured delta")
        return attempt, baseline, d, after, sha

    def test_the_private_index_lives_outside_the_worktree_and_the_scratch(self):
        """§8.4, confirmed by probe finding F4: an index inside either becomes
        a delta path of its own and convicts the node it was measuring."""
        attempt = _attempt(self.repo, self.root)
        self.assertFalse(str(attempt.private_index).startswith(str(attempt.path) + os.sep))
        self.assertFalse(str(attempt.private_index).startswith(str(attempt.scratch) + os.sep))

    def test_the_commit_set_is_exactly_the_measured_delta(self):
        attempt, _, _, _, sha = self._committed()
        listed = _git(attempt.path, "diff-tree", "--no-commit-id", "--name-only",
                      "-r", sha).splitlines()
        self.assertEqual(listed, ["calc.py"])

    def test_the_recorded_base_is_the_commit_s_sole_parent(self):
        attempt, _, _, _, sha = self._committed()
        self.assertEqual(_git(attempt.path, "rev-list", "--parents", "-n", "1", sha).split()[1:],
                         [attempt.base])

    def test_a_survivor_staging_everything_cannot_influence_the_commit(self):
        """§8.4: the worktree's own index is never the source of the commit, so
        `git add -A` by a survivor changes nothing about what is committed."""
        attempt = _attempt(self.repo, self.root)
        baseline = wt.take_baseline(attempt)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        (attempt.path / "survivor.txt").write_text("staged by a survivor\n")
        _git(attempt.path, "add", "-A")
        sha = wt.commit_measured_delta(attempt, d, after, "nodeA: measured delta")
        listed = _git(attempt.path, "diff-tree", "--no-commit-id", "--name-only",
                      "-r", sha).splitlines()
        self.assertEqual(listed, ["calc.py"])

    def test_a_survivor_commit_is_caught_by_the_pre_commit_head_assertion(self):
        """§8.4's early detection: HEAD no longer equals the recorded base."""
        attempt = _attempt(self.repo, self.root)
        baseline = wt.take_baseline(attempt)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        _git(attempt.path, "add", "-A")
        _git(attempt.path, "commit", "-qm", "a survivor committed on its own")
        with self.assertRaises(wt.HeadMoved):
            wt.commit_measured_delta(attempt, d, after, "nodeA: measured delta")

    def test_compare_and_swap_refuses_a_ref_that_moved_underneath(self):
        """§8.4's enforcement, tested where the early assertion cannot reach:
        a survivor that moves the attempt ref *after* the HEAD assertion. The
        swap expects the recorded base and must refuse rather than build on it.
        The probe recorded this as F5, a positive control — the refusal is what
        makes the staging-to-commit interval closed against a survivor commit."""
        attempt = _attempt(self.repo, self.root)
        wt.take_baseline(attempt)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _git(attempt.path, "add", "-A")
        _git(attempt.path, "commit", "-qm", "a survivor committed on its own")
        moved = _git(attempt.path, "rev-parse", "HEAD")
        self.assertNotEqual(moved, attempt.base)
        with self.assertRaises(wt.CompareAndSwapRefused):
            wt.advance_attempt_ref(attempt, moved)

    def test_a_write_between_the_after_inventory_and_staging_fails_the_staging_assertion(self):
        """§8.4: staging asserts rather than assumes that the staged bytes are
        the measured bytes, so a write inside the window where nothing may
        write surfaces as a mismatch instead of passing a tautology."""
        attempt = _attempt(self.repo, self.root)
        baseline = wt.take_baseline(attempt)
        target = attempt.path / "calc.py"
        target.write_text("def add(a, b):\n    return a + b\n")
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        target.write_text("def add(a, b):\n    return 999\n")  # the planted write
        with self.assertRaises(wt.StagingMismatch):
            wt.commit_measured_delta(attempt, d, after, "nodeA: measured delta")
        self.assertEqual(_git(attempt.path, "rev-parse", "HEAD"), attempt.base)

    def test_a_node_with_an_empty_delta_still_gets_an_output_sha(self):
        """§8.4: nodes with no outputs commit empty, so every node has an output
        SHA and the merge guard stays uniform — a code node whose acceptance is
        "nothing broke" (§6.7) is the ordinary case, not an exception."""
        attempt = _attempt(self.repo, self.root)
        baseline = wt.take_baseline(attempt)
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        self.assertTrue(d.is_empty)
        sha = wt.commit_measured_delta(attempt, d, after, "nodeA: nothing broke")
        self.assertEqual(_git(attempt.path, "diff-tree", "--no-commit-id", "--name-only",
                              "-r", sha), "")
        self.assertEqual(_git(attempt.path, "rev-parse", "HEAD"), sha)

    def test_the_worktree_index_is_refreshed_to_the_committed_tree(self):
        """§8.4: without the `read-tree` refresh the worktree's index still
        holds the base tree, and every honest node with a non-empty delta looks
        dirty over its own committed content."""
        attempt, _, _, _, _ = self._committed()
        tracked_dirt = [line for line in
                        _git(attempt.path, "status", "--porcelain").splitlines()
                        if not line.startswith("??")]
        self.assertEqual(tracked_dirt, [])


# ── §8.3 the four checks and the cleanliness comparison ─────────────────────

class FourChecks(WorktreeTestCase):

    def _verified_bracket(self, pre_gate_writes: str | None = None):
        attempt = _attempt(self.repo, self.root)
        if pre_gate_writes:
            (attempt.path / pre_gate_writes).write_text("byproduct of the pre-gate\n")
        baseline = wt.take_baseline(attempt)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        sha = wt.commit_measured_delta(attempt, d, after, "nodeA: measured delta")
        return attempt, wt.expected_inventory(baseline, d, after), sha

    def test_the_four_checks_hold_at_create(self):
        attempt = _attempt(self.repo, self.root)
        checks = wt.check_at_create(attempt)
        self.assertTrue(checks.ok, checks)
        self.assertTrue(checks.branch_checked_out)
        self.assertTrue(checks.head_resolves)
        self.assertTrue(checks.base_is_ancestor)

    def test_a_pre_gate_byproduct_does_not_convict_at_the_post_commit_check(self):
        """§13.3's negative control, first half. This is the case that convicted
        two fully-verified nodes in the 2026-08-13 probe when the check read
        `git status` instead of comparing against the expected inventory."""
        attempt, expected, _ = self._verified_bracket(pre_gate_writes=".pre_gate_cache")
        self.assertTrue((attempt.path / ".pre_gate_cache").is_file())
        self.assertIn("?? .pre_gate_cache", _git(attempt.path, "status", "--porcelain"))
        verdict = wt.check_post_commit(attempt, expected)
        self.assertTrue(verdict.ok, verdict)
        self.assertEqual(verdict.cleanliness.divergences, ())

    def test_an_identically_named_agent_created_file_does_convict(self):
        """§13.3's negative control, second half, and the reason the first half
        is not an ignore list: the same *name* convicts when its measured tuple
        is not in the expected inventory. Here the agent's survivor writes it
        after the after-inventory, so the commit never carried it."""
        attempt, expected, _ = self._verified_bracket()
        (attempt.path / ".pre_gate_cache").write_text("written by the agent, after settle\n")
        verdict = wt.check_post_commit(attempt, expected)
        self.assertFalse(verdict.ok)
        self.assertEqual([d.path for d in verdict.cleanliness.divergences],
                         [".pre_gate_cache"])
        self.assertEqual(verdict.cleanliness.consequence, "convict")

    def test_a_relocated_byproduct_is_judged_on_its_tuple_not_on_its_name(self):
        """The comparison permits nothing by name: the same baseline byproduct
        moved to a new path is a divergence at both ends."""
        attempt, expected, _ = self._verified_bracket(pre_gate_writes=".pre_gate_cache")
        (attempt.path / ".pre_gate_cache").rename(attempt.path / ".pre_gate_cache_moved")
        verdict = wt.check_post_commit(attempt, expected)
        self.assertFalse(verdict.ok)
        self.assertEqual([d.path for d in verdict.cleanliness.divergences],
                         [".pre_gate_cache", ".pre_gate_cache_moved"])

    def test_an_in_place_rewrite_of_a_committed_path_diverges(self):
        attempt, expected, _ = self._verified_bracket()
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return 999\n")
        verdict = wt.check_post_commit(attempt, expected)
        self.assertFalse(verdict.ok)
        self.assertEqual([d.kind for d in verdict.cleanliness.divergences], ["changed"])

    def test_post_gate_residue_is_reported_at_pre_merge_rather_than_convicting(self):
        """§8.3: the commit was sealed before the gate, so residue is an adapter
        hygiene defect with the paths named, never a verdict about the work."""
        attempt, expected, sha = self._verified_bracket()
        (attempt.path / "coverage.xml").write_text("<coverage/>\n")  # post-gate residue
        verdict = wt.check_pre_merge(attempt, expected)
        self.assertFalse(verdict.cleanliness.clean)
        self.assertEqual(verdict.cleanliness.consequence, "report")
        self.assertEqual([d.path for d in verdict.cleanliness.divergences], ["coverage.xml"])
        self.assertTrue(verdict.merge_permitted)
        self.assertEqual(_git(attempt.path, "rev-parse", "HEAD"), sha)


# ── F3: gate scope ──────────────────────────────────────────────────────────

class GateScope(WorktreeTestCase):

    def test_a_node_gate_without_a_selector_is_refused(self):
        """The per-node gate is scoped to the node's own declared selector, so
        an unscoped node gate is not a default anyone can fall into."""
        attempt = _attempt(self.repo, self.root)
        with self.assertRaises(ValueError):
            wt.run_node_gate(attempt, NODE_GATE, selector="",
                             cancel_requested=lambda: False)

    def test_the_whole_suite_is_red_for_a_node_whose_sibling_has_not_merged(self):
        """Probe finding F3, executed. nodeA does its own work correctly; the
        whole suite is still red because nodeB's work is not in nodeA's
        worktree. Scoping the post-node gate to the node's own selector is what
        keeps §7.3's VERIFIED predicate satisfiable at all."""
        attempt = _attempt(self.repo, self.root)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")

        whole = wt.run_integration_gate(
            attempt.path, INTEGRATION_GATE, attempt.scratch,
            cancel_requested=lambda: False)
        self.assertFalse(whole.green)
        self.assertEqual(whole.scope, "integration")

        scoped = wt.run_node_gate(
            attempt, NODE_GATE, selector="test_calc",
            cancel_requested=lambda: False)
        self.assertTrue(scoped.green, scoped)
        self.assertEqual(scoped.scope, "node")

    def test_the_pre_gate_is_red_and_the_post_gate_is_green_at_the_same_selector(self):
        """§7.4 / §7.3 clauses 2 and 3, at the scope F3 requires."""
        attempt = _attempt(self.repo, self.root)
        pre = wt.run_node_gate(
            attempt, NODE_GATE, selector="test_calc",
            cancel_requested=lambda: False, label="pre-gate")
        self.assertFalse(pre.green)
        (attempt.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        post = wt.run_node_gate(
            attempt, NODE_GATE, selector="test_calc",
            cancel_requested=lambda: False, label="post-gate")
        self.assertTrue(post.green)



    def test_cancellation_at_any_gate_scope_is_a_typed_non_result(self):
        attempt = _attempt(self.repo, self.root)
        cancelled = lambda: True

        with self.assertRaises(wt.GateCancelled):
            wt.run_node_gate(
                attempt, NODE_GATE, selector="test_calc",
                cancel_requested=cancelled)
        with self.assertRaises(wt.GateCancelled):
            wt.run_integration_gate(
                attempt.path, INTEGRATION_GATE, attempt.scratch,
                cancel_requested=cancelled)

# ── §8.5 deterministic merge order ──────────────────────────────────────────

class MergeOrder(unittest.TestCase):

    def _graph(self) -> list[wt.NodeRecord]:
        return [
            wt.NodeRecord("nodeB", depth=1, needs=(), state="VERIFIED"),
            wt.NodeRecord("nodeA", depth=1, needs=(), state="VERIFIED"),
            wt.NodeRecord("nodeC", depth=2, needs=("nodeA",), state="VERIFIED"),
        ]

    def test_order_is_the_minimum_by_depth_then_node_id(self):
        frontier = wt.merge_frontier(self._graph())
        self.assertEqual(frontier, ("nodeA", "nodeB"))

    def test_order_is_identical_under_inverted_finish_order(self):
        """§8.5: the frontier is a function of the graph, the merged set, and
        the blocked set only — never of timing."""
        forwards = self._graph()
        backwards = list(reversed(self._graph()))
        self.assertEqual(wt.merge_frontier(forwards), wt.merge_frontier(backwards))

    def test_a_dependent_is_not_on_the_frontier_until_its_dependency_merged(self):
        nodes = self._graph()
        self.assertNotIn("nodeC", wt.merge_frontier(nodes))
        merged = [n if n.node_id != "nodeA" else n.with_state("MERGED") for n in nodes]
        self.assertEqual(wt.merge_frontier(merged)[0], "nodeB")
        merged = [n if n.node_id != "nodeB" else n.with_state("MERGED") for n in merged]
        self.assertEqual(wt.merge_frontier(merged), ("nodeC",))

    def test_the_frontier_minimum_is_waited_for_rather_than_skipped(self):
        nodes = [wt.NodeRecord("nodeA", 1, (), "RUNNING"),
                 wt.NodeRecord("nodeB", 1, (), "VERIFIED")]
        self.assertIsNone(wt.merge_ready(nodes))
        self.assertEqual(wt.next_merge_candidate(nodes).node_id, "nodeA")

    def test_a_blocked_frontier_node_is_excluded_and_cascades_to_descendants(self):
        """§8.5 / §8.7: the exclusion is load-bearing — without it the merge
        thread waits on a node that will never verify, and verified independent
        nodes queue behind it forever."""
        nodes = [wt.NodeRecord("nodeA", 1, (), "BLOCKED"),
                 wt.NodeRecord("nodeB", 1, (), "VERIFIED"),
                 wt.NodeRecord("nodeC", 2, ("nodeA",), "VERIFIED")]
        self.assertEqual(wt.merge_frontier(nodes), ("nodeB",))
        self.assertEqual(wt.upstream_blocked(nodes), ("nodeC",))

    def test_an_abandoned_node_cascades_the_same_way(self):
        nodes = [wt.NodeRecord("nodeA", 1, (), "CANCELLED"),
                 wt.NodeRecord("nodeC", 2, ("nodeA",), "VERIFIED")]
        self.assertEqual(wt.merge_frontier(nodes), ())
        self.assertEqual(wt.upstream_blocked(nodes), ("nodeC",))


# ── §8.6 / §8.7 / §8.8 merge, conflict, acceptance ──────────────────────────

class MergeAndAcceptance(WorktreeTestCase):

    def _integration(self) -> Path:
        path = self.root / "integration"
        wt.create_worktree(self.repo, path, "integration",
                           _git(self.repo, "rev-parse", "HEAD"))
        return path

    def _node(self, node_id: str, filename: str, body: str) -> tuple[wt.AttemptWorktree, str]:
        attempt = _attempt(self.repo, self.root, node_id=node_id)
        baseline = wt.take_baseline(attempt)
        (attempt.path / filename).write_text(body)
        after = wt.inventory(attempt.path)
        d = wt.delta(baseline, after)
        sha = wt.commit_measured_delta(attempt, d, after, f"{node_id}: measured delta")
        return attempt, sha

    def test_two_nodes_merge_in_frontier_order_and_the_integration_gate_is_green(self):
        """The end-to-end shape the probe executed: two independent nodes, both
        merged by output SHA in (depth, node_id) order, with the whole suite
        green only after both are integrated."""
        integration = self._integration()
        _, sha_a = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        _, sha_b = self._node("nodeB", "strutil.py",
                              'def shout(s):\n    return s.upper() + "!"\n')

        order = wt.merge_frontier([wt.NodeRecord("nodeB", 1, (), "VERIFIED"),
                                   wt.NodeRecord("nodeA", 1, (), "VERIFIED")])
        self.assertEqual(order, ("nodeA", "nodeB"))

        results = [wt.merge_verified_node(integration, node, sha)
                   for node, sha in (("nodeA", sha_a), ("nodeB", sha_b))]
        for result in results:
            self.assertTrue(result.ancestry_proven, result)
            self.assertEqual(result.conflicted_paths, ())

        gate = wt.run_integration_gate(
            integration, INTEGRATION_GATE, self.root / "scratch" / "integration",
            cancel_requested=lambda: False)
        self.assertTrue(gate.green, gate.tail)

    def test_the_merge_consumes_the_output_sha_and_never_the_branch(self):
        """§8.6: a merge by name would carry a survivor's later commit while the
        ancestry proof still passed, because the recorded output SHA is an
        ancestor of its own descendants."""
        integration = self._integration()
        attempt, sha = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        (attempt.path / "smuggled.txt").write_text("a survivor's later commit\n")
        _git(attempt.path, "add", "-A")
        _git(attempt.path, "commit", "-qm", "survivor commit on the attempt branch")
        survivor_sha = _git(attempt.path, "rev-parse", "HEAD")
        self.assertNotEqual(survivor_sha, sha)

        result = wt.merge_verified_node(integration, "nodeA", sha)
        self.assertTrue(result.ancestry_proven)
        self.assertFalse((integration / "smuggled.txt").exists())
        history = _git(integration, "rev-list", "HEAD").splitlines()
        self.assertIn(sha, history)
        self.assertNotIn(survivor_sha, history)

    def test_a_conflict_aborts_and_leaves_the_integration_head_byte_identical(self):
        """§8.7: capture the conflicted paths, abort, block the node, and let
        independent branches keep running."""
        integration = self._integration()
        _, sha_a = self._node("nodeA", "shared.py", "VALUE = 'a'\n")
        _, sha_b = self._node("nodeB", "shared.py", "VALUE = 'b'\n")
        first = wt.merge_verified_node(integration, "nodeA", sha_a)
        self.assertTrue(first.ancestry_proven)

        head_before = _git(integration, "rev-parse", "HEAD")
        second = wt.merge_verified_node(integration, "nodeB", sha_b)
        self.assertEqual(second.conflicted_paths, ("shared.py",))
        self.assertFalse(second.ancestry_proven)
        self.assertEqual(_git(integration, "rev-parse", "HEAD"), head_before)
        self.assertEqual(_git(integration, "status", "--porcelain"), "")

    def test_final_acceptance_is_the_deduplicated_union_of_merged_specs(self):
        """§8.8: derived rather than hand-authored, and deterministic."""
        nodes = [wt.NodeRecord("nodeB", 1, (), "MERGED", specs=("test_strutil", "lint")),
                 wt.NodeRecord("nodeA", 1, (), "MERGED", specs=("test_calc", "lint")),
                 wt.NodeRecord("nodeC", 1, (), "BLOCKED", specs=("test_never",))]
        self.assertEqual(wt.acceptance_specs(nodes), ("lint", "test_calc", "test_strutil"))

    def test_the_final_sweep_reproves_every_merged_node_against_the_final_head(self):
        integration = self._integration()
        _, sha_a = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        _, sha_b = self._node("nodeB", "strutil.py",
                              'def shout(s):\n    return s.upper() + "!"\n')
        wt.merge_verified_node(integration, "nodeA", sha_a)
        wt.merge_verified_node(integration, "nodeB", sha_b)
        sweep = wt.final_ancestry_sweep(integration, {"nodeA": sha_a, "nodeB": sha_b})
        self.assertEqual(sweep, {"nodeA": True, "nodeB": True})

    def test_the_sweep_fails_for_a_node_that_was_never_merged(self):
        integration = self._integration()
        _, sha_a = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        _, sha_b = self._node("nodeB", "strutil.py", "def shout(s):\n    return s\n")
        wt.merge_verified_node(integration, "nodeA", sha_a)
        sweep = wt.final_ancestry_sweep(integration, {"nodeA": sha_a, "nodeB": sha_b})
        self.assertEqual(sweep, {"nodeA": True, "nodeB": False})

    def test_cleanup_refuses_before_ancestry_is_proven(self):
        """§8.8: deleting an unmerged branch destroys the only copy."""
        attempt, _ = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        with self.assertRaises(wt.WorktreeError):
            wt.remove_attempt_worktree(attempt, ancestry_proven=False)
        self.assertTrue(attempt.path.is_dir())

    def test_cleanup_removes_the_worktree_and_branch_after_ancestry(self):
        integration = self._integration()
        attempt, sha = self._node("nodeA", "calc.py", "def add(a, b):\n    return a + b\n")
        result = wt.merge_verified_node(integration, "nodeA", sha)
        wt.remove_attempt_worktree(attempt, ancestry_proven=result.ancestry_proven,
                                   integration_path=integration)
        self.assertFalse(attempt.path.exists())
        self.assertNotIn(attempt.branch, _git(self.repo, "branch", "--list", attempt.branch))


if __name__ == "__main__":
    unittest.main()
