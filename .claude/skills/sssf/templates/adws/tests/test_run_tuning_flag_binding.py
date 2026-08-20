"""#91 — a tuning flag on `run resume` must not disable configuration binding.

`_apply_repository_config` had one rule for the run verbs: any flag outside
`{--run-id, --json, --discard}` means the operator is driving every path by
hand, so bind nothing. The rule's reason is sound — a `--plan-file` typed at
the prompt beside a `--digest` bound from `maestro.config.yaml` is two halves
describing different runs — but its allowlist was three flags wide while
`_add_run_execution_options` advertises twenty-nine, so

    maestro run resume run-… --environmental-retries 6 --launcher-retries 5

refused with fifteen missing values. Not one of those fifteen has anything to
do with a retry budget; every one binds from configuration, and the identical
command without the two flags works. The consequence was that no supported
command-line way to raise a retry budget for one resume existed at all: the
only route was editing `execution:` in a deployment-owned config file, a
persistent change made to express a one-run intent.

Two halves, tested here in that order:

* the partition — a flag that names a *setting* binds; a flag that names a
  *path, key or executable* still disables binding, because that is the
  disagreement the rule exists to refuse; and an explicit setting wins over the
  configured one, which is `_bind_salvage_configuration`'s rule (#83) applied
  to the run verbs;
* the refusal — when binding *is* disabled, the message names the flag that
  did it and the two ways out. This is #78's correction: the refusal was right
  and its vocabulary named a consequence rather than the rule.

`TheTuningPartitionIsTotalTest` is the class guard. The instance above is one
flag; the class is "a new `execution:` setting is added, wired to a flag, and
classified as identity by omission", which would silently restore the defect
for that setting alone.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import lifecycle as lc
from adw_modules import plan_digest
from adw_modules import scheduler_types as st

ENVIRONMENT = {
    "MAESTRO_TEST_VERIFY_KEY": "11" * 32,
    "MAESTRO_TEST_SIGNING_SEED": "22" * 32,
    "MAESTRO_TEST_ROUTE_VERIFY_KEY": "33" * 32,
}

#: Every run-execution flag that names a path, a key or an executable. None of
#: these may become a tuning flag: each one either selects the run or locates
#: something the configured half would have located differently, which is the
#: half-disagreement the all-or-nothing rule exists to refuse.
IDENTITY_OPTIONS = frozenset({
    "--plan-file", "--repo", "--receipt-dir", "--data-dir", "--verify-key",
    "--db", "--run-id", "--integration-path", "--worktrees-root",
    "--scratch-root", "--route-receipt", "--route-verify-key", "--digest",
    "--herdr", "--omp", "--claude",
})


def _installation(root: Path, execution_extra: dict):
    """A configured repository, laid out exactly as `maestro bootstrap` leaves
    one: a config file under `adws/`, one installed plan, and executables the
    loader can stat."""
    repo = root / "project"
    (repo / "adws").mkdir(parents=True)
    (repo / "plans" / "demo").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "plans" / "demo" / "maestro-plan.v1").write_text(
        "{}", encoding="utf-8")
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
        "semantic_ceiling": 3, "environmental_retries": 2,
        "launcher_retries": 4, "credential_retries": 0,
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


def _resumable(root: Path, execution_extra: dict = None):
    """An installation with one run already in the ledger, so `run resume` has
    something real to resolve. Returns `(repo, run_id)`."""
    repo, path = _installation(root, execution_extra or {})
    with mock.patch.dict(os.environ, ENVIRONMENT):
        layout = maestro._load_maestro_layout(repo, path)
    digest = plan_digest.digest_of(
        (repo / "plans" / "demo" / "maestro-plan.v1").read_bytes())
    run_id = "run-" + "5" * 32
    store = lc.LifecycleStore(layout["database"])
    try:
        store.create_run(run_id, digest, ())
    finally:
        store.close()
    return repo, run_id


@contextlib.contextmanager
def _inside(repo: Path):
    cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        with mock.patch.dict(os.environ, ENVIRONMENT):
            yield
    finally:
        os.chdir(cwd)


def _resume_args(run_id: str, **typed):
    """The namespace `argparse` would hand `_apply_repository_config` for a
    `run resume`, with the flags the operator typed already parsed onto it."""
    namespace = argparse.Namespace(
        command="run", run_command="resume", selector=run_id, run_id=None)
    for name, value in typed.items():
        setattr(namespace, name, value)
    return namespace


class ATuningFlagDoesNotDisableBindingTest(unittest.TestCase):
    """The instance: `--environmental-retries 6` on a configured resume."""

    def test_every_configured_value_still_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, run_id = _resumable(Path(tmp).resolve())
            args = _resume_args(run_id, environmental_retries=6)
            with _inside(repo):
                maestro._apply_repository_config(
                    args, ("run", "resume", run_id,
                           "--environmental-retries", "6"))
        # The fifteen the refusal used to list. Each one of them binding is the
        # whole of the report in #91: the identical command without the flag
        # bound all fifteen, and one tuning flag turned them all off.
        for name in ("plan_file", "receipt_dir", "data_dir", "verify_key",
                     "digest", "db", "integration_path", "worktrees_root",
                     "scratch_root", "concurrency", "node_timeout_s",
                     "turn_timeout_s", "final_acceptance_timeout_s",
                     "backstop_t_s", "semantic_ceiling"):
            self.assertTrue(getattr(args, name, None),
                            name + " did not bind from configuration")

    def test_the_resumed_run_id_is_the_one_named_not_a_minted_one(self):
        """Binding a resume must re-enter the existing run. A minted id here
        would hand the scheduler an empty ledger, which is a worse failure than
        the refusal this fix removes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, run_id = _resumable(Path(tmp).resolve())
            args = _resume_args(run_id, environmental_retries=6)
            with _inside(repo):
                maestro._apply_repository_config(
                    args, ("run", "resume", run_id,
                           "--environmental-retries", "6"))
        self.assertEqual(args.run_id, run_id)
        self.assertIn(run_id, args.integration_path)

    def test_the_typed_budget_wins_over_the_configured_one(self):
        """`_bind_salvage_configuration`'s rule (#83): only the options the
        operator did not type are derived. Without this the flag would stop
        disabling binding and start being silently overwritten instead, which
        is the same defect wearing a quieter face."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, run_id = _resumable(Path(tmp).resolve())
            args = _resume_args(run_id, environmental_retries=6)
            with _inside(repo):
                maestro._apply_repository_config(
                    args, ("run", "resume", run_id,
                           "--environmental-retries", "6"))
        self.assertEqual(args.environmental_retries, 6)
        # Untyped, so still configuration's: overriding one setting must not
        # quietly reset its neighbours to a default.
        self.assertEqual(args.launcher_retries, 4)

    def test_it_survives_all_the_way_into_the_scheduler_configuration(self):
        """The projection is where §7.4's dropped `min_cases` lived, so the
        assertion is made against `SchedulerConfig` rather than against
        `args`."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, run_id = _resumable(Path(tmp).resolve())
            args = _resume_args(
                run_id, environmental_retries=6, launcher_retries=5)
            with _inside(repo):
                maestro._apply_repository_config(
                    args, ("run", "resume", run_id,
                           "--environmental-retries", "6",
                           "--launcher-retries", "5"))
                config = maestro._run_configuration(args)
        self.assertEqual(
            (config.environmental_retries, config.launcher_retries), (6, 5))
        self.assertEqual(config.concurrency, 2)

    def test_the_reproducing_command_line_no_longer_refuses(self):
        """#91's command, driven through `main` and its real refusal path."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, run_id = _resumable(Path(tmp).resolve())
            stream = io.StringIO()
            with _inside(repo):
                with contextlib.redirect_stdout(stream):
                    with contextlib.redirect_stderr(io.StringIO()):
                        maestro.main(("run", "resume", run_id,
                                      "--environmental-retries", "6",
                                      "--launcher-retries", "5"))
        self.assertNotIn("missing run configuration", stream.getvalue())
        self.assertNotIn("RUN_CONFIGURATION_REQUIRED", stream.getvalue())


class APathFlagStillDisablesBindingTest(unittest.TestCase):
    """The half of the rule that is not a defect, and must stay."""

    def _manual(self, tmp: Path, option: str, value: str):
        repo, run_id = _resumable(tmp)
        args = _resume_args(run_id)
        with _inside(repo):
            maestro._apply_repository_config(
                args, ("run", "resume", run_id, option, value))
        return args

    def test_a_path_flag_binds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._manual(Path(tmp).resolve(), "--db", "/tmp/elsewhere")
        self.assertFalse(getattr(args, "plan_file", None))
        self.assertFalse(getattr(args, "receipt_dir", None))

    def test_it_records_the_flag_that_did_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._manual(Path(tmp).resolve(), "--db", "/tmp/elsewhere")
        self.assertEqual(getattr(args, "manual_run_options", ()), ("--db",))

    def test_an_unclassified_flag_falls_on_the_safe_side(self):
        """A flag this partition has never heard of may well name a path, so
        it disables binding rather than being assumed harmless."""
        with tempfile.TemporaryDirectory() as tmp:
            args = self._manual(
                Path(tmp).resolve(), "--not-a-real-flag", "x")
        self.assertEqual(getattr(args, "manual_run_options", ()),
                         ("--not-a-real-flag",))


class TheRefusalNamesTheRuleTest(unittest.TestCase):
    """#78's correction applied to #91: the refusal is right, its vocabulary
    named a consequence. Fifteen missing values read as a broken installation;
    what happened was one word on the command line."""

    def _detail(self, tmp: Path):
        repo, run_id = _resumable(tmp)
        stream = io.StringIO()
        with _inside(repo):
            with contextlib.redirect_stdout(stream):
                with contextlib.redirect_stderr(io.StringIO()):
                    maestro.main(("run", "resume", run_id,
                                  "--plan-file", "/nowhere/plan.v1"))
        return json.loads(stream.getvalue().strip().splitlines()[-1])

    def test_it_is_still_the_same_typed_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._detail(Path(tmp).resolve())
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertIn("missing run configuration", payload["detail"])

    def test_it_names_the_flag_that_switched_binding_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._detail(Path(tmp).resolve())
        self.assertIn("--plan-file", payload["detail"])

    def test_it_states_both_remedies(self):
        with tempfile.TemporaryDirectory() as tmp:
            detail = self._detail(Path(tmp).resolve())["detail"]
        # Drop the flag, or supply the rest by hand. An operator who can read
        # only one sentence of this must still be able to act on it.
        self.assertIn("drop", detail.lower())
        self.assertIn(str(maestro._MAESTRO_CONFIG_FILE), detail)

    def test_it_says_tuning_flags_are_not_this(self):
        """Otherwise the message teaches the wrong lesson — that flags in
        general are incompatible with a configured repository, which is the
        belief that made a persistent config edit look like the only way to
        raise a budget."""
        with tempfile.TemporaryDirectory() as tmp:
            detail = self._detail(Path(tmp).resolve())["detail"]
        self.assertIn("--environmental-retries", detail)

    def test_a_run_that_bound_nothing_for_other_reasons_blames_no_flag(self):
        """An unconfigured tree reaches the same refusal, and there is no flag
        to name. Inventing one would be the #78 defect in mirror image."""
        args = argparse.Namespace(command="run", run_command="resume")
        with self.assertRaises(
                maestro._PlanReceiptConfigurationError) as caught:
            maestro._run_configuration(args)
        self.assertNotIn("binding was disabled", str(caught.exception))


class TheTuningPartitionIsTotalTest(unittest.TestCase):
    """The class guard. The instance is one flag; the class is a new
    `execution:` setting wired to a flag and classified as identity by
    omission, which restores the defect for that setting alone."""

    @staticmethod
    def _resume_parser() -> argparse.ArgumentParser:
        parser = maestro.build_parser()
        root = next(action for action in parser._actions
                    if isinstance(action, argparse._SubParsersAction))
        run = root.choices["run"]
        run_sub = next(action for action in run._actions
                       if isinstance(action, argparse._SubParsersAction))
        return run_sub.choices["resume"]

    def _declared(self):
        """Every long option `run resume` accepts, mapped to its real
        `argparse` destination. Read off the parser rather than restated, so a
        renamed `dest` fails here instead of writing to an attribute nothing
        reads."""
        declared = {}
        for action in self._resume_parser()._actions:
            # `--help` is argparse's own and exits during parsing, so it never
            # reaches `_apply_repository_config` and has no side to fall on.
            if isinstance(action, argparse._HelpAction):
                continue
            for option in action.option_strings:
                if option.startswith("--"):
                    declared[option] = action.dest
        return declared

    def test_every_tuning_option_is_a_flag_resume_actually_accepts(self):
        declared = self._declared()
        for option in maestro._RUN_TUNING_OPTIONS:
            self.assertIn(option, declared,
                          option + " is classified but not declared")

    def test_every_tuning_option_names_the_destination_argparse_writes(self):
        """The write-back sets attributes by name. A mapping that disagreed
        with `argparse` would restore an attribute nobody reads and leave the
        configured value in place — a fix that passes its own unit test and
        changes no run."""
        declared = self._declared()
        for option, attribute in maestro._RUN_TUNING_OPTIONS.items():
            self.assertEqual(declared[option], attribute,
                             option + " writes " + declared[option])

    def test_every_scheduler_setting_is_tuning(self):
        """`SchedulerConfig`'s fields are settings by definition — the
        scheduler reads them to decide readiness, concurrency, timeouts and
        retries, and not one of them locates a file. Adding a field, wiring a
        flag for it, and leaving it unclassified is exactly how #91 happens
        again, so the enumeration is derived from the dataclass."""
        declared = self._declared()
        for field in dataclasses.fields(st.SchedulerConfig):
            option = "--" + field.name.replace("_", "-")
            if option not in declared:
                continue
            self.assertIn(
                option, maestro._RUN_TUNING_OPTIONS,
                option + " is a SchedulerConfig setting but is classified as "
                "identity, so passing it disables configuration binding")
            self.assertEqual(
                maestro._RUN_TUNING_OPTIONS[option], field.name)

    def test_no_path_key_or_executable_flag_is_tuning(self):
        """The other direction, and the one that would be a correctness bug
        rather than an ergonomic one: a path reclassified as tuning would let a
        configured half and a typed half describe different runs."""
        for option in IDENTITY_OPTIONS:
            self.assertNotIn(option, maestro._RUN_TUNING_OPTIONS,
                             option + " names a path and must keep disabling "
                             "configuration binding")

    def test_the_three_sets_are_disjoint(self):
        self.assertFalse(
            maestro._RUN_SELECTION_OPTIONS
            & frozenset(maestro._RUN_TUNING_OPTIONS))
        self.assertFalse(IDENTITY_OPTIONS & frozenset(maestro._RUN_TUNING_OPTIONS))

    def test_the_partition_covers_every_flag_resume_declares(self):
        """Not an assertion that the union is total — an unclassified flag is
        deliberately allowed and falls on the identity side. It asserts that
        the reason it falls there is a decision someone recorded, by naming the
        set it belongs to."""
        unclassified = sorted(
            set(self._declared())
            - maestro._RUN_SELECTION_OPTIONS
            - frozenset(maestro._RUN_TUNING_OPTIONS)
            - IDENTITY_OPTIONS)
        self.assertEqual(unclassified, [])


if __name__ == "__main__":
    unittest.main()
