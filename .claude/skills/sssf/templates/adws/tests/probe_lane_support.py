"""A real factory run with a scripted reviewer, for the scheduler's probe tests.

Everything below the actor is production: the real `ArtifactStore`, the real
`FactoryScheduler`, real git, a real review tree and a real pytest process for
the accepted suite and for every probe. Only the agents are scripted -- the
builder writes `a.txt` from a list, the reviewer answers from a list and
submits probe files from it, and the acceptor answers from a function.

The accepted suite is numeric: it has eight cases and case `i` passes when
`i < the first token of a.txt`, so a candidate's first word is its passed count.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import probes
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests.test_tests_lane_handoff import (
    HandoffActor,
    _finding_for,
    _git,
    _init_repo,
    _unified_plan_bytes,
)

REQUIREMENT = "a.txt is written"
LANE = "lane-a"

NUMERIC_SUITE = {
    "tests/test_counts.py": (
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('index', range(8))\n"
        "def test_outcome(index):\n"
        "    assert index < int(Path('a.txt').read_text().split()[0])\n"
    )
}


def probe_source(token: str) -> str:
    return (
        "from pathlib import Path\n\n\n"
        "def test_{0}():\n    assert '{0}' in Path('a.txt').read_text().split()\n"
    ).format(token)


def probe_path(token: str) -> str:
    return "probes/test_{0}.py".format(token)


def submission(token: str, requirement: str = REQUIREMENT) -> probes.ProbeSubmission:
    return probes.ProbeSubmission(
        probe_path(token), probe_source(token), requirement, "a.txt carries " + token
    )


def all_pass(items, mode):
    return {
        "verdict": "PASS",
        "probe_verdicts": [
            {"probe_id": item["probe_id"], "verdict": "PASS", "reason": "follows from the requirement"}
            for item in items
        ],
    }


def all_revise(items, mode):
    return {
        "verdict": "REVISE",
        "probe_verdicts": [
            {"probe_id": item["probe_id"], "verdict": "REVISE", "reason": "does not follow"}
            for item in items
        ],
    }


class ProbeActor(HandoffActor):
    """Builder, reviewer and acceptor, all scripted.

    `bodies[i]` is the text of `a.txt` in build `i`. `script[i]` is the i-th
    code-review answer (the last repeats): `verdict`, `probes` (tokens),
    `submissions` (ready-made `ProbeSubmission`s, instead of `probes`),
    `challenges` (probe ids), `rejections`. `acceptance(items, mode)` is the
    acceptor's raw envelope.
    """

    def __init__(self, repo, worktrees, bodies, script, *, acceptance=all_pass):
        super().__init__(repo, worktrees)
        self.bodies = list(bodies)
        self.script = list(script)
        self.acceptance = acceptance
        self.acceptor = ("test-reviewer", "openai", "xai")
        self.builds = 0
        self.review_calls = 0
        self.review_ctxs = []
        self.builder_ctxs = []
        self.accept_calls = []
        self._stash = {}

    def write_tests(self, ctx):
        result = super().write_tests(ctx)
        result["files"] = dict(NUMERIC_SUITE)
        return result

    def build(self, ctx):
        self.builder_ctxs.append(ctx)
        body = self.bodies[min(self.builds, len(self.bodies) - 1)]
        self.builds += 1
        work = self.worktrees / "probe-build" / str(self.builds)
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        (work / "a.txt").write_text(body, encoding="utf-8")
        _git(work, "add", "a.txt")
        _git(work, "commit", "-m", "build-{0}".format(self.builds))
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}

    def review_code(self, ctx):
        step = self.script[min(self.review_calls, len(self.script) - 1)]
        self.review_calls += 1
        self.review_ctxs.append(ctx)
        self._stash[(ctx.lane.lane_id, ctx.input_digest)] = (
            tuple(step["submissions"])
            if "submissions" in step
            else tuple(submission(token) for token in step.get("probes", ())),
            tuple(step.get("rejections", ())),
            tuple(probes.Challenge(pid, "the probe is wrong") for pid in step.get("challenges", ())),
        )
        verdict = step.get("verdict", st.ReviewerVerdict.REVISE)
        findings = (_finding_for(ctx),) if verdict is st.ReviewerVerdict.REVISE else ()
        return verdict, findings

    def take_probes(self, ctx):
        return self._stash.pop((ctx.lane.lane_id, ctx.input_digest), ((), (), ()))

    def acceptor_for(self, ctx):
        return self.acceptor

    def accept_probes(self, ctx, role, items, mode):
        self.accept_calls.append((role, mode, [dict(item) for item in items]))
        return self.acceptance(items, mode)


class ProbeLaneCase(unittest.TestCase):
    """A one-lane run whose rounds the test drives one stage at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        _init_repo(self.repo)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.addCleanup(self.runtime.close)
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.run_id = "run-probes"
        self.lane_id = LANE

    def start(self, bodies, script, *, acceptance=all_pass, **scheduler_kwargs):
        compiled = plan_compiler.compile_plan(
            _unified_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:probes"
        )
        sch.create_factory_run(
            store=self.store, run_id=self.run_id, compiled=compiled,
            runtime=self.runtime, target=self.target,
        )
        self.actor = ProbeActor(
            self.repo, self.state / "worktrees", bodies, script, acceptance=acceptance
        )
        self.scheduler = sch.FactoryScheduler(
            self.store, self.run_id, self.actor, self.runtime, self.target,
            compiled=compiled, **scheduler_kwargs,
        )
        for step in ("_planned", "_writing_tests", "_reviewing_tests", "_tests_sealed"):
            getattr(self.scheduler, step)(self.lane_id)

    def round(self):
        self.scheduler._building(self.lane_id)
        if self.stage() is st.LaneStage.REVIEWING_CODE:
            self.scheduler._reviewing_code(self.lane_id)

    def stage(self):
        return self.store.lane_stage(self.run_id, self.lane_id)

    def artifacts(self, kind):
        return [
            json.loads(row["payload_json"])
            for row in self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
                "AND artifact_kind=? ORDER BY sequence",
                (self.run_id, self.lane_id, kind.value),
            )
        ]

    def reviews(self):
        return self.artifacts(st.ArtifactKind.CODE_REVIEW)

    def waits(self):
        return self.artifacts(st.ArtifactKind.USER_WAIT)
