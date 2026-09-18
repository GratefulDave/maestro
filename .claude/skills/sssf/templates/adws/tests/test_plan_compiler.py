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

_UNSET = object()

_INTERFACE_ENTRY = {
    "kind": "callable",
    "module": "src/b.py",
    "name": "build_contract",
    "signature": {
        "parameters": [{"name": "record", "type": "Mapping"}],
        "returns": "dict",
    },
    "errors": ["ValueError"],
    "consumed_by": {"deferred_to": "WP2 wires build_contract into the CLI"},
}


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
        for path in ("src/*.py", "src/?", "src/pkg/", "src/a*/b.py", "a?.py"):
            with self.subTest(path=path):
                with self.assertRaises(PlanCompileError) as caught:
                    compile_plan(_dump(_plan(_lane("lane-a", outputs=[path]))))
                self.assertIn(pv.OUTPUT_PATH_INVALID, _codes(caught.exception))

    def test_a_bracketed_dynamic_route_is_an_ordinary_file_path(self):
        """`[slug].astro` is a filename, not a glob, and lanes must own it.

        Astro, Next.js, SvelteKit, Remix and Nuxt all spell a dynamic route
        with brackets, so refusing the character made every dynamic page in
        those projects unownable by any lane -- and a file no lane can own is
        one no builder may repair and no reviewer holds a contract over, which
        is the defect the outputs rule exists to prevent. FDAdb's paid-panel
        regression lives in a helper whose ten callers are all `[slug].astro`.

        Safe because nothing globs a declared output: ownership validation is
        set membership on the exact strings, `permissions._matches` compares
        byte equality unless the pattern itself carries `*` or `?`, and
        `outputs_conflict` is prefix arithmetic.
        """
        paths = [
            "src/pages/faers/drugs/[slug].astro",
            "src/pages/maude/product-codes/[code]/trend.astro",
            "app/routes/[id].tsx",
        ]
        compiled = compile_plan(_dump(_plan(_lane("lane-a", outputs=paths))))
        # Sorted, because the compiler orders declared outputs.
        self.assertEqual(sorted(compiled.lanes[0].declared_outputs), sorted(paths))

    def test_two_dynamic_routes_still_conflict_when_equal(self):
        # Admitting the character must not weaken one-owner-per-path.
        payload = _plan(
            _lane("lane-a", outputs=["src/pages/[slug].astro"]),
            _lane("lane-b", outputs=["src/pages/[slug].astro"]),
        )
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(payload))
        self.assertIn(pv.OUTPUT_OWNERSHIP_CONFLICT, _codes(caught.exception))

    def test_a_bracket_never_matches_a_sibling_it_would_glob(self):
        # If anything ever globbed these, `[ab].astro` would own `a.astro`.
        # Ownership is byte-exact, so the two are unrelated paths and a lane
        # declaring both is admitted rather than refused as a conflict.
        paths = ["src/pages/[ab].astro", "src/pages/a.astro"]
        compiled = compile_plan(_dump(_plan(_lane("lane-a", outputs=paths))))
        self.assertEqual(sorted(compiled.lanes[0].declared_outputs), sorted(paths))

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
                    _lane(
                        "lane-build",
                        needs=("lane-tests",),
                        lane_kind="build",
                        spec={"intent": "build", "interface": [_INTERFACE_ENTRY]},
                    ),
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
                            spec={
                                "intent": "build",
                                "interface": [_INTERFACE_ENTRY],
                            },
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
                        spec={"intent": "lib", "interface": [_INTERFACE_ENTRY]},
                    ),
                    _lane(
                        "lane-app",
                        needs=("lane-tests", "lane-lib"),
                        outputs=["src/app.py"],
                        lane_kind="build",
                        spec={"intent": "app", "interface": [_INTERFACE_ENTRY]},
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
                            spec={
                                "intent": "build",
                                "interface": [_INTERFACE_ENTRY],
                            },
                        ),
                    )
                )
            )
        self.assertIn(pv.BUILD_LANE_NEEDS, _codes(caught.exception))
        self.assertEqual(caught.exception.refusals[0].pointer, "/lanes/2/needs/1")


class InterfaceDeclaredTests(unittest.TestCase):
    """A build lane paired with a tests lane declares its public interface.

    The tester binds to the declared interface, not to names it invents; an
    undeclared binding is an unclosable review loop. ``spec.interface`` is
    the authored declaration, projected into ``public_contract`` so tester,
    reviewers and builder read the same bytes.
    """

    def _paired(self, *, build_spec=None, tests_spec=None) -> dict:
        return _plan(
            _lane(
                "lane-tests",
                lane_kind="tests",
                outputs=["tests/test_b.py"],
                spec=tests_spec if tests_spec is not None else {"intent": "tests"},
            ),
            _lane(
                "lane-build",
                needs=("lane-tests",),
                outputs=["src/b.py"],
                lane_kind="build",
                spec=build_spec if build_spec is not None else {"intent": "build"},
            ),
        )

    def test_paired_build_lane_without_interface_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(self._paired()))
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))
        refusal = [
            item
            for item in caught.exception.refusals
            if item.code == pv.INTERFACE_UNDECLARED
        ][0]
        self.assertEqual(refusal.pointer, "/lanes/1/spec/interface")

    def test_paired_build_lane_with_empty_interface_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(self._paired(build_spec={"intent": "b", "interface": []}))
            )
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))

    def test_paired_build_lane_with_interface_compiles(self):
        compiled = compile_plan(
            _dump(
                self._paired(
                    build_spec={"intent": "b", "interface": [_INTERFACE_ENTRY]}
                )
            )
        )
        build = _lane_of(compiled, "lane-build")
        self.assertEqual(tuple([_INTERFACE_ENTRY]), tuple(build.public_interface))

    def test_tests_lane_public_interface_is_the_paired_builds(self):
        compiled = compile_plan(
            _dump(
                self._paired(
                    build_spec={"intent": "b", "interface": [_INTERFACE_ENTRY]}
                )
            )
        )
        tests = _lane_of(compiled, "lane-tests")
        self.assertEqual(tuple([_INTERFACE_ENTRY]), tuple(tests.public_interface))

    def test_malformed_interface_entry_is_refused(self):
        bad = dict(_INTERFACE_ENTRY)
        bad["kind"] = "widget"
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(self._paired(build_spec={"intent": "b", "interface": [bad]}))
            )
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))

    def test_interface_entry_missing_signature_field_is_refused(self):
        bad = {
            "kind": "callable",
            "consumed_by": {"deferred_to": "WP2"},
            "module": "src/b.py",
            "name": "build_contract",
            "signature": {"parameters": [{"name": "record"}], "returns": "dict"},
        }
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(self._paired(build_spec={"intent": "b", "interface": [bad]}))
            )
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))

    def test_route_entry_needs_method_path_response(self):
        bad = {
            "kind": "route",
            "consumed_by": {"deferred_to": "WP2"},
            "module": "src/bff.py",
            "name": "list_items",
            "signature": {"method": "GET", "path": "/items"},
        }
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(self._paired(build_spec={"intent": "b", "interface": [bad]}))
            )
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))

    def test_route_entry_needs_module_and_name(self):
        # A route that names only its HTTP surface leaves the suite guessing
        # which module and export serves it -- the WP5 failure class.
        bad = {
            "kind": "route",
            "consumed_by": {"deferred_to": "WP2"},
            "signature": {
                "method": "GET",
                "path": "/items",
                "response": {"status": 200},
            },
        }
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(self._paired(build_spec={"intent": "b", "interface": [bad]}))
            )
        self.assertIn(pv.INTERFACE_UNDECLARED, _codes(caught.exception))

    def test_malformed_interface_on_tests_lane_compiles(self):
        compiled = compile_plan(
            _dump(
                self._paired(
                    tests_spec={"intent": "t", "interface": [{"kind": "widget"}]},
                    build_spec={"intent": "b", "interface": [_INTERFACE_ENTRY]},
                )
            )
        )
        self.assertEqual(
            tuple([_INTERFACE_ENTRY]),
            tuple(_lane_of(compiled, "lane-build").public_interface),
        )

    def test_malformed_interface_on_untyped_lane_compiles(self):
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-tests",
                        lane_kind="tests",
                        outputs=["tests/test_b.py"],
                    ),
                    _lane(
                        "lane-untyped",
                        needs=("lane-tests",),
                        outputs=["src/b.py"],
                        spec={"intent": "u", "interface": [{"kind": "widget"}]},
                    ),
                )
            )
        )
        self.assertEqual((), _lane_of(compiled, "lane-tests").public_interface)

    def test_malformed_interface_on_unpaired_lane_compiles(self):
        # Inert data: the projection carries the lane's own spec verbatim and
        # the refusal never fires, because nothing binds to this lane.
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-solo",
                        spec={"intent": "s", "interface": [{"kind": "widget"}]},
                    ),
                )
            )
        )
        self.assertEqual(
            ({"kind": "widget"},),
            tuple(_lane_of(compiled, "lane-solo").public_interface),
        )

    def test_untyped_lane_needing_tests_needs_no_interface(self):
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-tests",
                        lane_kind="tests",
                        outputs=["tests/test_b.py"],
                    ),
                    _lane(
                        "lane-untyped",
                        needs=("lane-tests",),
                        outputs=["src/b.py"],
                    ),
                )
            )
        )
        self.assertEqual((), _lane_of(compiled, "lane-untyped").public_interface)

    def test_bound_run_does_not_refuse_undeclared_interface(self):
        refusals = pv.validate_objective_plan(self._paired(), bound_run=True)
        self.assertNotIn(
            pv.INTERFACE_UNDECLARED, tuple(item.code for item in refusals)
        )


class InterfaceConsumedTests(unittest.TestCase):
    """A declared interface names who will call it, or does not ship.

    FDAdb WP5 converged, published `5ebb652c3037` and shipped a module
    nothing calls -- no producer, no mount, nothing importing
    `RegulatorySection`. Every gate passed, because no gate asked who
    consumes the interface. The deferral to WP5b was legitimate; it was
    silent, and therefore unreviewable. `consumed_by` makes the author state
    it: a lane of this plan, or a named deferral. It is a declaration, never
    a measurement -- nothing here reads the repository.
    """

    def _entry(self, consumed_by=_UNSET, *, name="build_contract"):
        entry = dict(_INTERFACE_ENTRY)
        entry["name"] = name
        if consumed_by is _UNSET:
            entry.pop("consumed_by", None)
        else:
            entry["consumed_by"] = consumed_by
        return entry

    def _paired(self, *entries, extra_lanes=(), tests_spec=None):
        return _plan(
            _lane(
                "lane-tests",
                lane_kind="tests",
                outputs=["tests/test_b.py"],
                spec=tests_spec if tests_spec is not None else {"intent": "tests"},
            ),
            _lane(
                "lane-build",
                needs=("lane-tests",),
                outputs=["src/b.py"],
                lane_kind="build",
                spec={"intent": "build", "interface": list(entries)},
            ),
            *extra_lanes,
        )

    def _refusals(self, payload):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(payload))
        return caught.exception

    def _message(self, caught):
        return " ".join(
            item.message
            for item in caught.refusals
            if item.code == pv.INTERFACE_UNCONSUMED
        )

    def test_entry_without_consumed_by_is_refused(self):
        caught = self._refusals(self._paired(self._entry()))
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
        refusal = [
            item for item in caught.refusals
            if item.code == pv.INTERFACE_UNCONSUMED
        ][0]
        self.assertEqual(
            refusal.pointer, "/lanes/1/spec/interface/0/consumed_by"
        )

    def test_empty_consumed_by_is_refused(self):
        for value in ({}, None, "", [], {"lane": ""}, {"deferred_to": "   "}):
            with self.subTest(value=value):
                caught = self._refusals(self._paired(self._entry(value)))
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))

    def test_both_or_unknown_keys_are_refused(self):
        for value in (
            {"lane": "lane-app", "deferred_to": "WP2"},
            {"consumer": "lane-app"},
            {"lane": "lane-app", "note": "x"},
        ):
            with self.subTest(value=value):
                caught = self._refusals(self._paired(self._entry(value)))
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))

    def test_unknown_consumer_lane_is_refused(self):
        caught = self._refusals(
            self._paired(self._entry({"lane": "lane-nowhere"}))
        )
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))

    def test_paired_tests_lane_as_consumer_is_refused(self):
        """The cheapest wrong answer, and the WP5 shape itself.

        `lane-tests` is already in the build lane's `needs`, so it is the
        first string an author under review pressure reaches for -- and a
        tests lane's declared outputs are its accepted suite, never a call
        site. Accepting it would certify a published export with no caller,
        which is the defect `consumed_by` exists to name.
        """
        caught = self._refusals(
            self._paired(self._entry({"lane": "lane-tests"}))
        )
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
        refusal = [
            item for item in caught.refusals
            if item.code == pv.INTERFACE_UNCONSUMED
        ][0]
        self.assertIn("tests lane", refusal.message)

    def test_a_distinct_build_lane_with_its_own_outputs_is_a_consumer(self):
        """The accepting case the tests-lane refusal has to be told apart from.

        Same plan, same `needs` edge back to the tests lane, but the consumer
        is a build lane declaring a file of its own for the call to live in.
        """
        compiled = compile_plan(
            _dump(
                self._paired(
                    self._entry({"lane": "lane-app"}),
                    extra_lanes=(
                        _lane(
                            "lane-app",
                            needs=("lane-tests", "lane-build"),
                            outputs=["src/app.py"],
                            lane_kind="build",
                            spec={
                                "intent": "app",
                                "interface": [
                                    dict(
                                        _INTERFACE_ENTRY,
                                        module="src/app.py",
                                        name="main",
                                    )
                                ],
                            },
                        ),
                    ),
                )
            )
        )
        self.assertEqual(
            {"lane": "lane-app"},
            _lane_of(compiled, "lane-build").public_interface[0]["consumed_by"],
        )

    def test_self_consumption_is_refused(self):
        caught = self._refusals(
            self._paired(self._entry({"lane": "lane-build"}))
        )
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))

    def test_consumer_lane_in_this_plan_compiles(self):
        compiled = compile_plan(
            _dump(
                self._paired(
                    self._entry({"lane": "lane-app"}),
                    extra_lanes=(
                        _lane(
                            "lane-app",
                            needs=("lane-build",),
                            outputs=["src/app.py"],
                        ),
                    ),
                )
            )
        )
        build = _lane_of(compiled, "lane-build")
        self.assertEqual(
            {"lane": "lane-app"}, build.public_interface[0]["consumed_by"]
        )

    def test_named_deferral_compiles(self):
        deferral = "WP5b mounts RegulatorySection in the app shell"
        compiled = compile_plan(
            _dump(self._paired(self._entry({"deferred_to": deferral})))
        )
        build = _lane_of(compiled, "lane-build")
        self.assertEqual(
            {"deferred_to": deferral}, build.public_interface[0]["consumed_by"]
        )
        # The tests lane reads its paired build lane's entries, so the
        # consumer declaration reaches the tester as the same bytes.
        tests = _lane_of(compiled, "lane-tests")
        self.assertEqual(
            {"deferred_to": deferral}, tests.public_interface[0]["consumed_by"]
        )

    def test_unconsumed_interface_on_tests_lane_compiles(self):
        compile_plan(
            _dump(
                self._paired(
                    _INTERFACE_ENTRY,
                    tests_spec={"intent": "t", "interface": [self._entry()]},
                )
            )
        )

    def test_unconsumed_interface_on_untyped_lane_compiles(self):
        compile_plan(
            _dump(
                _plan(
                    _lane("lane-tests", lane_kind="tests", outputs=["tests/t.py"]),
                    _lane(
                        "lane-untyped",
                        needs=("lane-tests",),
                        outputs=["src/b.py"],
                        spec={"intent": "u", "interface": [self._entry()]},
                    ),
                )
            )
        )

    def test_unconsumed_interface_on_unpaired_lane_compiles(self):
        compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-solo",
                        spec={"intent": "s", "interface": [self._entry()]},
                    ),
                )
            )
        )

    def test_existing_call_sites_compiles(self):
        """A consumer that already shipped is named by its paths, not deferred.

        FDAdb's WP5b declared three of four entries as `deferred_to` WP5 and
        WP7 -- both MERGED, both with call sites in the repository already.
        This is the shape that says so, and it must reach every role as the
        same bytes the author wrote.
        """
        sites = [
            "src/lib/api/maude-device.ts",
            "src/pages/device/[key].astro",
        ]
        compiled = compile_plan(
            _dump(self._paired(self._entry({"existing_call_sites": sites})))
        )
        build = _lane_of(compiled, "lane-build")
        self.assertEqual(
            {"existing_call_sites": sites},
            build.public_interface[0]["consumed_by"],
        )
        tests = _lane_of(compiled, "lane-tests")
        self.assertEqual(
            {"existing_call_sites": sites},
            tests.public_interface[0]["consumed_by"],
        )

    def test_existing_call_sites_is_not_measured_against_the_repository(self):
        """A declaration the author answers, never a measurement.

        Nothing here opens the repository, so a path that names no file today
        compiles exactly as one that does. That is what lets the check answer
        identically at ship, start and amend.
        """
        compile_plan(
            _dump(
                self._paired(
                    self._entry(
                        {"existing_call_sites": ["src/no/such/file.ts"]}
                    )
                )
            )
        )

    def test_existing_call_sites_must_be_a_list(self):
        for value in ("src/app.ts", {"path": "src/app.ts"}, 3, None):
            with self.subTest(value=value):
                caught = self._refusals(
                    self._paired(self._entry({"existing_call_sites": value}))
                )
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
                self.assertIn(
                    "must be an array of repo-relative paths",
                    self._message(caught),
                )

    def test_empty_existing_call_sites_is_refused(self):
        """An empty list answers the question with silence.

        It is the WP5 shape wearing the new key: an entry that claims a
        consumer exists and names none.
        """
        caught = self._refusals(
            self._paired(self._entry({"existing_call_sites": []}))
        )
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
        self.assertIn("at least one existing call site", self._message(caught))

    def test_non_string_or_blank_call_site_is_refused(self):
        for value in ([None], [3], [""], ["   "], ["src/a.ts", ""]):
            with self.subTest(value=value):
                caught = self._refusals(
                    self._paired(self._entry({"existing_call_sites": value}))
                )
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
                self.assertIn(
                    "must be a nonempty repo-relative path",
                    self._message(caught),
                )

    def test_absolute_call_site_is_refused(self):
        caught = self._refusals(
            self._paired(
                self._entry(
                    {"existing_call_sites": ["/Users/me/repo/src/app.ts"]}
                )
            )
        )
        self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
        self.assertIn("may not begin with", self._message(caught))

    def test_call_site_with_parent_segment_is_refused(self):
        for value in (["../sibling/src/app.ts"], ["src/../../etc/passwd"]):
            with self.subTest(value=value):
                caught = self._refusals(
                    self._paired(self._entry({"existing_call_sites": value}))
                )
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
                self.assertIn("'..' segment", self._message(caught))

    def test_all_three_consumer_keys_together_are_refused(self):
        """Exactly one, still. A third shape does not loosen `len != 1`."""
        for value in (
            {
                "lane": "lane-app",
                "deferred_to": "WP2",
                "existing_call_sites": ["src/app.ts"],
            },
            {"deferred_to": "WP2", "existing_call_sites": ["src/app.ts"]},
            {"lane": "lane-app", "existing_call_sites": ["src/app.ts"]},
        ):
            with self.subTest(value=value):
                caught = self._refusals(self._paired(self._entry(value)))
                self.assertIn(pv.INTERFACE_UNCONSUMED, _codes(caught))
                self.assertIn(
                    "exactly one of lane, deferred_to or existing_call_sites",
                    self._message(caught),
                )

    def test_bound_run_does_not_refuse_unconsumed_interface(self):
        refusals = pv.validate_objective_plan(
            self._paired(self._entry()), bound_run=True
        )
        self.assertNotIn(
            pv.INTERFACE_UNCONSUMED, tuple(item.code for item in refusals)
        )


class ObservationSeamTests(unittest.TestCase):
    """A gating obligation names its observation seam or does not ship.

    FDAdb run d246ae95's `lane-faq-producer-tests` carried "serving never calls
    the FAQ producer" with nothing said about how a case observes it. A
    same-module call whose result is discarded is unobservable from the public
    contract, so the tester could not discharge it, the reviewer correctly
    refused three rounds, and the lane parked. The defect was in the plan and
    it is decidable at ship time.
    """

    _SEAM = (
        "src/faq/producer.py is the producer's public module; serving's import "
        "of it is the observable"
    )

    _EXAMPLES = [{"input": {"path": "/faq"}, "expect": {"producer_imported": False}}]
    _RESTRICTION = {
        "polarity": "positive",
        "has_exception_ids": False,
        "has_preconditions": False,
        "external_store": False,
    }

    def _gating(self, **overrides) -> dict:
        criterion = {
            "criterion": "serving never calls the FAQ producer",
            "gating": True,
            "decided_by": self._EXAMPLES,
            "restriction": self._RESTRICTION,
        }
        criterion.update(overrides)
        return criterion

    def test_gating_obligation_without_a_seam_is_refused(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(_plan(_lane("lane-a", acceptance=[self._gating()])))
            )
        self.assertIn(pv.OBLIGATION_UNOBSERVABLE, _codes(caught.exception))
        self.assertEqual(
            caught.exception.refusals[0].pointer, "/lanes/0/acceptance/0"
        )

    def test_an_empty_seam_is_not_a_seam(self):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(
                _dump(
                    _plan(
                        _lane(
                            "lane-a",
                            acceptance=[self._gating(observation_seam="   ")],
                        )
                    )
                )
            )
        self.assertIn(pv.ACCEPTANCE_MISSING, _codes(caught.exception))

    def test_the_same_obligation_ships_once_it_names_its_seam(self):
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-a",
                        lane_kind="tests",
                        acceptance=[self._gating(observation_seam=self._SEAM)],
                    )
                )
            )
        )
        self.assertEqual(
            (
                "serving never calls the FAQ producer [observable: {0}] "
                '[decided by: [{{"expect":{{"producer_imported":false}},'
                '"input":{{"path":"/faq"}}}}]]'.format(self._SEAM),
            ),
            _lane_of(compiled, "lane-a").public_acceptance,
        )

    def test_a_declared_seam_is_part_of_the_plan_identity(self):
        """The seam is authored data, not a rendering of the criterion text."""
        one = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-a",
                        acceptance=[self._gating(observation_seam=self._SEAM)],
                    )
                )
            )
        )
        other = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-a",
                        acceptance=[
                            self._gating(observation_seam="a different seam")
                        ],
                    )
                )
            )
        )
        self.assertNotEqual(one.plan_digest, other.plan_digest)

    def test_an_advisory_obligation_needs_no_seam(self):
        for acceptance in (
            ["serving is fast enough"],
            [{"criterion": "serving is fast enough"}],
            [{"criterion": "serving is fast enough", "gating": False}],
        ):
            with self.subTest(acceptance=acceptance):
                compiled = compile_plan(
                    _dump(
                        _plan(
                            _lane(
                                "lane-a", lane_kind="tests", acceptance=acceptance
                            )
                        )
                    )
                )
                self.assertEqual(
                    ("serving is fast enough",),
                    _lane_of(compiled, "lane-a").public_acceptance,
                )

    def test_gating_is_declared_and_never_read_out_of_the_prose(self):
        """"must" / "never" / "always" in the text does not make a criterion gate."""
        compiled = compile_plan(
            _dump(
                _plan(
                    _lane(
                        "lane-a",
                        lane_kind="tests",
                        acceptance=["serving must never call the FAQ producer"],
                    )
                )
            )
        )
        self.assertEqual(
            ("serving must never call the FAQ producer",),
            _lane_of(compiled, "lane-a").public_acceptance,
        )

    def test_an_unknown_acceptance_key_is_refused(self):
        for item in (
            {"criterion": "x", "gating": True, "seam": self._SEAM},
            {"criterion": "x", "gating": "yes", "observation_seam": self._SEAM},
            {"gating": True, "observation_seam": self._SEAM},
            {"criterion": "  ", "gating": True, "observation_seam": self._SEAM},
        ):
            with self.subTest(item=item):
                with self.assertRaises(PlanCompileError) as caught:
                    compile_plan(_dump(_plan(_lane("lane-a", acceptance=[item]))))
                self.assertIn(pv.ACCEPTANCE_MISSING, _codes(caught.exception))


class DecidedByTests(unittest.TestCase):
    """A gating obligation states its expected answers or does not ship.

    FDAdb's amended runs parked NO_PROGRESS on contracts that named where to
    observe but not what value is correct: onset-tests declared provenance only
    as a Mapping, so whether `{}` is valid was left to a tester/reviewer
    argument that ran three and four rounds before a human amended the plan.
    """

    _SEAM = "ObservationStore.record is the public export a case calls"
    _EXPECT = {"input": {"provenance": {"source": "spl"}}, "expect": {"recorded": True}}
    _REFUSES = {
        "input": {"provenance": {}},
        "refuses": {"error": "ValueError", "message": "provenance is empty"},
    }

    _OPEN = {
        "polarity": "positive",
        "has_exception_ids": False,
        "has_preconditions": False,
        "external_store": False,
    }
    _NEGATIVE = dict(_OPEN, polarity="negative")

    def _criterion(self, **fields) -> dict:
        payload = {
            "criterion": "claim-onset-provenance (positive): every observation carries provenance",
            "gating": True,
            "observation_seam": self._SEAM,
            "restriction": self._OPEN,
        }
        payload.update(fields)
        return payload

    def _compile(self, *acceptance, bound_run=False):
        return compile_plan(
            _dump(_plan(_lane("lane-a", lane_kind="tests", acceptance=list(acceptance)))),
            bound_run=bound_run,
        )

    def _refusal(self, *acceptance):
        with self.assertRaises(PlanCompileError) as caught:
            self._compile(*acceptance)
        undecided = [
            item for item in caught.exception.refusals
            if item.code == pv.OBLIGATION_UNDECIDED
        ]
        self.assertTrue(undecided, _codes(caught.exception))
        return undecided[0]

    def test_a_gating_obligation_without_examples_is_refused_by_name(self):
        refusal = self._refusal(self._criterion())
        self.assertEqual("/lanes/0/acceptance/0/decided_by", refusal.pointer)
        self.assertIn("claim-onset-provenance", refusal.message)
        self.assertIn("no decided_by", refusal.message)

    def test_examples_that_only_refuse_are_refused(self):
        refusal = self._refusal(self._criterion(decided_by=[self._REFUSES]))
        self.assertIn("no expect example", refusal.message)

    def test_expect_and_refuses_are_accepted(self):
        compiled = self._compile(
            self._criterion(decided_by=[self._EXPECT, self._REFUSES], restriction=self._NEGATIVE)
        )
        self.assertEqual(1, len(_lane_of(compiled, "lane-a").public_acceptance))

    def test_a_restricted_obligation_with_only_expect_is_refused(self):
        """The compiler derives the refusal obligation; no flag stands in for it."""
        for restriction in (
            self._NEGATIVE,
            dict(self._OPEN, has_exception_ids=True),
            dict(self._OPEN, has_preconditions=True),
            dict(self._OPEN, external_store=True),
        ):
            with self.subTest(restriction=restriction):
                refusal = self._refusal(
                    self._criterion(decided_by=[self._EXPECT], restriction=restriction)
                )
                self.assertIn("no refuses example", refusal.message)

    def test_a_negative_obligation_cannot_drop_its_refusal_by_omitting_a_flag(self):
        """Direct `run start` of canonical bytes: no flag exists to omit.

        The reviewed bypass: a negative claim's criterion copied into a plan with
        `refusal_required` left out and only an `expect` example compiled. The
        obligation is now derived from `restriction`, and the old flag is not an
        admissible key at all.
        """
        criterion = self._criterion(decided_by=[self._EXPECT], restriction=self._NEGATIVE)
        self.assertNotIn("refusal_required", criterion)
        refusal = self._refusal(criterion)
        self.assertEqual(pv.OBLIGATION_UNDECIDED, refusal.code)
        with self.assertRaises(PlanCompileError) as caught:
            self._compile(dict(criterion, refusal_required=False))
        self.assertIn(pv.ACCEPTANCE_MISSING, _codes(caught.exception))

    def test_an_external_source_obligation_owes_its_unavailable_refusal(self):
        """FDAdb reads FAERSdb and lexgenius-maude; a builder invented SOURCE_UNAVAILABLE."""
        external = dict(self._OPEN, external_store=True)
        refusal = self._refusal(
            self._criterion(decided_by=[self._EXPECT], restriction=external)
        )
        self.assertEqual(pv.OBLIGATION_UNDECIDED, refusal.code)
        self.assertIn("unavailable", refusal.message)
        unavailable = {
            "input": {"release_id": "2026Q2", "response": {"status": 503}},
            "refuses": {"error": "SOURCE_UNAVAILABLE", "message": "FAERSdb returned 503"},
        }
        self._compile(
            self._criterion(decided_by=[self._EXPECT, unavailable], restriction=external)
        )

    def test_a_gating_obligation_without_restriction_is_refused(self):
        for restriction in (None, {"polarity": "negative"}, dict(self._OPEN, polarity="maybe")):
            with self.subTest(restriction=restriction):
                criterion = self._criterion(decided_by=[self._EXPECT])
                if restriction is None:
                    criterion.pop("restriction")
                else:
                    criterion["restriction"] = restriction
                refusal = self._refusal(criterion)
                self.assertIn("restriction", refusal.message)

    def test_a_malformed_example_names_the_missing_part(self):
        for example, part in (
            ({"expect": 1}, "exact input"),
            ({"input": 1, "expect": 1, "refuses": {"error": "E"}}, "exactly one"),
            ({"input": 1}, "exactly one"),
            ({"input": 1, "refuses": {"message": "m"}}, "exact error"),
        ):
            with self.subTest(example=example):
                refusal = self._refusal(
                    self._criterion(decided_by=[self._EXPECT, example])
                )
                self.assertIn(part, refusal.message)

    def test_an_advisory_obligation_needs_no_examples(self):
        compiled = self._compile("serving is fast enough", {"criterion": "p99 under 200ms"})
        self.assertEqual(
            ("serving is fast enough", "p99 under 200ms"),
            _lane_of(compiled, "lane-a").public_acceptance,
        )

    def test_examples_reach_the_public_contract_verbatim(self):
        compiled = self._compile(
            self._criterion(decided_by=[self._EXPECT, self._REFUSES], restriction=self._NEGATIVE)
        )
        (text,) = _lane_of(compiled, "lane-a").public_acceptance
        self.assertIn(
            json.dumps(
                [self._EXPECT, self._REFUSES],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            text,
        )

    def test_a_plan_a_run_is_already_bound_to_is_not_re_judged(self):
        """resume/status/attend re-read the bound revision with bound_run=True."""
        undecided = self._criterion()
        with self.assertRaises(PlanCompileError):
            self._compile(undecided)
        bound = self._compile(undecided, bound_run=True)
        self.assertEqual(
            ("{0} [observable: {1}]".format(undecided["criterion"], self._SEAM),),
            _lane_of(bound, "lane-a").public_acceptance,
        )

    def test_a_pre_seam_plan_bound_to_a_run_is_not_re_judged_either(self):
        """Both authoring obligations: strict refuses both, bound refuses neither."""
        pre_seam = {"criterion": "claim-a (positive): a.txt holds provenance", "gating": True}
        with self.assertRaises(PlanCompileError) as caught:
            self._compile(pre_seam)
        self.assertEqual(
            {pv.OBLIGATION_UNOBSERVABLE, pv.OBLIGATION_UNDECIDED},
            set(_codes(caught.exception)),
        )
        bound = self._compile(pre_seam, bound_run=True)
        self.assertEqual(
            (pre_seam["criterion"],), _lane_of(bound, "lane-a").public_acceptance
        )

    def test_examples_are_part_of_the_plan_identity(self):
        one = self._compile(self._criterion(decided_by=[self._EXPECT]))
        other = self._compile(
            self._criterion(decided_by=[self._EXPECT, {"input": 2, "expect": 3}])
        )
        self.assertNotEqual(one.plan_digest, other.plan_digest)


class TestSuiteOutputsAreNobodyElsesToDeclare(unittest.TestCase):
    """A lane may not declare an output covering another lane's test suite.

    A `lane_kind=tests` lane's declared outputs ARE its accepted suite: the
    tester authors its private acceptance files exactly there
    (`MAESTRO_architecture.md` §11). A build lane that also declares one of
    them is asking to own the bytes it is graded against.

    The generic one-owner-per-path rule already refuses the same plans, and
    keeps doing so -- nothing here replaces it. What it cannot say is WHICH
    invariant was broken, and that distinction is about to become load-bearing:
    once the accepted suite lives in the builder's checkout rather than behind
    a vault overlay, "two lanes want this path" and "a lane wants to rewrite
    its own grader" stop being the same sentence.
    """

    def _plan_with(self, build_outputs):
        return _plan(
            _lane("lane-t", lane_kind="tests", outputs=["tests/x_test.py"]),
            _lane(
                "lane-b",
                lane_kind="build",
                needs=["lane-t"],
                outputs=build_outputs,
                spec={"intent": "build", "interface": [_INTERFACE_ENTRY]},
            ),
        )

    def _refusals(self, payload):
        with self.assertRaises(PlanCompileError) as caught:
            compile_plan(_dump(payload))
        return caught.exception

    def test_a_build_lane_declaring_a_suite_path_is_refused_by_name(self):
        exc = self._refusals(self._plan_with(["src/a.py", "tests/x_test.py"]))

        self.assertIn(pv.OUTPUT_OVERLAPS_TEST_SUITE, _codes(exc))
        named = [
            item for item in exc.refusals
            if item.code == pv.OUTPUT_OVERLAPS_TEST_SUITE
        ]
        self.assertEqual("/lanes/1/outputs/1", named[0].pointer)
        self.assertIn("lane-b", named[0].message)
        self.assertIn("lane-t", named[0].message)
        self.assertIn("tests/x_test.py", named[0].message)

    def test_an_ancestor_of_a_suite_path_is_refused(self):
        exc = self._refusals(self._plan_with(["tests"]))

        self.assertIn(pv.OUTPUT_OVERLAPS_TEST_SUITE, _codes(exc))

    def test_an_untyped_lane_may_not_declare_a_suite_path_either(self):
        payload = _plan(
            _lane("lane-t", lane_kind="tests", outputs=["tests/x_test.py"]),
            _lane("lane-u", outputs=["tests/x_test.py"]),
        )

        self.assertIn(pv.OUTPUT_OVERLAPS_TEST_SUITE, _codes(self._refusals(payload)))

    def test_a_tests_lane_still_owns_its_own_outputs(self):
        compiled = compile_plan(_dump(self._plan_with(["src/a.py"])))

        self.assertEqual(
            ("tests/x_test.py",), _lane_of(compiled, "lane-t").declared_outputs
        )
        self.assertEqual(("src/a.py",), _lane_of(compiled, "lane-b").declared_outputs)

    def test_a_plan_with_no_tests_lane_is_untouched(self):
        payload = _plan(
            _lane("lane-a", outputs=["tests/x_test.py"]),
            _lane("lane-b", outputs=["src/a.py"]),
        )

        compiled = compile_plan(_dump(payload))
        self.assertEqual(
            ("tests/x_test.py",), _lane_of(compiled, "lane-a").declared_outputs
        )


if __name__ == "__main__":
    unittest.main()
