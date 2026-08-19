"""D12 — a retry budget set in `maestro.config.yaml` reaches the scheduler.

`SchedulerConfig.environmental_retries`, `.launcher_retries` and
`.credential_retries` existed at one end and `execution:` existed at the other,
and nothing connected them: `_run_configuration` built `SchedulerConfig` by
naming six fields and not these three, so every deployment ran on the dataclass
defaults whatever its configuration said. §7.5's CREDENTIAL row — the one entry
in the retry table whose whole purpose is not to retry — could not be set at
all.

This is the same shape as §7.4's dropped `min_cases`: a typed value present at
both ends, dropped by a field-by-field projection between them, invisible
because the source has the field, the destination has the field, and nothing
compares the two. `TheProjectionIsTotalTest` below is the guard against the
next one, and it is the point of this file — the three tests above it fix an
instance, that one fixes the class.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import code_review as cr
from adw_modules import retry_policy as rp
from adw_modules import scheduler_types as st

ADWS = Path(__file__).resolve().parent.parent


def _installation(root: Path, execution_extra: dict):
    repo = root / "project"
    (repo / "adws").mkdir(parents=True)
    (repo / "plans").mkdir(parents=True)
    (repo / ".git").mkdir()
    binaries = {}
    for name in ("herdr", "omp", "claude"):
        binary = root / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        binaries[name] = str(binary)
    execution = {
        "route": "omp", "model": "execution-model", "effort": "medium",
        "concurrency": 2, "node_timeout_s": 120, "turn_timeout_s": 30,
        "final_acceptance_timeout_s": 45, "backstop_t_s": 600,
        "semantic_ceiling": 3,
    }
    execution.update(execution_extra)
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
        "execution": execution,
    }
    path = repo / "adws" / "maestro.config.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")
    return repo, path


def _load(execution_extra: dict, root: Path) -> dict:
    repo, path = _installation(root, execution_extra)
    return maestro._load_maestro_layout(repo, path)


def _scheduler_config(execution: dict) -> st.SchedulerConfig:
    """The production projection, driven end to end from a config section.

    `_run_configuration` is the function that dropped these, so a test that
    constructed `SchedulerConfig` itself would prove nothing about it.
    """
    args = argparse.Namespace(
        plan_file="p", repo="r", receipt_dir="rc", data_dir="d",
        verify_key=["k"], digest="a" * 64, db="db", run_id="run-1",
        integration_path="i", worktrees_root="w", scratch_root="s")
    for name in ("concurrency", "node_timeout_s", "turn_timeout_s",
                 "final_acceptance_timeout_s", "backstop_t_s",
                 "semantic_ceiling", "review_ceiling",
                 "environmental_retries", "launcher_retries",
                 "credential_retries"):
        if name in execution:
            setattr(args, name, execution[name])
    return maestro._run_configuration(args)


class ConfiguredBudgetsReachTheSchedulerTest(unittest.TestCase):

    def test_configured_budgets_survive_the_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({"environmental_retries": 5,
                            "launcher_retries": 4,
                            "credential_retries": 1},
                           Path(tmp).resolve())
        execution = layout["execution"]
        self.assertEqual(execution["environmental_retries"], 5)
        self.assertEqual(execution["launcher_retries"], 4)
        self.assertEqual(execution["credential_retries"], 1)

    def test_configured_budgets_survive_the_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({"environmental_retries": 5,
                            "launcher_retries": 4,
                            "credential_retries": 1},
                           Path(tmp).resolve())
        config = _scheduler_config(layout["execution"])
        self.assertEqual(config.environmental_retries, 5)
        self.assertEqual(config.launcher_retries, 4)
        self.assertEqual(config.credential_retries, 1)

    def test_the_budget_the_scheduler_spends_is_the_configured_one(self):
        """The end the run actually reads: §7.5's budget lookups."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({"environmental_retries": 5,
                            "launcher_retries": 4,
                            "credential_retries": 0},
                           Path(tmp).resolve())
        config = _scheduler_config(layout["execution"])
        self.assertEqual(
            config.retry_budget(st.RetryClass.ENVIRONMENTAL), 5)
        self.assertEqual(
            config.retry_budget(st.RetryClass.LAUNCHER_TRANSIENT), 4)
        self.assertEqual(
            rp.launcher_retry_budget(config, rp.LauncherFailure.CREDENTIAL), 0)
        self.assertEqual(
            rp.launcher_retry_budget(config,
                                     rp.LauncherFailure.PANE_ALLOCATION),
            4)

    def test_a_credential_budget_of_zero_is_configurable(self):
        """§7.5's one row whose purpose is not to retry. A positive-integer
        validator would have made it unexpressible."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({"credential_retries": 0}, Path(tmp).resolve())
        self.assertEqual(layout["execution"]["credential_retries"], 0)

    def test_a_negative_budget_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                _load({"launcher_retries": -1}, Path(tmp).resolve())
        self.assertIn("execution.launcher_retries", str(caught.exception))

    def test_a_non_integer_budget_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(maestro._MaestroConfigurationError):
                _load({"environmental_retries": 2.5}, Path(tmp).resolve())

    def test_an_absent_key_takes_the_dataclass_default_and_only_that(self):
        """One representation of each number, read off `SchedulerConfig`.

        A literal in the config loader would be a second copy that drifts the
        first time either changes — the shape RC1 names and the reason these
        keys were unreachable to begin with.
        """
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({}, Path(tmp).resolve())
        declared = {
            field.name: field.default
            for field in dataclasses.fields(st.SchedulerConfig)
            if field.default is not dataclasses.MISSING
        }
        for name in ("environmental_retries", "launcher_retries",
                     "credential_retries"):
            self.assertEqual(layout["execution"][name], declared[name])


class TheRunBranchWritesEveryExecutionKeyTest(unittest.TestCase):
    """The middle step: `maestro.config.yaml` -> `args`, driven for real.

    `_run_configuration` reads `args`, so a budget that never reaches `args` is
    dropped exactly as thoroughly as one `_run_configuration` forgets to name.
    Both halves are the same defect and both are asserted, here and above.
    """

    def _configured_args(self, execution_extra: dict, root: Path):
        repo, _path = _installation(root, execution_extra)
        (repo / "plans" / "demo").mkdir(parents=True)
        (repo / "plans" / "demo" / "maestro-plan.v1").write_text(
            "{}", encoding="utf-8")
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

    def test_every_new_execution_key_lands_on_the_run_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._configured_args(
                {"environmental_retries": 5, "launcher_retries": 4,
                 "credential_retries": 1, "provision": ["npm", "ci"]},
                Path(tmp).resolve())
        self.assertEqual(args.environmental_retries, 5)
        self.assertEqual(args.launcher_retries, 4)
        self.assertEqual(args.credential_retries, 1)
        self.assertEqual(args.provision_argv, ["npm", "ci"])

    def test_the_configured_budgets_survive_all_the_way_to_the_scheduler(self):
        """One assertion over the whole path, because the defect lived in the
        gap between two steps that each looked correct on their own."""
        with tempfile.TemporaryDirectory() as tmp:
            args = self._configured_args(
                {"environmental_retries": 5, "launcher_retries": 4,
                 "credential_retries": 1}, Path(tmp).resolve())
            config = maestro._run_configuration(args)
        self.assertEqual(
            (config.environmental_retries, config.launcher_retries,
             config.credential_retries), (5, 4, 1))


class TheRejectGradeReachesTheReviewerTest(unittest.TestCase):
    """`execution.review_reject_grade`, end to end — and why its destination
    is not `SchedulerConfig`.

    Every other `execution:` key this file drives continues into
    `SchedulerConfig`, and this one deliberately stops at `args`. That is not
    an oversight and it is not a quieter version of the defect above.

    `SchedulerConfig` is the **scheduler's** contract: the fields it reads to
    decide readiness, concurrency, timeouts and retries. The reject threshold
    is read by `_code_review_runner`'s closure, which the scheduler receives as
    a dependency and never inspects. Carrying it through `SchedulerConfig`
    would add a field with **zero readers**, and §3.6 B15 states why that is
    worse than leaving it out rather than merely redundant: a field nothing
    reads looks checked and is not — which is exactly how Strav shipped ten
    quality gates whose fields survived on a model with no consumers.

    So the asymmetry is correct, and the coverage is not optional because of
    it. `TheProjectionIsTotalTest` below guards the class by enumerating
    `SchedulerConfig`'s fields, and therefore structurally cannot see a key
    that is not one. Until this class, `maestro.py`'s assignment and
    `_code_review_runner`'s read were joined by nothing under test: typed at
    both ends, unasserted in the middle, which is the shape this file exists to
    convict.
    """

    def _configured_args(self, execution_extra: dict, root: Path):
        repo, _path = _installation(root, execution_extra)
        (repo / "plans" / "demo").mkdir(parents=True)
        (repo / "plans" / "demo" / "maestro-plan.v1").write_text(
            "{}", encoding="utf-8")
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

    def test_the_configured_grade_survives_the_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = _load({"review_reject_grade": "warning"},
                           Path(tmp).resolve())
        self.assertIs(cr.FindingGrade.WARNING,
                      loaded["execution"]["review_reject_grade"])

    def test_an_absent_key_takes_the_declared_default(self):
        """The default is the module's constant rather than a literal repeated
        at the loader, so there is one place the answer can be wrong."""
        with tempfile.TemporaryDirectory() as tmp:
            loaded = _load({}, Path(tmp).resolve())
        self.assertIs(cr.DEFAULT_REJECT_GRADE,
                      loaded["execution"]["review_reject_grade"])

    def test_an_unknown_grade_is_refused_at_load(self):
        """§6.2: configuration is validated where it is read. A threshold
        nobody can name must not reach a run at all — and `blocking` is the
        plausible wrong answer, because it is a `Severity` and severities are
        stamped by code, never chosen by an installation (§6.5)."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(
                    maestro._MaestroConfigurationError) as caught:
                _load({"review_reject_grade": "blocking"}, Path(tmp).resolve())
        self.assertIn("execution.review_reject_grade", str(caught.exception))

    def test_the_configured_grade_lands_on_the_run_arguments(self):
        """The middle step, driven through the real `_apply_repository_config`
        exactly as the budgets above are."""
        with tempfile.TemporaryDirectory() as tmp:
            args = self._configured_args({"review_reject_grade": "note"},
                                         Path(tmp).resolve())
        self.assertIs(cr.FindingGrade.NOTE, args.review_reject_grade)

    def test_the_default_lands_there_too(self):
        """A run whose configuration names nothing still carries an explicit
        threshold, rather than one inferred at the review site from an absent
        attribute."""
        with tempfile.TemporaryDirectory() as tmp:
            args = self._configured_args({}, Path(tmp).resolve())
        self.assertIs(cr.DEFAULT_REJECT_GRADE, args.review_reject_grade)

    def test_the_review_stage_reads_it_from_those_arguments(self):
        """The last step, read out of `_code_review_runner`'s own source.

        `TheProjectionIsTotalTest` parses `_run_configuration` rather than
        calling it for the same reason this parses rather than calls: the
        defect is a *name* that stops being passed, and a test that supplied
        the argument itself would prove nothing about the call site. Driving
        the closure would need a real repository, a real diff, a resolved model
        catalog and a launcher, none of which is this file's subject.
        """
        source = (ADWS / "maestro.py").read_text(encoding="utf-8")
        runner = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.name == "_code_review_runner")
        calls = [
            node for node in ast.walk(runner)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "review_attempt"]
        self.assertEqual(1, len(calls), "expected one review dispatch")
        passed = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("reject_at", passed,
                      "the review dispatch names no threshold, so the "
                      "installation's configuration decides nothing")
        read = passed["reject_at"]
        self.assertIsInstance(read, ast.Attribute)
        self.assertEqual("review_reject_grade", read.attr)
        self.assertIsInstance(read.value, ast.Name)
        self.assertEqual("args", read.value.id)

    def test_the_detector_convicts_a_dispatch_that_dropped_it(self):
        """§13.4's pair: the check above must return non-zero on a planted
        violation, or it is a grep nobody has shown to work."""
        planted = ast.parse(
            "def _code_review_runner(args, runner):\n"
            "    def review(a, b, c, d, e):\n"
            "        return code_review.review_attempt(store=None)\n")
        runner = next(node for node in ast.walk(planted)
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_code_review_runner")
        calls = [node for node in ast.walk(runner)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "review_attempt"]
        self.assertNotIn("reject_at", {kw.arg for kw in calls[0].keywords})

    def test_the_key_is_deliberately_not_a_scheduler_setting(self):
        """The decision, pinned. Adding it to `SchedulerConfig` gives the
        scheduler a field it never reads, which §3.6 B15 calls a build failure;
        this fails first and points at the comment that says why."""
        fields = {field.name for field in dataclasses.fields(st.SchedulerConfig)}
        self.assertNotIn("review_reject_grade", fields)


class TheProjectionIsTotalTest(unittest.TestCase):
    """The class fix. §7.4 states the rule this enforces.

    "A projection that copies a gate field by field will drop it silently...
    the source has a field, the destination does not, and neither the type
    checker nor any test comparing the two ends is looking at the field that
    did not survive."

    Here the destination is `SchedulerConfig` and the projection is
    `maestro._run_configuration`. Adding a field to `SchedulerConfig` without
    giving it a path from `maestro.config.yaml` fails this test rather than
    running every deployment on a default nobody chose.
    """

    @staticmethod
    def _projected_field_names() -> set:
        source = (ADWS / "maestro.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="maestro.py")
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != "_run_configuration":
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                if name != "SchedulerConfig":
                    continue
                if any(keyword.arg is None for keyword in call.keywords):
                    raise AssertionError(
                        "_run_configuration builds SchedulerConfig by splat. "
                        "tests/test_no_dead_seams.py skips a class built that "
                        "way, so the reader-without-writer sweep that caught "
                        "the dropped min_cases would stop seeing this class "
                        "entirely. Name every field.")
                return {keyword.arg for keyword in call.keywords}
        raise AssertionError(
            "no SchedulerConfig construction found in _run_configuration")

    def test_every_scheduler_config_field_is_projected(self):
        declared = {field.name
                    for field in dataclasses.fields(st.SchedulerConfig)}
        projected = self._projected_field_names()
        self.assertEqual(
            set(), declared - projected,
            "SchedulerConfig fields that `maestro run start` never sets, so "
            "every deployment runs them at their dataclass default whatever "
            "maestro.config.yaml says: "
            + ", ".join(sorted(declared - projected)))
        self.assertEqual(
            set(), projected - declared,
            "_run_configuration names fields SchedulerConfig does not have")

    def test_every_projected_field_has_a_configuration_key(self):
        """The other half of the path: `SchedulerConfig`'s optional settings
        are all reachable from `execution:`, not merely from a flag."""
        optional = {
            field.name
            for field in dataclasses.fields(st.SchedulerConfig)
            if field.default is not dataclasses.MISSING
        }
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({}, Path(tmp).resolve())
        missing = sorted(optional - set(layout["execution"]))
        self.assertEqual(
            [], missing,
            "SchedulerConfig settings with no key in the loaded `execution` "
            "section, so no config file can set them: " + ", ".join(missing))


class TheManualPathTakesNoInventedDefaultTest(unittest.TestCase):
    """`maestro run start --concurrency ...` without these flags.

    The fallback must be the dataclass's own default, never a literal in
    `maestro.py`, and a field with no default must raise rather than acquire
    one.
    """

    def test_an_unsupplied_setting_falls_back_to_the_dataclass_default(self):
        args = SimpleNamespace(environmental_retries=None)
        self.assertEqual(
            maestro._scheduler_setting(args, "environmental_retries"),
            st.SchedulerConfig.environmental_retries)

    def test_a_field_without_a_default_has_no_fallback_to_invent(self):
        with self.assertRaises(KeyError):
            maestro._scheduler_setting(
                SimpleNamespace(concurrency=None), "concurrency")


if __name__ == "__main__":
    unittest.main()
