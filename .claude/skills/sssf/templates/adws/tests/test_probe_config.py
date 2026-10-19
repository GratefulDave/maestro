"""`review.max_probe_rounds` from the config to every FactoryScheduler."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

import maestro


def _load(review: Any = "unset") -> dict[str, Any]:
    text = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    if review == "unset":
        config.pop("review", None)
    else:
        config["review"] = review
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        config["runtime_state_root"] = str(repo / "state")
        path = repo / "adws" / "maestro.config.yaml"
        path.parent.mkdir()
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return maestro._load_maestro_config(repo, path)


class MaxProbeRoundsConfigTest(unittest.TestCase):
    def test_absent_means_five(self) -> None:
        self.assertEqual(_load()["review_max_probe_rounds"], 5)
        self.assertEqual(maestro._max_probe_rounds(_load()), 5)

    def test_template_stamps_five(self) -> None:
        text = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(text)["review"]["max_probe_rounds"], 5)

    def test_zero_is_off_and_is_kept(self) -> None:
        loaded = _load({"max_probe_rounds": 0})
        self.assertEqual(loaded["review_max_probe_rounds"], 0)
        self.assertEqual(maestro._max_probe_rounds(loaded), 0)

    def test_value_is_read(self) -> None:
        self.assertEqual(_load({"max_probe_rounds": 2})["review_max_probe_rounds"], 2)

    def test_bad_values_raise(self) -> None:
        for bad in (True, -1, "3", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(maestro._MaestroConfigurationError):
                    _load({"max_probe_rounds": bad})
        with self.assertRaises(maestro._MaestroConfigurationError):
            _load("on")

    def test_every_scheduler_construction_site_passes_it(self) -> None:
        tree = ast.parse(Path(maestro.__file__).read_text(encoding="utf-8"))
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FactoryScheduler"
        ]
        self.assertEqual(len(sites), 6)
        for node in sites:
            keywords = {item.arg: item.value for item in node.keywords}
            self.assertIn("max_probe_rounds", keywords, "line {0}".format(node.lineno))
            self.assertEqual(
                ast.unparse(keywords["max_probe_rounds"]), "_max_probe_rounds(layout)"
            )


if __name__ == "__main__":
    unittest.main()
