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
            {"max_amendments_per_lane": 2, "route": CLAUDE_ROUTE}
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
                    "route": CLAUDE_ROUTE,
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
                    "route": CLAUDE_ROUTE,
                    "plan_ir": ".maestro/plans/plan.ir.json",
                }
            )
        self.assertIn("absolute", str(caught.exception))
