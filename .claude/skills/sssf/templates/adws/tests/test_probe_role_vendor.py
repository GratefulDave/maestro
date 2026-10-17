"""Operator-declared `vendor:` on the five lane roles, and only there."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

import maestro
from adw_modules import probes


def _template() -> dict[str, Any]:
    text = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _load(mutate: Any = None) -> dict[str, Any]:
    config = _template()
    if mutate is not None:
        mutate(config)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        config["runtime_state_root"] = str(repo / "state")
        path = repo / "adws" / "maestro.config.yaml"
        path.parent.mkdir()
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return maestro._load_maestro_config(repo, path)


def _routes(loaded: dict[str, Any]) -> str:
    return json.dumps(
        {role: dict(route) for role, route in loaded["role_routes"].items()},
        sort_keys=True,
    )


class RoleVendorTest(unittest.TestCase):
    def test_template_declares_each_roles_provider(self) -> None:
        loaded = _load()
        self.assertEqual(
            dict(loaded["role_vendors"]),
            {
                "tester": "xai",
                "test-reviewer": "openai",
                "builder": "anthropic",
                "code-reviewer": "openai",
                "integration-reviewer": "openai",
            },
        )

    def test_template_resolves_an_independent_acceptor(self) -> None:
        selected = probes.select_acceptor(_load()["role_vendors"])
        self.assertNotIsInstance(selected, probes.Rejection)
        self.assertEqual(selected, ("tester", "openai", "xai"))

    def test_vendor_is_accepted_on_each_lane_role_and_normalized(self) -> None:
        for role in maestro.lch.LANE_PANE_ROLES:
            def declare(config: dict[str, Any], role: str = role) -> None:
                config["role_routes"][role]["vendor"] = "  OpenAI "

            with self.subTest(role=role):
                self.assertEqual(_load(declare)["role_vendors"][role], "openai")

    def test_absent_vendor_is_none(self) -> None:
        def drop(config: dict[str, Any]) -> None:
            del config["role_routes"]["code-reviewer"]["vendor"]

        self.assertIsNone(_load(drop)["role_vendors"]["code-reviewer"])

    def test_non_string_and_blank_vendors_are_refused(self) -> None:
        for bad in (1, True, ["openai"], "", "   "):
            def declare(config: dict[str, Any], bad: Any = bad) -> None:
                config["role_routes"]["code-reviewer"]["vendor"] = bad

            with self.subTest(bad=bad):
                with self.assertRaises(maestro._MaestroConfigurationError):
                    _load(declare)

    def test_attend_route_refuses_vendor(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_role_route(
                {"route": "claude", "model": "opus", "effort": "high", "vendor": "x"},
                "attend.route",
            )

        def declare(config: dict[str, Any]) -> None:
            config["attend"]["route"] = {
                "route": "claude", "model": "opus", "effort": "high", "vendor": "x",
            }

        with self.assertRaises(maestro._MaestroConfigurationError):
            _load(declare)

    def test_canonical_routes_are_byte_identical_with_and_without_vendor(self) -> None:
        plain = _load()

        def declare(config: dict[str, Any]) -> None:
            for binding in config["role_routes"].values():
                binding["vendor"] = "acme"

        declared = _load(declare)
        self.assertEqual(_routes(plain), _routes(declared))
        for route in declared["role_routes"].values():
            self.assertNotIn("vendor", route)


if __name__ == "__main__":
    unittest.main()
