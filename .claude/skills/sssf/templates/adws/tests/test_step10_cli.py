"""Frozen operator CLI: run start/resume/amend/status only."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

import maestro


FROZEN_VERBS = (
    "run start",
    "run resume",
    "run amend",
    "run status",
)

DELETED_VERBS = (
    "retry",
    "skip",
    "abandon",
    "cancel",
    "pause",
    "list",
    "attempt salvage",
    "plan ship",
    "plan validate",
    "plan finalize",
    "workspace start",
)


class FrozenOperatorCliTest(unittest.TestCase):
    def test_parser_exposes_only_frozen_verbs(self) -> None:
        verbs = maestro.parser_verbs(maestro.build_parser())
        self.assertEqual(verbs, FROZEN_VERBS)
        for gone in DELETED_VERBS:
            self.assertNotIn(gone, verbs)
            self.assertFalse(any(gone in item for item in verbs))

    def test_start_requires_repo_and_main_ref(self) -> None:
        parser = maestro.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "start", "plan.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "start", "plan.json", "--repo", "/tmp/repo"])
        args = parser.parse_args(
            [
                "run",
                "start",
                "plan.json",
                "--repo",
                "/tmp/repo",
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertEqual(args.plan, "plan.json")
        self.assertEqual(args.repo, "/tmp/repo")
        self.assertEqual(args.main_ref, "refs/heads/main")

    def test_resume_amend_and_status_take_run_identity(self) -> None:
        parser = maestro.build_parser()
        resume = parser.parse_args(["run", "resume", "run-1"])
        self.assertEqual(resume.run_id, "run-1")
        amend = parser.parse_args(["run", "amend", "plan.json", "--run", "run-1"])
        self.assertEqual(amend.plan, "plan.json")
        self.assertEqual(amend.run_id, "run-1")
        status = parser.parse_args(["run", "status", "run-1"])
        self.assertEqual(status.run_id, "run-1")

    def test_deleted_escape_verbs_are_unknown(self) -> None:
        parser = maestro.build_parser()
        for argv in (
            ["run", "retry"],
            ["run", "skip"],
            ["run", "abandon"],
            ["run", "cancel"],
            ["retry"],
            ["skip"],
            ["abandon"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_start_without_installed_config_is_typed_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "product"
            repo.mkdir()
            code = maestro.main(
                [
                    "run",
                    "start",
                    str(repo / "plan.json"),
                    "--repo",
                    str(repo),
                    "--main-ref",
                    "refs/heads/main",
                ]
            )
        self.assertEqual(code, 3)

    def test_relative_runtime_state_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "adws" / "maestro.config.yaml"
            config.parent.mkdir()
            config.write_text(
                "schema: maestro-config.v1\nruntime_state_root: relative/state\n",
                encoding="utf-8",
            )
            with self.assertRaises(maestro._MaestroConfigurationError):
                maestro._load_maestro_config(root, config)


class ParserHasNoBudgetOrSessionFlags(unittest.TestCase):
    def test_start_resume_have_no_ceiling_pid_or_session_flags(self) -> None:
        parser = maestro.build_parser()
        run = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["run"]
        run_sub = next(
            action
            for action in run._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in ("start", "resume", "amend", "status"):
            flags = []
            for action in run_sub.choices[name]._actions:
                flags.extend(action.option_strings)
            joined = " ".join(flags)
            self.assertNotIn("ceiling", joined)
            self.assertNotIn("pid", joined)
            self.assertNotIn("session", joined)
            self.assertNotIn("retry", joined)


if __name__ == "__main__":
    unittest.main()
