"""Plan authoring writes canonical maestro-plan.v1 bytes and nothing else."""

from __future__ import annotations

import contextlib
import hashlib
import io
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

import maestro
from adw_modules import plan_author
from adw_modules import plan_canonical as pc
from adw_modules import plan_digest as pd
from adw_modules import plan_validate as pv

from test_step2_plan_validation import (
    Collector,
    Receipts,
    README,
    make_repo,
    plan_mapping,
    sha256_text,
)


class PlanAuthorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _head(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_author_fills_observed_sha_and_writes_canonical_bytes(self):
        draft = plan_mapping(self.base)
        draft["evidence"][0]["sha256"] = "0" * 64
        destination = self.root / "out" / "maestro-plan.v1"
        stored = plan_author.author_from_draft(
            _write_draft(self.root / "draft.json", draft),
            destination, self.repo)
        self.assertTrue(pc.is_canonical(stored))
        self.assertEqual(destination.read_bytes(), stored)
        parsed = json.loads(stored)
        self.assertEqual(parsed["evidence"][0]["sha256"], sha256_text(README))
        result = pv.validate_plan(
            stored, self.repo, receipts=Receipts(), collector=Collector())
        self.assertTrue(result.eligible)
        self.assertEqual(result.digest, pd.digest_of(stored))

    def test_author_resolves_head_when_base_commit_omitted(self):
        draft = plan_mapping(self.base)
        draft.pop("base_commit")
        stored = plan_author.author_plan(draft, self.repo)
        self.assertEqual(json.loads(stored)["base_commit"], self.base)

    def test_author_hashes_prompt_assets_from_disk(self):
        prompt = self.repo / "prompts" / "write.md"
        prompt.parent.mkdir()
        prompt.write_text("write the greeting\n", encoding="utf-8")
        draft = plan_mapping(self.base)
        draft["nodes"][0]["prompt_assets"][0]["sha256"] = ""
        stored = plan_author.author_plan(draft, self.repo)
        self.assertEqual(
            json.loads(stored)["nodes"][0]["prompt_assets"][0]["sha256"],
            hashlib.sha256(prompt.read_bytes()).hexdigest())

    def test_author_refuses_to_overwrite_an_existing_plan(self):
        draft = plan_mapping(self.base)
        destination = self.root / "maestro-plan.v1"
        plan_author.author_from_draft(
            _write_draft(self.root / "draft.json", draft),
            destination, self.repo)
        with self.assertRaisesRegex(plan_author.AuthoringError, "PLAN_EXISTS"):
            plan_author.author_from_draft(
                self.root / "draft.json", destination, self.repo)

    def test_missing_draft_is_a_typed_refusal(self):
        with self.assertRaisesRegex(plan_author.AuthoringError, "PLAN_DRAFT_MISSING"):
            plan_author.find_draft(self.root / "empty")

    def test_absent_observed_path_is_refused_before_write(self):
        draft = plan_mapping(self.base)
        draft["evidence"][0]["path"] = "docs/never.md"
        destination = self.root / "maestro-plan.v1"
        with self.assertRaisesRegex(plan_author.AuthoringError, "OBSERVED_PATH_ABSENT"):
            plan_author.author_from_draft(
                _write_draft(self.root / "draft.json", draft),
                destination, self.repo)
        self.assertFalse(destination.exists())

    def test_configured_plan_author_writes_named_canonical_file(self):
        from test_step10_cli import OperatorCliTest

        helper = OperatorCliTest()
        with tempfile.TemporaryDirectory() as tmp:
            fixture = helper._named_plan_configuration(Path(tmp))
            (fixture["repo"] / "README.md").write_text(README, encoding="utf-8")
            tests = fixture["repo"] / "tests"
            tests.mkdir(exist_ok=True)
            (tests / "test_existing.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n"
                "    def test_one(self):\n        self.assertTrue(True)\n"
                "    def test_two(self):\n        self.assertTrue(True)\n",
                encoding="utf-8")
            _git_init(fixture["repo"], Path(tmp))
            plan_dir = fixture["repo"] / "plans" / "phase-1"
            plan_dir.mkdir(parents=True)
            draft = plan_mapping(_head(fixture["repo"]))
            draft["repo"] = fixture["repo"].name
            (plan_dir / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
            output = io.StringIO()
            with helper._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(["plan", "author", "phase-1"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "PLAN_AUTHORED")
            stored = Path(payload["plan"]).read_bytes()
            self.assertTrue(pc.is_canonical(stored))
            self.assertEqual(pd.digest_of(stored), payload["digest"])


def _git_init(repo: Path, hook_root: Path) -> None:
    import subprocess
    def git(*args: str) -> None:
        result = subprocess.run(["git", *args], cwd=str(repo),
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "harness@example.invalid")
    git("config", "user.name", "Harness")
    git("config", "core.hooksPath", str(hook_root / "no-such-hooks"))
    git("add", "-A")
    git("commit", "-qm", "base")


def _write_draft(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _head(repo: Path) -> str:
    return pv._git(repo, "rev-parse", "HEAD")[1].decode("ascii").strip()


if __name__ == "__main__":
    unittest.main()
