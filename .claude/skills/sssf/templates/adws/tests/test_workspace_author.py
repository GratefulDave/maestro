"""Workspace author consumes only finalized child PASS receipts."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import finalization as fin
from adw_modules import plan_author
from adw_modules import plan_digest as pd
from adw_modules import workspace_author as wa
from adw_modules import workspace_canonical as wc
from adw_modules import workspace_model as wm

from test_step2_plan_validation import make_repo, plan_mapping


def _pass_receipt(digest: str) -> bytes:
    return fin.Receipt(
        plan_digest=digest,
        rubric_version="test",
        verdict=fin.Verdict.PASS,
        cells=(),
        reviewer=fin.ReviewerIdentity("route", "model", "session"),
        created_at_epoch=1.0,
    ).to_bytes()


class WorkspaceAuthorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        head = __import__("subprocess").check_output(
            ("git", "rev-parse", "HEAD"), cwd=str(self.repo),
            text=True).strip()
        draft = plan_mapping(head)
        draft["evidence"][0].pop("sha256", None)
        self.plan_path = self.root / "child" / "maestro-plan.v1"
        stored = plan_author.author_from_draft(
            self._write(self.root / "draft.json", draft),
            self.plan_path, self.repo)
        self.digest = pd.digest_of(stored)
        self.receipt_path = self.root / "child.receipt.json"
        self.receipt_path.write_bytes(_pass_receipt(self.digest))
    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, path: Path, payload) -> Path:
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _draft(self, **overrides):
        data = {
            "workspace_id": "smoke",
            "publication_mode": "none",
            "repositories": [{
                "repository_id": "child",
                "mode": "write",
                "path": "child-repo",
                "plan_path": "child/maestro-plan.v1",
                "receipt_path": "child.receipt.json",
                "target_branch": "main",
                "run_argv": ["python3", "adws/maestro.py", "run", "start"],
            }],
        }
        data.update(overrides)
        return data

    def test_authors_none_workspace_from_pass_child(self):
        stored = wa.author_workspace(self._draft(), self.root)
        workspace = wm.parse_bytes(stored)
        self.assertTrue(wc.is_canonical(stored))
        self.assertEqual(workspace.publication_mode, wm.PublicationMode.NONE)
        child = workspace.repositories[0]
        self.assertEqual(child.plan_digest, self.digest)

    def test_refuses_non_pass_receipt(self):
        fail = fin.Receipt(
            plan_digest=self.digest, rubric_version="test",
            verdict=fin.Verdict.FAIL, cells=(),
            reviewer=fin.ReviewerIdentity("route", "model", "session"),
            created_at_epoch=1.0,
        ).to_bytes()
        self.receipt_path.write_bytes(fail)
        with self.assertRaisesRegex(wa.WorkspaceAuthoringError, "CHILD_RECEIPT_NOT_PASS"):
            wa.author_workspace(self._draft(), self.root)

    def test_refuses_publication_mode_other_than_none(self):
        with self.assertRaisesRegex(wa.WorkspaceAuthoringError, "PUBLICATION_NOT_NONE"):
            wa.author_workspace(self._draft(publication_mode="local_refs"), self.root)

    def test_two_package_workspace(self):
        (self.root / "second-src").mkdir()
        second = make_repo(self.root / "second-src")
        head = __import__("subprocess").check_output(
            ("git", "rev-parse", "HEAD"), cwd=str(second), text=True).strip()
        draft = plan_mapping(head)
        draft["plan_id"] = "plan-002"
        draft["evidence"][0].pop("sha256", None)
        plan_b = self.root / "other" / "maestro-plan.v1"
        stored = plan_author.author_from_draft(
            self._write(self.root / "draft-b.json", draft), plan_b, second)
        digest_b = pd.digest_of(stored)
        receipt_b = self.root / "other.receipt.json"
        receipt_b.write_bytes(_pass_receipt(digest_b))
        payload = self._draft()
        payload["repositories"].append({
            "repository_id": "other",
            "mode": "write",
            "path": "other-repo",
            "plan_path": "other/maestro-plan.v1",
            "receipt_path": "other.receipt.json",
            "target_branch": "main",
            "run_argv": ["python3", "adws/maestro.py", "run", "start"],
            "needs": ["child"],
        })
        workspace = wm.parse_bytes(wa.author_workspace(payload, self.root))
        self.assertEqual(
            [item.repository_id for item in workspace.repositories],
            ["child", "other"])


if __name__ == "__main__":
    unittest.main()
