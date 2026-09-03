"""Canonical authoring of maestro-plan.artifact-factory.v1 drafts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adw_modules.plan_model import SCHEMA_VERSION


def _lane(lane_id: str, *, needs=(), outputs=None, lane_kind: str) -> dict:
    return {
        "id": lane_id,
        "needs": list(needs),
        "outputs": list(outputs if outputs is not None else ["src/{0}.py".format(lane_id)]),
        "spec": {"goal": lane_id},
        "acceptance": ["{0} holds".format(lane_id)],
        "lane_kind": lane_kind,
    }


def _draft() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "lanes": [
            _lane("lane-t", outputs=["tests/test_t.py"], lane_kind="tests"),
            _lane("lane-b", needs=("lane-t",), outputs=["src/b.py"], lane_kind="build"),
        ],
    }


class AuthorPlanTests(unittest.TestCase):
    def test_author_plan_round_trips(self) -> None:
        from adw_modules import plan_author
        from adw_modules import plan_canonical
        from adw_modules.plan_compiler import compile_plan

        stored = plan_author.author_plan(_draft())
        self.assertTrue(plan_canonical.is_canonical(stored))
        compile_plan(stored)

    def test_author_plan_refuses_what_the_compiler_refuses(self) -> None:
        from adw_modules import plan_author
        from adw_modules.plan_author import AuthoringError

        draft = _draft()
        draft["nodes"] = []
        with self.assertRaises(AuthoringError) as caught:
            plan_author.author_plan(draft)
        self.assertIn("SCHEMA_INVALID", str(caught.exception))

    def test_write_is_create_once(self) -> None:
        from adw_modules import plan_author
        from adw_modules.plan_author import AuthoringError

        stored = plan_author.author_plan(_draft())
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "plan"
            plan_author.write_canonical_plan(destination, stored)
            with self.assertRaises(AuthoringError) as exists:
                plan_author.write_canonical_plan(destination, stored)
            self.assertIn("PLAN_EXISTS", str(exists.exception))
            other = Path(tmp) / "not-canonical"
            with self.assertRaises(AuthoringError) as uncanonical:
                plan_author.write_canonical_plan(other, stored + b" ")
            self.assertIn("PLAN_NOT_CANONICAL", str(uncanonical.exception))


if __name__ == "__main__":
    unittest.main()
