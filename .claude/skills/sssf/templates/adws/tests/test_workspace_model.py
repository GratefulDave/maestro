"""Executable contract for the frozen ``maestro-workspace.v1`` schema."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from pydantic import ValidationError  # noqa: E402

from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import workspace_canonical as wc  # noqa: E402
from adw_modules import workspace_digest as wd  # noqa: E402
from adw_modules import workspace_model as wm  # noqa: E402


def workspace_mapping() -> dict:
    """A workspace with one executable repository and one dependency."""
    return {
        "schema_version": "maestro-workspace.v1",
        "workspace_id": "workspace-001",
        "repositories": [
            {
                "repository_id": "application",
                "mode": "write",
                "path": "repos/application",
                "base_commit": "a" * 40,
                "remote": "origin",
                "needs": ["documentation"],
                "plan_path": "plans/application.json",
                "plan_digest": "b" * 64,
                "target_branch": "main",
                "run_argv": ["python", "-m", "pytest"],
            },
            {
                "repository_id": "documentation",
                "mode": "read_only",
                "path": "repos/documentation",
                "base_commit": "c" * 64,
                "remote": "origin",
            },
        ],
        "publication_mode": "pull_requests",
        "integration_gates": [
            {
                "runner": "pytest",
                "argv": ["tests/integration"],
                "cwd": ".",
                "min_cases": 1,
            },
        ],
    }


def parse(data: dict) -> wm.WorkspacePlan:
    return wm.parse_mapping(data)


class WorkspaceModelTests(unittest.TestCase):
    def assert_parse_refused(self, data: dict) -> wm.WorkspaceParseError:
        with self.assertRaises(wm.WorkspaceParseError) as caught:
            parse(data)
        error = caught.exception
        self.assertTrue(error.pointers)
        self.assertTrue(all(pointer.startswith("/")
                            for pointer, _ in error.pointers))
        return error

    def test_valid_write_and_read_only_repository_specs_parse(self):
        workspace = parse(workspace_mapping())

        self.assertEqual(workspace.schema_version, wm.SCHEMA_V1)
        self.assertEqual(
            tuple(repository.repository_id
                  for repository in workspace.repositories),
            ("application", "documentation"),
        )
        self.assertEqual(workspace.repositories[0].mode, wm.RepositoryMode.WRITE)
        self.assertEqual(workspace.repositories[1].mode,
                         wm.RepositoryMode.READ_ONLY)
        self.assertEqual(workspace.repositories[0].run_argv,
                         ("python", "-m", "pytest"))
        self.assertEqual(workspace.repositories[1].needs, ())
        self.assertIsInstance(workspace.integration_gates[0], pm.Gate)

    def test_models_are_immutable(self):
        workspace = parse(workspace_mapping())

        with self.assertRaises(ValidationError):
            workspace.workspace_id = "other"

    def test_unknown_fields_are_refused_at_every_schema_level(self):
        for mutate in (
            lambda data: data.update({"unrecognized": True}),
            lambda data: data["repositories"][0].update({"unrecognized": True}),
            lambda data: data["integration_gates"][0].update(
                {"unrecognized": True}),
        ):
            data = workspace_mapping()
            mutate(data)
            self.assert_parse_refused(data)

    def test_write_repository_requires_every_execution_field(self):
        for field in ("plan_path", "plan_digest", "target_branch", "run_argv"):
            data = workspace_mapping()
            del data["repositories"][0][field]
            self.assert_parse_refused(data)
        empty_argv = workspace_mapping()
        empty_argv["repositories"][0]["run_argv"] = []
        self.assert_parse_refused(empty_argv)

    def test_read_only_repository_forbids_execution_fields_and_needs(self):
        for field, value in (
            ("plan_path", "plans/docs.json"),
            ("plan_digest", "d" * 64),
            ("target_branch", "main"),
            ("run_argv", ["python", "-m", "pytest"]),
            ("needs", []),
        ):
            data = workspace_mapping()
            data["repositories"][1][field] = value
            self.assert_parse_refused(data)

    def test_repository_ids_and_paths_are_unique(self):
        duplicate_id = workspace_mapping()
        duplicate_id["repositories"][1]["repository_id"] = "application"
        self.assert_parse_refused(duplicate_id)

        duplicate_path = workspace_mapping()
        duplicate_path["repositories"][1]["path"] = "repos/application"
        self.assert_parse_refused(duplicate_path)

    def test_repository_id_is_a_portable_identity(self):
        for repository_id in ("../escape", "repo/name", "repo name",
                              "_repository", "-repository"):
            data = workspace_mapping()
            data["repositories"][0]["repository_id"] = repository_id
            self.assert_parse_refused(data)

    def test_dependencies_must_resolve_and_cannot_reference_self(self):
        unresolved = workspace_mapping()
        unresolved["repositories"][0]["needs"] = ["missing"]
        self.assert_parse_refused(unresolved)

        self_referential = workspace_mapping()
        self_referential["repositories"][0]["needs"] = ["application"]
        self.assert_parse_refused(self_referential)

    def test_cross_repository_dependency_cycles_are_refused(self):
        data = workspace_mapping()
        data["repositories"][1] = {
            "repository_id": "worker",
            "mode": "write",
            "path": "repos/worker",
            "base_commit": "c" * 64,
            "remote": "origin",
            "needs": ["application"],
            "plan_path": "plans/worker.json",
            "plan_digest": "d" * 64,
            "target_branch": "main",
            "run_argv": ["python", "-m", "pytest"],
        }
        data["repositories"][0]["needs"] = ["worker"]

        self.assert_parse_refused(data)

    def test_invalid_commit_digest_and_portable_paths_are_refused(self):
        invalid_commit = workspace_mapping()
        invalid_commit["repositories"][0]["base_commit"] = "not-a-commit"
        self.assert_parse_refused(invalid_commit)

        invalid_digest = workspace_mapping()
        invalid_digest["repositories"][0]["plan_digest"] = "b" * 63
        self.assert_parse_refused(invalid_digest)

        for path in ("/outside", "../outside", "repos/../other", r"repos\\other"):
            invalid_path = workspace_mapping()
            invalid_path["repositories"][0]["path"] = path
            self.assert_parse_refused(invalid_path)

    def test_remote_cannot_be_parsed_as_a_git_option(self):
        data = workspace_mapping()
        data["repositories"][0]["remote"] = "--upload-pack=/tmp/payload"

        self.assert_parse_refused(data)

    def test_target_branch_must_be_a_safe_git_ref_fragment(self):
        valid = workspace_mapping()
        valid["repositories"][0]["target_branch"] = "release/2026.08"
        self.assertEqual(
            parse(valid).repositories[0].target_branch, "release/2026.08")

        for target_branch in (
                "-option", "release..next", "release@{1}", "release branch",
                "release\tbranch", r"release\branch", "release~branch",
                "release^branch", "release:branch", "release?branch",
                "release*branch", "release[branch", "release//next",
                "release/./next", "release/.hidden", "release/topic.lock",
                "/release", "release/", ".release", "release."):
            data = workspace_mapping()
            data["repositories"][0]["target_branch"] = target_branch
            self.assert_parse_refused(data)

    def test_workspace_identities_and_object_digests_are_lowercase(self):
        for field, value in (
            ("repository_id", "Application"),
            ("base_commit", "A" * 40),
            ("plan_digest", "B" * 64),
        ):
            data = workspace_mapping()
            data["repositories"][0][field] = value
            self.assert_parse_refused(data)

    def test_workspace_requires_at_least_one_repository(self):
        data = workspace_mapping()
        data["repositories"] = []

        self.assert_parse_refused(data)

    def test_pull_request_publication_requires_remote_for_each_writer(self):
        data = workspace_mapping()
        del data["repositories"][0]["remote"]

        self.assert_parse_refused(data)

    def test_parse_bytes_exposes_typed_parse_errors(self):
        with self.assertRaises(wm.WorkspaceParseError) as caught:
            wm.parse_bytes(b"not json")

        self.assertEqual(caught.exception.pointers, (("", "not UTF-8 JSON"),))

    def test_lone_surrogate_bytes_are_a_typed_refusal(self):
        data = workspace_mapping()
        data["workspace_id"] = "\ud800"
        stored = json.dumps(data, ensure_ascii=True).encode("utf-8")

        with self.assertRaises(wm.WorkspaceParseError):
            wm.parse_bytes(stored)
        self.assertFalse(wc.is_canonical(stored))

    def test_deep_json_is_a_typed_refusal(self):
        stored = b"[" * 4096 + b"]" * 4096

        with self.assertRaises(wm.WorkspaceParseError):
            wm.parse_bytes(stored)
        self.assertFalse(wc.is_canonical(stored))


class CanonicalWorkspaceBytesTests(unittest.TestCase):
    def test_canonical_bytes_are_stable_and_already_canonical(self):
        workspace = parse(workspace_mapping())
        stored = wc.canonicalize_workspace(workspace)

        self.assertEqual(stored, wc.canonicalize_workspace(wm.parse_bytes(stored)))
        self.assertTrue(stored.endswith(b"\n"))
        self.assertTrue(wc.is_canonical(stored))

    def test_noncanonical_valid_bytes_are_refused_by_canonical_check(self):
        noncanonical = json.dumps(workspace_mapping(), indent=2).encode("utf-8")

        self.assertIsInstance(wm.parse_bytes(noncanonical), wm.WorkspacePlan)
        self.assertFalse(wc.is_canonical(noncanonical))

    def test_digest_changes_when_any_stored_byte_changes(self):
        stored = wc.canonicalize_workspace(parse(workspace_mapping()))
        original = wd.digest_of(stored)

        for offset, value in enumerate(stored):
            changed = stored[:offset] + bytes((value ^ 1,)) + stored[offset + 1:]
            self.assertNotEqual(original, wd.digest_of(changed), offset)

    def test_digest_refuses_a_parsed_workspace(self):
        with self.assertRaises(TypeError):
            wd.digest_of(parse(workspace_mapping()))  # type: ignore[arg-type]


class DigestImportBoundaryTests(unittest.TestCase):
    def test_digest_module_cannot_reach_workspace_parsing_or_canonicalization(self):
        module = ast.parse((ADWS / "adw_modules" / "workspace_digest.py").read_text())
        imported = set()
        for statement in ast.walk(module):
            if isinstance(statement, ast.Import):
                imported.update(alias.name for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom):
                imported.add(statement.module or "")

        self.assertFalse(any("workspace_model" in name or
                             "workspace_canonical" in name
                             for name in imported))


if __name__ == "__main__":
    unittest.main()
