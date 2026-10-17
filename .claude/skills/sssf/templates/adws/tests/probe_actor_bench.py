"""A real `HerdrStageActor` over a real git repository and a scripted launcher.

Shared by the probe actor tests. The launcher is the only fake: it reads the
prompt the actor wrote, lets a test script write files into the dispatch tree,
and writes the envelope the actor then reads. Everything between -- role
directories, worktrees, the dispatch identity, `_review_payload`, the probe
parser and the bounded reader -- is production code.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules.scheduler import LaneContext
from adw_modules.scheduler_types import LaneProjection, LaneStage, lane_projection_digest

ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}

DIFFERENT_VENDORS: Mapping[str, str | None] = {
    "code-reviewer": "openai",
    "test-reviewer": "anthropic",
    "tester": "xai",
}

SPEC = "ab" * 32
OUTPUT = "src/service.py"
CONTRACT_QUOTE = "the service rejects a negative quantity"


@dataclass(frozen=True)
class ProbeCtx(LaneContext):
    """`LaneContext` with the probe fields the scheduler owns.

    Declared here so these tests do not depend on the scheduler work package
    landing first; once it does, the same names are redeclared harmlessly.
    """

    rejected_probes: tuple = ()
    existing_probes: tuple = ()
    probe_budget_left: int = 0
    probe_texts: Mapping[str, str] | None = None
    challenges: tuple = ()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def lane(lane_id: str = "lane-a") -> LaneProjection:
    outputs = (OUTPUT,)
    return LaneProjection(
        lane_id=lane_id,
        needs=(),
        spec_digest=SPEC,
        declared_outputs=outputs,
        lane_projection_digest=lane_projection_digest(SPEC, (), outputs, lane_kind=None),
        public_acceptance=("the service validates quantity",),
    )


def context(
    candidate: str,
    *,
    lane_id: str = "lane-a",
    input_digest: str = "44" * 32,
    **fields: Any,
) -> ProbeCtx:
    return ProbeCtx(
        run_id="run-1",
        lane=lane(lane_id),
        plan_revision=1,
        plan_digest="ef" * 32,
        plan_artifact_ref="plan:x",
        input_digest=input_digest,
        stage=LaneStage.REVIEWING_CODE,
        artifacts={},
        candidate_sha=candidate,
        public_contract={
            "acceptance_criteria": [CONTRACT_QUOTE],
            "declared_outputs": [OUTPUT],
        },
        test_suite_digest="test-suite.v1:" + "55" * 32,
        **fields,
    )


Responder = Callable[[lch.LaunchSpec, Mapping[str, Any]], Mapping[str, Any]]


class ScriptedLauncher:
    """Hands every dispatch to `responder(spec, prompt)` and records the prompt."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.prompts: list[Mapping[str, Any]] = []
        self.launches = 0

    @staticmethod
    def invoking_repository(_env: object) -> None:
        return None

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        self.launches += 1
        prompt = json.loads(Path(spec.prompt_path).read_text(encoding="utf-8"))
        self.prompts.append(prompt)
        envelope = Path(spec.envelope_path)
        envelope.parent.mkdir(parents=True, exist_ok=True)
        envelope.write_text(
            json.dumps(self.responder(spec, prompt)), encoding="utf-8"
        )
        return SimpleNamespace(
            correlation_token=spec.correlation_token,
            envelope_path=spec.envelope_path,
            lane_key=str(spec.lane_key or ""),
            launched_cwd=Path(spec.worktree).resolve(),
            pane_id="pane-{0}".format(self.launches),
            pane_role=str(spec.pane_role or ""),
        )

    def resubmit(self, handle: SimpleNamespace, *args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("scripted launcher never resubmits")

    def cancel(self, handle: object, deadline: float) -> None:
        del handle, deadline

    def poll(self, handle: object) -> object:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s

    def retain(self, handle: object) -> None:
        del handle


class Bench:
    def __init__(
        self,
        root: str,
        responder: Responder,
        *,
        role_vendors: Mapping[str, str | None] | None = None,
    ) -> None:
        base = Path(root)
        self.product = base / "product"
        self.state = base / "state"
        self.state.mkdir(mode=0o700)
        self.product.mkdir()
        git(self.product, "init", "-b", "main")
        git(self.product, "config", "user.email", "factory@example.test")
        git(self.product, "config", "user.name", "factory")
        (self.product / OUTPUT).parent.mkdir(parents=True)
        (self.product / OUTPUT).write_text("def accept(q):\n    return q\n", encoding="utf-8")
        git(self.product, "add", ".")
        git(self.product, "commit", "-m", "seed")
        self.head = git(self.product, "rev-parse", "HEAD")
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.launcher = ScriptedLauncher(responder)
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, self.launcher),
            self.state,
            self.target,
            ROLE_ROUTES,
            role_vendors=role_vendors,
        )
        self.actor.step = lambda lane_id, message, detail="": None

    def checkout(self, lane_id: str, role: str) -> Path:
        return self.state / "worktrees" / "run-1" / lane_id / role / "checkout"
