"""Observational reporting registry: create, dedupe, preserve, fail-open."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import reporting_registry as rr


class ReportingRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "registry.json"
        self._previous = os.environ.get("MAESTRO_REGISTRY")
        os.environ["MAESTRO_REGISTRY"] = str(self.path)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("MAESTRO_REGISTRY", None)
        else:
            os.environ["MAESTRO_REGISTRY"] = self._previous
        self._tmp.cleanup()

    def _register(self, name: str) -> Path:
        database = self.root / name / "lifecycle.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_text("", encoding="utf-8")
        plans = self.root / name / "plans"
        plans.mkdir()
        repo = self.root / name / "repo"
        repo.mkdir()
        state = self.root / name / "state"
        state.mkdir()
        rr.register_installation(
            database=database,
            plans_dir=plans,
            repository=repo,
            state=state,
        )
        return database.resolve()

    def test_creates_entry(self) -> None:
        database = self._register("one")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["installations"]), 1)
        entry = payload["installations"][0]
        self.assertEqual(entry["database"], str(database))
        self.assertTrue(entry["plans_dir"].endswith("/plans"))
        self.assertTrue(entry["repository"].endswith("/repo"))
        self.assertTrue(entry["state"].endswith("/state"))

    def test_same_database_dedupes_and_updates(self) -> None:
        database = self._register("one")
        plans = self.root / "one" / "plans-2"
        plans.mkdir()
        rr.register_installation(
            database=database,
            plans_dir=plans,
            repository=self.root / "one" / "repo",
            state=self.root / "one" / "state",
        )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["installations"]), 1)
        self.assertEqual(
            payload["installations"][0]["plans_dir"], str(plans.resolve())
        )

    def test_preserves_unrelated_and_legacy_entries(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "extra": True,
                    "installations": [
                        {
                            "database": "/legacy/fdadb/lifecycle.sqlite3",
                            "plans_dir": "/legacy/plans",
                            "repository": "/legacy/repo",
                            "state": "/legacy/state",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._register("fresh")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        databases = [item["database"] for item in payload["installations"]]
        self.assertEqual(len(databases), 2)
        self.assertIn("/legacy/fdadb/lifecycle.sqlite3", databases)
        self.assertTrue(payload["extra"] is True)

    def test_malformed_registry_fail_open(self) -> None:
        self.path.write_text("not-json", encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        rr.register_installation(
            database=self.root / "x.sqlite3",
            plans_dir=self.root,
            repository=self.root,
            state=self.root,
        )
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_concurrent_starts_keep_both_entries(self) -> None:
        errors: list[BaseException] = []

        def one() -> None:
            try:
                self._register("left")
            except BaseException as exc:
                errors.append(exc)

        def two() -> None:
            try:
                self._register("right")
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=one)
        second = threading.Thread(target=two)
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(errors, [])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        databases = {item["database"] for item in payload["installations"]}
        self.assertEqual(len(databases), 2)


if __name__ == "__main__":
    unittest.main()
