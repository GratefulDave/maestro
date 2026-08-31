"""Objective plan-compiler checks. No git, reachability, or review-node fixtures."""

from __future__ import annotations

import json
import unittest

from adw_modules.plan_compiler import compile_plan
from adw_modules.plan_model import SCHEMA_VERSION, PlanCompileError
from adw_modules import plan_validate as pv
from adw_modules.scheduler_types import (
    CompiledPlan,
    LaneProjection,
    digest_bytes,
    digest_canonical,
    lane_projection_digest,
    topological_integration_order,
)


def _dump(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _lane(
    lane_id: str,
    *,
    needs=(),
    outputs=None,
    spec=None,
    acceptance=None,
    lane_kind=None,
) -> dict:
    payload = {
        "id": lane_id,
        "needs": list(needs),
        "outputs": list(
            outputs if outputs is not None else ["src/{0}.py".format(lane_id)]
        ),
        "spec": dict(spec if spec is not None else {"intent": lane_id}),
        "acceptance": list(
            acceptance
            if acceptance is not None
            else ["{0} produces its declared file".format(lane_id)]
        ),
    }
    if lane_kind is not None:
        payload["lane_kind"] = lane_kind
    return payload


def _plan(*lanes: dict, **extra) -> dict:
    payload = {"schema_version": SCHEMA_VERSION, "lanes": list(lanes)}
    payload.update(extra)
    return payload


def _codes(exc: PlanCompileError) -> tuple:
    return tuple(item.code for item in exc.refusals)


def _lane_of(compiled: CompiledPlan, lane_id: str) -> LaneProjection:
    for lane in compiled.lanes:
        if lane.lane_id == lane_id:
            return lane
    raise KeyError(lane_id)


class ObjectiveCompilerTests(unittest.TestCase):
    def test_two_dependent_lanes_compile_with_store_kahn_order(self):
        authored = _plan(
            _lane("lane-b", needs=("lane-a",), outputs=["src/b.py"]),
            _lane("lane-a", outputs=["src/a.py"]),
        )
        compiled = compile_plan(_dump(authored), plan_revision=1)

        self.assertIsInstance(compiled, CompiledPlan)
        self.assertIsInstance(compiled.lanes[0], LaneProjection)
        self.assertEqual(("lane-a", "lane-b"), compiled.integration_order)
        self.assertEqual(
            compiled.integration_order,
            topological_integration_order(compiled.lanes),
        )
        self.assertEqual(
            ("lane-a", "lane-b"), tuple(lane.lane_id for lane in compiled.lanes)
        )
        self.assertEqual(("lane-a",), _lane_of(compiled, "lane-b").needs)
        self.assertEqual(compiled.plan_digest, digest_bytes(compiled.plan_bytes))
        self.assertEqual(1, compiled.plan_revision)
        lane_a = _lane_of(compiled, "lane-a")
        self.assertEqual(digest_canonical({"intent": "lane-a"}), lane_a.spec_digest)
        self.assertEqual(
            lane_projection_digest(
                lane_a.spec_digest, lane_a.needs, lane_a.declared_outputs
            ),
            lane_a.lane_projection_digest,
        )

    def test_canonical_digest_ignores_authored_whitespace_and_lane_order(self):
        compact = compile_plan(
            _dump(_plan(_lane("lane-b", needs=("lane-a",)), _lane("lane-a")))
        )
        pretty = compile_plan(
            json.dumps(
                _plan(_lane("lane-a"), _lane("lane-b", needs=("lane-a",))),
                indent=2,
            ).encode("utf-8")
        )
        self.assertEqual(compact.plan_digest, pretty.plan_digest)
        self.assertEqual(compact.plan_bytes, pretty.plan_bytes)

    def test_unknown_needs_id_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(_plan(_lane("lane-a", needs=("missing",)))))
        self.assertIn(pv.NEEDS_UNKNOWN, _codes(caught.exception))

    def test_cycle_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(
                    _plan(
                        _lane("lane-a", needs=("lane-b",)),
                        _lane("lane-b", needs=("lane-a",)),
                    )
                )
            )
        self.assertIn(pv.GRAPH_CYCLE, _codes(caught.exception))

    def test_absolute_empty_dot_and_parent_paths_are_refused(self):
        cases = (
            "/abs/a.py",
            "",
            "src/./a.py",
            "src/../a.py",
            "../a.py",
            "src//a.py",
        )
        for path in cases:
            with self.subTest(path=path):
                with self.assertRaises(PlanCompileError) as caught:
                    compile_plan(_dump(_plan(_lane("lane-a", outputs=[path]))))
                self.assertIn(pv.OUTPUT_PATH_INVALID, _codes(caught.exception))

    def test_globs_and_directories_are_refused(self):
        for path in ("src/*.py", "src/?", "src/[a].py", "src/pkg/"):
            with self.subTest(path=path):
                with self.assertRaises(PlanCompileError) as caught:
                    compile_plan(_dump(_plan(_lane("lane-a", outputs=[path]))))
                self.assertIn(pv.OUTPUT_PATH_INVALID, _codes(caught.exception))

    def test_equal_and_ancestor_outputs_conflict_across_lanes(self):
        equal = _plan(
            _lane("lane-a", outputs=["src/shared.py"]),
            _lane("lane-b", outputs=["src/shared.py"]),
        )
        nested = _plan(
            _lane("lane-a", outputs=["src/pkg/a.py"]),
            _lane("lane-b", outputs=["src/pkg"]),
        )
        for payload in (equal, nested):
            with self.subTest(payload=payload):
                with self.assertRaises(PlanCompileError) as caught:
                    compile_plan(_dump(payload))
                self.assertIn(pv.OUTPUT_OWNERSHIP_CONFLICT, _codes(caught.exception))

    def test_missing_acceptance_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(_plan(_lane("lane-a", acceptance=[]))))
        self.assertIn(pv.ACCEPTANCE_MISSING, _codes(caught.exception))

    def test_legacy_schema_and_runtime_policy_fields_are_refused(self):
        legacy = _plan(_lane("lane-a"))
        legacy["schema_version"] = "maestro-plan.v5"
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(legacy))
        self.assertIn(pv.SCHEMA_INVALID, _codes(caught.exception))

        policy = _plan(_lane("lane-a"), retry_ceiling=3)
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(policy))
        self.assertIn(pv.SCHEMA_INVALID, _codes(caught.exception))

    def test_synthetic_review_node_id_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(_plan(_lane("lane-a::review"))))
        self.assertIn(pv.REVIEW_NODE_FORBIDDEN, _codes(caught.exception))

    def test_projection_digest_changes_with_spec_needs_and_outputs(self):
        base = compile_plan(
            _dump(_plan(_lane("lane-a"), _lane("lane-b", needs=("lane-a",))))
        )
        spec_changed = compile_plan(
            _dump(
                _plan(
                    _lane("lane-a", spec={"intent": "changed"}),
                    _lane("lane-b", needs=("lane-a",)),
                )
            )
        )
        needs_changed = compile_plan(_dump(_plan(_lane("lane-a"), _lane("lane-b"))))
        outputs_changed = compile_plan(
            _dump(
                _plan(
                    _lane("lane-a", outputs=["src/renamed.py"]),
                    _lane("lane-b", needs=("lane-a",)),
                )
            )
        )
        base_a = _lane_of(base, "lane-a")
        self.assertNotEqual(
            base_a.lane_projection_digest,
            _lane_of(spec_changed, "lane-a").lane_projection_digest,
        )
        self.assertNotEqual(
            base_a.spec_digest, _lane_of(spec_changed, "lane-a").spec_digest
        )
        self.assertNotEqual(
            _lane_of(base, "lane-b").lane_projection_digest,
            _lane_of(needs_changed, "lane-b").lane_projection_digest,
        )
        self.assertEqual(
            _lane_of(base, "lane-b").spec_digest,
            _lane_of(needs_changed, "lane-b").spec_digest,
        )
        self.assertNotEqual(
            base_a.lane_projection_digest,
            _lane_of(outputs_changed, "lane-a").lane_projection_digest,
        )
        self.assertEqual(
            base_a.spec_digest, _lane_of(outputs_changed, "lane-a").spec_digest
        )
        self.assertNotEqual(base.plan_digest, spec_changed.plan_digest)

    def test_kahn_picks_one_lexicographically_smallest_ready_lane(self):
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane("lane-d"),
                    _lane("lane-b", needs=("lane-a",)),
                    _lane("lane-a"),
                )
            )
        )
        self.assertEqual(("lane-a", "lane-b", "lane-d"), compiled.integration_order)
        self.assertEqual(
            compiled.integration_order,
            topological_integration_order(compiled.lanes),
        )

    def test_independent_ready_lanes_sort_by_lane_id(self):
        compiled = compile_plan(
            _dump(_plan(_lane("lane-z"), _lane("lane-m"), _lane("lane-a")))
        )
        self.assertEqual(("lane-a", "lane-m", "lane-z"), compiled.integration_order)

    def test_absent_lane_kind_keeps_unified_projection_digest(self):
        compiled = compile_plan(_dump(_plan(_lane("lane-a"))))
        lane = _lane_of(compiled, "lane-a")
        self.assertIsNone(lane.lane_kind)
        self.assertEqual(
            lane.lane_projection_digest,
            lane_projection_digest(lane.spec_digest, lane.needs, lane.declared_outputs),
        )

    def test_authored_lane_kind_changes_projection_digest(self):
        unified = compile_plan(_dump(_plan(_lane("lane-a"))))
        tests = compile_plan(_dump(_plan(_lane("lane-a", lane_kind="tests"))))
        tests_lane = _lane_of(tests, "lane-a")
        self.assertEqual(tests_lane.lane_kind, "tests")
        self.assertEqual(
            tests_lane.lane_projection_digest,
            lane_projection_digest(
                tests_lane.spec_digest,
                tests_lane.needs,
                tests_lane.declared_outputs,
                lane_kind="tests",
            ),
        )
        self.assertNotEqual(
            _lane_of(unified, "lane-a").lane_projection_digest,
            tests_lane.lane_projection_digest,
        )
        self.assertNotEqual(unified.plan_digest, tests.plan_digest)

    def test_unknown_lane_kind_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(_plan(_lane("lane-a", lane_kind="review"))))
        self.assertIn(pv.SCHEMA_INVALID, _codes(caught.exception))

    def test_build_lane_requires_exactly_one_tests_dependency(self):
        ok = compile_plan(
            _dump(
                _plan(
                    _lane("lane-tests", lane_kind="tests"),
                    _lane("lane-build", needs=("lane-tests",), lane_kind="build"),
                )
            )
        )
        self.assertEqual(_lane_of(ok, "lane-build").lane_kind, "build")
        with self.assertRaises(PlanCompileError) as missing:
            compile_plan(_dump(_plan(_lane("lane-build", lane_kind="build"))))
        self.assertIn(pv.BUILD_LANE_NEEDS, _codes(missing.exception))
        self.assertEqual(missing.exception.refusals[0].pointer, "/lanes/0/needs")
        with self.assertRaises(PlanCompileError) as extra:
            compile_plan(
                _dump(
                    _plan(
                        _lane("lane-t1", lane_kind="tests"),
                        _lane("lane-t2", lane_kind="tests", outputs=["src/t2.py"]),
                        _lane(
                            "lane-build",
                            needs=("lane-t1", "lane-t2"),
                            lane_kind="build",
                        ),
                    )
                )
            )
        self.assertIn(pv.BUILD_LANE_NEEDS, _codes(extra.exception))
        self.assertEqual(extra.exception.refusals[0].pointer, "/lanes/2/needs/1")

    def test_build_lane_may_also_depend_on_build_not_untyped(self):
        compile_plan(
            _dump(
                _plan(
                    _lane("lane-tests", lane_kind="tests"),
                    _lane(
                        "lane-lib",
                        needs=("lane-tests",),
                        outputs=["src/lib.py"],
                        lane_kind="build",
                    ),
                    _lane(
                        "lane-app",
                        needs=("lane-tests", "lane-lib"),
                        outputs=["src/app.py"],
                        lane_kind="build",
                    ),
                )
            )
        )
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(
                    _plan(
                        _lane("lane-tests", lane_kind="tests"),
                        _lane("lane-legacy"),
                        _lane(
                            "lane-build",
                            needs=("lane-tests", "lane-legacy"),
                            lane_kind="build",
                        ),
                    )
                )
            )
        self.assertIn(pv.BUILD_LANE_NEEDS, _codes(caught.exception))
        self.assertEqual(caught.exception.refusals[0].pointer, "/lanes/2/needs/1")



if __name__ == "__main__":
    unittest.main()
