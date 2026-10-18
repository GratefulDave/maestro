"""`attend.*` is an opt-in a deployment makes in its own config file.

`runtime_sync` holds `maestro.config.yaml` back from every mirror, which is
what makes this key safe: mirroring the runtime into a deployment can never
turn `run attend` on there. The template ships 0, absent means 0, and 0 is
what the verb refuses on -- the same shape `concurrency` uses, and for the
same reason.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402


CLAUDE_ROUTE = {"route": "claude", "model": "opus", "effort": "high"}
#: The smallest enabled section: an author on a declared vendor and an
#: independent reviewer on a different one.
ENABLED = {
    "route": dict(CLAUDE_ROUTE, vendor="vendor-a"),
    "reviewer_route": {"route": "omp", "profile": "profile-b"},
    "reviewer_vendor": "vendor-b",
}


class AttendConfig(unittest.TestCase):
    def test_an_absent_section_disables_the_verb(self) -> None:
        policy = maestro._attend_policy({"attend": maestro._canonical_attend(None)})
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.max_amendments_per_lane, 0)
        self.assertEqual(policy.max_amendments_per_run, 10)

    def test_the_template_ships_the_verb_off(self) -> None:
        loaded = yaml.safe_load((ADWS / "maestro.config.yaml").read_text())
        policy = maestro._attend_policy(
            {"attend": maestro._canonical_attend(loaded.get("attend"))}
        )
        self.assertFalse(policy.enabled)

    def test_a_bound_lane_cap_enables_the_verb(self) -> None:
        canonical = maestro._canonical_attend(
            {"max_amendments_per_lane": 2, **ENABLED}
        )
        policy = maestro._attend_policy({"attend": canonical})
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.max_amendments_per_lane, 2)
        self.assertEqual(policy.route["model"], "opus")

    def test_enabling_the_verb_without_a_route_refuses(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend({"max_amendments_per_lane": 1})
        self.assertIn("attend.route", str(caught.exception))

    def test_an_attend_route_is_validated_like_a_lane_route(self) -> None:
        # An omp profile carrying a Claude model is the shape that used to be
        # accepted here and refused at dispatch, in front of an operator
        # waiting on a parked run.
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_attend(
                {
                    "max_amendments_per_lane": 1,
                    "route": {"route": "omp", "profile": "grok", "model": "opus"},
                }
            )

    def test_a_relative_validator_path_refuses(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(
                {
                    "max_amendments_per_lane": 1,
                    **ENABLED,
                    "planctl": "scripts/planctl.py",
                }
            )
        self.assertIn("absolute", str(caught.exception))

    def test_an_unknown_attend_field_refuses(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend({"max_amendmnets_per_lane": 2})
        self.assertIn("unsupported", str(caught.exception))

    def test_a_negative_bound_refuses(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_attend({"max_amendments_per_lane": -1})

    def test_an_enabled_verb_binds_the_reviewer_agent_identity(self) -> None:
        canonical = maestro._canonical_attend({"max_amendments_per_lane": 1, **ENABLED})
        policy = maestro._attend_policy({"attend": canonical})
        # The author's vendor rides beside the route, never inside it: route
        # admission binds the route mapping.
        self.assertNotIn("vendor", canonical["route"])
        self.assertEqual(canonical["vendor"], "vendor-a")
        self.assertEqual(policy.reviewer_vendor, "vendor-b")
        self.assertEqual(policy.reviewer_id, "attend-reviewer:omp:profile-b")
        self.assertNotEqual(policy.reviewer_id, "maestro-attend")
        self.assertEqual(policy.max_review_rounds, 3)

    def test_enabling_the_verb_without_a_reviewer_refuses(self) -> None:
        section = {"max_amendments_per_lane": 1, **ENABLED}
        del section["reviewer_route"]
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(section)
        self.assertIn("attend.reviewer_route", str(caught.exception))

    def test_a_reviewer_on_the_authors_vendor_is_refused(self) -> None:
        for reviewer_vendor in ("vendor-a", "  Vendor-A "):
            with self.subTest(reviewer_vendor=reviewer_vendor):
                with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                    maestro._canonical_attend(
                        {
                            "max_amendments_per_lane": 1,
                            **ENABLED,
                            "reviewer_vendor": reviewer_vendor,
                        }
                    )
                self.assertIn("must differ", str(caught.exception))

    def test_an_undeclared_vendor_on_either_side_is_refused(self) -> None:
        no_reviewer_vendor = {"max_amendments_per_lane": 1, **ENABLED}
        del no_reviewer_vendor["reviewer_vendor"]
        no_author_vendor = {
            "max_amendments_per_lane": 1, **ENABLED, "route": CLAUDE_ROUTE
        }
        for section, key in (
            (no_reviewer_vendor, "attend.reviewer_vendor"),
            (no_author_vendor, "attend.route.vendor"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                    maestro._canonical_attend(section)
                self.assertIn(key, str(caught.exception))

    def test_a_reviewer_route_is_validated_like_a_lane_route(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(
                {
                    "max_amendments_per_lane": 1,
                    **ENABLED,
                    "reviewer_route": {"route": "omp", "profile": "p", "model": "m"},
                }
            )
        self.assertIn("attend.reviewer_route", str(caught.exception))

    def test_zero_review_rounds_cannot_enable_the_verb(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(
                {"max_amendments_per_lane": 1, **ENABLED, "max_review_rounds": 0}
            )
        self.assertIn("max_review_rounds", str(caught.exception))

    def test_the_removed_self_signing_identity_key_is_refused(self) -> None:
        # The author used to answer its own review and Maestro signed it as a
        # configured `reviewer_id`. The reviewer is now an agent, and its
        # identity is the admitted route, so the key is gone.
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(
                {"max_amendments_per_lane": 1, **ENABLED, "reviewer_id": "x"}
            )
        self.assertIn("unsupported", str(caught.exception))

    def test_the_reviewer_route_is_admitted_by_the_route_receipt(self) -> None:
        """An unadmitted reviewer route refuses the actor, like the author's."""
        from unittest import mock

        admitted = mock.Mock()
        admitted.admits.side_effect = lambda route: route == "claude"
        layout = {
            "role_routes": {
                role: {"route": "claude", "model": "opus", "effort": "high", "profile": ""}
                for role in maestro.lch.LANE_PANE_ROLES
            },
            "route_receipts": {"claude": "/nonexistent"},
            "route_verify_keys": ("/nonexistent",),
            "attend": maestro._canonical_attend(
                {"max_amendments_per_lane": 1, **ENABLED}
            ),
        }
        with mock.patch.object(maestro, "load_public_key"), mock.patch.object(
            maestro, "load_admitted_routes", return_value=admitted
        ):
            with self.assertRaises(maestro.FactoryRefused) as caught:
                maestro._actor_for(None, layout, None, "run-1", None)
        self.assertIn("ROUTE_RECEIPTS_REQUIRED", str(caught.exception))
        self.assertIn(mock.call("omp"), admitted.admits.call_args_list)

    def test_the_verb_is_on_the_parser(self) -> None:
        args = maestro.build_parser().parse_args(["run", "attend", "--run", "run-1"])
        self.assertEqual(args.run_id, "run-1")
        self.assertIs(args.handler, maestro._run_attend)

    def test_the_disabled_refusal_names_the_key_to_set(self) -> None:
        # The refusal an operator sees is the whole opt-in documentation for
        # someone who did not read the config file.
        self.assertEqual(att.DISABLED, "ATTEND_DISABLED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class PlanIrResolution(unittest.TestCase):
    """Where the IR an attended revision edits comes from."""

    def setUp(self) -> None:
        import tempfile

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.plans = Path(holder.name) / "plans"
        self.plans.mkdir()

    def _policy(self, plan_ir: Path | None = None) -> att.AttendPolicy:
        return att.AttendPolicy(max_amendments_per_lane=1, plan_ir=plan_ir)

    def test_an_attend_written_revision_is_preferred(self) -> None:
        written = self.plans / "run-1.r3.ir.json"
        written.write_text("{}", encoding="utf-8")
        configured = self.plans / "authored.ir.json"
        configured.write_text("{}", encoding="utf-8")
        self.assertEqual(
            maestro._attend_plan_ir(self._policy(configured), self.plans, "run-1", 3),
            written,
        )

    def test_the_configured_ir_answers_the_first_attend(self) -> None:
        configured = self.plans / "authored.ir.json"
        configured.write_text("{}", encoding="utf-8")
        self.assertEqual(
            maestro._attend_plan_ir(self._policy(configured), self.plans, "run-1", 1),
            configured,
        )

    def test_no_ir_refuses_rather_than_offering_the_projected_plan(self) -> None:
        # The pinned plan artifact is a projection of an IR. Handing it to an
        # agent asked to edit an IR produces a revision the ingress cannot
        # read and a refusal three steps later that names none of this.
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._attend_plan_ir(self._policy(), self.plans, "run-1", 1)
        self.assertEqual(caught.exception.code, att.PLAN_IR_UNRESOLVED)
        self.assertIn("run-1.r1.ir.json", caught.exception.detail)
        self.assertIn("attend.plan_ir", caught.exception.detail)

    def test_a_relative_configured_ir_refuses_at_config_time(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._canonical_attend(
                {
                    "max_amendments_per_lane": 1,
                    **ENABLED,
                    "plan_ir": ".maestro/plans/plan.ir.json",
                }
            )
        self.assertIn("absolute", str(caught.exception))


class AttendSurfacesSchedulerFailure(unittest.TestCase):
    """A scheduler failure inside `run attend` is reported as itself.

    FDAdb run be064e58: restore_layout raised LaunchFailed, and the STOP
    record's `len(applied)` replaced that traceback with UnboundLocalError.
    """

    def test_a_non_attend_exception_propagates_and_is_recorded(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        class LaunchBoom(RuntimeError):
            pass

        stops: list[dict] = []

        class Store:
            def record_attend_session(self, run_id, *, session_id, phase, payload):
                if phase == "STOP":
                    stops.append(dict(payload))

            def active_projection(self, run_id):
                return ()

            def close(self):
                pass

        runtime = SimpleNamespace(
            path=Path("/nonexistent"),
            close=lambda: None,
            ledger_path=lambda: Path("/nonexistent/lifecycle.sqlite3"),
        )
        row = {
            "plan_revision": 1,
            "target_repository_root": "/nonexistent/repo",
            "target_main_ref": "refs/heads/main",
            "plan_digest": "0" * 64,
        }
        layout = {"attend": {"max_amendments_per_lane": 3, "route": CLAUDE_ROUTE}}
        compiled = SimpleNamespace(plan_revision=1, lanes=())

        def boom(**_kwargs):
            raise LaunchBoom("BINDING_MISMATCH:DUPLICATE_ROLE_PANE:test-reviewer")

        with mock.patch.object(
            maestro,
            "_bind_existing_run",
            return_value=(layout, runtime, Store(), row, object(), compiled),
        ), mock.patch.object(maestro, "maybe_autoload_dashboard"), mock.patch.object(
            maestro, "plan_artifact_ref_for", return_value="/nonexistent/plans/p.json"
        ), mock.patch.object(
            maestro.step_log, "RunReporter"
        ), mock.patch.object(
            maestro.att, "attend_run", side_effect=boom
        ):
            with self.assertRaises(LaunchBoom):
                maestro._run_attend(SimpleNamespace(run_id="run-1"))
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["stop_reason"], "LaunchBoom")
        self.assertEqual(stops[0]["revisions_applied"], 0)
