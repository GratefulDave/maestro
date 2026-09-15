"""End-to-end acceptance: one plan name, a whole factory run, a real launcher.

Nothing is stubbed between the operator command and Herdr. `maestro.main` runs
the real `_run_plan`, `_run_start`, `_run_resume`, `FactoryScheduler`,
`HerdrStageActor` and `HerdrLauncher`; only the Herdr CLI itself is replaced,
by `tests/herdr_fake.py`, and the coding agent is simulated by writing the
files and envelope the pane would have produced when a prompt is offered to it.
Assertions are durable ledger rows, Git refs, and the Herdr resource graph.

The Herdr topology asserted here is one existing repository workspace. Each
lane is one tab in that workspace, and every lane-role pane lives inside its
lane tab at its role checkout. Maestro neither creates, tags, renames, nor
closes the repository workspace. `NoRepositoryWorkspaceTest` covers refusal when no
repository workspace is open.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from collections import defaultdict
from io import StringIO
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import yaml

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import launcher as lch
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests.herdr_fake import FakeHerdr, FakeHerdrStopped, flag
from tests.test_single_entry_cli import (
    SingleEntryBase,
    _git,
    install_deployment,
    ship_plan,
    working_directory,
)

FIXTURES = ADWS / "tests" / "fixtures" / "step8"
LANES = ("lane-a", "lane-b")
OUTPUTS = {"lane-a": "a.txt", "lane-b": "b.txt"}
#: A final-review REVISE this suite can rely on. It names a declared output --
#: a file the integration reviewer actually reads at the integration SHA -- so
#: the fixture is a finding a real reviewer could have written. This case is
#: about the amendment wait, not about what a reviewer may say.
FINDING = {
    "implementation_area": "a.txt",
    "observed_behavior": "the declared output is empty",
    "required_behavior": "behavior is asserted",
    "violated_requirement": "a.txt is written",
}
_PROMPT = re.compile(r"@(\S+\.json)")


def two_lane_plan() -> bytes:
    """Two independent lanes: both are dependency-ready in the same pass."""
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": lane,
                "needs": [],
                "outputs": [OUTPUTS[lane]],
                "spec": {
                    "goal": "emit " + OUTPUTS[lane],
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": [OUTPUTS[lane] + " is written"],
            }
            for lane in LANES
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _prompt_body(path: Path) -> dict | None:
    """The turn's JSON prompt, past whatever preamble the route prepended.

    `prepare_route_prompt_text` prefixes the Claude route's prompt file with
    route-wide policy text, so the file a pane is handed is not JSON from its
    first byte.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        body = json.loads(text[start:])
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class SimulatedPanes:
    """Every role's turn, produced by the pane the real launcher opened.

    Registered as Herdr hooks: starting an agent gives it a transcript, and
    offering it a prompt makes it do the role's work and write the envelope
    the actor is waiting for. Nothing here talks to Maestro directly.
    """

    def __init__(
        self,
        herdr: FakeHerdr,
        scratch: Path,
        *,
        test_revisions: int = 1,
        code_revisions: int = 1,
        integration_revisions: int = 0,
        nonce: str = "1",
    ) -> None:
        self.herdr = herdr
        self.scratch = scratch
        self.test_revisions = test_revisions
        self.code_revisions = code_revisions
        self.integration_revisions = integration_revisions
        self.integration_rounds = 0
        #: Distinguishes one simulated builder's bytes from another's, so a
        #: second run of the same plan does not publish an identical tree.
        self.nonce = nonce
        self.build_rounds: dict[str, int] = defaultdict(int)
        self.turns: list[tuple[str, str]] = []
        self.verdicts: list[tuple[str, str, str]] = []
        self.test_rounds: dict[str, int] = defaultdict(int)
        self.code_rounds: dict[str, int] = defaultdict(int)
        self.role_worktrees: dict[tuple[str, str], Path] = {}
        herdr.hooks_after.setdefault(("agent", "start"), []).append(self._started)
        herdr.hooks_after.setdefault(("pane", "send-text"), []).append(self._offered)

    # -- hooks --------------------------------------------------------------

    def _started(self, args: tuple[str, ...]) -> None:
        name = args[2]
        transcript = self.scratch / (name.replace("/", "_") + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.touch()
        agent = self.herdr.agents.get(name)
        if agent is not None:
            agent["agent_session"] = {"kind": "path", "value": str(transcript)}

    def _offered(self, args: tuple[str, ...]) -> None:
        pane_id, text = args[2], args[3]
        match = _PROMPT.search(text)
        if match is None:
            return
        prompt_path = Path(match.group(1))
        if not prompt_path.is_file():
            return
        self._record_submission(pane_id, prompt_path)
        prompt = _prompt_body(prompt_path)
        if prompt is None:
            return
        role = str(prompt.get("role") or "")
        if not role:
            return
        cwd = Path(str(prompt["working_directory"]))
        envelope = Path(str(prompt["envelope_path"]))
        payload = self._work(role, prompt, cwd)
        envelope.parent.mkdir(parents=True, exist_ok=True)
        envelope.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _record_submission(self, pane_id: str, prompt_path: Path) -> None:
        """A real composer records the offer in the agent's transcript."""
        for agent in self.herdr.agents.values():
            if str(agent.get("pane_id") or "") != pane_id:
                continue
            session = agent.get("agent_session") or {}
            value = str(session.get("value") or "")
            if value:
                with open(value, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"text": "@" + str(prompt_path.resolve())}) + "\n"
                    )

    # -- the roles ----------------------------------------------------------

    def _work(self, role: str, prompt: Mapping[str, Any], cwd: Path) -> dict:
        lane = str(prompt.get("lane_id") or "")
        self.role_worktrees[(lane, role)] = cwd.resolve()
        self.turns.append((lane, role))
        if role == "tester":
            return self._tester(lane, prompt, cwd)
        if role == "builder":
            return self._builder(prompt, cwd)
        if role == "test-reviewer":
            return self._reviewer(lane, role, self.test_rounds, self.test_revisions)
        if role == "code-reviewer":
            return self._reviewer(lane, role, self.code_rounds, self.code_revisions)
        return self._integration(prompt)

    def _integration(self, prompt: Mapping[str, Any]) -> dict:
        seen = self.integration_rounds
        self.integration_rounds += 1
        lanes = [str(item) for item in (prompt.get("lane_ids") or ())]
        if seen < self.integration_revisions and lanes:
            self.verdicts.append(("", "integration-reviewer", "REVISE"))
            return {
                "verdict": "REVISE",
                "findings": [FINDING],
                "affected_lanes": lanes[:1],
            }
        self.verdicts.append(("", "integration-reviewer", "PASS"))
        return {"verdict": "PASS", "findings": [], "affected_lanes": []}

    def _tester(self, lane: str, prompt: Mapping[str, Any], cwd: Path) -> dict:
        outputs = [str(item) for item in (prompt.get("declared_outputs") or ())]
        name = lane.replace("-", "_")
        lines = ["# secret-selector", "from pathlib import Path", ""]
        for output in outputs:
            ident = Path(output).stem.replace("-", "_")
            lines.append("def test_{0}_{1}_exists():".format(name, ident))
            lines.append("    assert Path({0!r}).is_file()".format(output))
            lines.append("")
        path = cwd / "tests" / "test_{0}_private.py".format(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return {"private_files": {}}

    def _builder(self, prompt: Mapping[str, Any], cwd: Path) -> dict:
        lane = str(prompt.get("lane_id") or "")
        self.build_rounds[lane] += 1
        body = "{0}:{1}:{2}\n".format(lane, self.nonce, self.build_rounds[lane])
        for output in prompt.get("declared_outputs") or ():
            path = cwd / str(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return {"changed": True}

    def _reviewer(
        self, lane: str, role: str, rounds: dict[str, int], revisions: int
    ) -> dict:
        seen = rounds[lane]
        rounds[lane] += 1
        verdict = "REVISE" if seen < revisions else "PASS"
        self.verdicts.append((lane, role, verdict))
        if verdict == "REVISE":
            return {"verdict": "REVISE", "findings": [FINDING]}
        return {"verdict": "PASS", "findings": []}


def herdr_graph(herdr: FakeHerdr) -> dict[str, Any]:
    """The live repository-workspace → lane-tab → role-pane graph."""
    with herdr.lock:
        live = {
            key: record
            for key, record in herdr.workspaces.items()
            if key not in herdr.closed_workspaces
        }
        parents = list(live)
        tabs: dict[str, dict[str, str]] = defaultdict(dict)
        for tab_id, tab in herdr.tabs.items():
            workspace_id = str(tab.get("workspace_id") or "")
            if workspace_id in live:
                tabs[workspace_id][tab_id] = str(tab.get("label") or "")

        panes: dict[str, dict[str, dict[str, str]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        lane_tabs: dict[str, list[str]] = defaultdict(list)
        lane_panes: dict[str, dict[str, dict[str, str]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        pane_cwds: dict[str, str] = {}
        for pane_id, pane in herdr.panes.items():
            if pane_id in herdr.closed_panes:
                continue
            workspace_id = str(pane.get("workspace_id") or "")
            tab_id = str(pane.get("tab_id") or "")
            if workspace_id not in live:
                continue
            label = str(pane.get("label") or "")
            panes[workspace_id][tab_id][pane_id] = label
            pane_cwds[pane_id] = str(pane.get("cwd") or "")
            tokens = lch._herdr_tokens(pane)
            lane = str(tokens.get(lch.METADATA_TOKEN_LANE) or "")
            if (
                tokens.get(lch.METADATA_TOKEN_KIND) == lch.METADATA_KIND_LANE
                and tokens.get(lch.METADATA_TOKEN_PARENT) == workspace_id
                and lane
            ):
                if tab_id not in lane_tabs[lane]:
                    lane_tabs[lane].append(tab_id)
                lane_panes[lane][tab_id][pane_id] = label

        agents = {
            name: str(record.get("pane_id") or "")
            for name, record in herdr.agents.items()
            if str(record.get("pane_id") or "") not in herdr.closed_panes
        }
        return {
            "parents": sorted(parents),
            "tabs": {
                workspace_id: dict(sorted(items.items()))
                for workspace_id, items in tabs.items()
            },
            "lane_tabs": {lane: sorted(tab_ids) for lane, tab_ids in lane_tabs.items()},
            "panes": {
                workspace_id: {
                    tab_id: dict(sorted(items.items()))
                    for tab_id, items in by_tab.items()
                }
                for workspace_id, by_tab in panes.items()
            },
            "lane_panes": {
                lane: {
                    tab_id: dict(sorted(items.items()))
                    for tab_id, items in by_tab.items()
                }
                for lane, by_tab in lane_panes.items()
            },
            "pane_cwds": pane_cwds,
            "agents": agents,
            "live_workspaces": sorted(live),
        }


def plant_operator_workspace(herdr: FakeHerdr, primary: Path) -> str:
    """Plant the repository workspace from which the operator invokes Maestro."""
    operator = herdr.add_workspace(primary.name, primary)
    tab_id = str(herdr.workspaces[operator]["active_tab_id"])
    tab = herdr.tabs[tab_id]
    tab["label"] = "notes"
    pane = next(
        pane
        for pane in herdr.panes.values()
        if pane["workspace_id"] == operator and pane["tab_id"] == tab_id
    )
    herdr.start_agent("operator-claude", pane["pane_id"], status="working")
    return operator


def workspace_records(herdr: FakeHerdr, workspace_id: str) -> set[str]:
    """The original tab, pane, and agent records the operator owns."""
    tab_ids = {
        key for key, tab in herdr.tabs.items() if tab["workspace_id"] == workspace_id
    }
    pane_ids = {
        key for key, pane in herdr.panes.items() if pane["workspace_id"] == workspace_id
    }
    return (
        tab_ids
        | pane_ids
        | {
            name
            for name, agent in herdr.agents.items()
            if str(agent.get("pane_id") or "") in pane_ids
        }
    )


#: Verbs that change the Herdr resource graph. The topology is asserted after
#: every one of them, not only once the run has finished.
TOPOLOGY_VERBS = frozenset(
    {
        ("workspace", "create"),
        ("workspace", "close"),
        ("worktree", "open"),
        ("tab", "create"),
        ("tab", "close"),
        ("pane", "split"),
        ("pane", "rename"),
        ("pane", "report-metadata"),
        ("pane", "close"),
        ("agent", "start"),
    }
)


class RecordingHerdr:
    """The fake CLI, plus the resource graph after every topology change.

    An instance, not a function: patched onto `HerdrLauncher._herdr` a plain
    function would bind as a method and be handed the launcher as `self`.
    """

    def __init__(self, herdr: FakeHerdr, graphs: list[dict[str, Any]]) -> None:
        self.herdr = herdr
        self.graphs = graphs

    def __call__(self, *args: str, **kwargs: object) -> dict:
        try:
            return self.herdr(*args, **kwargs)
        finally:
            if tuple(args[:2]) in TOPOLOGY_VERBS:
                self.graphs.append(herdr_graph(self.herdr))


class FactoryEndToEndBase(SingleEntryBase):
    #: One operator-owned repository workspace is required.
    #: `False` models the required refusal when none is open.
    operator_workspace = True

    def setUp(self) -> None:
        super().setUp()
        # The shipped one-lane plan from the base fixture is replaced by the
        # two-lane plan this file drives; both live at the installed path.
        self.plan_path.write_bytes(two_lane_plan())
        _git(self.repo, "add", "-f", str(self.plan_path))
        _git(self.repo, "commit", "-m", "ship two-lane")
        self.maestro_file = self._install_full_deployment()
        self.final_graph: dict[str, Any] = {}
        self.graphs: list[dict[str, Any]] = []
        self.reset_herdr("transcripts")

    def reset_herdr(self, transcripts: str, **panes: Any) -> None:
        """A fresh Herdr, the operator's workspace, and a clean baseline."""
        self.herdr = FakeHerdr()
        self.panes = SimulatedPanes(self.herdr, self.root / transcripts, **panes)
        self.operator = (
            plant_operator_workspace(self.herdr, self.repo)
            if self.operator_workspace
            else ""
        )
        self.operator_records = (
            workspace_records(self.herdr, self.operator) if self.operator else set()
        )
        self.before = self.herdr.snapshot()
        self.graphs = []

    def _install_full_deployment(self) -> Path:
        """The stamped deployment plus the route receipts `_actor_for` needs."""
        adws = self.repo / "adws"
        config = adws / "maestro.config.yaml"
        loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
        loaded["executables"] = {"herdr": "herdr", "omp": "omp", "claude": "claude"}
        loaded["route_receipts"] = {
            "claude": str(FIXTURES / "claude.json"),
            "omp": str(FIXTURES / "omp.json"),
        }
        loaded["route_verify_keys"] = [str(FIXTURES / "route_receipts.pub")]
        config.write_text(yaml.safe_dump(loaded, sort_keys=True), encoding="utf-8")
        _git(self.repo, "add", "-f", str(config))
        _git(self.repo, "commit", "-m", "route receipts")
        return adws / "maestro.py"

    def run_cli(self, *, expect: int | None = 0) -> tuple[int, Mapping[str, Any]]:
        buf = StringIO()
        original = maestro.HerdrStageActor.complete_run_spaces

        def capture(actor: Any, run_id: str) -> None:
            # The graph as it stands with the run finished and nothing closed
            # yet: the only moment the complete topology is observable.
            self.final_graph = herdr_graph(self.herdr)
            return original(actor, run_id)

        with (
            mock.patch.object(maestro.HerdrStageActor, "complete_run_spaces", capture),
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(
                lch.HerdrLauncher, "_herdr", RecordingHerdr(self.herdr, self.graphs)
            ),
            mock.patch.object(lch, "AGENT_START_BUSY_WINDOW_S", 0.0),
            mock.patch.object(lch, "AGENT_QUIESCENCE_CONFIRM_S", 0.0),
            # Wall-clock settles measured against a live composer. The fake
            # composer takes the paste synchronously, so waiting for it only
            # makes the suite slow; the sequence of calls is unchanged.
            mock.patch.object(lch, "PASTE_SETTLE_S", 0.0),
            mock.patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": os.devnull, "PATH": os.environ["PATH"]},
                clear=True,
            ),
            mock.patch("sys.stdout", buf),
            working_directory(self.repo),
        ):
            code = maestro.main(["--plan", self.plan_name])
        text = buf.getvalue().strip()
        payload = json.loads(text.splitlines()[-1]) if text else {}
        if expect is not None:
            self.assertEqual(code, expect, payload)
        return code, payload

    # -- topology assertions ------------------------------------------------

    def calls_of(self, *verb: str) -> list[tuple[str, ...]]:
        return [call for call in self.herdr.calls if call[:2] == verb]

    def repository_workspace(self) -> str:
        """The repository workspace selected for this run."""
        if self.operator:
            return self.operator
        parents = herdr_graph(self.herdr)["parents"]
        self.assertEqual(len(parents), 1, parents)
        return parents[0]

    def assert_lanes_share_repository_workspace(self, parent: str) -> None:
        """Every lane is one direct tab inside `parent`."""
        self.assertEqual(self.calls_of("workspace", "create"), [])
        self.assertEqual(self.calls_of("worktree", "open"), [])
        if self.operator:
            self.assertTrue(
                self.herdr.records_unchanged(self.before, self.operator_records),
                "the operator's original tab, pane, or agent was modified",
            )
        tab_creates = self.calls_of("tab", "create")
        self.assertTrue(tab_creates, "no lane tab was ever created")
        for call in tab_creates:
            self.assertEqual(flag(call, "--workspace"), parent, call)
        self.assertNotIn(parent, self.herdr.closed_workspaces)
        self.assertTrue(self.graphs, "no topology change was observed")
        for graph in self.graphs:
            self.assertEqual(graph["live_workspaces"], [parent], graph)
            self.assertLessEqual(set(graph["lane_tabs"]), set(LANES), graph)

    def assert_direct_topology(
        self, *, unowned_pane_ids: set[str] | None = None
    ) -> str:
        """Prove repository workspace → lane tabs → role panes."""
        parent = self.repository_workspace()
        self.assert_lanes_share_repository_workspace(parent)
        final = self.final_graph
        allowed_unowned_pane_ids = unowned_pane_ids or set()
        seen_unowned_pane_ids: set[str] = set()
        self.assertEqual(final["parents"], [parent], final)
        self.assertEqual(final["live_workspaces"], [parent], final)
        self.assertEqual(set(final["lane_tabs"]), set(LANES), final)
        expected_roles = ("tester", "builder", "test-reviewer", "code-reviewer")
        agents_by_pane: dict[str, list[str]] = defaultdict(list)
        for name, pane_id in final["agents"].items():
            agents_by_pane[pane_id].append(name)
        for lane in LANES:
            self.assertEqual(len(final["lane_tabs"][lane]), 1, (lane, final))
            lane_roles = expected_roles + (
                ("integration-reviewer",) if lane == LANES[-1] else ()
            )
            expected_labels = {role: lch.pane_label_for(role) for role in lane_roles}
            tabs = final["lane_tabs"][lane]
            self.assertEqual(len(tabs), 1, (lane, final))
            tab_id = tabs[0]
            role_panes = final["lane_panes"].get(lane, {}).get(tab_id, {})
            unowned_role_panes = {
                pane_id: label
                for pane_id, label in role_panes.items()
                if pane_id in allowed_unowned_pane_ids
            }
            seen_unowned_pane_ids.update(unowned_role_panes)
            self.assertTrue(
                all(label == "" for label in unowned_role_panes.values()),
                (lane, unowned_role_panes, final),
            )
            self.assertTrue(
                all(pane_id not in agents_by_pane for pane_id in unowned_role_panes),
                (lane, unowned_role_panes, final),
            )
            role_panes = {
                pane_id: label
                for pane_id, label in role_panes.items()
                if pane_id not in allowed_unowned_pane_ids
            }
            self.assertEqual(
                set(role_panes.values()), set(expected_labels.values()), (lane, final)
            )
            self.assertEqual(len(role_panes), len(expected_labels), (lane, final))
            for role, label in expected_labels.items():
                pane_ids = [
                    pane_id
                    for pane_id, pane_label in role_panes.items()
                    if pane_label == label
                ]
                self.assertEqual(len(pane_ids), 1, (lane, role, final))
                pane_id = pane_ids[0]
                self.assertEqual(
                    Path(final["pane_cwds"][pane_id]).resolve(),
                    self.panes.role_worktrees[(lane, role)],
                    (lane, role, final),
                )
                self.assertEqual(len(agents_by_pane[pane_id]), 1, (pane_id, final))
        self.assertEqual(
            seen_unowned_pane_ids,
            allowed_unowned_pane_ids,
            (allowed_unowned_pane_ids, final),
        )
        return parent

    # -- durable assertions -------------------------------------------------

    def lane_stages(self, run_id: str) -> dict[str, str]:
        with self.ledger() as store:
            return {
                lane.lane_id: store.lane_stage(run_id, lane.lane_id).value
                for lane in store.active_projection(run_id)
            }

    def run_status(self, run_id: str) -> st.RunStatus:
        with self.ledger() as store:
            return store.derive_run_status(
                run_id, sch.durable_integration_tip(store, run_id)
            )

    def artifact_kinds(self, run_id: str) -> list[str]:
        with self.ledger() as store:
            return [
                str(row[0])
                for row in store.conn.execute(
                    "SELECT artifact_kind FROM lane_artifacts WHERE run_id=? "
                    "ORDER BY sequence",
                    (run_id,),
                ).fetchall()
            ]

    def run_artifact_kinds(self, run_id: str) -> list[str]:
        with self.ledger() as store:
            return [
                str(row[0])
                for row in store.conn.execute(
                    "SELECT artifact_kind FROM run_artifacts WHERE run_id=? "
                    "ORDER BY sequence",
                    (run_id,),
                ).fetchall()
            ]


class WholeFactoryRunTest(FactoryEndToEndBase):
    def test_one_plan_name_runs_the_whole_factory_and_publishes(self) -> None:
        self.run_cli()
        run_ids = self.run_ids()
        self.assertEqual(len(run_ids), 1)
        run_id = run_ids[0]

        self.assertEqual(self.lane_stages(run_id), {lane: "MERGED" for lane in LANES})
        self.assertEqual(self.run_status(run_id), st.RunStatus.COMPLETE)

        kinds = self.artifact_kinds(run_id)
        for expected in (
            "LANE_PLAN",
            "TEST_DRAFT",
            "TEST_REVIEW",
            "SEALED_TEST_BUNDLE",
            "BUILDER_OUTPUT",
            "CODE_REVIEW",
            "INTEGRATION_MERGE",
        ):
            self.assertIn(expected, kinds)
        run_kinds = self.run_artifact_kinds(run_id)
        self.assertIn("FINAL_INTEGRATION_REVIEW", run_kinds)
        self.assertIn("MAIN_PUBLICATION", run_kinds)

        # The test reviewer sent every lane back before passing it: nothing
        # outranks it, because a draft has no measurement to appeal to.
        for lane in LANES:
            self.assertIn((lane, "test-reviewer", "REVISE"), self.panes.verdicts)
            self.assertIn((lane, "test-reviewer", "PASS"), self.panes.verdicts)

        stages = self.lane_stages(run_id)
        # The code reviewer behaves the same way, and a green sealed suite does
        # not overrule it. It said REVISE on a candidate whose suite had just
        # passed, the lane went back to BUILDING, and it was asked again on the
        # next candidate before the lane merged. A reviewer's verdict is the
        # transition; the suite is what the next candidate has to satisfy.
        for lane in LANES:
            self.assertIn((lane, "code-reviewer", "REVISE"), self.panes.verdicts)
            self.assertIn((lane, "code-reviewer", "PASS"), self.panes.verdicts)
            self.assertEqual(stages[lane], st.LaneStage.MERGED.value)

        # The publication reached the target repository's main ref, and it
        # carries the candidate the code reviewer passed rather than the one
        # it revised. The fake builder stamps its round number into the body,
        # so this is the REVISE-does-not-merge assertion stated on published
        # bytes: with code_revisions=1 the reviewer sent round 1 back, the
        # builder produced round 2, and only round 2 may be on main. Reading
        # the verdict list alone would not catch a lane that merged its first
        # candidate and asked the reviewer again afterwards.
        head = _git(self.repo, "rev-parse", "refs/heads/main")
        for lane in LANES:
            blob = _git(self.repo, "cat-file", "-p", head + ":" + OUTPUTS[lane])
            self.assertIn(lane, blob)
            self.assertEqual(self.panes.build_rounds[lane], 2)
            revised = "{0}:{1}:1".format(lane, self.panes.nonce)
            passed = "{0}:{1}:2".format(lane, self.panes.nonce)
            self.assertEqual(blob.strip(), passed)
            self.assertNotEqual(blob.strip(), revised)

        parent = self.assert_direct_topology()
        self.assertEqual(parent, self.operator)
        self.assertEqual(self.final_graph["parents"], [self.operator])

    def test_a_repeat_invocation_after_completion_starts_a_second_run(self) -> None:
        self.run_cli()
        first = self.run_ids()
        self.assertEqual(len(first), 1)
        self.assert_direct_topology()
        self.reset_herdr("transcripts-2", nonce="2")
        self.run_cli()
        second = self.run_ids()
        self.assertEqual(len(second), 2)
        self.assertEqual(set(first) - set(second), set())
        for run_id in second:
            self.assertEqual(self.run_status(run_id), st.RunStatus.COMPLETE)
        # The second run reused the same operator workspace and left it as it was.
        self.assert_direct_topology()


class AmendmentWaitTest(FactoryEndToEndBase):
    def test_a_final_review_revise_waits_and_the_same_command_does_not_amend(
        self,
    ) -> None:
        self.panes = SimulatedPanes(
            self.herdr, self.root / "transcripts", integration_revisions=1
        )
        code, payload = self.run_cli()
        self.assertEqual(payload["status"], st.RunStatus.WAITING.value)
        run_ids = self.run_ids()
        self.assertEqual(len(run_ids), 1)
        run_id = run_ids[0]
        waiting = [
            lane
            for lane, stage in self.lane_stages(run_id).items()
            if stage == "WAITING_FOR_USER"
        ]
        self.assertTrue(waiting, self.lane_stages(run_id))

        # The single entry resumes the waiting run and leaves the wait alone:
        # only `run amend` moves an AMENDMENT_REQUIRED lane.
        second, again = self.run_cli()
        self.assertEqual(again["outcome"], "RESUMED")
        self.assertEqual(again["run_id"], run_id)
        self.assertEqual(again["status"], st.RunStatus.WAITING.value)
        self.assertEqual(self.run_ids(), (run_id,))
        self.assertEqual(
            [
                lane
                for lane, stage in self.lane_stages(run_id).items()
                if stage == "WAITING_FOR_USER"
            ],
            waiting,
        )
        with self.ledger() as store:
            kinds = [
                str(row[0])
                for row in store.conn.execute(
                    "SELECT artifact_kind FROM lane_artifacts WHERE run_id=?",
                    (run_id,),
                ).fetchall()
            ]
        self.assertIn("USER_WAIT", kinds)
        self.assertNotIn("MAIN_PUBLICATION", self.run_artifact_kinds(run_id))
        # A waiting run keeps its lane tabs inside the repository workspace.
        self.assert_lanes_share_repository_workspace(self.operator)
        self.assertNotIn(self.operator, self.herdr.closed_workspaces)
        self.assertEqual(set(herdr_graph(self.herdr)["lane_tabs"]), set(LANES))


class ConcurrentLaneTopologyTest(FactoryEndToEndBase):
    def test_two_ready_lanes_share_one_parent_and_get_one_tab_each(self) -> None:
        graphs: list[dict[str, Any]] = []

        original = maestro.HerdrStageActor.write_tests

        def observed(actor, ctx):
            result = original(actor, ctx)
            graphs.append(herdr_graph(self.herdr))
            return result

        with mock.patch.object(maestro.HerdrStageActor, "write_tests", observed):
            self.run_cli()

        self.assertTrue(graphs)
        for graph in graphs:
            self.assertEqual(graph["parents"], [self.operator], graph)
            self.assertEqual(graph["live_workspaces"], [self.operator], graph)
            self.assertLessEqual(set(graph["lane_tabs"]), set(LANES), graph)
            self.assertTrue(
                all(len(tab_ids) == 1 for tab_ids in graph["lane_tabs"].values()),
                graph,
            )
        last = graphs[-1]
        self.assertEqual(set(last["lane_tabs"]), set(LANES), last)
        self.assertEqual(self.assert_direct_topology(), self.operator)

    def test_each_role_gets_exactly_one_pane_and_one_agent_per_lane(self) -> None:
        graphs: list[dict[str, Any]] = []
        original = maestro.HerdrStageActor.review_code

        def observed(actor, ctx):
            graphs.append(herdr_graph(self.herdr))
            return original(actor, ctx)

        with mock.patch.object(maestro.HerdrStageActor, "review_code", observed):
            self.run_cli()

        self.assertTrue(graphs)
        graph = graphs[-1]
        expected = {
            lch.pane_label_for(role)
            for role in ("tester", "builder", "test-reviewer", "code-reviewer")
        }
        agents_by_pane: dict[str, list[str]] = defaultdict(list)
        for name, pane_id in graph["agents"].items():
            agents_by_pane[pane_id].append(name)
        for lane in LANES:
            tabs = graph["lane_tabs"].get(lane, [])
            self.assertEqual(len(tabs), 1, (lane, graph))
            role_panes = graph["lane_panes"].get(lane, {}).get(tabs[0], {})
            self.assertEqual(set(role_panes.values()), expected, (lane, graph))
            self.assertEqual(len(role_panes), len(expected), (lane, graph))
            for pane_id in role_panes:
                self.assertEqual(len(agents_by_pane[pane_id]), 1, (pane_id, graph))


class ReconstructionBase(FactoryEndToEndBase):
    """Termination after an external side effect, then a plain re-invocation."""

    def converge(
        self, run_id: str, *, unowned_pane_ids: set[str] | None = None
    ) -> None:
        """One run, one repository workspace, one tab per lane and pane per role."""
        self.assertEqual(self.run_ids(), (run_id,))
        self.assertEqual(self.run_status(run_id), st.RunStatus.COMPLETE)
        self.assert_direct_topology(unowned_pane_ids=unowned_pane_ids)

    def _crash_then_resume(
        self,
        verb: tuple[str, str],
        nth: int = 1,
        *,
        before: bool = False,
        expected_unowned_blank_panes: int | None = None,
    ) -> None:
        """Terminate right after one external side effect, then re-invoke.

        The termination surfaces either as the injected `FakeHerdrStopped`
        escaping or as a nonzero exit — the launcher converts some of them
        into a typed launch refusal. Both are "this process stopped there";
        what the row asserts is what the *next* plain invocation does.
        """
        if before:
            self.herdr.crash_before(verb, nth)
        else:
            self.herdr.crash_after(verb, nth)
        try:
            code, payload = self.run_cli(expect=None)
        except FakeHerdrStopped:
            pass
        else:
            self.assertNotEqual(code, 0, payload)
        created = self.run_ids()
        self.assertEqual(len(created), 1, "a stopped process created no second run")
        crash_snapshot = self.herdr.snapshot()
        unowned_pane_ids: set[str] = set()
        if expected_unowned_blank_panes is not None:
            seed_pane_ids = {
                str(workspace.get("tokens", {}).get("seed_pane") or "")
                for workspace in crash_snapshot["workspaces"].values()
            }
            unowned_pane_ids = {
                pane_id
                for pane_id, pane in crash_snapshot["panes"].items()
                if pane_id not in self.before["panes"]
                and pane_id not in crash_snapshot["closed_panes"]
                and pane_id not in seed_pane_ids
                and not pane.get("label")
                and not pane.get("tokens")
                and not any(
                    str(agent.get("pane_id") or "") == pane_id
                    for agent in crash_snapshot["agents"].values()
                )
            }
            self.assertEqual(
                len(unowned_pane_ids),
                expected_unowned_blank_panes,
                crash_snapshot,
            )
        self.run_cli()
        if unowned_pane_ids:
            self.assertTrue(
                self.herdr.records_unchanged(crash_snapshot, unowned_pane_ids),
                "the split-before-label pane was modified or closed",
            )
        self.converge(created[0], unowned_pane_ids=unowned_pane_ids)


class ReconstructionTest(ReconstructionBase):
    """Reconstruction with the operator's repository workspace."""


    def test_termination_after_a_role_pane_is_split(self) -> None:
        """A split-before-label pane stays unowned through one restart."""
        self._crash_then_resume(("pane", "split"), expected_unowned_blank_panes=1)

    def test_termination_after_a_role_agent_is_started(self) -> None:
        self._crash_then_resume(("agent", "start"))

    def test_termination_in_the_middle_of_a_role_turn(self) -> None:
        # Before the offer, not after: a simulated pane that has been handed
        # the prompt has already written its envelope, and the launcher then
        # rightly keeps that declared result instead of dying.
        self._crash_then_resume(("pane", "send-text"), nth=2, before=True)

    def test_termination_after_pane_metadata_tagging(self) -> None:
        self._crash_then_resume(("pane", "report-metadata"))


class NoRepositoryWorkspaceTest(ReconstructionBase):
    """No repository workspace is open, so the run refuses without creating one."""

    operator_workspace = False

    def test_the_run_refuses_and_creates_no_workspace(self) -> None:
        code, _ = self.run_cli(expect=None)
        self.assertNotEqual(code, 0)
        run_ids = self.run_ids()
        for run_id in run_ids:
            self.assertNotEqual(self.run_status(run_id), st.RunStatus.COMPLETE)
        self.assertEqual(
            [call for call in self.herdr.calls if call[:2] == ("workspace", "create")],
            [],
        )
        self.assertEqual(
            [call for call in self.herdr.calls if call[:2] == ("worktree", "open")],
            [],
        )
        self.assertEqual(herdr_graph(self.herdr)["parents"], [])


if __name__ == "__main__":
    unittest.main()
