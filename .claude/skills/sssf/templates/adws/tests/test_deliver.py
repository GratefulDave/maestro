"""`maestro deliver`: one verb from a source document to shipped, runnable plans.

These tests hold the five things the verb is actually for: that a spec reaches
Maestro as a Plan IR and never through a converter, that a reviewer FAIL feeds
its finding cells back into the authoring lane and re-ships, that the repair
loop is bounded and surfaces what it could not fix, that the packages run in
dependency order one at a time and halt at the first run that is not ACCEPTED,
and that a resumed `deliver` redoes neither a shipped package nor an accepted
run.

The lane, the pane, planctl, the reviewer, and the scheduler are all injected,
because none of them is the thing under test here.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import deliver


ARCHITECTURE_IR = {"schema_version": "plan-contract.v1", "plan_id": "arch",
                   "plan_kind": "architecture"}


def brownfield_ir(plan_id, outputs=(), sources=()):
    return {
        "schema_version": "plan-contract.v1",
        "plan_id": plan_id,
        "plan_kind": "brownfield",
        "source_artifacts": [{"source_id": "s{}".format(index), "path": path}
                             for index, path in enumerate(sources)],
        "extensions": {"maestro": {
            "repo": "repo",
            "outputs": {"lane-a": list(outputs)} if outputs else {"lane-a": []},
            "integration_branch": "integration/" + plan_id,
            "integration_gate": {"runner": "pytest", "argv": ["tests"],
                                 "cwd": ".", "min_cases": 1},
        }},
    }


class DeliveryFixture(unittest.TestCase):
    """A plans directory, a recording authoring lane, and scripted plan verbs."""

    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.plans = self.repo / ".maestro"
        self.plans.mkdir(parents=True)
        self.envelopes = self.root / "state" / "deliver"
        self.envelopes.mkdir(parents=True)
        (self.repo / "docs").mkdir()
        self.spec = "docs/spec.md"
        (self.repo / self.spec).write_text("# spec\n", encoding="utf-8")
        self.turns = []
        self.steps = []
        self.removed = []
        self.released = []
        self.reports = {}
        self.step_script = {}
        self.lane_script = {}
        self.started = []
        self.run_script = {}
        self.already_accepted = {}
        self.already_shipped = set()
        self.lanes = {}
        self.releases = {}

    # -- injected collaborators ----------------------------------------------
    def write_ir(self, name, payload):
        path = self.plans / (name + deliver.IR_SUFFIX)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_receipt(self, name, verdict="PASS", ir_bytes=None):
        if ir_bytes is None:
            ir_bytes = (self.plans / (name + deliver.IR_SUFFIX)).read_bytes()
        path = self.plans / (name + deliver.RECEIPT_SUFFIX)
        path.write_text(json.dumps({
            "verdict": verdict,
            "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
        }), encoding="utf-8")
        return path

    def author_turn(self, kind, prompt, envelope):
        self.turns.append({"kind": kind, "prompt": prompt,
                           "envelope": str(envelope)})
        action = self.lane_script.get(kind)
        if callable(action):
            return action(len([t for t in self.turns if t["kind"] == kind]))
        if isinstance(action, dict):
            return action
        return {"success": True, "summary": kind}

    def plan_step(self, verb, name):
        index = len([row for row in self.steps
                     if row[0] == verb and row[1] == name])
        self.steps.append((verb, name))
        script = self.step_script.get((verb, name))
        if callable(script):
            return script(index)
        if script is not None:
            return script
        if verb == "ship":
            return 0, [{"outcome": "PLAN_SHIPPED", "verdict": "PASS"}]
        return 0, [{"outcome": "OK"}]

    def reviewer_report(self, name):
        report = self.reports.get(name)
        if callable(report):
            return report()
        return report

    def remove_plan_dir(self, name):
        self.removed.append(name)

    def run_start(self, name):
        index = len([row for row in self.started if row == name])
        self.started.append(name)
        script = self.run_script.get(name)
        if callable(script):
            return script(index)
        if script is not None:
            return script
        return 0, [{"outcome": "ACCEPTED", "run_id": "run-" + name,
                    "merged": ["lane-a"], "blocked": []}]

    def accepted_run(self, name):
        return self.already_accepted.get(name)

    def blocked_lanes(self, run_id):
        return self.lanes.get(run_id, ())

    def release_run(self, name):
        self.released.append(name)
        return self.releases.get(name, ())

    def delivery(self, **overrides):
        kwargs = dict(
            spec=self.spec, repo=self.repo, plans_dir=self.plans,
            envelope_dir=self.envelopes, author_turn=self.author_turn,
            plan_step=self.plan_step, reviewer_report=self.reviewer_report,
            remove_plan_dir=self.remove_plan_dir)
        kwargs.update(overrides)
        return deliver.Delivery(**kwargs)

    def running_delivery(self, **overrides):
        kwargs = dict(
            run_start=self.run_start, accepted_run=self.accepted_run,
            blocked_lanes=self.blocked_lanes, release_run=self.release_run,
            shipped=lambda name: name in self.already_shipped)
        kwargs.update(overrides)
        return self.delivery(**kwargs)

    def ok(self, verb, name, payloads=None):
        self.step_script[(verb, name)] = (0, payloads if payloads is not None
                                          else [{"outcome": "OK"}])

    def ships(self, name, verdict="PASS"):
        self.ok("gate", name)
        self.ok("review", name)
        self.ok("ship", name,
                [{"outcome": "PLAN_SHIPPED", "verdict": verdict}])


# ── step 1 and 2: a source document becomes packages ────────────────────────

class SpecToPackagesTest(DeliveryFixture):
    def test_the_happy_path_authors_an_anchor_then_packages_then_ships(self):
        def brownfield(_attempt):
            self.write_ir("alpha", brownfield_ir("alpha"))
            return {"success": True, "summary": "one package",
                    "packages": [{"name": "alpha", "depends_on": []}]}

        def architecture(_attempt):
            self.write_ir("arch", ARCHITECTURE_IR)
            self.write_receipt("arch")
            return {"success": True, "summary": "anchor",
                    "architecture_ir": ".maestro/arch.plan.json"}

        self.lane_script["architecture"] = architecture
        self.lane_script["brownfield"] = brownfield
        self.ships("alpha")

        result = self.delivery().run()

        self.assertEqual(result["outcome"], "DELIVERED_NOT_RUN")
        self.assertTrue(result["architecture_authored"])
        self.assertEqual(result["order"], ["alpha"])
        self.assertEqual(result["run_commands"], ["maestro run start alpha"])
        self.assertEqual([turn["kind"] for turn in self.turns],
                         ["architecture", "brownfield"])
        self.assertEqual(self.steps,
                         [("gate", "alpha"), ("review", "alpha"),
                          ("ship", "alpha")])

    def test_an_approved_architecture_ir_is_reused_not_reauthored(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", brownfield_ir("alpha"))
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["alpha"]}
        self.ships("alpha")

        result = self.delivery().run()

        self.assertFalse(result["architecture_authored"])
        self.assertEqual([turn["kind"] for turn in self.turns], ["brownfield"])
        self.assertIn("reused approved architecture IR", result["notes"][0])

    def test_an_unapproved_architecture_ir_is_not_treated_as_the_anchor(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch", verdict="FAIL")
        self.assertIsNone(self.delivery()._approved_architecture())

    def test_a_new_architecture_must_have_a_matching_pass_receipt(self):
        def architecture(_attempt):
            self.write_ir("arch", ARCHITECTURE_IR)
            self.write_receipt("arch", ir_bytes=b"stale architecture")
            return {"success": True, "summary": "anchor",
                    "architecture_ir": ".maestro/arch.plan.json"}

        self.lane_script["architecture"] = architecture

        with self.assertRaisesRegex(
                deliver.DeliverError, "ARCHITECTURE_RECEIPT_MISMATCH"):
            self.delivery().architecture()
        self.assertEqual([turn["kind"] for turn in self.turns], ["architecture"])


    def test_the_lane_may_not_declare_a_path_outside_the_plans_directory(self):
        self.lane_script["architecture"] = lambda _a: {
            "success": True, "summary": "", "architecture_ir": "../escape.plan.json"}
        with self.assertRaises(deliver.DeliverError) as caught:
            self.delivery().architecture()
        self.assertIn("DECLARED_PATH_OUTSIDE_PLANS_DIR", str(caught.exception))

    def test_an_architecture_package_is_refused_as_a_work_package(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", ARCHITECTURE_IR)
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["alpha"]}
        with self.assertRaises(deliver.DeliverError) as caught:
            self.delivery().run()
        self.assertIn("ARCHITECTURE_NOT_EXECUTABLE", str(caught.exception))

    def test_a_package_whose_ir_was_never_written_is_refused(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["ghost"]}
        with self.assertRaises(deliver.DeliverError) as caught:
            self.delivery().run()
        self.assertIn("PACKAGE_IR_MISSING", str(caught.exception))

    def test_the_lane_is_told_every_rule_that_has_actually_refused_a_plan(self):
        prompt = deliver.architecture_prompt(
            "docs/spec.md", Path("/p/arch.plan.json"), Path("/e/env.json"))
        for rule in ("GATE_EXECUTABLE", "GATE_CORE_UNSHARED",
                     "--collect-only -q -o addopts=", "pv.blob_at",
                     "git add -f", "RECEIPT_IR_MISMATCH", "PLAN_EXISTS",
                     "--repo-root ."):
            self.assertIn(rule, prompt, rule + " is not in the prompt")


# ── step 3: the bounded repair loop ─────────────────────────────────────────

class RepairLoopTest(DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", brownfield_ir("alpha"))
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["alpha"]}

    def test_a_validate_blocker_feeds_its_cells_back_and_the_reship_succeeds(self):
        blocker = {
            "outcome": "PLAN_SHIP_FAILED", "step": "validate",
            "blockers": [{"obligation": "GATE_EXECUTABLE",
                          "pointer": "/nodes/0/gate/argv",
                          "message": "collects 0 cases at base"}],
        }
        self.ok("gate", "alpha")
        self.ok("review", "alpha")
        self.step_script[("ship", "alpha")] = lambda index: (
            (2, [blocker]) if index == 0
            else (0, [{"outcome": "PLAN_SHIPPED", "verdict": "PASS"}]))

        result = self.delivery().run()

        self.assertEqual(result["outcome"], "DELIVERED_NOT_RUN")
        self.assertEqual(result["packages"][0]["attempts"], 2)
        repair = [turn for turn in self.turns if turn["kind"] == "repair"]
        self.assertEqual(len(repair), 1)
        self.assertIn("GATE_EXECUTABLE", repair[0]["prompt"])
        self.assertIn("/nodes/0/gate/argv", repair[0]["prompt"])
        self.assertIn("collects 0 cases at base", repair[0]["prompt"])
        self.assertIn("attempt 2 of 3", repair[0]["prompt"])

    def test_a_reviewer_fail_verdict_is_a_failed_attempt_though_ship_exits_zero(self):
        self.ok("gate", "alpha")
        self.ok("review", "alpha")
        self.step_script[("ship", "alpha")] = lambda index: (
            0, [{"outcome": "PLAN_SHIPPED",
                 "verdict": "FAIL" if index == 0 else "PASS"}])
        self.reports["alpha"] = {"cells": [
            {"check_id": "evidence-supports-claim", "object_id": "evidence:s0",
             "status": "finding", "message": "the cited blob has no such claim"},
            {"check_id": "gate-counts", "object_id": "node:lane-a#gate",
             "status": "clear", "message": ""},
        ]}

        result = self.delivery().run()

        self.assertEqual(result["outcome"], "DELIVERED_NOT_RUN")
        self.assertEqual(result["packages"][0]["attempts"], 2)
        repair = [turn for turn in self.turns if turn["kind"] == "repair"][0]
        self.assertIn("evidence-supports-claim", repair["prompt"])
        self.assertIn("the cited blob has no such claim", repair["prompt"])
        # A clear cell is not a finding and must not be handed back as one.
        self.assertNotIn("gate-counts", repair["prompt"])

    def test_three_failures_stop_and_surface_the_findings(self):
        failure = {"outcome": "PLAN_GATE_FAILED", "step": "validate",
                   "stdout": json.dumps({"valid": False,
                                         "diagnostics": ["maestro.lane_gate"]})}
        self.step_script[("gate", "alpha")] = (2, [failure])

        result = self.delivery().run()

        self.assertEqual(result["outcome"], "DELIVER_BLOCKED")
        package = result["packages"][0]
        self.assertFalse(package["shipped"])
        self.assertEqual(package["attempts"], 3)
        self.assertIn("maestro.lane_gate",
                      json.dumps(package["findings"]))
        # Three gate attempts, and exactly two repair turns between them.
        self.assertEqual(
            len([row for row in self.steps if row[0] == "gate"]), 3)
        self.assertEqual(
            len([turn for turn in self.turns if turn["kind"] == "repair"]), 2)
        self.assertEqual(result["run_commands"], [])

    def test_the_bound_is_configurable_and_still_bounds(self):
        self.step_script[("gate", "alpha")] = (2, [{"outcome": "PLAN_GATE_FAILED"}])
        result = self.delivery(max_attempts=1).run()
        self.assertEqual(result["packages"][0]["attempts"], 1)
        self.assertEqual(
            len([turn for turn in self.turns if turn["kind"] == "repair"]), 0)

    def test_every_attempt_clears_the_plan_directory_because_author_is_create_once(self):
        self.step_script[("gate", "alpha")] = (2, [{"outcome": "PLAN_GATE_FAILED"}])
        self.delivery().run()
        self.assertEqual(self.removed, ["alpha", "alpha", "alpha"])

    def test_a_step_that_fails_without_a_typed_cell_is_still_a_finding(self):
        self.step_script[("gate", "alpha")] = (2, [])
        result = self.delivery(max_attempts=1).run()
        findings = result["packages"][0]["findings"]
        self.assertEqual(findings[0]["code"], "STEP_FAILED")
        self.assertEqual(findings[0]["source"], "gate")

    def test_a_fail_verdict_with_an_unreadable_report_still_yields_a_finding(self):
        self.ok("gate", "alpha")
        self.ok("review", "alpha")
        self.ok("ship", "alpha", [{"outcome": "PLAN_SHIPPED", "verdict": "FAIL"}])
        result = self.delivery(max_attempts=1).run()
        self.assertEqual(result["packages"][0]["findings"][0]["code"],
                         "REVIEWER_FAIL")

    def test_review_is_not_reached_when_the_gate_fails(self):
        self.step_script[("gate", "alpha")] = (2, [{"outcome": "PLAN_GATE_FAILED"}])
        self.delivery(max_attempts=1).run()
        self.assertEqual([row[0] for row in self.steps], ["gate"])

    def test_ship_is_not_reached_when_review_fails(self):
        self.ok("gate", "alpha")
        self.step_script[("review", "alpha")] = (
            2, [{"outcome": "PLAN_REVIEW_FAILED"}])
        self.delivery(max_attempts=1).run()
        self.assertEqual([row[0] for row in self.steps], ["gate", "review"])


# ── step 4: the printed commands ────────────────────────────────────────────

class OrderingTest(DeliveryFixture):
    def _three_packages(self, edges):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        for name in ("alpha", "beta", "gamma"):
            self.write_ir(name, brownfield_ir(name))
            self.ships(name)
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": edges}

    def test_the_printed_commands_follow_the_declared_dependency_order(self):
        self._three_packages([
            {"name": "gamma", "depends_on": ["beta"]},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "alpha", "depends_on": []},
        ])
        result = self.delivery().run()
        self.assertEqual(result["order"], ["alpha", "beta", "gamma"])
        self.assertEqual(result["run_commands"], [
            "maestro run start alpha",
            "maestro run start beta",
            "maestro run start gamma",
        ])

    def test_an_edge_derived_from_the_ir_bytes_outranks_a_silent_envelope(self):
        # beta pins a path alpha declares as an output, so beta is downstream
        # of alpha whatever beta's envelope claimed.
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", brownfield_ir("alpha", outputs=["src/new.py"]))
        self.write_ir("beta", brownfield_ir("beta", sources=["src/new.py"]))
        for name in ("alpha", "beta"):
            self.ships(name)
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "",
            "packages": [{"name": "beta"}, {"name": "alpha"}]}

        result = self.delivery().run()

        self.assertEqual(result["order"], ["alpha", "beta"])

    def test_a_dependency_cycle_is_refused_rather_than_ordered_arbitrarily(self):
        self._three_packages([
            {"name": "alpha", "depends_on": ["beta"]},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "gamma", "depends_on": []},
        ])
        with self.assertRaises(deliver.DeliverError) as caught:
            self.delivery().run()
        self.assertIn("PACKAGE_CYCLE", str(caught.exception))

    def test_a_package_behind_a_blocked_one_is_never_printed_as_ready(self):
        self._three_packages([
            {"name": "alpha", "depends_on": []},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "gamma", "depends_on": []},
        ])
        self.step_script[("gate", "alpha")] = (2, [{"outcome": "PLAN_GATE_FAILED"}])

        result = self.delivery().run()

        self.assertEqual(result["outcome"], "DELIVER_BLOCKED")
        # gamma shipped and depends on nothing, so it is runnable; beta is
        # downstream of a package that never shipped, so it is not offered.
        self.assertEqual(result["run_commands"], ["maestro run start gamma"])
        blocked = {row["package"]: row for row in result["packages"]
                   if not row["shipped"]}
        self.assertEqual(set(blocked), {"alpha", "beta"})
        self.assertEqual(blocked["beta"]["findings"][0]["code"],
                         "UPSTREAM_BLOCKED")
        # beta is never gated: its input does not exist, so three opus
        # attempts on it would be three wasted attempts.
        self.assertEqual(blocked["beta"]["attempts"], 0)
        self.assertNotIn(("gate", "beta"), self.steps)

    def test_an_edge_naming_an_unknown_package_is_refused(self):
        with self.assertRaises(deliver.DeliverError) as caught:
            deliver.declared_packages(
                {"packages": [{"name": "alpha", "depends_on": ["nowhere"]}]})
        self.assertIn("PACKAGE_EDGE_UNKNOWN", str(caught.exception))

    def test_a_hostile_package_name_never_becomes_a_path(self):
        for hostile in ("../escape", "nested/plan", "..", "", "Alpha"):
            with self.assertRaises(deliver.DeliverError):
                deliver.declared_packages({"packages": [{"name": hostile}]})

    def test_an_empty_package_list_is_refused_rather_than_reported_delivered(self):
        with self.assertRaises(deliver.DeliverError) as caught:
            deliver.declared_packages({"packages": []})
        self.assertIn("NO_PACKAGES_DECLARED", str(caught.exception))

    def test_order_is_stable_across_runs_for_the_same_plans(self):
        packages = tuple(deliver.Package(name)
                         for name in ("zeta", "alpha", "mu"))
        self.assertEqual(deliver.order_packages(packages),
                         ("alpha", "mu", "zeta"))


# ── step 5: running each package, in order, one at a time ───────────────────

class RunPhaseTest(DeliveryFixture):
    def _packages(self, edges):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        for row in edges:
            name = row["name"] if isinstance(row, dict) else row
            self.write_ir(name, brownfield_ir(name))
            self.ships(name)
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": edges}

    def test_every_shipped_package_runs_in_dependency_order_one_at_a_time(self):
        self._packages([{"name": "gamma", "depends_on": ["beta"]},
                        {"name": "beta", "depends_on": ["alpha"]},
                        {"name": "alpha", "depends_on": []}])
        result = self.running_delivery().run()
        self.assertEqual(result["outcome"], "DELIVERED")
        self.assertEqual(self.started, ["alpha", "beta", "gamma"])
        self.assertEqual([row["outcome"] for row in result["runs"]],
                         ["ACCEPTED"] * 3)

    def test_a_blocked_run_halts_the_rest_and_names_the_lane_and_attempt(self):
        self._packages([{"name": "alpha", "depends_on": []},
                        {"name": "beta", "depends_on": ["alpha"]},
                        {"name": "gamma", "depends_on": ["beta"]}])
        self.run_script["beta"] = (0, [{
            "outcome": "BLOCKED", "run_id": "run-beta",
            "merged": ["lane-a"],
            "blocked": [{"node_id": "lane-b",
                         "reason": "SEMANTIC_BUDGET_EXHAUSTED"}]}])
        self.lanes["run-beta"] = ({"lane": "lane-b", "state": "BLOCKED",
                                   "attempt": 3,
                                   "reason": "SEMANTIC_BUDGET_EXHAUSTED"},)

        result = self.running_delivery().run()

        self.assertEqual(result["outcome"], "DELIVER_HALTED")
        # gamma is never started: the halt is a halt.
        self.assertEqual(self.started, ["alpha", "beta"])
        halted = result["halted_on"]
        self.assertEqual(halted["package"], "beta")
        self.assertEqual(halted["outcome"], "BLOCKED")
        self.assertEqual(halted["not_started"], ["gamma"])
        self.assertEqual(halted["lanes"], [
            {"lane": "lane-b", "state": "BLOCKED", "attempt": 3,
             "reason": "SEMANTIC_BUDGET_EXHAUSTED"}])

    def test_a_run_that_exits_zero_while_blocked_is_still_a_halt(self):
        # `run start` returns 0 whenever the scheduler reached quiescence, so
        # the exit status alone would report a blocked run as a success.
        self._packages([{"name": "alpha", "depends_on": []},
                        {"name": "beta", "depends_on": []}])
        self.run_script["alpha"] = (0, [{"outcome": "STUCK",
                                         "run_id": "run-alpha"}])
        result = self.running_delivery().run()
        self.assertEqual(result["outcome"], "DELIVER_HALTED")
        self.assertEqual(self.started, ["alpha"])

    def test_a_nonzero_run_start_is_a_halt_even_without_a_report(self):
        self._packages([{"name": "alpha", "depends_on": []}])
        self.run_script["alpha"] = (2, [{"detail": "no", "outcome":
                                         "INTEGRATION_BRANCH_CHECKED_OUT"}])
        result = self.running_delivery().run()
        self.assertEqual(result["outcome"], "DELIVER_HALTED")
        self.assertEqual(result["halted_on"]["outcome"],
                         "INTEGRATION_BRANCH_CHECKED_OUT")

    def test_an_already_accepted_package_is_not_run_again(self):
        self._packages([{"name": "alpha", "depends_on": []},
                        {"name": "beta", "depends_on": ["alpha"]}])
        self.already_accepted["alpha"] = "run-earlier"
        result = self.running_delivery().run()
        self.assertEqual(self.started, ["beta"])
        alpha = result["runs"][0]
        self.assertTrue(alpha["skipped"])
        self.assertEqual(alpha["run_id"], "run-earlier")
        self.assertEqual(result["outcome"], "DELIVERED")

    def test_an_already_shipped_package_is_not_regated_or_reauthored(self):
        self._packages([{"name": "alpha", "depends_on": []}])
        self.already_shipped.add("alpha")
        result = self.running_delivery().run()
        self.assertEqual(self.steps, [])
        self.assertEqual(self.removed, [])
        self.assertTrue(result["packages"][0]["shipped"])
        self.assertEqual(result["packages"][0]["attempts"], 0)

    def test_each_package_releases_the_previous_holds_before_it_starts(self):
        self._packages([{"name": "alpha", "depends_on": []},
                        {"name": "beta", "depends_on": ["alpha"]}])
        self.releases["beta"] = ("/state/runs/run-1/integration",)
        result = self.running_delivery().run()
        self.assertEqual(self.released, ["alpha", "beta"])
        beta = [row for row in result["runs"] if row["package"] == "beta"][0]
        self.assertEqual(beta["released"], ["/state/runs/run-1/integration"])

    def test_a_blocked_package_never_reaches_the_run_phase_at_all(self):
        self._packages([{"name": "alpha", "depends_on": []}])
        self.step_script[("gate", "alpha")] = (2, [{"outcome":
                                                    "PLAN_GATE_FAILED"}])
        result = self.running_delivery().run()
        self.assertEqual(result["outcome"], "DELIVER_BLOCKED")
        self.assertEqual(self.started, [])
        self.assertEqual(result["runs"], [])

    def test_no_run_start_reports_the_commands_instead_of_running_them(self):
        self._packages([{"name": "alpha", "depends_on": []}])
        result = self.delivery().run()
        self.assertEqual(result["outcome"], "DELIVERED_NOT_RUN")
        self.assertEqual(result["run_commands"], ["maestro run start alpha"])
        self.assertEqual(self.started, [])


class ResumabilityTest(DeliveryFixture):
    def test_a_recorded_package_set_is_reused_instead_of_reauthored(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        for name in ("alpha", "beta"):
            self.write_ir(name, brownfield_ir(name))
            self.ships(name)
        ledger = self.envelopes / "packages.json"
        ledger.write_text(json.dumps({
            "spec": self.spec,
            "packages": [{"name": "alpha", "depends_on": []},
                         {"name": "beta", "depends_on": ["alpha"]}],
        }), encoding="utf-8")

        result = self.running_delivery(ledger_path=ledger).run()

        self.assertEqual([turn["kind"] for turn in self.turns], [])
        self.assertEqual(result["order"], ["alpha", "beta"])

    def test_the_package_set_is_recorded_the_first_time_through(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", brownfield_ir("alpha"))
        self.ships("alpha")
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["alpha"]}
        ledger = self.envelopes / "packages.json"

        self.running_delivery(ledger_path=ledger).run()

        recorded = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(recorded["spec"], self.spec)
        self.assertEqual(recorded["packages"],
                         [{"name": "alpha", "depends_on": []}])

    def test_a_ledger_naming_a_vanished_package_is_not_trusted(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        self.write_ir("alpha", brownfield_ir("alpha"))
        self.ships("alpha")
        ledger = self.envelopes / "packages.json"
        ledger.write_text(json.dumps({
            "spec": self.spec,
            "packages": [{"name": "alpha"}, {"name": "vanished"}]}),
            encoding="utf-8")
        self.lane_script["brownfield"] = lambda _a: {
            "success": True, "summary": "", "packages": ["alpha"]}

        result = self.running_delivery(ledger_path=ledger).run()

        self.assertEqual([turn["kind"] for turn in self.turns], ["brownfield"])
        self.assertEqual(result["order"], ["alpha"])

    def test_a_ledger_for_a_different_document_is_ignored(self):
        ledger = self.envelopes / "packages.json"
        ledger.write_text(json.dumps({
            "spec": "docs/other.md", "packages": [{"name": "alpha"}]}),
            encoding="utf-8")
        self.assertIsNone(
            self.delivery(ledger_path=ledger)._remembered_packages())

    def test_resuming_after_a_halt_redoes_neither_the_ship_nor_the_run(self):
        self.write_ir("arch", ARCHITECTURE_IR)
        self.write_receipt("arch")
        for name in ("alpha", "beta", "gamma"):
            self.write_ir(name, brownfield_ir(name))
            self.ships(name)
        ledger = self.envelopes / "packages.json"
        ledger.write_text(json.dumps({"spec": self.spec, "packages": [
            {"name": "alpha", "depends_on": []},
            {"name": "beta", "depends_on": ["alpha"]},
            {"name": "gamma", "depends_on": ["beta"]}]}), encoding="utf-8")
        # The first `deliver` shipped all three and accepted the first two.
        self.already_shipped.update({"alpha", "beta", "gamma"})
        self.already_accepted.update({"alpha": "run-1", "beta": "run-2"})

        result = self.running_delivery(ledger_path=ledger).run()

        self.assertEqual([turn["kind"] for turn in self.turns], [])
        self.assertEqual(self.steps, [])
        self.assertEqual(self.started, ["gamma"])
        self.assertEqual(result["outcome"], "DELIVERED")


# ── the verb surface ────────────────────────────────────────────────────────

class DeliverVerbTest(unittest.TestCase):
    def test_deliver_takes_the_source_document_as_its_only_positional(self):
        parser = maestro.build_parser()
        self.assertIn("deliver", maestro.parser_verbs(parser))
        args = parser.parse_args(["deliver", "docs/spec.md"])
        self.assertEqual(args.spec, "docs/spec.md")
        self.assertIs(args.handler, maestro._deliver)
        verb = None
        for action in parser._actions:
            if isinstance(action, type(parser._subparsers._group_actions[0])):
                verb = action.choices.get("deliver")
        positionals = [action.dest for action in verb._actions
                       if not action.option_strings]
        self.assertEqual(positionals, ["spec"])

    def test_deliver_starts_runs_through_the_operator_verb_not_the_scheduler(self):
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _deliver_run_start("):
                      source.index("def _deliver_plan_bytes(")]
        # It resolves the run exactly as `maestro run start <name>` does, so a
        # run deliver starts is indistinguishable from one an operator starts.
        self.assertIn('argv = ("run", "start", name)', body)
        self.assertIn("_apply_repository_config(args, argv)", body)
        self.assertIn("_run_start(args)", body)
        # It never reaches past that verb into the scheduler itself.
        self.assertNotIn("scheduler.Scheduler", body)
        self.assertNotIn("SchedulerConfig", body)

    def test_no_run_suppresses_the_run_phase_entirely(self):
        parser = maestro.build_parser()
        self.assertFalse(parser.parse_args(["deliver", "s.md"]).no_run)
        self.assertTrue(
            parser.parse_args(["deliver", "s.md", "--no-run"]).no_run)
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        self.assertIn("run_start=None if args.no_run else _deliver_run_start",
                      source)

    def test_the_printed_commands_are_the_configured_named_plan_form(self):
        parser = maestro.build_parser()
        for command in deliver.run_commands(("alpha", "beta")):
            argv = command.split()
            self.assertEqual(argv[0], "maestro")
            args = parser.parse_args(argv[1:])
            self.assertEqual(args.command, "run")
            self.assertEqual(args.run_command, "start")

    def test_deliver_refuses_a_source_document_outside_the_repository(self):
        self.assertIn("DELIVER_SPEC_OUTSIDE_REPOSITORY",
                      Path(maestro.__file__).read_text(encoding="utf-8"))

    def test_the_author_block_is_optional_so_installed_configs_keep_working(self):
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        self.assertIn('("plan_contract", "author", "runners")', source)


class AuthorConfigurationTest(unittest.TestCase):
    """The `author:` block: same shape as `reviewer:` and `execution:`."""

    def _config(self, root, author):
        repo = root / "repo"
        (repo / "adws").mkdir(parents=True)
        (repo / "specs").mkdir()
        (repo / ".git").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        state = (repo / "../maestro-state").resolve() / repo.name
        (state / "route-receipts").mkdir(parents=True)
        payload = {
            "schema": "maestro-config.v1",
            "plans_dir": "specs",
            "state_root": "../maestro-state",
            "keys": {"verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                     "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                     "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY"},
            "executables": binaries,
            "route_receipts": {"omp": "route-receipts/omp.json",
                               "claude": "route-receipts/claude.json"},
            "reviewer": {"route": "omp", "model": "m", "effort": "high",
                         "finalization_timeout_s": 60, "turn_timeout_s": 20,
                         "poll_interval_s": 1},
            "execution": {"route": "omp", "model": "m", "effort": "high",
                          "concurrency": 1, "node_timeout_s": 60,
                          "turn_timeout_s": 30,
                          "final_acceptance_timeout_s": 45,
                          "backstop_t_s": 600, "semantic_ceiling": 3},
        }
        if author is not None:
            payload["author"] = author
        config_path = repo / "adws" / "maestro.config.yaml"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return repo, config_path

    def test_an_author_block_binds_the_opus_lane_onto_an_admitted_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), {
                "route": "claude", "model": "opus", "effort": "high",
                "author_timeout_s": 3600, "turn_timeout_s": 300,
                "poll_interval_s": 2})
            layout = maestro._load_maestro_layout(repo.resolve(), path)
            lane = maestro._deliver_author_lane(layout)
            self.assertEqual((lane.route, lane.model, lane.effort),
                             ("claude", "opus", "high"))
            self.assertIsNone(lane.profile)

    def test_a_configuration_without_an_author_block_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), None)
            layout = maestro._load_maestro_layout(repo.resolve(), path)
            self.assertIsNone(layout["author"])
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                maestro._deliver_author_lane(layout)
            self.assertIn("requires an author: block", str(caught.exception))

    def test_an_author_route_with_no_route_receipt_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), {
                "route": "nowhere", "model": "opus", "effort": "high",
                "author_timeout_s": 60, "turn_timeout_s": 30,
                "poll_interval_s": 1})
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                maestro._load_maestro_layout(repo.resolve(), path)
            self.assertIn("author.route", str(caught.exception))

    def test_an_unknown_author_key_is_refused_rather_than_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), {
                "route": "claude", "model": "opus", "effort": "high",
                "author_timeout_s": 60, "turn_timeout_s": 30,
                "poll_interval_s": 1, "tempreture": 0})
            with self.assertRaises(maestro._MaestroConfigurationError):
                maestro._load_maestro_layout(repo.resolve(), path)

    def test_the_shipped_configuration_carries_the_lane_deliver_needs(self):
        import yaml
        path = Path(maestro.__file__).parent / "maestro.config.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["author"]["route"], "claude")
        self.assertEqual(payload["author"]["model"], "opus")
        self.assertIn(payload["author"]["route"], payload["route_receipts"])


class DeliverEndToEndTest(AuthorConfigurationTest):
    """`maestro deliver` through `main`, as far as it gets without an agent."""

    @contextlib.contextmanager
    def _cwd(self, path):
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)

    def _author(self):
        return {"route": "claude", "model": "opus", "effort": "high",
                "author_timeout_s": 60, "turn_timeout_s": 30,
                "poll_interval_s": 1}

    def _invoke(self, repo, argv, environment):
        stream = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=False), \
                self._cwd(repo), contextlib.redirect_stdout(stream):
            status = maestro.main(argv)
        payloads = [json.loads(line) for line in stream.getvalue().splitlines()
                    if line.strip().startswith("{")]
        return status, payloads

    def _keys(self):
        from adw_modules import receipt_crypto
        signing = receipt_crypto.generate_seed()
        route = receipt_crypto.generate_seed()
        return {
            "MAESTRO_TEST_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(signing).hex(),
            "MAESTRO_TEST_SIGNING_SEED": signing.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(route).hex(),
        }

    def test_a_source_document_that_does_not_exist_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _path = self._config(Path(tmp), self._author())
            status, payloads = self._invoke(
                repo, ["deliver", "docs/nope.md"], self._keys())
            self.assertEqual(status, 3)
            self.assertEqual(payloads[0]["outcome"], "DELIVER_SPEC_MISSING")
            self.assertIn("docs/nope.md", payloads[0]["detail"])

    def test_a_source_document_outside_the_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _path = self._config(Path(tmp), self._author())
            outside = Path(tmp) / "outside.md"
            outside.write_text("# spec\n", encoding="utf-8")
            status, payloads = self._invoke(
                repo, ["deliver", str(outside)], self._keys())
            self.assertEqual(status, 3)
            self.assertEqual(payloads[0]["outcome"],
                             "DELIVER_SPEC_OUTSIDE_REPOSITORY")

    def test_a_repository_with_no_author_block_refuses_before_any_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _path = self._config(Path(tmp), None)
            (repo / "spec.md").write_text("# spec\n", encoding="utf-8")
            status, payloads = self._invoke(
                repo, ["deliver", "spec.md"], self._keys())
            self.assertEqual(status, 3)
            self.assertEqual(payloads[0]["outcome"],
                             "MAESTRO_CONFIGURATION_INVALID")
            self.assertIn("author:", payloads[0]["detail"])

    def test_the_plan_directory_a_reship_clears_may_not_escape_plans_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), self._author())
            with mock.patch.dict(os.environ, self._keys(), clear=False):
                layout = maestro._load_maestro_layout(repo.resolve(), path)
            victim = repo / "victim"
            victim.mkdir()
            for hostile in ("../victim", "..", "nested/plan"):
                with self.assertRaises(maestro._MaestroConfigurationError):
                    maestro._deliver_remove_plan(layout, hostile)
            self.assertTrue(victim.is_dir())

    def test_a_reship_removes_only_the_named_plan_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = self._config(Path(tmp), self._author())
            with mock.patch.dict(os.environ, self._keys(), clear=False):
                layout = maestro._load_maestro_layout(repo.resolve(), path)
            doomed = layout["plans_dir"] / "alpha"
            doomed.mkdir()
            (doomed / "maestro-plan.v1").write_text("{}", encoding="utf-8")
            keeper = layout["plans_dir"] / "beta"
            keeper.mkdir()
            maestro._deliver_remove_plan(layout, "alpha")
            self.assertFalse(doomed.exists())
            self.assertTrue(keeper.is_dir())


class FindingParsingTest(unittest.TestCase):
    def test_planctl_diagnostics_are_read_out_of_the_captured_stdout(self):
        payload = {"outcome": "PLAN_GATE_FAILED", "step": "validate",
                   "stdout": '{"valid": false, "diagnostics": '
                             '["maestro.command", "maestro.lane_gate"]}'}
        codes = [item.message for item in deliver.step_findings("gate", [payload])
                 if item.code == "planctl"]
        self.assertEqual(codes, ["maestro.command", "maestro.lane_gate"])

    def test_a_clear_cell_is_never_reported_as_a_finding(self):
        report = {"cells": [
            {"check_id": "a", "object_id": "plan", "status": "clear"},
            {"check_id": "b", "object_id": "plan", "status": "finding",
             "message": "no"},
        ]}
        found = deliver.report_findings(report)
        self.assertEqual([item.code for item in found], ["b"])

    def test_a_missing_report_yields_no_findings_rather_than_raising(self):
        self.assertEqual(deliver.report_findings(None), ())


class RunStartIsolationTest(unittest.TestCase):
    """The one behaviour that must not regress: `deliver` stops before a run."""

    def test_the_module_never_references_the_run_verb_as_something_to_execute(self):
        source = (Path(deliver.__file__)).read_text(encoding="utf-8")
        self.assertIn("maestro run start ", source)
        # It builds the string; it never spawns it.
        for spawn in ("subprocess", "os.system", "os.exec", "Popen"):
            self.assertNotIn(spawn, source)


class AuthorTurnEnvironmentTest(unittest.TestCase):
    """§8.3 binds the author's pane exactly as it binds a node's or a
    reviewer's.

    `LaunchSpec.environment` defaults to an empty mapping, and this launch was
    one of three constructions that never passed one. `pane_env_flags` refuses
    an incomplete redirection rather than degrading it — a redirect that
    silently fails to arrive convicts an agent for a harness defect — so the
    omission is a hard `SCRATCH_REDIRECT_MISSING` refusal the moment the verb
    runs, not a quiet cache written into the repository.
    """

    def test_the_author_turn_launch_carries_every_scratch_redirection(self):
        from adw_modules import launcher as lch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            session_root = root / "state" / "deliver"
            session_root.mkdir(parents=True)
            envelope = root / "state" / "envelope.json"

            captured = {}

            class FakeRunner:
                def launch(self, spec):
                    captured["spec"] = spec
                    envelope.parent.mkdir(parents=True, exist_ok=True)
                    envelope.write_text(json.dumps({"success": True}),
                                        encoding="utf-8")
                    # An empty pane id keeps the best-effort pane close from
                    # spawning anything: this test owns no terminal.
                    return lch.LaunchHandle(
                        correlation_token=spec.correlation_token, pane_id="",
                        agent_name="author", launched_cwd=spec.worktree,
                        environment=spec.environment)

                def poll(self, _handle):
                    return lch.PollResult(lch.PollState.EXITED, 0, "")

                def cancel(self, _handle, _deadline):
                    return None

            lane = deliver.AuthorLane(
                route="claude", model="opus", effort="high", profile=None,
                author_timeout_s=5.0, turn_timeout_s=5.0, poll_interval_s=0.01)
            turn = maestro._deliver_author_turn(
                {"repo": str(repo), "executables": {"herdr": "/bin/true"}},
                lane, cast(lch.HerdrLauncher, FakeRunner()), session_root)
            self.assertEqual(turn("spec", "write it", envelope),
                             {"success": True})

            spec = captured["spec"]
            # The assertion that matters is that `pane_env_flags` does not
            # refuse: it is the function that raised in production.
            flags = lch.pane_env_flags(spec.environment)
            pane_env = {}
            for index, token in enumerate(flags):
                if token == "--env":
                    key, _, value = flags[index + 1].partition("=")
                    pane_env[key] = value
            self.assertEqual(set(pane_env), set(lch.SCRATCH_ENV_KEYS))
            # Beside this turn's own session directory, under the harness-owned
            # session root — never inside the repository being authored against.
            for key, value in pane_env.items():
                path = (value.split("cache_dir=", 1)[-1]
                        if key == "PYTEST_ADDOPTS" else value)
                self.assertTrue(Path(path).is_relative_to(session_root), key)
                self.assertFalse(Path(path).is_relative_to(repo), key)
            self.assertEqual(
                Path(pane_env["XDG_CACHE_HOME"]).parent,
                spec.session_dir.with_name(spec.session_dir.name + ".scratch"))


class DeliverReleaseSafetyTest(unittest.TestCase):
    """Only conclusively terminal Maestro runs release their worktrees."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.plans = self.root / "plans"
        plan = self.plans / "alpha"
        plan.mkdir(parents=True)
        (plan / "maestro-plan.v1").write_text("plan", encoding="utf-8")
        self.state = self.root / "state"
        self.occupant = self.state / "runs" / "run-old" / "integration"
        self.occupant.mkdir(parents=True)
        self.database = self.root / "lifecycle.db"
        self.config = {
            "repo": self.repo,
            "plans_dir": self.plans,
            "repository_state": self.state,
            "database": self.database,
        }

    def record_outcome(self, outcome):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY, latest_outcome TEXT)")
            connection.execute(
                "INSERT INTO runs (run_id, latest_outcome) VALUES (?, ?)",
                ("run-old", outcome))

    def parsed_plan(self):
        return SimpleNamespace(merge_policy=SimpleNamespace(
            integration_branch="integration/alpha"))

    def test_a_resumable_integration_worktree_is_preserved(self):
        self.record_outcome("BLOCKED")
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=self.occupant), mock.patch.object(
                        maestro.subprocess, "run") as run:
            self.assertEqual(maestro._deliver_release_run(self.config, "alpha"), ())
        run.assert_not_called()

    def test_an_active_integration_worktree_is_preserved(self):
        self.record_outcome(None)
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=self.occupant), mock.patch.object(
                        maestro.subprocess, "run") as run:
            self.assertEqual(maestro._deliver_release_run(self.config, "alpha"), ())
        run.assert_not_called()

    def test_an_accepted_run_worktree_is_removed_without_force(self):
        self.record_outcome("ACCEPTED")
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=self.occupant), mock.patch.object(
                        maestro.subprocess, "run",
                        return_value=subprocess.CompletedProcess((), 0)) as run:
            self.assertEqual(
                maestro._deliver_release_run(self.config, "alpha"),
                (str(self.occupant),))
        self.assertEqual(run.call_args_list, [
            mock.call(
                ("git", "-C", str(self.repo), "worktree", "remove",
                 str(self.occupant)),
                capture_output=True, text=True, check=False),
            mock.call(
                ("git", "-C", str(self.repo), "worktree", "prune"),
                capture_output=True, text=True, check=False),
        ])

    # -- what the three cases above imply but do not themselves pin ----------

    def _release(self, discard_live=False):
        """`_deliver_release_run` with git stubbed and stderr captured."""
        stderr = io.StringIO()
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=self.occupant), mock.patch.object(
                        maestro.subprocess, "run",
                        return_value=subprocess.CompletedProcess((), 0)) as run:
            with contextlib.redirect_stderr(stderr):
                released = maestro._deliver_release_run(
                    self.config, "alpha", discard_live)
        return released, stderr.getvalue(), run

    def test_the_refusal_names_the_run_and_the_state_that_refused(self):
        """A refusal an operator cannot act on is a refusal that gets forced."""
        self.record_outcome("BLOCKED")
        released, reported, run = self._release()
        self.assertEqual(released, ())
        run.assert_not_called()
        self.assertIn("run-old", reported)
        self.assertIn("BLOCKED", reported)
        self.assertIn("--discard-live-runs", reported)

    def test_a_run_with_no_declared_outcome_says_so_rather_than_guessing(self):
        self.record_outcome(None)
        released, reported, _ = self._release()
        self.assertEqual(released, ())
        self.assertIn("run-old", reported)
        self.assertIn("no declared outcome", reported)

    def test_an_unrecorded_run_is_treated_as_live_not_as_finished(self):
        """No row at all is the crashed-run case, and it is not evidence."""
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " latest_outcome TEXT)")
        released, reported, run = self._release()
        self.assertEqual(released, ())
        run.assert_not_called()
        self.assertIn("run-old", reported)

    def test_the_operator_override_discards_a_live_run_deliberately(self):
        """§11.3: the escape exists, and nothing but the flag reaches it."""
        self.record_outcome("BLOCKED")
        released, _, run = self._release(discard_live=True)
        self.assertEqual(released, (str(self.occupant),))
        self.assertEqual(run.call_args_list[0], mock.call(
            ("git", "-C", str(self.repo), "worktree", "remove",
             str(self.occupant)),
            capture_output=True, text=True, check=False))

    def test_a_cancelled_run_an_operator_can_reopen_is_preserved(self):
        """`run cancel` is reopenable, so its checkout is not backlog."""
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " latest_outcome TEXT, cancel_cause TEXT)")
            connection.execute(
                "INSERT INTO runs (run_id, latest_outcome, cancel_cause)"
                " VALUES (?, ?, ?)", ("run-old", "CANCELLED", "RUN_CANCEL"))
        released, reported, run = self._release()
        self.assertEqual(released, ())
        run.assert_not_called()
        self.assertIn("RUN_CANCEL", reported)

    def test_a_discarded_run_is_over_and_its_checkout_is_reclaimed(self):
        """`run cancel --discard` ends a run for good, so this one is litter."""
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " latest_outcome TEXT, cancel_cause TEXT)")
            connection.execute(
                "INSERT INTO runs (run_id, latest_outcome, cancel_cause)"
                " VALUES (?, ?, ?)", ("run-old", "CANCELLED", "DISCARDED"))
        released, _, _ = self._release()
        self.assertEqual(released, (str(self.occupant),))

    def test_an_unreadable_ledger_preserves_rather_than_removes(self):
        """An unmeasured run is not a finished one; fail closed."""
        self.database.write_bytes(b"not a database at all")
        released, reported, run = self._release()
        self.assertEqual(released, ())
        run.assert_not_called()
        self.assertIn("unreadable lifecycle ledger", reported)

    def test_a_worktree_that_will_not_come_away_is_reported_not_forced(self):
        """Unforced removal: git's refusal is evidence, not noise (§8.8)."""
        self.record_outcome("ACCEPTED")
        stderr = io.StringIO()
        refused = subprocess.CompletedProcess(
            (), 1, stdout="", stderr="fatal: contains modified files")
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=self.occupant), mock.patch.object(
                        maestro.subprocess, "run",
                        return_value=refused) as run:
            with contextlib.redirect_stderr(stderr):
                released = maestro._deliver_release_run(self.config, "alpha")
        self.assertEqual(released, ())
        self.assertNotIn("--force", str(run.call_args_list))
        self.assertIn("contains modified files", stderr.getvalue())

    def test_an_operators_own_checkout_is_never_reached_by_the_state_check(self):
        """Containment still decides first; a foreign worktree is untouched."""
        self.record_outcome("BLOCKED")
        outside = self.root / "operator" / "integration"
        outside.mkdir(parents=True)
        with mock.patch.object(
                maestro.plan_model, "parse_bytes",
                return_value=self.parsed_plan()), mock.patch.object(
                    maestro, "_worktree_holding_branch",
                    return_value=outside), mock.patch.object(
                        maestro.subprocess, "run",
                        return_value=subprocess.CompletedProcess((), 0)) as run:
            self.assertEqual(
                maestro._deliver_release_run(self.config, "alpha"), ())
        self.assertEqual(run.call_args_list, [mock.call(
            ("git", "-C", str(self.repo), "worktree", "prune"),
            capture_output=True, text=True, check=False)])


class DeliverPlanRemovalSafetyTest(unittest.TestCase):
    """The same predicate over the other destructive step inside `deliver`.

    `_deliver_remove_plan` proved *whose* directory it was deleting and never
    whether anything still needed it. A run is bound to plan bytes by their
    digest, so clearing the directory under a resumable run destroys the
    evidence `_deliver_accepted_run` finds that run by.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved, because `_deliver_remove_plan` compares a resolved
        # directory against the configured `plans_dir` as given, and the real
        # configuration hands it a resolved path.
        self.root = Path(self._tmp.name).resolve()
        self.plans = self.root / "plans"
        self.directory = self.plans / "alpha"
        self.directory.mkdir(parents=True)
        self.plan_bytes = b"plan bytes"
        (self.directory / "maestro-plan.v1").write_bytes(self.plan_bytes)
        self.database = self.root / "lifecycle.db"
        self.config = {
            "plans_dir": self.plans,
            "repository_state": self.root / "state",
            "database": self.database,
        }

    def record(self, outcome):
        digest = maestro.plan_digest.digest_of(self.plan_bytes)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " plan_digest TEXT, latest_outcome TEXT)")
            connection.execute(
                "INSERT INTO runs (run_id, plan_digest, latest_outcome)"
                " VALUES (?, ?, ?)", ("run-old", digest, outcome))

    def test_a_resumable_runs_plan_bytes_are_not_deleted(self):
        self.record("BLOCKED")
        with self.assertRaises(deliver.DeliverError) as raised:
            maestro._deliver_remove_plan(self.config, "alpha")
        self.assertIn("run-old", str(raised.exception))
        self.assertIn("BLOCKED", str(raised.exception))
        self.assertTrue(self.directory.is_dir())

    def test_an_accepted_runs_plan_bytes_are_cleared_for_the_re_ship(self):
        self.record("ACCEPTED")
        maestro._deliver_remove_plan(self.config, "alpha")
        self.assertFalse(self.directory.is_dir())

    def test_plan_bytes_no_run_is_keyed_to_are_cleared(self):
        maestro._deliver_remove_plan(self.config, "alpha")
        self.assertFalse(self.directory.is_dir())

    def test_the_operator_override_clears_a_live_runs_plan_bytes(self):
        self.record("BLOCKED")
        maestro._deliver_remove_plan(self.config, "alpha", True)
        self.assertFalse(self.directory.is_dir())

    def test_an_unreadable_ledger_preserves_the_plan_bytes(self):
        self.database.write_bytes(b"not a database at all")
        with self.assertRaises(deliver.DeliverError):
            maestro._deliver_remove_plan(self.config, "alpha")
        self.assertTrue(self.directory.is_dir())


if __name__ == "__main__":
    unittest.main()
