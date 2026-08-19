"""Where a run's panes land, and why that is a correctness property.

The operator watches these panes to see what the factory is doing, so a run
whose agents scatter across workspaces is a run nobody can supervise — the
guarantee that every agent node runs in a *visible* pane buys nothing if the
panes are somewhere the operator is not looking. One run observed on
2026-08-19 opened its first pane in `w13A` and then created `w13F`, `w13G`,
`w13H`, `w13J` and `w13K`.

Two causes, both at `HerdrLauncher.launch`'s split call:

* **The `--current` fallback.** `_split_parent` resolves herdr's `--current`
  selector once and caches the pane id, which is what removed the 2026-08-18
  race between two lanes reading focus at two different instants. When herdr
  could not answer, it used the selector unchanged — reinstating that race,
  and putting the pane wherever focus happened to be, which can be another
  workspace entirely. §1.2 forbids a decision keyed on ambient mutable state.
  It is now a typed `SPLIT_PARENT_UNRESOLVED` refusal that splits nothing, and
  the failure is not cached, so the refusal is retryable and the next launch
  genuinely re-asks (driven in `test_launch_refusal_cleanup.py`).

* **A constant `--direction right`.** Every split names the same fixed parent,
  so a constant direction halved that parent's width again on every launch and
  six concurrent lanes produced six unreadable slivers. The direction is now a
  pure function of a lock-guarded counter the launcher owns.

The binding is checked rather than assumed: a child reporting a workspace
other than the parent's is reaped and refused, because a guarantee nothing
measures is a hope.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch          # noqa: E402
from adw_modules import worktree as worktree_module  # noqa: E402
from adw_modules.route_receipts import (load_admitted_routes,  # noqa: E402
                                        load_public_key)

from test_launch_refusal_cleanup import FakeHerdr   # noqa: E402


class _PlacementHerdr(FakeHerdr):
    """A herdr stand-in that hands out a distinct child pane per split.

    The base fake returns one fixed child id, which cannot show whether two
    concurrent launches collided. This one allocates `w0:p1`, `w0:p2`, … under
    its own lock — the child lands in its parent's workspace, as a real split
    does — so two launches that selected the same pane would be visible as one
    id handed out twice.
    """

    def __init__(self, *, worktree: Path, transcript: Path,
                 workspace: str = "w0") -> None:
        super().__init__(worktree=worktree, transcript=transcript)
        self.current_pane_id = workspace + ":p0"
        self._alloc_lock = threading.Lock()
        self._next_pane = 0
        self._workspace = workspace

    def __call__(self, *args, env=None, timeout=30.0):
        head = tuple(args[:2])
        if head == ("pane", "split"):
            with self._alloc_lock:
                self._next_pane += 1
                self.split_pane_id = "{0}:p{1}".format(
                    self._workspace, self._next_pane)
        return super().__call__(*args, env=env, timeout=timeout)

    def directions(self):
        return [call[call.index("--direction") + 1]
                for call in self.argv_for(("pane", "split"))]

    def parents(self):
        return [call[2] for call in self.argv_for(("pane", "split"))]


class PanePlacementTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))

    def spec(self, token: str = "run1-node_a-1") -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token=token, worktree=self.worktree,
            prompt_path=self.prompt, envelope_path=self.root / "envelope.json",
            route="omp", model="openai-codex/gpt-5.6-sol", effort="high",
            profile="openai-performance", session_dir=self.root / "session",
            context_window_tokens=400_000,
            environment=worktree_module.launch_env(self.scratch))

    def build(self, **kwargs):
        harness = lch.HerdrLauncher(
            herdr_path=self.root / "herdr", omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"), admitted_routes=self.admitted)
        # These cases are about placement, not about the busy window; zero so
        # a refusal here would surface at once rather than after ten seconds.
        harness.agent_start_busy_window_s = 0.0
        fake = _PlacementHerdr(worktree=self.worktree,
                               transcript=self.root / "session.jsonl", **kwargs)
        harness._herdr = fake
        return harness, fake

    # ── one workspace per run ───────────────────────────────────────────────

    def test_two_concurrent_launches_share_one_parent_and_get_distinct_panes(self):
        """The 2026-08-18 race, driven with both lanes on the same instant.

        The parent is resolved once and cached, so both launches name it —
        that is the fix, not a violation of it. What must never collide is the
        *child*: two launches selecting one pane is how a lane's agent landed
        in the pane its sibling had opened 30ms earlier.
        """
        harness, fake = self.build()
        handles = {}
        errors = []
        # Both lanes enter `launch` on the same instant. The selector read is
        # serialised inside the launcher, which is the fix; the barrier makes
        # sure the test is exercising that serialisation rather than an
        # interleaving it happened to get.
        gate = threading.Barrier(2, timeout=10.0)

        def launch(token):
            try:
                gate.wait()
                handles[token] = harness.launch(self.spec(token))
            except BaseException as exc:  # noqa: BLE001 — reported below
                errors.append(exc)

        threads = [threading.Thread(target=launch, args=(token,))
                   for token in ("run1-node_a-1", "run1-node_b-1")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(handles), 2)
        # Resolved once, under contention: one `pane current` call for the run.
        self.assertEqual(len(fake.argv_for(("pane", "current"))), 1)
        self.assertEqual(set(fake.parents()), {"w0:p0"})
        self.assertNotIn("--current", fake.parents())
        # Distinct children, which is the collision that actually matters.
        pane_ids = {handle.pane_id for handle in handles.values()}
        self.assertEqual(len(pane_ids), 2)

    def test_every_pane_of_a_run_reports_one_workspace(self):
        harness, fake = self.build()
        panes = [harness.launch(self.spec("run1-node_{0}-1".format(name))).pane_id
                 for name in ("a", "b", "c", "d")]

        self.assertEqual(len(set(panes)), 4)
        self.assertEqual({lch.workspace_of(pane) for pane in panes}, {"w0"})
        self.assertEqual(lch.workspace_of(fake.parents()[0]), "w0")

    def test_a_split_that_escaped_the_workspace_is_reaped_and_refused(self):
        """The measurement behind the guarantee.

        A child landing outside the run's workspace is the scatter symptom
        itself, so it is refused rather than adopted — and reaped, so the
        refusal can honestly report that nothing was left behind.
        """
        harness, fake = self.build()
        harness.launch(self.spec("run1-node_a-1"))
        fake.split_pane_id = "w13F:p1"

        def escape(*args, **kwargs):
            if tuple(args[:2]) == ("pane", "split"):
                fake.calls.append(list(args))
                return {"result": {"pane": {"pane_id": "w13F:p1",
                                            "cwd": str(self.worktree)}}}
            return original(*args, **kwargs)

        original = fake.__call__
        harness._herdr = escape
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec("run1-node_b-1"))

        self.assertIs(caught.exception.refusal, lch.LaunchRefusal.WORKSPACE_DRIFT)
        self.assertIn("w13F", str(caught.exception))
        self.assertIn("w13F:p1", fake.closed)
        self.assertFalse(caught.exception.pane_created)
        # Retryable: another split of the fixed parent may land correctly.
        self.assertFalse(caught.exception.deterministic)

    def test_the_workspace_helper_reads_a_field_not_a_message(self):
        self.assertEqual(lch.workspace_of("w13A:p29"), "w13A")
        self.assertEqual(lch.workspace_of("p29"), "")

    # ── the layout stays readable as the count grows ────────────────────────

    def test_the_split_direction_alternates_in_pairs(self):
        """`right, right, down, down, …` — two side by side, then two stacked.

        A constant direction halves the same fixed parent's width on every
        launch; alternating in pairs degrades both dimensions together instead
        of one of them without limit.
        """
        harness, fake = self.build()
        for index in range(8):
            harness.launch(self.spec("run1-node_{0}-1".format(index)))

        self.assertEqual(
            fake.directions(),
            ["right", "right", "down", "down",
             "right", "right", "down", "down"])

    def test_the_direction_for_launch_k_is_a_pure_function_of_k(self):
        """Derived from a counter the launcher owns, never from focus.

        Two launchers taking the same number take the same direction, and a
        concurrent pair takes two distinct numbers because the counter is read
        and incremented under one lock.
        """
        first, _ = self.build()
        second, _ = self.build()
        self.assertEqual([first._split_direction() for _ in range(8)],
                         [second._split_direction() for _ in range(8)])

        harness, _fake = self.build()
        taken = []
        lock = threading.Lock()

        def take():
            direction = harness._split_direction()
            with lock:
                taken.append(direction)

        threads = [threading.Thread(target=take) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        # Four concurrent takes consume 0..3 exactly once, so the multiset is
        # fixed even though the order is not.
        self.assertEqual(sorted(taken), ["down", "down", "right", "right"])
        self.assertEqual(harness._splits_taken, 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
