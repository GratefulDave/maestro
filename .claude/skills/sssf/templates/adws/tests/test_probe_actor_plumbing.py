"""Vendors from the loaded layout to the actor, and the acceptance dispatch.

Covers the WP3 half of the probe contract that the scheduler calls: declared
vendors reach `HerdrStageActor` through `_actor_for` (never inside the route
mapping), `acceptor_for` follows them, and `accept_probes` is a read-only pass
in this lane's own session.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import yaml

import maestro
from adw_modules import probes

from tests import probe_actor_bench as pb

_ITEM = {
    "probe_id": "p-abc",
    "path": "probes/test_negative.py",
    "source": "def test_negative():\n    assert False\n",
    "requirement": pb.CONTRACT_QUOTE,
    "failing_case_ids": ["probes.test_negative::test_negative"],
    "failure_lines": ["assert False"],
}


def _acceptor(verdict: str = "PASS"):
    def respond(spec: Any, prompt: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "verdict": verdict,
            "probe_verdicts": [
                {"probe_id": item["probe_id"], "verdict": verdict, "reason": "ok"}
                for item in prompt["probes"]
            ],
        }

    return respond


class LayoutToActorTest(unittest.TestCase):
    def _layout(self, root: Path, vendors: Mapping[str, str] | None) -> dict[str, Any]:
        text = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(text)
        for role, vendor in (vendors or {}).items():
            config["role_routes"][role]["vendor"] = vendor
        config["runtime_state_root"] = str(root / "state")
        path = root / "adws" / "maestro.config.yaml"
        path.parent.mkdir()
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        layout = maestro._load_maestro_config(root, path)
        layout["route_receipts"] = {"omp": str(root / "r1"), "claude": str(root / "r2")}
        layout["route_verify_keys"] = [str(root / "k")]
        return layout

    def _actor(self, vendors: Mapping[str, str] | None) -> maestro.HerdrStageActor:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = self._layout(root, vendors)
            (root / "bench").mkdir()
            bench = pb.Bench(str(root / "bench"), _acceptor())
            runtime = SimpleNamespace(path=bench.state)
            compiled = SimpleNamespace(plan_bytes=json.dumps({"lanes": []}).encode())
            admitted = SimpleNamespace(admits=lambda route: True)
            with (
                mock.patch.object(maestro, "load_public_key", return_value=object()),
                mock.patch.object(maestro, "load_admitted_routes", return_value=admitted),
                mock.patch.object(maestro.lch, "HerdrLauncher", return_value=bench.launcher),
            ):
                return maestro._actor_for(runtime, layout, bench.target, "run-1", compiled)  # type: ignore[arg-type]

    def test_declared_vendors_reach_the_actor(self) -> None:
        actor = self._actor({"code-reviewer": "OpenAI", "test-reviewer": "anthropic"})
        self.assertEqual(actor.role_vendors["code-reviewer"], "openai")
        self.assertEqual(actor.role_vendors["test-reviewer"], "anthropic")
        self.assertIsNone(actor.role_vendors["tester"])
        self.assertEqual(actor.acceptor_for(mock.Mock()), "test-reviewer")

    def test_vendors_never_ride_inside_the_route_mapping(self) -> None:
        actor = self._actor({"code-reviewer": "openai", "test-reviewer": "anthropic"})
        for route in actor.role_routes.values():
            self.assertNotIn("vendor", route)

    def test_an_actor_built_without_vendors_is_not_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = pb.Bench(tmp, _acceptor())
            self.assertIsNone(bench.actor.acceptor_for(mock.Mock()))
            self.assertIsInstance(
                probes.select_acceptor(bench.actor.role_vendors), probes.Rejection
            )
            self.assertEqual(
                probes.select_acceptor(bench.actor.role_vendors).code,
                probes.PROBE_ACCEPTOR_NOT_INDEPENDENT,
            )

    def test_template_vendors_unset_means_no_acceptor(self) -> None:
        self.assertIsNone(self._actor(None).acceptor_for(mock.Mock()))


class AcceptorSelectionTest(unittest.TestCase):
    def _for(self, vendors: Mapping[str, str | None] | None) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            return pb.Bench(tmp, _acceptor(), role_vendors=vendors).actor.acceptor_for(mock.Mock())

    def test_different_vendors_select_the_test_reviewer(self) -> None:
        self.assertEqual(self._for(pb.DIFFERENT_VENDORS), "test-reviewer")

    def test_falls_back_to_the_tester_when_only_it_differs(self) -> None:
        self.assertEqual(
            self._for({"code-reviewer": "openai", "test-reviewer": "openai", "tester": "xai"}),
            "tester",
        )

    def test_equal_or_undeclared_vendors_select_nothing(self) -> None:
        for vendors in (
            {"code-reviewer": "openai", "test-reviewer": "openai", "tester": "openai"},
            {"test-reviewer": "anthropic", "tester": "xai"},
            {"code-reviewer": "openai"},
        ):
            self.assertIsNone(self._for(vendors), vendors)


class AcceptProbesTest(unittest.TestCase):
    def _bench(self, tmp: str, verdict: str = "PASS") -> pb.Bench:
        return pb.Bench(tmp, _acceptor(verdict), role_vendors=pb.DIFFERENT_VENDORS)

    def test_returns_an_envelope_the_strict_parser_accepts(self) -> None:
        for role in probes.ACCEPTOR_ROLES:
            for mode in (probes.MODE_ACCEPT, probes.MODE_CHALLENGE):
                with self.subTest(role=role, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    bench = self._bench(tmp)
                    raw = bench.actor.accept_probes(
                        pb.context(bench.head), role, [_ITEM], mode
                    )
                    self.assertEqual(
                        probes.parse_acceptance(raw, ["p-abc"], mode),
                        {"p-abc": ("PASS", "ok")},
                    )

    def test_dispatch_is_an_acceptance_pass_not_an_authoring_turn(self) -> None:
        for role in probes.ACCEPTOR_ROLES:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                bench = self._bench(tmp)
                bench.actor.accept_probes(
                    pb.context(bench.head), role, [_ITEM], probes.MODE_ACCEPT
                )
                prompt = bench.launcher.prompts[0]
                self.assertEqual(prompt["mode"], probes.MODE_ACCEPT)
                self.assertEqual(prompt["candidate_sha"], bench.head)
                self.assertEqual(prompt["probes"], [_ITEM])
                self.assertIn("probe_verdicts", prompt["envelope_schema"])
                self.assertIn("write nothing in this checkout", prompt["instructions"])
                self.assertNotIn("Author acceptance files", prompt["instructions"])
                self.assertNotIn("Create tests here", prompt["instructions"])
                self.assertNotIn("PASS requires findings=[]", prompt["instructions"])

    def test_a_reviser_verdict_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = self._bench(tmp, "REVISE")
            raw = bench.actor.accept_probes(
                pb.context(bench.head), "test-reviewer", [_ITEM], probes.MODE_CHALLENGE
            )
            self.assertEqual(
                probes.parse_acceptance(raw, ["p-abc"], probes.MODE_CHALLENGE),
                {"p-abc": ("REVISE", "ok")},
            )

    def test_writes_nothing_to_the_checkout(self) -> None:
        for role in probes.ACCEPTOR_ROLES:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                bench = self._bench(tmp)
                bench.actor.accept_probes(
                    pb.context(bench.head), role, [_ITEM], probes.MODE_ACCEPT
                )
                checkout = bench.checkout("lane-a", role)
                self.assertEqual(pb.git(checkout, "rev-parse", "HEAD"), bench.head)
                dirty = [
                    line
                    for line in pb.git(checkout, "status", "--porcelain", "--ignored").splitlines()
                    if maestro.lch.ROLE_AGENT_DIR not in line
                ]
                self.assertEqual(dirty, [])

    def test_unknown_role_mode_or_candidate_is_refused_before_any_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = self._bench(tmp)
            ctx = pb.context(bench.head)
            for role, mode, context in (
                ("builder", probes.MODE_ACCEPT, ctx),
                ("test-reviewer", "approve", ctx),
                ("test-reviewer", probes.MODE_ACCEPT, pb.context("")),
            ):
                with self.assertRaises(maestro.FactoryRefused):
                    bench.actor.accept_probes(context, role, [_ITEM], mode)
            self.assertEqual(bench.launcher.launches, 0)

    def test_session_key_and_directory_are_this_lanes_own(self) -> None:
        """R9: a build lane's acceptor cannot be a tests lane's session."""
        with tempfile.TemporaryDirectory() as tmp:
            bench = self._bench(tmp)
            build = pb.context(bench.head, lane_id="lane-build")
            tests = pb.context(bench.head, lane_id="lane-tests")
            for role in probes.ACCEPTOR_ROLES:
                self.assertNotEqual(
                    bench.actor._role_key(build, role), bench.actor._role_key(tests, role)
                )
                self.assertEqual(bench.actor._role_key(build, role)[0], "lane-build")
            bench.actor.accept_probes(build, "test-reviewer", [_ITEM], probes.MODE_ACCEPT)
            bench.actor.accept_probes(tests, "test-reviewer", [_ITEM], probes.MODE_ACCEPT)
            self.assertNotEqual(
                bench.checkout("lane-build", "test-reviewer"),
                bench.checkout("lane-tests", "test-reviewer"),
            )
            self.assertTrue(bench.checkout("lane-build", "test-reviewer").is_dir())
            self.assertTrue(bench.checkout("lane-tests", "test-reviewer").is_dir())


if __name__ == "__main__":
    unittest.main()
