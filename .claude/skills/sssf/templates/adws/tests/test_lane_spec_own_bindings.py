"""A lane's spec carries only the bindings of the claims it discharges.

`_obligations` used to embed the plan-wide `rendered_bindings` list -- every
entry, for every claim in the plan -- into each tests lane's spec. Editing any
one claim's binding therefore re-digested every tests lane that carried the
list, so `apply_amendment`, which resets a lane whose `lane_projection_digest`
changed, reset lanes the amendment never named.

On FDAdb run d246ae9592be478396ad5146a89f00ae (2026-09-09) an r1 -> r2
amendment edited one claim's binding on the FAQ lanes. `lane-geo-subset-tests`,
whose own requirements were byte-identical, went MERGED -> PLANNED and the
build lane behind it was un-merged, while the amendment artifact recorded
`invalidated_inputs: []`.

The other half of the invariant is that nothing is dropped: a lane's projected
bindings must equal *exactly* its claims' bindings, no fewer. That is asserted
here and inside `_assert_ingress_projection_is_total`.
"""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_contract_minimal.json"


def _binding(claim_id: str, text: str) -> dict:
    return {
        "binding_id": "bind-{}".format(claim_id),
        "claim_id": claim_id,
        "element_id": claim_id,
        "text": text,
        "text_sha256": "0" * 64,
        "claim_sha256": "1" * 64,
    }


def _two_pair_ir() -> dict:
    """Four lanes, five claims, one rendered binding per claim."""
    ir = json.loads(FIXTURE.read_text(encoding="utf-8"))

    ir["requirements"].append({
        "requirement_id": "req-t2",
        "text": "Tests lane writes tests/test_c.py covering the second contract.",
        "source_ids": ["src-a"],
        "surface": [{"path": "tests/test_c.py", "mutation": "written"}],
        "effects": [{"effect": "network", "disposition": "none"}],
    })
    ir["requirements"].append({
        "requirement_id": "req-b2",
        "text": "Build lane writes src/c.py implementing the second contract.",
        "source_ids": ["src-b"],
        "surface": [{"path": "src/c.py", "mutation": "written"}],
        "effects": [{"effect": "network", "disposition": "none"}],
    })
    ir["claims"].append({
        "claim_id": "claim-t2",
        "kind": "behavior",
        "subject": "tests/test_c.py",
        "predicate": "covers",
        "object": "the second contract",
        "observation_seam": (
            "src/c.py is imported by its public module path; the contract it "
            "exports is what a case asserts on."),
        "polarity": "positive",
        "source_requirement_ids": ["req-t2"],
        "source_ids": ["src-a"],
        "verifier_ids": ["verify-t2"],
        "rendered_binding_ids": ["bind-claim-t2"],
    })
    ir["claims"].append({
        "claim_id": "claim-c1",
        "kind": "behavior",
        "subject": "src/c.py",
        "predicate": "implements",
        "object": "the second happy path",
        "polarity": "positive",
        "value": 1,
        "unit": "case",
        "mutation_kinds": ["actor-replacement"],
        "source_requirement_ids": ["req-b2"],
        "source_ids": ["src-b"],
        "verifier_ids": ["verify-b2"],
        "rendered_binding_ids": ["bind-claim-c1"],
    })
    ir["fixtures"].append({
        "fixture_id": "fx-t2-unique-token",
        "record_selector": "selector-t2-unique",
        "observed_value": "observed-t2-unique",
        "consumer_obligation": "obligation-t2-unique",
        "prohibited_behavior": "prohibited-t2-unique",
        "meaning": "meaning-t2-unique",
        "path": "docs/a.md",
        "source_id": "src-a",
        "affected_lane_ids": ["lane-t2"],
        "seam_ids": ["seam-second"],
        "verifier_ids": ["verify-t2"],
        "producer_metadata": {"origin": "tests"},
    })
    ir["fixtures"].append({
        "fixture_id": "fx-b2-unique-token",
        "record_selector": "selector-b2-unique",
        "observed_value": "observed-b2-unique",
        "consumer_obligation": "obligation-b2-unique",
        "prohibited_behavior": "prohibited-b2-unique",
        "meaning": "meaning-b2-unique",
        "path": "docs/b.md",
        "source_id": "src-b",
        "affected_lane_ids": ["lane-b2"],
        "seam_ids": ["seam-second"],
        "verifier_ids": ["verify-b2"],
        "producer_metadata": {"origin": "build"},
    })
    ir["seams"].append({
        "seam_id": "seam-second",
        "producer": "tests/test_c.py",
        "consumer": "src/c.py",
        "contract": "src/c.py satisfies the contract tests/test_c.py records.",
        "claim_ids": ["claim-t2", "claim-c1"],
        "fixture_ids": ["fx-t2-unique-token", "fx-b2-unique-token"],
        "requirement_ids": ["req-t2", "req-b2"],
        "source_ids": ["src-a", "src-b"],
        "verifier_ids": ["verify-t2", "verify-b2"],
    })
    ir["lanes"].append({
        "lane_id": "lane-t2",
        "title": "Author the second contract's tests",
        "lane_kind": "tests",
        "execution_context": ".",
        "depends_on": [],
        "requirement_ids": ["req-t2"],
        "verifier_ids": ["verify-t2"],
        "claim_ids": ["claim-t2"],
        "seam_ids": ["seam-second"],
        "fixture_ids": ["fx-t2-unique-token"],
    })
    ir["lanes"].append({
        "lane_id": "lane-b2",
        "title": "Implement the second contract",
        "lane_kind": "build",
        "execution_context": ".",
        "depends_on": ["lane-t2"],
        "requirement_ids": ["req-b2"],
        "verifier_ids": ["verify-b2"],
        "claim_ids": ["claim-c1"],
        "seam_ids": ["seam-second"],
        "fixture_ids": ["fx-b2-unique-token"],
    })
    ir["verifiers"].append({
        "verifier_id": "verify-t2",
        "lane_ids": ["lane-t2"],
        "requirement_ids": ["req-t2"],
        "source_ids": ["src-a"],
        "fixture_ids": ["fx-t2-unique-token"],
        "seam_ids": ["seam-second"],
        "claim_ids": ["claim-t2"],
        "command": "pytest tests/test_c.py -q",
        "min_executed": 2,
        "oracle": "the second contract holds",
        "falsifiability": {
            "mutation": "drop the happy-path assertion",
            "expected_failure": "the covering case fails",
        },
        "independent": True,
        "test_strength": {
            "requirement_coverage": [
                {"requirement_id": "req-t2", "case_ids": ["case-second"]}
            ],
            "negative_control": {
                "kind": "controlled_mutation",
                "mutation": "invert polarity",
                "expected_failure": "claim-t2 fails",
            },
        },
    })
    ir["verifiers"].append({
        "verifier_id": "verify-b2",
        "lane_ids": ["lane-b2"],
        "requirement_ids": ["req-b2"],
        "source_ids": ["src-b"],
        "fixture_ids": ["fx-b2-unique-token"],
        "seam_ids": ["seam-second"],
        "claim_ids": ["claim-c1"],
        "command": "pytest tests/test_c.py -q",
        "min_executed": 2,
        "oracle": "src/c.py meets its claim",
        "falsifiability": {
            "mutation": "accept invalid input",
            "expected_failure": "the covering case fails",
        },
        "independent": True,
    })
    ir["extensions"]["maestro"]["outputs"]["lane-t2"] = ["tests/test_c.py"]
    ir["extensions"]["maestro"]["outputs"]["lane-b2"] = ["src/c.py"]
    ir["rendered_bindings"] = [
        _binding("claim-t", "tests/test_t.py exercises the public contract."),
        _binding("claim-b1", "src/b.py implements the happy path."),
        _binding("claim-b2", "src/b.py refuses invalid input."),
        _binding("claim-t2", "tests/test_c.py exercises the second contract."),
        _binding("claim-c1", "src/c.py implements the second happy path."),
    ]
    return ir


def _compiled_lanes(ir: dict) -> dict:
    from adw_modules import plan_canonical
    from adw_modules.plan_compiler import compile_plan
    from adw_modules.plan_contract_ingress import project_draft

    draft = project_draft(ir, Path("."))
    compiled = compile_plan(plan_canonical.canonicalize(draft))
    return {lane.lane_id: lane for lane in compiled.lanes}


def _projected_bindings(ir: dict, lane_id: str) -> list:
    from adw_modules.plan_contract_ingress import project_draft

    draft = project_draft(ir, Path("."))
    for lane in draft["lanes"]:
        if lane["id"] == lane_id:
            return list(
                (lane["spec"].get("obligations") or {}).get("rendered_bindings")
                or [])
    raise KeyError(lane_id)


class LaneSpecOwnBindingsTests(unittest.TestCase):
    def test_a_lane_carries_exactly_its_claims_bindings(self) -> None:
        ir = _two_pair_ir()
        # lane-t discharges claim-t and is paired with lane-b (claim-b1,
        # claim-b2), whose obligations its spec also carries.
        self.assertEqual(
            [item["binding_id"] for item in _projected_bindings(ir, "lane-t")],
            ["bind-claim-t", "bind-claim-b1", "bind-claim-b2"])
        self.assertEqual(
            [item["binding_id"] for item in _projected_bindings(ir, "lane-t2")],
            ["bind-claim-t2", "bind-claim-c1"])

    def test_editing_one_lanes_claim_leaves_the_others_byte_identical(self) -> None:
        before = _compiled_lanes(_two_pair_ir())

        amended = _two_pair_ir()
        for binding in amended["rendered_bindings"]:
            if binding["claim_id"] == "claim-b1":
                binding["text"] = "src/b.py implements the happy path, exactly once."
                binding["text_sha256"] = "2" * 64
        after = _compiled_lanes(amended)

        self.assertEqual(sorted(before), sorted(after))
        changed = "lane-t"
        for lane_id in before:
            if lane_id == changed:
                continue
            self.assertEqual(
                before[lane_id].spec_digest, after[lane_id].spec_digest,
                "{} spec_digest moved on an amendment it does not "
                "discharge".format(lane_id))
            self.assertEqual(
                before[lane_id].lane_projection_digest,
                after[lane_id].lane_projection_digest,
                "{} lane_projection_digest moved on an amendment it does "
                "not discharge".format(lane_id))
        self.assertNotEqual(
            before[changed].spec_digest, after[changed].spec_digest)
        self.assertNotEqual(
            before[changed].lane_projection_digest,
            after[changed].lane_projection_digest)

    def test_editing_a_binding_no_lane_discharges_moves_nothing(self) -> None:
        before = _compiled_lanes(_two_pair_ir())

        amended = _two_pair_ir()
        amended["rendered_bindings"].append({
            "binding_id": "bind-orphan",
            "claim_id": "claim-does-not-exist",
            "element_id": "req-t",
            "text": "a binding no lane's claim names",
            "text_sha256": "3" * 64,
            "claim_sha256": "4" * 64,
        })
        after = _compiled_lanes(amended)

        for lane_id in before:
            self.assertEqual(
                before[lane_id].spec_digest, after[lane_id].spec_digest)
            self.assertEqual(
                before[lane_id].lane_projection_digest,
                after[lane_id].lane_projection_digest)

    def test_a_dropped_binding_is_refused_not_silently_projected(self) -> None:
        """The other half: a projection that drops a needed binding fails."""
        from unittest import mock
        from adw_modules import plan_contract_ingress as ingress

        real = ingress._obligations

        def drop_one(ir, lane, verifier, projected_build_lanes):
            packed = real(ir, lane, verifier, projected_build_lanes)
            packed["rendered_bindings"] = packed["rendered_bindings"][1:]
            return packed

        ir = _two_pair_ir()
        with mock.patch.object(ingress, "_obligations", drop_one):
            with self.assertRaises(ingress.IngressProjectionIncomplete) as caught:
                ingress.project_draft(ir, Path("."))
        self.assertIn("rendered_bindings", str(caught.exception))


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


def _compiled(ir: dict, *, revision: int, ref: str):
    from adw_modules import plan_canonical
    from adw_modules.plan_compiler import compile_plan
    from adw_modules.plan_contract_ingress import project_draft

    draft = project_draft(ir, Path("."))
    return compile_plan(
        plan_canonical.canonicalize(draft),
        plan_revision=revision,
        plan_artifact_ref=ref,
    )


class AmendmentDoesNotResetAnUnnamedLaneTest(unittest.TestCase):
    """Through `apply_factory_amendment`: an untouched merged lane survives."""

    def setUp(self) -> None:
        from adw_modules import git_publication as gitpub
        from adw_modules.lifecycle import ArtifactStore
        from adw_modules.runtime_state import RuntimeStateRoot

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.store.close)

    def _force_stage(self, run_id: str, lane_id: str, stage: str) -> None:
        self.store.conn.execute(
            "UPDATE lane_state SET stage=? WHERE run_id=? AND lane_id=?",
            (stage, run_id, lane_id),
        )
        self.store.conn.commit()

    def test_an_amendment_on_one_pairs_claim_leaves_the_other_pair_merged(self) -> None:
        from adw_modules import scheduler as sch
        from adw_modules import scheduler_types as st

        run_id = "run-ownbind"
        base = _compiled(_two_pair_ir(), revision=1, ref="plan:v1")
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=base,
            runtime=self.runtime,
            target=self.target,
        )
        # lane-t2 / lane-b2 are the finished pair the amendment never names.
        self._force_stage(run_id, "lane-t2", st.LaneStage.MERGED.value)
        self._force_stage(run_id, "lane-b2", st.LaneStage.MERGED.value)

        amended_ir = _two_pair_ir()
        for binding in amended_ir["rendered_bindings"]:
            if binding["claim_id"] == "claim-b1":
                binding["text"] = "src/b.py implements the happy path, exactly once."
                binding["text_sha256"] = "2" * 64
        amended = _compiled(amended_ir, revision=2, ref="plan:v2")

        record = sch.apply_factory_amendment(
            self.store,
            run_id,
            amended,
            runtime=self.runtime,
            target=self.target,
        )

        resets = {
            item["lane_id"]: item["to_stage"]
            for item in record.payload.get("resets") or []
        }
        self.assertEqual(resets.get("lane-t2"), st.LaneStage.TESTS_SEALED.value)
        self.assertEqual(resets.get("lane-b2"), st.LaneStage.READY_TO_MERGE.value)
        self.assertNotEqual(resets.get("lane-t2"), st.LaneStage.PLANNED.value)
        self.assertIsNot(
            self.store.lane_stage(run_id, "lane-t2"), st.LaneStage.PLANNED)
        self.assertIsNot(
            self.store.lane_stage(run_id, "lane-b2"), st.LaneStage.PLANNED)
        # The pair the amendment does touch is reset, as it must be.
        self.assertEqual(resets.get("lane-t"), st.LaneStage.PLANNED.value)


if __name__ == "__main__":
    unittest.main()
