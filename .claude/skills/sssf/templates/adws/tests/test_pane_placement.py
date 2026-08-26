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

Neither fix made the result *legible*. Panes still landed in whichever
workspace held focus when the run started, every node's agents shared one flat
tab, and a pane carried its coder's own session title rather than the node it
was running -- so the only way to tell two panes apart was to read their
working directories, and twice in one session a pane was lost track of
entirely. The layout is now a property of the run:

* **One workspace per run**, created by the launcher and named for the plan.
  Nothing reads focus, so an unrelated workspace cannot receive a run's pane.
* **One tab per lane**, labelled with the authored build-lane name. The lane's
  tester, builder, and reviewer are panes in that tab, so collapsing the
  workspace still shows which lanes exist and opening one lane shows its
  lifecycle actors together.
* **A balanced grid inside the tab**, at most 3x3. Two panes are two columns
  side by side, never a stack.
* **A durable role/generation pane label**, so `tester-a41`, `builder-a3`, and
  `reviewer-a1` remain distinguishable without repeating the tab label.

The grid and incremental dispatch are reconciled by making geometry a pure
function of a pane's ordinal and its tab's column count (`split_plan`), so
pane *k* is placed correctly without knowing whether pane *k+1* will exist.
Only the column count needs deciding up front, and the caller declares it as
`LaunchSpec.pane_group_size`.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch  # noqa: E402
from adw_modules import worktree as worktree_module  # noqa: E402
from adw_modules.route_receipts import (
    load_admitted_routes,  # noqa: E402
    load_public_key,
)
import maestro  # noqa: E402

from test_launch_refusal_cleanup import FakeHerdr  # noqa: E402


class _PlacementHerdr(FakeHerdr):
    """A herdr stand-in that hands out a distinct child pane per split.

    The base fake returns one fixed child id, which cannot show whether two
    concurrent launches collided. This one allocates `w0:p1`, `w0:p2`, … under
    its own lock — the child lands in its parent's workspace, as a real split
    does — so two launches that selected the same pane would be visible as one
    id handed out twice.
    """

    def __init__(
        self, *, worktree: Path, transcript: Path, workspace: str = "w0"
    ) -> None:
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
                self.split_pane_id = "{0}:p{1}".format(self._workspace, self._next_pane)
        return super().__call__(*args, env=env, timeout=timeout)

    def directions(self):
        return [
            call[call.index("--direction") + 1]
            for call in self.argv_for(("pane", "split"))
        ]

    def parents(self):
        return [call[2] for call in self.argv_for(("pane", "split"))]


class _WorkspaceHerdr(_PlacementHerdr):
    """A herdr stand-in that creates workspaces and tabs, as the real one does.

    `focus_workspace` is deliberately *not* the workspace it creates: the
    property under test is that a run's panes land in the run's own workspace
    even while focus sits somewhere else entirely, which is the failure the
    layout work exists to remove. Asking this fake for `pane current` at all
    is therefore a test failure, not a fallback.
    """

    def __init__(
        self, *, worktree: Path, transcript: Path, focus_workspace: str = "w99"
    ) -> None:
        super().__init__(
            worktree=worktree, transcript=transcript, workspace=focus_workspace
        )
        self._next_workspace = 0
        self._next_tab = 0
        self.renames = {}
        self.tab_labels = {}
        self.closed_tabs = []
        self._pane_tabs = {}
        self._live_tabs = set()

    def __call__(self, *args, env=None, timeout=30.0):
        head = tuple(args[:2])
        if head == ("workspace", "create"):
            self.calls.append(list(args))
            self._next_workspace += 1
            self._workspace = "wRUN{0}".format(self._next_workspace)
            self._next_tab += 1
            self._next_pane += 1
            seed_tab_id = "{0}:t{1}".format(self._workspace, self._next_tab)
            root_pane_id = "{0}:p{1}".format(self._workspace, self._next_pane)
            self._live_tabs.add(seed_tab_id)
            self._pane_tabs[root_pane_id] = seed_tab_id
            return {
                "result": {
                    "workspace": {
                        "workspace_id": self._workspace,
                        "label": args[args.index("--label") + 1],
                    },
                    "tab": {"tab_id": seed_tab_id},
                    "root_pane": {"pane_id": root_pane_id},
                }
            }
        if head == ("tab", "create"):
            self.calls.append(list(args))
            self._next_tab += 1
            self._next_pane += 1
            tab_id = "{0}:t{1}".format(self._workspace, self._next_tab)
            pane_id = "{0}:p{1}".format(self._workspace, self._next_pane)
            self.tab_labels[tab_id] = args[args.index("--label") + 1]
            # A fresh tab's root pane is what `pane get` must report next.
            self.split_pane_id = pane_id
            self._live_tabs.add(tab_id)
            self._pane_tabs[pane_id] = tab_id
            return {
                "result": {
                    "tab": {"tab_id": tab_id, "label": self.tab_labels[tab_id]},
                    "root_pane": {"pane_id": pane_id, "cwd": str(self.worktree)},
                }
            }
        if head == ("tab", "close"):
            self.calls.append(list(args))
            self.closed_tabs.append(args[2])
            self._live_tabs.discard(args[2])
            return {"result": {"type": "ok"}}
        if head == ("tab", "list"):
            self.calls.append(list(args))
            return {
                "result": {
                    "tabs": [
                        {"tab_id": tab_id, "label": self.tab_labels.get(tab_id, "")}
                        for tab_id in sorted(self._live_tabs)
                    ]
                }
            }
        if head == ("pane", "get"):
            self.calls.append(list(args))
            pane_id = args[2]
            index = (
                min(
                    len([call for call in self.calls if call[:2] == ["pane", "get"]]),
                    len(self.get_cwds),
                )
                - 1
            )
            return {
                "result": {
                    "pane": {
                        "pane_id": pane_id,
                        "cwd": self.get_cwds[index],
                        "workspace_id": lch.workspace_of(pane_id),
                        "tab_id": self._pane_tabs.get(pane_id, ""),
                        "revision": self.revision,
                    }
                }
            }
        if head == ("pane", "split"):
            response = super().__call__(*args, env=env, timeout=timeout)
            pane = response["result"]["pane"]
            pane_id = pane.get("pane_id")
            if pane_id:
                self._pane_tabs[pane_id] = self._pane_tabs.get(args[2], "")
            return response
        if head == ("pane", "rename"):
            self.calls.append(list(args))
            self.renames[args[2]] = args[3]
            return {"result": {"type": "ok"}}
        return super().__call__(*args, env=env, timeout=timeout)

    def register_existing_tab(self, tab_id: str, *pane_ids: str) -> None:
        self._live_tabs.add(tab_id)
        self._pane_tabs.update({pane_id: tab_id for pane_id in pane_ids})

    def hide_tab(self, tab_id: str) -> None:
        self._live_tabs.discard(tab_id)

    def tab_create_labels(self):
        return [
            call[call.index("--label") + 1] for call in self.argv_for(("tab", "create"))
        ]


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
            verify_keys=(key,),
        )

    def spec(
        self,
        token: str = "run1-node_a-1",
        *,
        group: str = "",
        role: str = "",
        size: int = 0,
        attempt: int | None = None,
        workspace_label: str = "",
    ) -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token=token,
            worktree=self.worktree,
            prompt_path=self.prompt,
            envelope_path=self.root / "envelope.json",
            route="omp",
            model="openai-codex/gpt-5.6-sol",
            effort="high",
            profile="openai-performance",
            session_dir=self.root / "session",
            context_window_tokens=400_000,
            workspace_label=workspace_label,
            lane_key=group,
            lane_label=group,
            pane_role=role,
            attempt_no=attempt,
            pane_group_size=size,
            environment=worktree_module.launch_env(self.scratch),
        )

    def build(
        self, *, workspace_label: str = "", factory=None, factory_kwargs=None, **kwargs
    ):
        harness = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label=workspace_label,
        )
        # These cases are about placement, not about the busy window; zero so
        # a refusal here would surface at once rather than after ten seconds.
        harness.agent_start_busy_window_s = 0.0
        if factory is None:
            # `factory_kwargs` selects the vanishing-workspace fake without
            # every existing caller having to know it exists.
            factory = (
                _VanishingWorkspaceHerdr
                if factory_kwargs
                else (_WorkspaceHerdr if workspace_label else _PlacementHerdr)
            )
        fake = factory(
            worktree=self.worktree,
            transcript=self.root / "session.jsonl",
            **(factory_kwargs or {}),
            **kwargs,
        )
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

        threads = [
            threading.Thread(target=launch, args=(token,))
            for token in ("run1-node_a-1", "run1-node_b-1")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(handles), 2)
        # Resolved once, under contention: one `pane current` call for the run.
        self.assertEqual(len(fake.argv_for(("pane", "current"))), 1)
        # Both parents are pane ids this launcher already knew — the resolved
        # seed and the pane the first launch created — never the `--current`
        # selector. Splitting a sibling's pane is not the 2026-08-18 defect:
        # that was two launches selecting one pane for their *agent*, and the
        # children below are still distinct.
        self.assertEqual(set(fake.parents()), {"w0:p0", "w0:p1"})
        self.assertNotIn("--current", fake.parents())
        # Distinct children, which is the collision that actually matters.
        pane_ids = {handle.pane_id for handle in handles.values()}
        self.assertEqual(len(pane_ids), 2)

    def test_every_pane_of_a_run_reports_one_workspace(self):
        harness, fake = self.build()
        panes = [
            harness.launch(self.spec("run1-node_{0}-1".format(name))).pane_id
            for name in ("a", "b", "c", "d")
        ]

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
                return {
                    "result": {
                        "pane": {"pane_id": "w13F:p1", "cwd": str(self.worktree)}
                    }
                }
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

    # ── the grid ────────────────────────────────────────────────────────────

    def test_the_declared_grid_table(self):
        """The geometry every count lays out in, capped at 3x3.

        Rows are taken first and columns derived from them, so the sequence
        widens before it stacks: a second pane goes beside the first rather
        than under it, which is the whole of "side by side, never a stack".
        """
        self.assertEqual(
            [lch.grid_for(n) for n in range(1, 10)],
            [(1, 1), (1, 2), (1, 3), (2, 2), (2, 3), (2, 3), (3, 3), (3, 3), (3, 3)],
        )

    def test_the_grid_does_not_grow_past_three_by_three(self):
        """Ten panes stay in nine cells' worth of columns.

        A fourth column of a 170-column terminal is unreadable, so the tenth
        pane makes a column taller instead. The ceiling is a ceiling.
        """
        self.assertEqual(lch.grid_for(10), (3, 3))
        self.assertEqual(lch.grid_for(64), (3, 3))
        # Column 0 takes the overflow: slot 9 splits slot 6, downward.
        self.assertEqual(lch.split_plan(9, 3), (6, "down"))

    def test_two_panes_are_two_columns(self):
        """`--direction right` is the vertical divider, verified live.

        Against the real binary, six panes built by this plan reported six
        rects of 57x25 in a 171x49 area — three x-offsets, two y-offsets —
        which is a 3-column, 2-row grid. So `right` is the direction that puts
        two panes left and right of each other, and slot 1 of a two-pane tab
        takes it.
        """
        self.assertEqual(lch.split_plan(1, 2), (0, "right"))

    def test_each_declared_size_builds_its_grid(self):
        """The plan for a declared size occupies exactly that grid.

        Read the plan as a placement: slot 0 is the tab's root pane, a `right`
        slot sits beside its parent in the same row, and a `down` slot sits
        under the slot one row above. Reconstructing the (row, column) of every
        slot from the plan alone must reproduce `grid_for`'s table with no cell
        used twice — which is what makes an incrementally split tab a grid.
        """
        for size in (2, 3, 4, 6, 9):
            with self.subTest(size=size):
                rows, cols = lch.grid_for(size)
                cells = {0: (0, 0)}
                for index in range(1, size):
                    parent, direction = lch.split_plan(index, cols)
                    row, col = cells[parent]
                    cells[index] = (
                        (row, col + 1) if direction == "right" else (row + 1, col)
                    )
                placed = set(cells.values())
                self.assertEqual(len(placed), size, "a cell was used twice")
                self.assertEqual(max(r for r, _ in placed) + 1, rows)
                self.assertEqual(max(c for _, c in placed) + 1, cols)

    def test_an_undeclared_group_still_widens_before_it_stacks(self):
        """No declared size: the first three panes are three columns.

        A launcher that learned the count only as panes arrived used to freeze
        at one column and stack, which is the shape S5 forbids. The column
        count grows with the observed count instead, and never shrinks.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        for index in range(3):
            harness.launch(
                self.spec("run1-node_{0}-1".format(index), group="lane-p2-s3-inventory")
            )

        # Three panes, two splits: the tab's own root pane is the first agent's.
        self.assertEqual(fake.directions(), ["right", "right"])

    def test_the_grid_of_a_declared_group_of_six(self):
        """Three columns then three rows below them, from real split calls."""
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        for index in range(6):
            harness.launch(
                self.spec(
                    "run1-node_{0}-1".format(index),
                    group="lane-p2-s3-inventory",
                    size=6,
                )
            )

        self.assertEqual(fake.directions(), ["right", "right", "down", "down", "down"])

    # ── one workspace per run, one tab per lane ─────────────────────────────

    def test_a_runs_panes_land_in_the_runs_own_workspace(self):
        """Focus is somewhere else entirely and no pane follows it.

        The fake's focused pane sits in `w99`. A launcher that read it — the
        `--current` shape in any of its forms — would put this run's agents
        there. Creating the workspace instead makes placement a property of
        the run, so `pane current` is never asked at all.
        """
        harness, fake = self.build(
            workspace_label="cmo-consolidation-l-r4", focus_workspace="w99"
        )
        panes = [
            harness.launch(
                self.spec(
                    "run1-node_a-{0}".format(index),
                    group="lane-p2-s3-inventory",
                    size=2,
                )
            ).pane_id
            for index in range(2)
        ]

        self.assertEqual(fake.argv_for(("pane", "current")), [])
        created = fake.argv_for(("workspace", "create"))
        self.assertEqual(len(created), 1)
        self.assertIn("cmo-consolidation-l-r4", created[0])
        self.assertEqual({lch.workspace_of(pane) for pane in panes}, {"wRUN1"})
        self.assertNotIn("w99", {lch.workspace_of(pane) for pane in panes})

    def test_the_workspace_is_created_once_for_the_whole_run(self):
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        for node in ("lane-p2-s3-inventory", "lane-p3-dedup-decisions"):
            harness.launch(self.spec("run1-" + node + "-1", group=node, size=2))

        self.assertEqual(len(fake.argv_for(("workspace", "create"))), 1)

    def test_one_tab_per_lane_shared_by_that_lanes_agents(self):
        """Builder and reviewer of one lane share a tab; two lanes do not."""
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        harness.launch(
            self.spec("run1-a-1", group="lane-p2-s3-inventory", role="builder", size=2)
        )
        harness.launch(
            self.spec("review-a", group="lane-p2-s3-inventory", role="reviewer", size=2)
        )
        harness.launch(
            self.spec(
                "run1-b-1", group="lane-p3-dedup-decisions", role="builder", size=2
            )
        )

        self.assertEqual(
            fake.tab_create_labels(),
            ["lane-p2-s3-inventory", "lane-p3-dedup-decisions"],
        )
        # The reviewer split its own lane's tab, not the other lane's.
        self.assertEqual(len(fake.argv_for(("pane", "split"))), 1)

    def test_the_seed_tab_is_closed_once_a_real_tab_exists(self):
        """`workspace create` opens a shell tab nobody asked for.

        It is closed after the first real tab exists, never before: herdr
        refuses to close a workspace's last tab, and that refusal is correct.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        harness.launch(self.spec("run1-a-1", group="lane-p2-s3-inventory", size=2))

        self.assertEqual(fake.closed_tabs, ["wRUN1:t1"])
        self.assertEqual(len(fake.argv_for(("tab", "create"))), 1)

    def test_a_pane_is_named_for_its_lane_role_and_attempt(self):
        """Visible identity is authored placement, never runtime metadata."""
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        builder = harness.launch(
            self.spec(
                "run1-a-19",
                group="lane-p2-s3-inventory",
                role="builder",
                attempt=19,
                size=2,
            )
        )
        reviewer = harness.launch(
            self.spec(
                "review-digest",
                group="lane-p2-s3-inventory",
                role="reviewer",
                attempt=2,
                size=2,
            )
        )

        self.assertEqual(fake.renames[builder.pane_id], "builder-a19")
        self.assertEqual(fake.renames[reviewer.pane_id], "reviewer-a2")

    def test_role_only_pane_has_no_attempt_suffix(self):
        harness, fake = self.build(factory=_WorkspaceHerdr)
        author = harness.launch(self.spec("deliver-author", role="author"))
        self.assertEqual(fake.renames[author.pane_id], "author")

    def test_the_first_agent_of_a_tab_takes_the_tab_root_pane(self):
        """A tab is created with the launch's cwd and redirection.

        Its root pane is therefore a usable shell, and splitting it to make a
        second pane for the same agent would waste a grid cell and leave an
        idle shell in every node's rectangle.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        handle = harness.launch(
            self.spec("run1-a-1", group="lane-p2-s3-inventory", size=2)
        )

        self.assertEqual(fake.argv_for(("pane", "split")), [])
        created = fake.argv_for(("tab", "create"))[0]
        self.assertIn("--cwd", created)
        self.assertEqual(
            created[created.index("--cwd") + 1], str(self.worktree.resolve())
        )
        self.assertEqual(lch.workspace_of(handle.pane_id), "wRUN1")

    def test_a_refused_workspace_is_typed_and_not_cached(self):
        """The same shape `SPLIT_PARENT_UNRESOLVED` has, for the same reason.

        A workspace herdr would not create is herdr's answer at one instant,
        not a property of the run, so the refusal is non-deterministic and the
        next launch genuinely re-asks rather than replaying a cached failure.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")

        def refuse(*args, **kwargs):
            if tuple(args[:2]) == ("workspace", "create"):
                fake.calls.append(list(args))
                raise lch.HerdrCallError("LAUNCH_REFUSED:nope", "no_workspace")
            return original(*args, **kwargs)

        original = fake.__call__
        harness._herdr = refuse
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec("run1-a-1", group="lane-p2-s3-inventory"))

        self.assertIs(caught.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
        self.assertFalse(caught.exception.pane_created)
        self.assertFalse(caught.exception.deterministic)
        # Not cached: the retry re-asks herdr rather than replaying the answer.
        harness._herdr = fake
        handle = harness.launch(self.spec("run1-a-1", group="lane-p2-s3-inventory"))
        self.assertEqual(lch.workspace_of(handle.pane_id), "wRUN1")
        self.assertEqual(len(fake.argv_for(("workspace", "create"))), 2)

    def test_a_refused_tab_is_typed_and_not_cached(self):
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")

        def refuse(*args, **kwargs):
            if tuple(args[:2]) == ("tab", "create"):
                fake.calls.append(list(args))
                raise lch.HerdrCallError("LAUNCH_REFUSED:nope", "no_tab")
            return original(*args, **kwargs)

        original = fake.__call__
        harness._herdr = refuse
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec("run1-a-1", group="lane-p2-s3-inventory"))

        self.assertIs(caught.exception.refusal, lch.LaunchRefusal.TAB_UNRESOLVED)
        self.assertFalse(caught.exception.pane_created)
        harness._herdr = fake
        harness.launch(self.spec("run1-a-1", group="lane-p2-s3-inventory"))
        self.assertEqual(len(fake.argv_for(("tab", "create"))), 2)

    def test_a_reviewer_still_gets_a_tab_after_its_builders_pane_closed(self):
        """The production sequence, which reaps between the two agents.

        `cancel` closes a builder's pane in a `finally` after every attempt,
        and the lane's reviewer launches afterwards — so by the time the
        reviewer arrives its lane tab may hold no live pane at all. A tab with
        nothing left in it is not a parent, and herdr has already closed it,
        so the group opens a fresh tab under the same name rather than
        refusing or splitting a dead pane.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        builder = harness.launch(
            self.spec("run1-a-1", group="lane-p2-s3-inventory", role="builder", size=2)
        )
        harness._reap_pane(builder.pane_id, {})

        reviewer = harness.launch(
            self.spec("review-a", group="lane-p2-s3-inventory", role="reviewer", size=2)
        )

        self.assertEqual(
            fake.tab_create_labels(), ["lane-p2-s3-inventory", "lane-p2-s3-inventory"]
        )
        self.assertEqual(fake.argv_for(("pane", "split")), [])
        self.assertEqual(fake.renames[reviewer.pane_id], "reviewer")
        self.assertEqual(lch.workspace_of(reviewer.pane_id), "wRUN1")

    def test_a_reaped_pane_leaves_the_grid(self):
        """A closed pane cannot be split, so it must not stay a parent.

        Its slot is emptied rather than removed: the panes after it are still
        where they were, and shifting them would move a grid that did not
        change.
        """
        harness, fake = self.build(workspace_label="cmo-consolidation-l-r4")
        first = harness.launch(
            self.spec("run1-a-1", group="lane-p2-s3-inventory", size=3)
        )
        second = harness.launch(
            self.spec("run1-a-2", group="lane-p2-s3-inventory", size=3)
        )
        harness._reap_pane(second.pane_id, dict(fake_env()))

        third = harness.launch(
            self.spec("run1-a-3", group="lane-p2-s3-inventory", size=3)
        )
        parents = fake.parents()
        # Slot 2 would have split the reaped slot 1; it falls back to slot 0,
        # which is still alive, and stays inside this lane's tab either way.
        self.assertEqual(parents[-1], first.pane_id)
        self.assertEqual(lch.workspace_of(third.pane_id), "wRUN1")

    def test_adoption_reclaims_only_the_persisted_pane_and_agent_ids(self):
        runtime, fake = self.build(workspace_label="approved-plan")
        original = runtime.launch(
            self.spec(
                "run1-build-a1",
                group="build",
                role="builder",
                attempt=1,
                workspace_label="approved-plan",
            )
        )
        resumed = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label="approved-plan",
        )
        resumed._herdr = fake
        adopted = resumed.adopt(
            lch.PersistedActorHandle(
                correlation_token=original.correlation_token,
                pane_id=original.pane_id,
                agent_name=original.agent_name,
                launched_cwd=original.launched_cwd,
                transcript_path=original.transcript_path,
                environment=original.environment,
                workspace_id=original.workspace_id,
                tab_id=original.tab_id,
                lane_key=original.lane_key,
            )
        )
        self.assertEqual(adopted.pane_id, original.pane_id)
        self.assertEqual(adopted.tab_id, original.tab_id)
        self.assertEqual(adopted.workspace_id, original.workspace_id)
        self.assertEqual(resumed.reclaim(original.correlation_token), (adopted,))

    def test_resume_restores_lane_placement_without_adopting_actor_token(self):
        runtime, fake = self.build(workspace_label="approved-plan")
        builder = runtime.launch(
            self.spec(
                "run1-build-a3",
                group="build",
                role="builder",
                attempt=3,
                workspace_label="approved-plan",
            )
        )
        resumed = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label="approved-plan",
        )
        resumed._herdr = fake

        resumed.restore_placement(
            workspace_id=builder.workspace_id,
            lane_key=builder.lane_key,
            tab_id=builder.tab_id,
            pane_id=builder.pane_id,
            environment=builder.environment,
        )
        reviewer = resumed.launch(
            self.spec(
                "review-run1-build-a5",
                group="build",
                role="reviewer",
                attempt=5,
                size=3,
                workspace_label="approved-plan",
            )
        )

        self.assertEqual(reviewer.workspace_id, builder.workspace_id)
        self.assertEqual(reviewer.tab_id, builder.tab_id)
        self.assertEqual(len(fake.argv_for(("workspace", "create"))), 1)
        self.assertEqual(len(fake.argv_for(("tab", "create"))), 1)

    def test_actorless_adopted_pane_is_closed_before_replacement(self):
        class ActorlessHerdr(_WorkspaceHerdr):
            agent_missing = False

            def __call__(self, *args, env=None, timeout=30.0):
                if self.agent_missing and tuple(args[:2]) == ("agent", "get"):
                    self.calls.append(list(args))
                    return {"result": {}}
                return super().__call__(*args, env=env, timeout=timeout)

        runtime, fake = self.build(
            workspace_label="approved-plan", factory=ActorlessHerdr
        )
        original = runtime.launch(
            self.spec(
                "run1-review-a1",
                group="build",
                role="reviewer",
                attempt=1,
                workspace_label="approved-plan",
            )
        )
        persisted = lch.PersistedActorHandle(
            correlation_token=original.correlation_token,
            pane_id=original.pane_id,
            agent_name=original.agent_name,
            launched_cwd=original.launched_cwd,
            transcript_path=original.transcript_path,
            environment=original.environment,
            workspace_id=original.workspace_id,
            tab_id=original.tab_id,
            lane_key=original.lane_key,
        )
        resumed = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label="approved-plan",
        )
        resumed._herdr = fake
        fake.agent_missing = True

        with self.assertRaises(lch.HandleAbsent):
            resumed.adopt(persisted)
        resumed.close_actorless_pane(persisted)

        self.assertEqual(fake.closed, [original.pane_id])

    def test_proven_tab_survives_agent_absence_and_replacement_reuses_it(self):
        runtime, fake = self.build(workspace_label="approved-plan")
        original = runtime.launch(
            self.spec(
                "run1-build-a1",
                group="build",
                role="builder",
                attempt=1,
                workspace_label="approved-plan",
            )
        )
        fake.closed.append(original.pane_id)
        resumed = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label="approved-plan",
        )
        resumed._herdr = fake
        with self.assertRaises(lch.HandleAbsent):
            resumed.adopt(
                lch.PersistedActorHandle(
                    correlation_token=original.correlation_token,
                    pane_id=original.pane_id,
                    agent_name=original.agent_name,
                    launched_cwd=original.launched_cwd,
                    environment=original.environment,
                    workspace_id=original.workspace_id,
                    tab_id=original.tab_id,
                    lane_key=original.lane_key,
                )
            )
        layout = resumed._tabs[original.lane_key]
        self.assertEqual(layout.tab_id, original.tab_id)
        self.assertIn(original.pane_id, layout.panes)

        replacement = resumed.launch(
            self.spec(
                "run1-build-a2",
                group="build",
                role="builder",
                attempt=2,
                workspace_label="approved-plan",
            )
        )

        self.assertEqual(replacement.tab_id, original.tab_id)
        self.assertEqual(replacement.workspace_id, original.workspace_id)
        self.assertEqual(replacement.lane_key, original.lane_key)
        self.assertEqual(len(fake.argv_for(("tab", "create"))), 1)

    def test_unproven_or_vanished_tab_cannot_restore_replacement_placement(self):
        runtime, fake = self.build(workspace_label="approved-plan")
        original = runtime.launch(
            self.spec(
                "run1-build-a1",
                group="build",
                role="builder",
                attempt=1,
                workspace_label="approved-plan",
            )
        )
        before_tab_creates = len(fake.argv_for(("tab", "create")))
        cases = (
            ("vanished", original.tab_id, original.tab_id, True),
            ("unknown", "wRUN1:t999", "wRUN1:t999", False),
        )
        for label, persisted_tab_id, pane_tab_id, hide in cases:
            with self.subTest(label=label):
                fake._pane_tabs[original.pane_id] = pane_tab_id
                if hide:
                    fake.hide_tab(persisted_tab_id)
                resumed = lch.HerdrLauncher(
                    herdr_path=self.root / "herdr",
                    omp_path=Path("/opt/omp"),
                    claude_path=Path("/opt/claude"),
                    admitted_routes=self.admitted,
                    workspace_label="approved-plan",
                )
                resumed._herdr = fake
                with self.assertRaisesRegex(lch.HandleAdoptionRefused, "TAB_ABSENT"):
                    resumed.adopt(
                        lch.PersistedActorHandle(
                            correlation_token=original.correlation_token,
                            pane_id=original.pane_id,
                            agent_name=original.agent_name,
                            launched_cwd=original.launched_cwd,
                            environment=original.environment,
                            workspace_id=original.workspace_id,
                            tab_id=persisted_tab_id,
                            lane_key=original.lane_key,
                        )
                    )
                self.assertEqual(resumed._tabs, {})
                self.assertEqual(
                    len(fake.argv_for(("tab", "create"))), before_tab_creates
                )

    def test_adoption_keeps_all_live_actors_in_the_lane_layout(self):
        resumed, fake = self.build(workspace_label="approved-plan")
        actors = (
            ("run1-build-a1", "wRUN1:p1"),
            ("run1-review-a1", "wRUN1:p2"),
        )
        fake.register_existing_tab("wRUN1:t2", *(pane_id for _, pane_id in actors))
        for token, pane_id in actors:
            fake.split_pane_id = pane_id
            resumed.adopt(
                lch.PersistedActorHandle(
                    correlation_token=token,
                    pane_id=pane_id,
                    agent_name=lch.agent_name_for(token),
                    launched_cwd=self.worktree,
                    environment=fake_env(),
                    workspace_id="wRUN1",
                    tab_id="wRUN1:t2",
                    lane_key="build",
                )
            )

        layout = resumed._tabs["build"]
        self.assertEqual(layout.panes, ["wRUN1:p1", "wRUN1:p2"])
        self.assertEqual(layout.claimed, 2)
        self.assertEqual(resumed.workspace_id, "wRUN1")

    def test_adoption_refuses_to_rebind_the_run_workspace(self):
        resumed, fake = self.build(workspace_label="approved-plan")
        fake.register_existing_tab("wRUN1:t2", "wRUN1:p1")
        fake.register_existing_tab("wRUN2:t2", "wRUN2:p1")
        for token, pane_id, workspace in (
            ("run1-build-a1", "wRUN1:p1", "wRUN1"),
            ("run2-build-a1", "wRUN2:p1", "wRUN2"),
        ):
            fake.split_pane_id = pane_id
            persisted = lch.PersistedActorHandle(
                correlation_token=token,
                pane_id=pane_id,
                agent_name=lch.agent_name_for(token),
                launched_cwd=self.worktree,
                environment=fake_env(),
                workspace_id=workspace,
                tab_id=workspace + ":t2",
                lane_key="build",
            )
            if workspace == "wRUN1":
                resumed.adopt(persisted)
            else:
                with self.assertRaisesRegex(
                    lch.HandleAdoptionRefused, "WORKSPACE_ID_MISMATCH"
                ):
                    resumed.adopt(persisted)

        self.assertEqual(resumed.workspace_id, "wRUN1")

    def test_stale_workspace_actor_is_retired_only_after_identity_proof(self):
        resumed, fake = self.build(workspace_label="approved-plan")
        fake.register_existing_tab("wRUN1:t2", "wRUN1:p1")
        fake.register_existing_tab("wOLD:t7", "wOLD:p9")
        current = lch.PersistedActorHandle(
            correlation_token="run1-build-a1",
            pane_id="wRUN1:p1",
            agent_name=lch.agent_name_for("run1-build-a1"),
            launched_cwd=self.worktree,
            environment=fake_env(),
            workspace_id="wRUN1",
            tab_id="wRUN1:t2",
            lane_key="build",
        )
        stale = lch.PersistedActorHandle(
            correlation_token="run1-review-a1",
            pane_id="wOLD:p9",
            agent_name=lch.agent_name_for("run1-review-a1"),
            launched_cwd=self.worktree,
            environment=fake_env(),
            workspace_id="wOLD",
            tab_id="wOLD:t7",
            lane_key="build",
        )
        fake.split_pane_id = current.pane_id
        resumed.adopt(current)
        fake.split_pane_id = stale.pane_id

        with self.assertRaisesRegex(lch.HandleAdoptionRefused, "WORKSPACE_ID_MISMATCH"):
            resumed.adopt(stale)
        resumed.retire_for_replacement(stale, 0.0)

        self.assertEqual(fake.closed, [stale.pane_id])
        self.assertEqual(resumed.workspace_id, "wRUN1")
        self.assertEqual(resumed._tabs["build"].tab_id, "wRUN1:t2")


class LanePlacementMappingTest(unittest.TestCase):
    """Authored test nodes render inside the build lane that consumes them."""

    @staticmethod
    def _node(node_id, needs=()):
        return SimpleNamespace(node_id=node_id, needs=tuple(needs))

    def test_unique_test_owner_shares_the_build_lane(self):
        test = self._node("lane-direct-port-tests")
        build = self._node("lane-direct-port", (test.node_id,))
        plan = SimpleNamespace(agent_nodes=(build,), tests_nodes=(test,))

        self.assertEqual(
            maestro._lane_placement_by_node(plan),
            {
                "lane-direct-port": "lane-direct-port",
                "lane-direct-port-tests": "lane-direct-port",
            },
        )

    def test_shared_and_unconsumed_tests_keep_their_own_tabs(self):
        shared = self._node("shared-tests")
        orphan = self._node("orphan-tests")
        first = self._node("lane-first", (shared.node_id,))
        second = self._node("lane-second", (shared.node_id,))
        plan = SimpleNamespace(
            agent_nodes=(first, second), tests_nodes=(shared, orphan)
        )

        placement = maestro._lane_placement_by_node(plan)
        self.assertEqual(placement[shared.node_id], shared.node_id)
        self.assertEqual(placement[orphan.node_id], orphan.node_id)


class LayoutWiringTest(unittest.TestCase):
    """The CLI half: a typed field nothing sets is a field that does nothing.

    The launcher can only put a pane in the right tab if a dispatch site names
    the node, and it can only create the run's workspace if the run's launcher
    is given a label. Both are one keyword argument at one call site, which is
    exactly the shape that gets wired in a design and left out of the code —
    the tool-in-the-list-but-never-called failure. Read structurally rather
    than by running a real dispatch, because there is no offline run.
    """

    def setUp(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "maestro.py").read_text()
        self.tree = ast.parse(source)

    def _launch_specs(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", "")
            )
            if name == "LaunchSpec":
                yield {kw.arg: kw.value for kw in node.keywords}

    def test_the_node_dispatch_sites_name_lane_role_and_attempt(self):
        roles = set()

        def collect(node, into):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                into.add(node.value)
            elif isinstance(node, ast.IfExp):
                collect(node.body, into)
                collect(node.orelse, into)

        actor_roles = {"builder", "reviewer", "tester"}
        for keywords in self._launch_specs():
            role = keywords.get("pane_role")
            if role is None:
                continue
            site_roles = set()
            collect(role, site_roles)
            roles.update(site_roles)
            if site_roles & actor_roles:
                for field in (
                    "workspace_label",
                    "lane_key",
                    "lane_label",
                    "attempt_no",
                    "pane_group_size",
                ):
                    self.assertIn(field, keywords)
        self.assertEqual(roles, {"author", "builder", "reviewer", "tester"})

    def test_the_runs_launcher_is_given_a_workspace_label(self):
        labelled = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", "")
            )
            if name != "HerdrLauncher":
                continue
            labelled.append(any(kw.arg == "workspace_label" for kw in node.keywords))
        self.assertTrue(labelled, "maestro builds no HerdrLauncher")
        # Exactly the run's launcher. The standalone reviewer and the plan
        # author are not runs and deliberately keep the pre-workspace path.
        self.assertEqual(labelled.count(True), 1)


class _VanishingWorkspaceHerdr(_WorkspaceHerdr):
    """A herdr whose workspace is destroyed under the run.

    `tab create` answers `workspace_not_found` for the first `vanish_times`
    calls, exactly as the real server did when a run's workspace was closed
    mid-run, and succeeds afterwards.
    """

    def __init__(self, *, vanish_times: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self.vanish_times = vanish_times
        self.refused_workspaces = []

    def __call__(self, *args, env=None, timeout=30.0):
        if tuple(args[:2]) == ("tab", "create") and self.vanish_times > 0:
            self.vanish_times -= 1
            named = args[args.index("--workspace") + 1]
            self.refused_workspaces.append(named)
            self.calls.append(list(args))
            raise lch.HerdrCallError(
                'LAUNCH_REFUSED:{"error":{"code":"workspace_not_found",'
                '"message":"workspace %s not found"},"id":"cli:tab:create"}' % named,
                "workspace_not_found",
            )
        return super().__call__(*args, env=env, timeout=timeout)


class _SplitVanishingWorkspaceHerdr(_WorkspaceHerdr):
    """A workspace that disappears after a lane has claimed its root pane."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.destroyed_workspace = ""
        self.refused_splits = []

    def destroy_workspace(self) -> None:
        self.destroyed_workspace = self._workspace

    def __call__(self, *args, env=None, timeout=30.0):
        if (
            tuple(args[:2]) == ("pane", "split")
            and lch.workspace_of(args[2]) == self.destroyed_workspace
        ):
            self.calls.append(list(args))
            self.refused_splits.append(args[2])
            raise lch.HerdrCallError(
                'LAUNCH_REFUSED:{"error":{"code":"workspace_not_found",'
                '"message":"workspace %s not found"},"id":"cli:pane:split"}'
                % self.destroyed_workspace,
                "workspace_not_found",
            )
        return super().__call__(*args, env=env, timeout=timeout)


class _AdoptionVanishingWorkspaceHerdr(_WorkspaceHerdr):
    """A persisted actor whose entire workspace disappeared before resume."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.destroyed_workspace = ""

    def destroy_workspace(self) -> None:
        self.destroyed_workspace = self._workspace
        self._live_tabs = {
            tab_id
            for tab_id in self._live_tabs
            if lch.workspace_of(tab_id) != self.destroyed_workspace
        }

    def __call__(self, *args, env=None, timeout=30.0):
        if (
            tuple(args[:2]) in {("pane", "get"), ("pane", "close")}
            and lch.workspace_of(args[2]) == self.destroyed_workspace
        ):
            self.calls.append(list(args))
            raise lch.HerdrCallError(
                'LAUNCH_REFUSED:{"error":{"code":"workspace_not_found",'
                '"message":"workspace %s not found"}}' % self.destroyed_workspace,
                "workspace_not_found",
            )
        return super().__call__(*args, env=env, timeout=timeout)


class DestroyedWorkspaceTest(PanePlacementTest):
    """#79. A run's workspace id is memoized, and nothing invalidated it when
    herdr answered `workspace_not_found` — so every relaunch named the same
    dead id, got the same refusal, and the node blocked
    LAUNCHER_BUDGET_EXHAUSTED after spending attempts that could not have
    succeeded. The operator is told a counter ran out; nothing was retryable.
    """

    def test_a_vanished_workspace_during_lane_split_replaces_its_layout(self):
        """A live lane cache must not replay a split against its dead tab."""
        lane = "lane-p2-s3-inventory"
        harness, fake = self.build(
            workspace_label="run-1", factory=_SplitVanishingWorkspaceHerdr
        )
        first = harness.launch(
            self.spec("run1-builder-3", group=lane, role="builder", attempt=3, size=2)
        )
        old_workspace = harness.workspace_id
        old_layout = harness._tabs[lane]
        fake.destroy_workspace()

        second = harness.launch(
            self.spec("run1-reviewer-5", group=lane, role="reviewer", attempt=5, size=2)
        )

        self.assertEqual(fake.refused_splits, [first.pane_id])
        self.assertNotEqual(lch.workspace_of(second.pane_id), old_workspace)
        self.assertEqual(harness.workspace_id, lch.workspace_of(second.pane_id))
        created = fake.argv_for(("workspace", "create"))
        self.assertEqual(created[-1][created[-1].index("--label") + 1], "run-1")
        self.assertEqual(len(fake.argv_for(("workspace", "create"))), 2)
        self.assertEqual(fake.tab_create_labels(), [lane, lane])
        self.assertIsNot(harness._tabs[lane], old_layout)
        self.assertEqual(harness._tabs[lane].panes, [second.pane_id])
        self.assertEqual(harness._tabs[lane].claimed, 1)
        self.assertEqual(fake.renames[first.pane_id], "builder-a3")
        self.assertEqual(fake.renames[second.pane_id], "reviewer-a5")

    def test_resume_replaces_an_actor_whose_workspace_is_proven_absent(self):
        lane = "lane-p2-s3-inventory"
        original_runtime, fake = self.build(
            workspace_label="run-1", factory=_AdoptionVanishingWorkspaceHerdr
        )
        original = original_runtime.launch(
            self.spec("run1-builder-3", group=lane, role="builder", attempt=3)
        )
        fake.destroy_workspace()
        resumed = lch.HerdrLauncher(
            herdr_path=self.root / "herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted,
            workspace_label="run-1",
        )
        resumed._herdr = fake
        persisted = lch.PersistedActorHandle(
            correlation_token=original.correlation_token,
            pane_id=original.pane_id,
            agent_name=original.agent_name,
            launched_cwd=original.launched_cwd,
            environment=original.environment,
            workspace_id=original.workspace_id,
            tab_id=original.tab_id,
            lane_key=original.lane_key,
        )

        with self.assertRaisesRegex(lch.HandleAbsent, "WORKSPACE_ABSENT"):
            resumed.adopt(persisted)
        resumed.close_actorless_pane(persisted)
        replacement = resumed.launch(
            self.spec("run1-builder-4", group=lane, role="builder", attempt=4)
        )

        self.assertNotEqual(replacement.workspace_id, original.workspace_id)
        self.assertEqual(resumed.workspace_id, replacement.workspace_id)
        self.assertEqual(replacement.lane_key, lane)
        self.assertEqual(len(fake.argv_for(("workspace", "create"))), 2)

    def test_a_vanished_workspace_is_re_resolved_and_the_launch_succeeds(self):
        harness, fake = self.build(
            workspace_label="run-1", factory_kwargs={"vanish_times": 1}
        )
        handle = harness.launch(self.spec(group="node_a", role="builder", size=2))
        self.assertTrue(handle.pane_id)
        created = fake.argv_for(("workspace", "create"))
        self.assertEqual(
            len(created), 2, "the dead workspace must be replaced, not reused"
        )
        tabs = fake.argv_for(("tab", "create"))
        first = tabs[0][tabs[0].index("--workspace") + 1]
        last = tabs[-1][tabs[-1].index("--workspace") + 1]
        self.assertNotEqual(first, last, "the retry must ask a different question")
        self.assertEqual(fake.refused_workspaces, [first])

    def test_the_cache_holds_the_new_workspace_afterwards(self):
        """One re-resolution per vanishing, not one per launch: the next node
        must not pay for it again."""
        harness, fake = self.build(
            workspace_label="run-1", factory_kwargs={"vanish_times": 1}
        )
        harness.launch(self.spec("run1-node_a-1", group="node_a", role="builder"))
        before = len(fake.argv_for(("workspace", "create")))
        harness.launch(self.spec("run1-node_b-1", group="node_b", role="builder"))
        self.assertEqual(
            len(fake.argv_for(("workspace", "create"))),
            before,
            "the second node reused the re-resolved workspace",
        )

    def test_a_workspace_that_vanishes_twice_is_refused_rather_than_looped(self):
        """Twice is not a stale cache. Something is destroying workspaces as
        fast as this makes them, and a third ask re-poses a question already
        answered the same way twice."""
        harness, _fake = self.build(
            workspace_label="run-1", factory_kwargs={"vanish_times": 2}
        )
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec(group="node_a", role="builder"))
        self.assertIs(caught.exception.refusal, lch.LaunchRefusal.TAB_UNRESOLVED)
        self.assertIn("vanished twice", str(caught.exception))


def fake_env():
    return {}


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
