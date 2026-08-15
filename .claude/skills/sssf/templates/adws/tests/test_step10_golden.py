"""§13.1 golden scenario: clean acceptance and conflict/rescue."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import lifecycle as lc
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules import worktree as wt
from adw_modules.launcher import FakeLauncher, LaunchSpec, PollState


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, run_id: str) -> tuple[Path, Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "maestro@example.invalid")
    git(repo, "config", "user.name", "Maestro Golden")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    branch = "integration/{}".format(run_id)
    integration = root / "integration"
    git(repo, "worktree", "add", "-q", "-b", branch, str(integration), "HEAD")
    return repo, integration, branch


def green() -> wt.GateResult:
    return wt.GateResult(label="integration", scope="integration", selector="all",
                         command=("pytest",), exit_code=0, green=True,
                         counts={"passed": 3, "failed": 0, "skipped": 0, "errored": 0})


def node(node_id: str, *, depth: int = 0, needs: tuple[str, ...] = (),
         outputs: tuple[str, ...] = ()) -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=needs, outputs=outputs,
                       command=("golden", node_id), expects_changes=True)


class GoldenFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = lc.LifecycleStore(self.root / "lifecycle.db")
        self.addCleanup(self.store.close)
        self.launcher = FakeLauncher()
        self.handles = {}
        self.intervals = {}
        self.integration_gate_calls = 0

    def config(self) -> st.SchedulerConfig:
        return st.SchedulerConfig(concurrency=2, node_timeout_s=30,
                                  turn_timeout_s=10,
                                  final_acceptance_timeout_s=30,
                                  backstop_t_s=120, semantic_ceiling=2)

    def build(self, run_id: str, nodes, body):
        repo, integration, branch = make_repo(self.root, run_id)

        def run_node(attempt, plan_node, record, retry_prompt, on_launch,
                     cancel_requested):
            prompt = self.root / (plan_node.node_id + ".prompt")
            envelope = self.root / (plan_node.node_id + ".envelope")
            prompt.write_text(plan_node.node_id)
            spec = LaunchSpec(
                correlation_token="{}-{}-{}".format(run_id, plan_node.node_id, record.attempt_no),
                worktree=attempt.path, prompt_path=prompt,
                envelope_path=envelope, route="omp", model="fixture",
                effort="low", profile="fixture", session_dir=self.root / "sessions" / plan_node.node_id,
            )
            handle = self.launcher.launch(spec)
            self.handles[plan_node.node_id] = handle
            on_launch(None)
            started = time.monotonic()
            body(attempt, plan_node)
            ended = time.monotonic()
            self.intervals[plan_node.node_id] = (started, ended)
            self.launcher.complete(spec.correlation_token)
            polled = self.launcher.poll(handle)
            self.assertIs(polled.state, PollState.EXITED)
            return sch.NodeExecution(envelope_parsed=True,
                                     exit_code=polled.exit_code or 0)

        def node_gate(attempt, plan_node, phase, cancel_requested):
            return green()

        def integration_gate(path, specs, cancel_requested):
            self.integration_gate_calls += 1
            return green()

        def quiesce_attempt(record, phase):
            self.assertEqual(record.run_id, run_id)

        deps = sch.SchedulerDeps(
            store=self.store, repo=repo, integration_path=integration,
            integration_branch=branch, worktrees_root=self.root / "worktrees",
            scratch_root=self.root / "scratch", run_node=run_node,
            run_gate=node_gate, run_integration_gate=integration_gate,
            provision=self.launcher.provision,
            quiesce_attempt=quiesce_attempt,
            kill_attempt=lambda *args: None,
        )
        scheduler = sch.Scheduler(run_id, nodes, self.config(), deps,
                                  plan_digest="golden-digest")
        self.addCleanup(scheduler.shutdown)
        return scheduler, repo, integration, deps


class GoldenScenarioTest(GoldenFixture):
    def test_run_a_accepts_with_real_concurrency_order_and_trace(self):
        barrier = threading.Barrier(2, timeout=3)
        dependent_snapshot = []
        nodes = [node("a", outputs=("a.py",)),
                 node("b", outputs=("b.py",)),
                 node("c", depth=1, needs=("a", "b"), outputs=("c.py",))]

        def body(attempt, plan_node):
            if plan_node.node_id in ("a", "b"):
                barrier.wait()
            else:
                dependent_snapshot.append({
                    item.node_id: self.store.get_node("golden-a", item.node_id).state.value
                    for item in nodes
                })
            (attempt.path / (plan_node.node_id + ".py")).write_text(plan_node.node_id + "\n")

        scheduler, repo, integration, deps = self.build("golden-a", nodes, body)
        report = scheduler.run()
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(self.integration_gate_calls, 1)
        self.assertEqual(dependent_snapshot, [{"a": "MERGED", "b": "MERGED", "c": "RUNNING"}])
        overlap = min(self.intervals["a"][1], self.intervals["b"][1]) - max(self.intervals["a"][0], self.intervals["b"][0])
        self.assertGreater(overlap, 0)
        launched_cwds = {handle.launched_cwd for handle in self.handles.values()}
        self.assertEqual(len(launched_cwds), 3)
        worktrees_root = (self.root / "worktrees").resolve()
        self.assertTrue(all(str(cwd).startswith(str(worktrees_root))
                            for cwd in launched_cwds))
        self.assertTrue(all(node_id in handle.correlation_token
                            for node_id, handle in self.handles.items()))
        subjects = git(integration, "log", "--format=%s", "--merges").splitlines()
        self.assertEqual([text.split()[-1] for text in reversed(subjects)], ["a", "b", "c"])
        transitions = self.store.audit_transitions("golden-a")
        # Nine node transitions plus acceptance-start and outcome declaration.
        self.assertEqual(len(transitions), 11)
        by_node = {
            node_id: [row["to_state"] for row in transitions
                      if row.get("node_id") == node_id]
            for node_id in ("a", "b", "c")
        }
        self.assertEqual(by_node, {
            "a": ["RUNNING", "VERIFIED", "MERGED"],
            "b": ["RUNNING", "VERIFIED", "MERGED"],
            "c": ["RUNNING", "VERIFIED", "MERGED"],
        })
        self.assertEqual(git(integration, "status", "--porcelain"), "")

    def test_run_b_blocks_then_real_skip_and_resume_accepts(self):
        nodes = [node("a", outputs=("*.py",)),
                 node("b", outputs=("shared.*",)),
                 node("c", depth=1, needs=("b",), outputs=("c.py",))]

        def body(attempt, plan_node):
            target = "shared.py" if plan_node.node_id in ("a", "b") else "c.py"
            (attempt.path / target).write_text(plan_node.node_id + "\n")

        scheduler, repo, integration, deps = self.build("golden-b", nodes, body)
        blocked = scheduler.run()
        self.assertIs(blocked.outcome, st.RunOutcome.BLOCKED)
        self.assertIs(self.store.get_node("golden-b", "b").block_reason,
                      st.BlockReason.MERGE_CONFLICT)
        self.assertEqual(self.store.get_node("golden-b", "c").state,
                         st.NodeState.PENDING)
        self.assertIn("c", self.store.upstream_blocked("golden-b"))
        self.assertEqual((integration / "shared.py").read_text(), "a\n")
        self.assertEqual(git(integration, "status", "--porcelain"), "")

        accepted_sha = git(integration, "rev-parse", "HEAD")
        self.store.skip("golden-b", "b", accept_sha=accepted_sha,
                        repo_path=integration)
        self.store.resume_run("golden-b")
        resumed = sch.Scheduler("golden-b", nodes, self.config(), deps,
                                plan_digest="golden-digest")
        self.addCleanup(resumed.shutdown)
        accepted = resumed.run()
        self.assertIs(accepted.outcome, st.RunOutcome.ACCEPTED,
                      (accepted, {item.node_id: self.store.get_node("golden-b", item.node_id)
                                  for item in nodes}))
        self.assertEqual(self.store.get_node("golden-b", "c").state,
                         st.NodeState.MERGED)
        self.assertEqual(self.store.latest_outcome("golden-b"),
                         st.RunOutcome.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
