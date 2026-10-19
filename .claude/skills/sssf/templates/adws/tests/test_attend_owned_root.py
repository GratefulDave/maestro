"""Attend's planctl root: owned, per revision, under `runtime_state_root`.

Run 2acd2f5f: revision 5 was a hand amendment whose IR lived in `/tmp` in a
planctl root of its own. Attend wrote the r6 IR beside it -- the directory of
whatever the current plan artifact was -- and passed the run repository as
`--repo-root`; planctl refused `ir.repo_root` and attend recorded that as a
verdict about the revision. These cases pin the replacement: every revision's
IR is written into `_attend_root`, the sources it cites are copied in by
sha256, a revision attend did not write is supplied there by preflight, and a
`--repo-root` that does not contain the IR is planctl's harness, not a
refusal of the plan.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import maestro
from adw_modules import attend as att
from adw_modules import route_admission as admission
from adw_modules import run_preflight as rp
from tests.test_attend_planctl_step import (
    _configured_planctl,
    _forbid_aws,
    _planctl_fixture,
)
from tests.test_artifact_store import RUN_ID
from tests.test_run_preflight import _Bed


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hand_amendment(tmp: Path, run_id: str, revision: int) -> tuple[Path, bytes]:
    """A revision laid out by hand outside the run repository.

    Its own planctl root: the IR at the top, the source it cites copied in
    beside it, the way an operator amends a run from a scratch directory.
    """
    hand = tmp / "hand" / "r{0}".format(revision)
    (hand / "docs").mkdir(parents=True)
    source = b"the spec the plan cites\n"
    (hand / "docs" / "spec.md").write_bytes(source)
    (hand / "docs" / "target.md").write_text("target\n", encoding="utf-8")
    ir = {
        "source_artifacts": [
            {"source_id": "spec", "path": "docs/spec.md", "sha256": _sha(source), "required": True}
        ],
        "links": [
            {
                "link_id": "l",
                "source_path": "docs/spec.md",
                "href": "target.md",
                "canonical_target": "docs/target.md",
            }
        ],
    }
    path = hand / "{0}.r{1}.ir.json".format(run_id, revision)
    path.write_text(json.dumps(ir), encoding="utf-8")
    return path, source


class _Paths(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.runtime = SimpleNamespace(path=self.tmp / "state")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def _pin(self, revision: int, data: bytes = b"plan") -> None:
        pinned = maestro._pinned_plan_artifact(self.runtime, RUN_ID, revision)
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_bytes(data)

    def _supply(self, ir: Path, revision: int) -> str:
        with mock.patch.object(
            maestro, "plan_artifact_ref_for", return_value=str(ir.parent / "plan.json")
        ):
            return maestro._attend_supply_root(
                att.AttendPolicy(max_amendments_per_lane=1),
                self.runtime, object(), self.repo, RUN_ID, revision,
            )


class TheRootIsOwned(_Paths):
    def test_the_root_is_the_revisions_pinned_plan_directory(self) -> None:
        root = maestro._attend_root(self.runtime, RUN_ID, 6)
        self.assertEqual(root, self.runtime.path / "plans" / RUN_ID / "r6")
        self.assertEqual(maestro._attend_owned_ir(self.runtime, RUN_ID, 6).parent, root)

    def test_a_hand_amendment_is_supplied_with_its_sources_and_recorded(self) -> None:
        ir, source = _hand_amendment(self.tmp, RUN_ID, 5)
        self._pin(5)
        self.assertFalse(maestro._attend_root_current(self.runtime, RUN_ID, 5)[0])
        self._supply(ir, 5)
        root = maestro._attend_root(self.runtime, RUN_ID, 5)
        self.assertEqual((root / "docs" / "spec.md").read_bytes(), source)
        self.assertTrue((root / "docs" / "target.md").is_file())
        self.assertEqual(
            maestro._attend_owned_ir(self.runtime, RUN_ID, 5).read_bytes(), ir.read_bytes()
        )
        current, detail = maestro._attend_root_current(self.runtime, RUN_ID, 5)
        self.assertTrue(current, detail)
        record = json.loads(maestro._attend_root_record(self.runtime, RUN_ID, 5).read_text())
        self.assertEqual(record["repo_root"], str(root))

    def test_an_ir_left_by_a_refused_attempt_is_not_the_revisions(self) -> None:
        """A hand `run amend` to the same number does not inherit the leftover."""
        ir, _ = _hand_amendment(self.tmp, RUN_ID, 5)
        self._pin(5, b"plan the attempt projected")
        self._supply(ir, 5)
        maestro._pinned_plan_artifact(self.runtime, RUN_ID, 5).write_bytes(b"hand plan")
        current, detail = maestro._attend_root_current(self.runtime, RUN_ID, 5)
        self.assertFalse(current)
        self.assertIn("another plan", detail)

    def test_a_source_with_other_bytes_is_never_copied(self) -> None:
        ir, _ = _hand_amendment(self.tmp, RUN_ID, 5)
        (ir.parent / "docs" / "spec.md").write_bytes(b"edited after pinning\n")
        self._pin(5)
        with self.assertRaises(att.AttendRefused) as caught:
            self._supply(ir, 5)
        self.assertEqual(caught.exception.code, att.PLAN_IR_UNRESOLVED)
        self.assertIn("docs/spec.md", caught.exception.detail)
        self.assertFalse(maestro._attend_root_current(self.runtime, RUN_ID, 5)[0])

    def test_a_source_outside_the_root_is_a_layout_failure(self) -> None:
        root = maestro._attend_root(self.runtime, RUN_ID, 6)
        document = {"source_artifacts": [{"path": "../../escape.md", "sha256": "0" * 64}]}
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._attend_copy_sources(document, root, (self.repo,))
        self.assertEqual(caught.exception.code, att.PLANCTL_FAILED)


class PreflightSuppliesTheRoot(unittest.TestCase):
    def test_a_scratch_ir_is_copied_under_the_state_root(self) -> None:
        bed = _Bed(self, attend=True)
        runtime = SimpleNamespace(path=bed.state)
        ir, _ = _hand_amendment(bed.root, RUN_ID, 3)
        pinned = maestro._pinned_plan_artifact(runtime, RUN_ID, 3)
        pinned.parent.mkdir(parents=True)
        pinned.write_bytes(b"plan")
        policy = att.AttendPolicy(max_amendments_per_lane=1)
        with mock.patch.object(
            maestro, "plan_artifact_ref_for", return_value=str(ir.parent / "plan.json")
        ):
            preflight = bed.preflight(
                plan_ir=maestro._attend_owned_ir(runtime, RUN_ID, 3),
                plan_ir_current=lambda: maestro._attend_root_current(runtime, RUN_ID, 3),
                plan_ir_supply=lambda: maestro._attend_supply_root(
                    policy, runtime, object(), bed.product, RUN_ID, 3
                ),
            )
            preflight.run_wide()
        item = {i.item_id: i for i in preflight.items}["attend:plan-ir"]
        self.assertEqual(item.status, rp.ADDED, item.detail)
        owned = maestro._attend_owned_ir(runtime, RUN_ID, 3)
        self.assertTrue(owned.is_relative_to(bed.state))
        self.assertEqual(_sha(owned.read_bytes()), _sha(ir.read_bytes()))


class RealPlanctl(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = _configured_planctl()
        if self.binary is None:
            self.skipTest("set MAESTRO_TEST_PLANCTL to a plan-contract validator")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        _forbid_aws(self)

    def test_an_ir_outside_the_repo_root_is_a_planctl_failure(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        ir = self.tmp / "elsewhere" / "plan.ir.json"
        ir.parent.mkdir()
        ir.write_text("{}", encoding="utf-8")
        for argv in (
            ["render", str(ir), "--out", str(self.tmp / "out.html")],
            ["validate", str(ir), "--json"],
        ):
            with self.subTest(command=argv[0]):
                with self.assertRaises(att.AttendRefused) as caught:
                    maestro._run_planctl(self.binary, [*argv, "--repo-root", str(repo)])
                self.assertEqual(caught.exception.code, att.PLANCTL_FAILED)
                self.assertIn("repo_root", caught.exception.detail)

    def test_a_revision_after_a_hand_amendment_renders_in_the_owned_root(self) -> None:
        """r5 in a scratch root, r6 projected through every planctl step."""
        work = self.tmp / "work"
        work.mkdir()
        hand, authored = _planctl_fixture(self.binary, work, RUN_ID, 5)
        # The hand amendment's IR lives in its own root, not the run repo.
        hand_ir = hand / "{0}.r5.ir.json".format(RUN_ID)
        hand_ir.write_bytes(authored.read_bytes())
        runtime = SimpleNamespace(path=self.tmp / "state")
        repo = self.tmp / "target"
        repo.mkdir()
        pinned = maestro._pinned_plan_artifact(runtime, RUN_ID, 5)
        pinned.parent.mkdir(parents=True)
        pinned.write_bytes(b"plan")
        with mock.patch.object(
            maestro, "plan_artifact_ref_for", return_value=str(hand / "plan.json")
        ):
            maestro._attend_supply_root(
                att.AttendPolicy(max_amendments_per_lane=1), runtime, object(), repo, RUN_ID, 5
            )
        keys = self.tmp / "keys"
        keys.mkdir()
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text("ab" * 32)
        policy = att.AttendPolicy(
            max_amendments_per_lane=1,
            planctl=self.binary,
            reviewer_id=maestro._attend_reviewer_id(
                {"route": "omp", "profile": "profile-b", "model": "", "effort": ""}
            ),
            reviewer_vendor="vendor-b",
        )
        root = maestro._attend_root(runtime, RUN_ID, 6)
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._attend_project(
                policy, {"keys_dir": keys}, repo, root, RUN_ID,
                authored.read_bytes(), 6, [], local=True,
                bases=(maestro._attend_root(runtime, RUN_ID, 5), repo),
            )
        # Past render, review and validate: only ingress refuses the fixture.
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED, caught.exception.detail)
        self.assertIn("MAESTRO_EXTENSION_MISSING", caught.exception.detail)
        for name in ("ir.json", "html", "receipt.json"):
            self.assertTrue((root / "{0}.r6.{1}".format(RUN_ID, name)).is_file(), name)
        self.assertTrue((root / "schemas" / "plan-ir-v1.schema.json").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
