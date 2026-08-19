"""A declared `runners:` block binds the run, or nothing declares anything.

`runner_resolution` states the rule its docstring calls "declaration binds,
discovery only proposes": when `maestro.config.yaml` names an interpreter under
`runners:`, that interpreter is used and never second-guessed, and only in its
absence does discovery run and print the adoption notice inviting the operator
to pin what it found.

Both halves existed. `_load_maestro_layout` parsed the block and returned it,
`_resolve_run_runners` accepted a `declared` mapping and honoured it -- and
between them, nothing. `args.runners` was assigned in exactly one place,
`_bind_layout_executables`, which serves `bootstrap`, `plan author`, and the
run *ledger* verbs. No run-execution verb goes through it. So every
`run start` and `run resume` reached `_resolve_run_runners` with an empty
declaration, discovered an interpreter, and printed

    maestro resolved these gate runners by discovery; declare them in
    maestro.config.yaml to pin them:

at a repository whose configuration had declared it already. The notice was
correct about what happened and useless as advice: adopting it changed nothing,
because the block it asked for was the block being ignored.

This is the shape `test_retry_budget_configuration` was written for -- a typed
value present at both ends, dropped by the projection between them, invisible
because each end looks right on its own. The first test below fixes the
instance. The second fixes the class for this key: it asserts the assignment
lives on the branch `start` and `resume` share, so a future edit cannot bind it
for one verb and drop it for the other.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro

ADWS = Path(__file__).resolve().parent.parent

DECLARED_RUNNERS = {"pytest": ".venv/bin/pytest"}


def _installation(root: Path, runners: dict | None):
    repo = root / "project"
    (repo / "adws").mkdir(parents=True)
    (repo / "plans" / "demo").mkdir(parents=True)
    (repo / "plans" / "demo" / "maestro-plan.v1").write_text(
        "{}", encoding="utf-8")
    (repo / ".git").mkdir()
    binaries = {}
    for name in ("herdr", "omp", "claude"):
        binary = root / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        binaries[name] = str(binary)
    config = {
        "schema": "maestro-config.v1",
        "plans_dir": "plans",
        "state_root": "../maestro-state",
        "keys": {
            "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
            "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
            "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
        },
        "executables": binaries,
        "route_receipts": {"omp": "route-receipts/omp.json"},
        "reviewer": {
            "route": "omp", "model": "review-model", "effort": "high",
            "finalization_timeout_s": 60, "turn_timeout_s": 20,
            "poll_interval_s": 1,
        },
        "execution": {
            "route": "omp", "model": "execution-model", "effort": "medium",
            "concurrency": 2, "node_timeout_s": 120, "turn_timeout_s": 30,
            "final_acceptance_timeout_s": 45, "backstop_t_s": 600,
            "semantic_ceiling": 3,
        },
    }
    if runners is not None:
        config["runners"] = dict(runners)
    path = repo / "adws" / "maestro.config.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    return repo, path


def _configured_start_args(root: Path, runners: dict | None):
    """`run start`, driven through the real configuration binder."""
    repo = _installation(root, runners)[0]
    args = argparse.Namespace(command="run", run_command="start",
                              plan_name="demo")
    environment = {
        "MAESTRO_TEST_VERIFY_KEY": "11" * 32,
        "MAESTRO_TEST_SIGNING_SEED": "22" * 32,
        "MAESTRO_TEST_ROUTE_VERIFY_KEY": "33" * 32,
    }
    cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        with mock.patch.dict(os.environ, environment):
            maestro._apply_repository_config(args, ())
    finally:
        os.chdir(cwd)
    return args


class DeclaredRunnersReachTheRunTest(unittest.TestCase):

    def test_the_declared_block_survives_the_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, path = _installation(Path(tmp).resolve(), DECLARED_RUNNERS)
            layout = maestro._load_maestro_layout(repo, path)
        self.assertEqual(layout["runners"], DECLARED_RUNNERS)

    def test_the_declared_block_lands_on_the_run_arguments(self):
        """The step that was missing: layout -> `args`, for a run verb.

        `_resolve_run_runners` reads `getattr(args, "runners", {})`, so a
        declaration that never reaches `args` is dropped exactly as thoroughly
        as one the loader never parsed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            args = _configured_start_args(Path(tmp).resolve(), DECLARED_RUNNERS)
        self.assertEqual(args.runners, DECLARED_RUNNERS)

    def test_an_absent_block_binds_an_empty_declaration(self):
        """Absence is what makes discovery legal, so it must be expressible."""
        with tempfile.TemporaryDirectory() as tmp:
            args = _configured_start_args(Path(tmp).resolve(), None)
        self.assertEqual(args.runners, {})

    def test_what_the_run_binds_is_what_the_resolver_reads(self):
        """The two ends named the same attribute, which is why nothing caught
        this: `args.runners` was spelled identically at both, and unwritten in
        between for the verbs that matter."""
        with tempfile.TemporaryDirectory() as tmp:
            args = _configured_start_args(Path(tmp).resolve(), DECLARED_RUNNERS)
        source = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        resolver = next(
            node for node in ast.walk(source)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_resolve_run_runners")
        read = {
            call.args[1].value
            for call in ast.walk(resolver)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name) and call.func.id == "getattr"
            and len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
        }
        self.assertIn("runners", read)
        self.assertEqual(dict(getattr(args, "runners", {})), DECLARED_RUNNERS)


class StartAndResumeBindTheSameRunnersTest(unittest.TestCase):
    """`resume` cannot be driven here without a ledger, a plan receipt and a
    live run, so the guard is structural instead: both verbs take the one
    `elif args.command == "run":` branch of `_apply_repository_config`, and the
    assignment must live on it rather than on a path only one of them reaches.

    Binding `runners` per-verb is how the two would come to disagree about
    which interpreter a gate runs under -- a `start` that pins and a `resume`
    that discovers, on the same repository, decided by which verb was typed.
    """

    def _run_branch(self) -> ast.If:
        source = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        binder = next(
            node for node in ast.walk(source)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_apply_repository_config")
        for node in ast.walk(binder):
            if not isinstance(node, ast.If):
                continue
            if "db" in self._assigned_names(node.body):
                return node
        self.fail("_apply_repository_config has no branch binding args.db")

    @staticmethod
    def _assigned_names(body) -> set:
        names = set()
        for statement in body:
            for assignment in ast.walk(statement):
                if not isinstance(assignment, ast.Assign):
                    continue
                for target in assignment.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "args"):
                        names.add(target.attr)
        return names

    def test_the_run_branch_binds_the_declared_runners(self):
        self.assertIn("runners", self._assigned_names(self._run_branch().body))


if __name__ == "__main__":
    unittest.main()
