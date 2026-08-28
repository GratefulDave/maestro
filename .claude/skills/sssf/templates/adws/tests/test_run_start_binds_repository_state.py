"""`run start` binds `repository_state`, and the class that key belongs to.

`test_declared_runners_reach_the_run` fixed one instance of a shape and said so
in its own docstring: a value the loader parses, a reader that wants it, and no
assignment in between for the verbs that execute a run. `args.runners` was
written in exactly one place, `_bind_layout_executables`, which serves
`bootstrap`, `plan author` and the run *ledger* verbs -- and no run-execution
verb goes through it.

`repository_state` was the next key in that same one place, and it was still
unbound on `run start` and `run resume` after `runners` was fixed. Its reader is
`_configured_runs_root`, which returns `None` when the attribute is missing.
`None` is a *deliberate* answer there -- a run spelled out entirely on the
command line declares no run root, so no worktree can be proven to be this
system's own and `_reclaim_stranded_integration_worktree` correctly stays out of
it. A configured `run start` is the opposite case and still landed on the same
answer, so the reclaim skipped its containment test on every configured run and
took back nothing, ever.

What that cost is a refusal that misdescribes its own subject. Asked to start a
run while a previous run's integration checkout still held the branch, Maestro
answered:

    Maestro reclaims the stranded integration checkouts inside its own run
    root without asking, and this one is not among them

about a checkout at `<state>/runs/<run_id>/integration` -- which is inside its
own run root, and is precisely what the reclaim was written to take back. The
operator was told to go move it by hand: the exact instruction the reclaim
exists to make unnecessary. Worse, the accurate refusal was reachable and
suppressed. That run had no declared outcome, so a live reclaim raises
`_RunStateStillHeld` and refuses `INTEGRATION_WORKTREE_RUN_NOT_OVER`, naming the
run and offering `run cancel --discard`. Both refusals stop the run; only one of
them tells the operator what is actually true.

The last test is the general one. Rather than waiting for a third key to be
added to `_bind_layout_executables` and silently dropped, it asserts that every
attribute that binder assigns is also assigned on the branch `start` and
`resume` share -- with `layout` exempt, and exempt for a stated reason rather
than by omission.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro

from test_declared_runners_reach_the_run import _configured_start_args

ADWS = Path(__file__).resolve().parent.parent

#: `args.layout` carries the whole parsed layout and has exactly one reader,
#: `_bootstrap`, which reaches it through `_bind_layout_executables`. A run
#: verb has no use for it -- the run branch binds the individual values it
#: needs -- so its absence from that branch is a decision, recorded here so
#: that the guard below tests the rule rather than restating the current code.
BOOTSTRAP_ONLY = {"layout"}


def _binder_assignments(name: str) -> set:
    """Attributes of `args` assigned inside the named function."""
    source = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == name)
    return _assigned_names(function.body)


def _assigned_names(body) -> set:
    names = set()
    for statement in body:
        for assignment in ast.walk(statement):
            if not isinstance(assignment, ast.Assign):
                continue
            for target in assignment.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "args"):
                    names.add(target.attr)
    return names


def _run_branch_assignments() -> set:
    """Attributes bound on the one branch `run start` and `run resume` share.

    Located by the presence of `args.db`, exactly as
    `StartAndResumeBindTheSameRunnersTest` locates it, so the two tests cannot
    disagree about which branch is the run branch.
    """
    source = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
    binder = next(
        node for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_apply_repository_config")
    for node in ast.walk(binder):
        if isinstance(node, ast.If) and "db" in _assigned_names(node.body):
            return _assigned_names(node.body)
    raise AssertionError(
        "_apply_repository_config has no branch binding args.db")


class RunStartBindsRepositoryStateTest(unittest.TestCase):

    def test_a_configured_start_binds_the_repository_state(self):
        """The instance. Without this the attribute is simply absent."""
        with tempfile.TemporaryDirectory() as tmp:
            args = _configured_start_args(Path(tmp).resolve(), None)
        self.assertTrue(
            hasattr(args, "repository_state"),
            "run start left repository_state unbound")
        self.assertTrue(Path(args.repository_state).is_absolute())

    def test_the_reclaim_can_name_a_run_root_after_a_configured_start(self):
        """What the binding is *for*.

        `_configured_runs_root` is the reader, and a `None` from it is what
        disables the containment test in
        `_reclaim_stranded_integration_worktree`. Asserting the attribute
        exists without asserting its reader answers would leave the defect
        expressible as a bound value the reader still rejects.
        """
        with tempfile.TemporaryDirectory() as tmp:
            args = _configured_start_args(Path(tmp).resolve(), None)
            runs_root = maestro._configured_runs_root(args)
        self.assertIsNotNone(
            runs_root,
            "a configured run start still declares no run root, so the "
            "stranded-integration reclaim cannot prove any worktree is its own")
        self.assertEqual(Path(str(runs_root)).name, "runs")

    def test_a_hand_spelled_run_still_declares_no_run_root(self):
        """The other half, and the reason `None` is not simply a bug.

        A run assembled from flags has no installed configuration behind it, so
        nothing can prove a worktree belongs to this system and the reclaim must
        stay out of it. If this stopped being expressible, the fix above would
        have bought the reclaim's correctness with its restraint.
        """
        args = argparse.Namespace(command="run", run_command="start")
        self.assertIsNone(maestro._configured_runs_root(args))

    def test_the_binding_lives_on_the_branch_start_and_resume_share(self):
        """`resume` re-enters a run whose integration checkout it must hold, so
        a `repository_state` bound for `start` alone would leave the reclaim
        dead on exactly the verb used to recover a run."""
        self.assertIn("repository_state", _run_branch_assignments())


class EveryLayoutBindingReachesTheRunTest(unittest.TestCase):
    """The class, not the key.

    `runners` was fixed, then `repository_state` was found unbound beside it in
    the same function. A third key added to `_bind_layout_executables` would be
    dropped the same way and stay invisible for the same reason: each end reads
    correctly on its own, and nothing compares them.
    """

    def test_the_run_branch_binds_everything_the_layout_binder_does(self):
        """Compared against the whole of `_apply_repository_config`, not
        against the `args.db` branch alone.

        The run path is not one block: `repo`, `herdr`, `omp`, `claude` and
        `route_receipt` are bound further out than the branch that binds `db`,
        and a guard reading only the inner block would report five false
        positives while still catching nothing it was written for. Every
        assignment in this function is on some configured path, which is the
        property that matters -- a value assigned nowhere in it, as
        `repository_state` was, reaches no configured verb at all.
        """
        layout_bound = _binder_assignments("_bind_layout_executables")
        run_bound = _binder_assignments("_apply_repository_config")
        missing = sorted(layout_bound - run_bound - BOOTSTRAP_ONLY)
        self.assertEqual(
            missing, [],
            "_bind_layout_executables binds {} for bootstrap, plan author "
            "and the run ledger verbs, and no run-execution verb goes through "
            "it, so nothing binds them for a configured run. Bind them in "
            "_apply_repository_config too, or name them in BOOTSTRAP_ONLY with "
            "the reason a run has no use for them.".format(missing))

    def test_the_exemption_names_only_attributes_that_binder_sets(self):
        """An exemption for a key that no longer exists silences nothing and
        hides that it is stale, so it is a failure rather than a no-op."""
        layout_bound = _binder_assignments("_bind_layout_executables")
        self.assertEqual(sorted(BOOTSTRAP_ONLY - layout_bound), [])


if __name__ == "__main__":
    unittest.main()
