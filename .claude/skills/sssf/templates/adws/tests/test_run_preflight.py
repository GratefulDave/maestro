"""`run start` / `run resume` preflight: every reachable dependency, made
available or named, before any lane is dispatched.

A run used to discover a missing dependency at the stage that first needed it:
an attend operator dispatched into a scratch tree whose `provision_argv`
could not run there, a receipt, a key, a credential -- each one a mid-run
refusal naming only itself. These cases pin the preflight that replaces
that: every check runs, what has a known source is provisioned and checked
again, and one refusal lists exactly what only a person can supply.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import provisioning  # noqa: E402
from adw_modules import run_preflight as rp  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.scheduler import FactoryScheduler, RunPreflightRefused  # noqa: E402
from tests.test_artifact_store import RUN_ID, make_binding, make_lane, make_plan  # noqa: E402
from tests.test_prepare_once_per_dispatch import (  # noqa: E402
    _ROLE_ROUTES,
    _AdoptingLauncher,
    _init_repo,
)

_OPERATOR = {"route": "claude", "model": "opus", "effort": "medium"}
_REVIEWER = {"route": "omp", "profile": "openai-astra"}


class _Console:
    def __init__(self) -> None:
        self.steps: list[tuple[str, str, str]] = []

    def step(self, lane_id: str, message: str, detail: str = "") -> None:
        self.steps.append((lane_id, message, detail))


class _Store:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_run_preflight(self, run_id, *, preflight_id, payload):
        self.records.append(dict(payload))


class _FakeRunner:
    """Every external tool, answered from a table. Unlisted commands succeed."""

    def __init__(self, failing=(), outputs=None) -> None:
        self.failing = set(failing)
        self.outputs = dict(outputs or {})
        self.calls: list[tuple[str, ...]] = []
        self.importable: set[str] = set()

    def __call__(self, argv, *, cwd=None, env=None, timeout=None) -> rp.Proc:
        argv = tuple(str(item) for item in argv)
        self.calls.append(argv)
        if len(argv) == 4 and argv[1] == "-c" and "find_spec" in argv[2]:
            return rp.Proc(0 if argv[3] in self.importable else 1)
        key = " ".join(argv[:3])
        if key in self.failing:
            return rp.Proc(1, "", "{0}: failed".format(key))
        return rp.Proc(0, self.outputs.get(key, ""), "")


class _Bed:
    """A deployment layout under a temporary root, every file present."""

    def __init__(self, case: unittest.TestCase, *, attend: bool) -> None:
        holder = tempfile.TemporaryDirectory()
        case.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.keys = self.root / "keys"
        self.keys.mkdir()
        (self.keys / "reviewer-hmac.key").write_text("ab" * 32)
        (self.keys / "route.pub").write_text("cd" * 32)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        for route in ("omp", "claude"):
            (self.receipts / (route + ".json")).write_text("{}")
        self.planctl = self.root / "planctl.py"
        self.planctl.write_text("")
        self.plan_ir = self.root / "plan.ir.json"
        self.plan_ir.write_text("{}")
        self.profiles = self.root / "profiles"
        for name in ("grok", "openai-performance", "openai-astra"):
            (self.profiles / name).mkdir(parents=True)
        self.product = self.root / "product"
        self.head = _init_repo(self.product)
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.attend = attend
        self.layout = {
            "keys_dir": self.keys,
            "role_routes": _ROLE_ROUTES,
            "route_receipts": {
                "omp": str(self.receipts / "omp.json"),
                "claude": str(self.receipts / "claude.json"),
            },
            "route_verify_keys": [str(self.keys / "route.pub")],
            "executables": {"herdr": "herdr", "omp": "omp", "claude": "claude"},
            "attend": {
                "max_amendments_per_lane": 1 if attend else 0,
                "route": _OPERATOR if attend else {},
                "reviewer_route": _REVIEWER if attend else {},
                "planctl": self.planctl if attend else None,
                "plan_ir": self.plan_ir if attend else None,
            },
        }
        self.runner = _FakeRunner(
            outputs={"gh repo view": '{"viewerPermission":"WRITE"}'}
        )
        self.console = _Console()
        self.store = _Store()
        self.issues: list[str] = []
        self.missing_binaries: set[str] = set()
        patcher = mock.patch.object(maestro, "_OMP_PROFILES_DIR", self.profiles)
        patcher.start()
        case.addCleanup(patcher.stop)
        # Receipt signatures are route admission's own subject; here a
        # receipt on disk is admitted and a missing one is not.
        admitted = mock.patch.object(
            maestro,
            "load_admitted_routes",
            side_effect=lambda paths, verify_keys: SimpleNamespace(
                admits=lambda route: all(Path(p).is_file() for p in paths.values())
            ),
        )
        admitted.start()
        case.addCleanup(admitted.stop)
        key = mock.patch.object(maestro, "load_public_key", return_value=b"k" * 32)
        key.start()
        case.addCleanup(key.stop)
        case.addCleanup(provisioning.set_tree_packages, ())

    def which(self, name: str):
        return None if name in self.missing_binaries else "/usr/bin/" + name

    def preflight(self, **overrides) -> maestro._RunPreflight:
        runtime = SimpleNamespace(path=self.state)
        options = dict(
            verb="resume",
            attend=self.attend,
            plan_ir=self.plan_ir if self.attend else None,
            runner=self.runner,
            which=self.which,
            file_issue=lambda title, body: (self.issues.append(title) or {"status": "FILED"}),
        )
        options.update(overrides)
        return maestro._RunPreflight(
            self.layout, runtime, self.store, RUN_ID, self.target, self.console, **options
        )

    def actor(self, *, provision_argv=None) -> maestro.HerdrStageActor:
        launcher = _AdoptingLauncher(target_cwd_fn=lambda tree: tree)
        if provision_argv is not None:
            launcher.provision_argv = provision_argv
        self.launcher = launcher
        return maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, launcher),
            self.state,
            self.target,
            _ROLE_ROUTES,
            operator_route=_OPERATOR if self.attend else None,
            attend_reviewer_route=_REVIEWER if self.attend else None,
        )

    def scheduler(self, actor, lanes) -> SimpleNamespace:
        calls = {"runners": 0}

        def runners() -> None:
            calls["runners"] += 1

        return SimpleNamespace(
            actor=actor,
            preflight_lanes=lambda: tuple(lanes),
            _integration_head=lambda: self.head,
            _assert_runners_usable=runners,
            calls=calls,
        )


def _lane(lane_id: str, *, imports=(), outputs=(), suite=(), fixtures=()) -> dict:
    return {
        "lane_id": lane_id,
        "lane_kind": "build",
        "declared_outputs": tuple(outputs),
        "suite_paths": tuple(suite),
        "python_specifiers": tuple(imports),
        "spec": {
            "gate": {"cwd": "."},
            "obligations": {"observed_baseline": [{"path": path} for path in fixtures]},
        },
    }


def _venv_provisioner() -> tuple[str, ...]:
    """A `provision_argv` that makes a tree venv, as `uv sync` would."""
    return (
        sys.executable,
        "-c",
        "import pathlib;p=pathlib.Path('.venv/bin/python');"
        "p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')",
    )


def _by_id(items, lane_id=rp.RUN_WIDE):
    return {item.item_id: item for item in items if item.lane_id == lane_id}


class TheEngine(unittest.TestCase):
    def test_every_failure_is_reported_not_only_the_first(self) -> None:
        checks = [
            rp.Check("a", lambda: (False, "a gone"), "fix a"),
            rp.Check("b", lambda: (True, "b ok")),
            rp.Check("c", lambda: (False, "c gone"), "fix c", human_secret=True),
            rp.Check("d", lambda: (_ for _ in ()).throw(OSError("d broke"))),
        ]
        items = rp.evaluate(checks, lambda *args: None)
        self.assertEqual([item.item_id for item in rp.refused(items)], ["a", "c", "d"])
        self.assertTrue(_by_id(items)["c"].human_secret)
        report = rp.render(items)
        for name in ("a gone", "c gone", "d broke", "fix a"):
            self.assertIn(name, report)

    def test_a_missing_item_with_a_source_is_provisioned_and_checked_again(self) -> None:
        state = {"present": False}
        said = []

        def provision() -> str:
            state["present"] = True
            return "made it"

        items = rp.evaluate(
            [rp.Check("x", lambda: (state["present"], "x"), provision=provision)],
            lambda *args: said.append(args),
        )
        self.assertEqual(items[0].status, rp.ADDED)
        self.assertEqual(items[0].added, "made it")
        self.assertEqual(said, [("", "preflight provisioned x", "made it")])

    def test_a_provision_that_does_not_fix_it_refuses(self) -> None:
        items = rp.evaluate(
            [rp.Check("x", lambda: (False, "x"), provision=lambda: "tried")],
            lambda *args: None,
        )
        self.assertEqual(items[0].status, rp.REFUSED)
        self.assertIn("still missing after provisioning (tried)", items[0].detail)


class TheRealRunner(unittest.TestCase):
    def test_a_real_binary_runs_through_the_production_path(self) -> None:
        proc = rp.real_runner(["uv", "--version"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("uv", proc.stdout)
        items = rp.evaluate(
            [
                rp.binary_check("binary:uv", "uv", "install uv"),
                rp.command_check("uv:version", ["uv", "--version"], "", rp.real_runner),
            ],
            lambda *args: None,
        )
        self.assertEqual([item.status for item in items], [rp.VERIFIED, rp.VERIFIED])

    def test_a_missing_binary_is_an_exit_not_an_exception(self) -> None:
        proc = rp.real_runner(["definitely-not-a-binary-xyz"])
        self.assertEqual(proc.returncode, 127)


class RunWideChecks(unittest.TestCase):
    def test_a_complete_deployment_verifies_and_skips_what_it_cannot_reach(self) -> None:
        bed = _Bed(self, attend=False)
        preflight = bed.preflight()
        preflight.run_wide()
        self.assertEqual(rp.refused(preflight.items), ())
        skipped = {item.item_id for item in preflight.items if item.status == rp.SKIPPED}
        # Attend is off: nothing it needs is checked, and each is named.
        self.assertTrue(
            {"attend:planctl", "attend:plan-ir", "aws:planctl-runner", "gh:auth",
             "gh:repo-permission"} <= skipped
        )
        self.assertFalse(any(call[0] in ("aws", "gh") for call in bed.runner.calls))

    def test_human_secrets_refuse_together_and_nothing_provisionable_is_listed(self) -> None:
        bed = _Bed(self, attend=True)
        (bed.keys / "reviewer-hmac.key").unlink()
        (bed.keys / "route.pub").unlink()
        (bed.profiles / "grok").rmdir()
        bed.runner.failing = {"aws sts get-caller-identity", "gh auth status"}
        preflight = bed.preflight()
        preflight.run_wide()
        refused = rp.refused(preflight.items)
        self.assertEqual(
            sorted(item.item_id for item in refused),
            ["aws:planctl-runner", "gh:auth", "key:reviewer-hmac", "key:route-verify:route.pub",
             "omp-profile:grok"],
        )
        self.assertTrue(all(item.human_secret for item in refused))
        self.assertEqual([i for i in preflight.items if i.status == rp.ADDED], [])
        # Keys are read, never written.
        self.assertFalse((bed.keys / "reviewer-hmac.key").exists())

    def test_each_category_names_its_own_failure(self) -> None:
        bed = _Bed(self, attend=True)
        bed.missing_binaries = {"uv", "claude"}
        bed.runner.failing = {"herdr workspace list"}
        bed.runner.outputs["gh repo view"] = '{"viewerPermission":"READ"}'
        bed.planctl.unlink()
        bed.plan_ir.write_text("not json")
        preflight = bed.preflight()
        preflight.run_wide()
        refused = {item.item_id: item for item in rp.refused(preflight.items)}
        self.assertEqual(
            sorted(refused),
            ["attend:plan-ir", "attend:planctl", "binary:claude", "binary:uv",
             "gh:repo-permission", "herdr:server"],
        )
        self.assertIn("READ", refused["gh:repo-permission"].detail)

    def test_ec2_is_not_checked_when_planctl_review_runs_locally(self) -> None:
        bed = _Bed(self, attend=True)
        bed.runner.failing = {"aws sts get-caller-identity"}
        preflight = bed.preflight(local=True)
        preflight.run_wide()
        self.assertEqual(_by_id(preflight.items)["aws:planctl-runner"].status, rp.SKIPPED)

    def test_a_missing_receipt_is_minted_by_route_admission(self) -> None:
        bed = _Bed(self, attend=False)
        (bed.receipts / "claude.json").unlink()
        (bed.keys / "route.seed").write_text("ef" * 32)

        def admit(specs, destinations, *, route_seed):
            self.assertEqual(route_seed, bytes.fromhex("ef" * 32))
            for path in destinations.values():
                Path(path).write_text("{}")
            return ()

        with mock.patch.object(maestro.admission, "admit_routes", side_effect=admit) as minted:
            preflight = bed.preflight()
            preflight.run_wide()
        item = _by_id(preflight.items)["route-receipt:claude"]
        self.assertEqual(item.status, rp.ADDED)
        self.assertEqual(minted.call_args.args[0][0].route, "claude")
        self.assertIn(
            ("-", "preflight provisioned route-receipt:claude", item.added), bed.console.steps
        )

    def test_a_missing_receipt_without_a_route_seed_refuses(self) -> None:
        bed = _Bed(self, attend=False)
        (bed.receipts / "claude.json").unlink()
        with mock.patch.object(maestro.admission, "admit_routes") as minted:
            preflight = bed.preflight()
            preflight.run_wide()
        minted.assert_not_called()
        item = _by_id(preflight.items)["route-receipt:claude"]
        self.assertEqual(item.status, rp.REFUSED)
        self.assertIn("route.seed", item.detail)
        self.assertFalse((bed.keys / "route.seed").exists())


class PerLaneChecks(unittest.TestCase):
    def test_a_missing_package_is_installed_tree_local_and_recorded(self) -> None:
        bed = _Bed(self, attend=False)
        actor = bed.actor(provision_argv=_venv_provisioner())
        lanes = [_lane("lane-a", imports=("requests.adapters", "os", "json"))]
        installed = []

        def install(dest, packages, timeout_s=None):
            installed.append(tuple(packages))
            bed.runner.importable.update(packages)

        preflight = bed.preflight()
        preflight.run_wide()
        scheduler = bed.scheduler(actor, lanes)
        with mock.patch.object(provisioning, "install_tree_packages", side_effect=install):
            preflight(cast(FactoryScheduler, scheduler))
        item = _by_id(preflight.items, "lane-a")["python:requests"]
        self.assertEqual(item.status, rp.ADDED)
        self.assertEqual(installed, [("requests",)])
        # Every later tree gets it too: the one provisioner applies it.
        self.assertEqual(provisioning.tree_packages(), ("requests",))
        record = bed.store.records[-1]
        self.assertEqual(record["lanes"]["lane-a"]["added"], ["python:requests"])
        self.assertEqual(record["needs_manifest"], ["requests"])
        self.assertEqual(len(bed.issues), 1)
        self.assertIn("requests", bed.issues[0])
        self.assertIn(("lane-a", "preflight", "verified 0, added 1, refused 0"), bed.console.steps)
        self.assertEqual(scheduler.calls["runners"], 1)
        # The throwaway lane tree is gone, and the target repo has no new worktree.
        self.assertEqual(list((bed.state / "worktrees").glob("preflight-*")), [])
        self.assertNotIn("preflight-", gitpub.BoundGit(bed.product).text("worktree", "list"))

    def test_a_declared_output_is_never_a_need(self) -> None:
        bed = _Bed(self, attend=False)
        actor = bed.actor(provision_argv=_venv_provisioner())
        lanes = [
            _lane(
                "lane-b",
                imports=("widgets.render", "tests.helpers"),
                outputs=("src/widgets/__init__.py", "src/widgets/render.py"),
                suite=("tests/helpers.py", "tests/test_render.py"),
                fixtures=("src/widgets/render.py",),
            )
        ]
        preflight = bed.preflight()
        preflight.run_wide()
        with mock.patch.object(provisioning, "install_tree_packages") as install:
            preflight(cast(FactoryScheduler, bed.scheduler(actor, lanes)))
        install.assert_not_called()
        lane_items = _by_id(preflight.items, "lane-b")
        self.assertEqual(lane_items, {})
        self.assertEqual(rp.refused(preflight.items), ())

    def test_a_declared_fixture_that_is_absent_refuses_naming_the_lane(self) -> None:
        bed = _Bed(self, attend=False)
        actor = bed.actor(provision_argv=_venv_provisioner())
        lanes = [_lane("lane-c", fixtures=("data/baseline.json", "seed.txt"))]
        preflight = bed.preflight()
        preflight.run_wide()
        with self.assertRaises(RunPreflightRefused) as caught:
            preflight(cast(FactoryScheduler, bed.scheduler(actor, lanes)))
        refused = [(item.lane_id, item.item_id) for item in caught.exception.items]
        self.assertEqual(refused, [("lane-c", "fixture:data/baseline.json")])
        self.assertEqual(_by_id(preflight.items, "lane-c")["fixture:seed.txt"].status, rp.VERIFIED)

    def test_a_lane_tree_whose_provisioning_fails_refuses_and_skips_its_needs(self) -> None:
        bed = _Bed(self, attend=False)
        actor = bed.actor(provision_argv=(sys.executable, "-c", "raise SystemExit(2)"))
        lanes = [_lane("lane-d", imports=("requests",))]
        preflight = bed.preflight()
        preflight.run_wide()
        with self.assertRaises(RunPreflightRefused) as caught:
            preflight(cast(FactoryScheduler, bed.scheduler(actor, lanes)))
        self.assertEqual([item.item_id for item in caught.exception.items], ["lane-tree"])
        self.assertIn("PROVISION_FAILED", caught.exception.items[0].detail)
        self.assertEqual(_by_id(preflight.items, "lane-d")["python:requests"].status, rp.SKIPPED)

    def test_mixed_provisionable_items_are_fixed_and_only_secrets_refuse(self) -> None:
        bed = _Bed(self, attend=True)
        (bed.keys / "reviewer-hmac.key").unlink()
        bed.runner.failing = {"gh auth status"}
        (bed.receipts / "omp.json").unlink()
        (bed.keys / "route.seed").write_text("ef" * 32)

        def admit(specs, destinations, *, route_seed):
            for path in destinations.values():
                Path(path).write_text("{}")
            return ()

        def install(dest, packages, timeout_s=None):
            bed.runner.importable.update(packages)

        actor = bed.actor(provision_argv=_venv_provisioner())
        lanes = [_lane("lane-e", imports=("yaml",))]
        with mock.patch.object(maestro.admission, "admit_routes", side_effect=admit), \
                mock.patch.object(provisioning, "install_tree_packages", side_effect=install):
            preflight = bed.preflight()
            preflight.run_wide()
            with self.assertRaises(RunPreflightRefused) as caught:
                preflight(cast(FactoryScheduler, bed.scheduler(actor, lanes)))
        self.assertEqual(
            sorted(item.item_id for item in caught.exception.items),
            ["gh:auth", "key:reviewer-hmac"],
        )
        added = sorted(item.item_id for item in preflight.items if item.status == rp.ADDED)
        self.assertEqual(added, ["python:yaml", "route-receipt:omp"])
        report = caught.exception.report
        self.assertIn("refused (2)", report)
        self.assertIn("provisioned (2)", report)


class AttendTrees(unittest.TestCase):
    """The attend operator's and reviewer's trees are not checkouts.

    Driven through the real `attend_operator` / `attend_reviewer` dispatch,
    stopped by `_PREPARE_ONLY` before a pane exists. With a `provision_argv`
    that fails outside a project -- `uv sync` in a tree with no
    `pyproject.toml` -- a launch path that provisions an attend tree refuses
    here, before any lane runs, instead of mid-run.
    """

    _NEEDS_PROJECT = (
        sys.executable,
        "-c",
        "import pathlib,sys;"
        "sys.exit(0 if pathlib.Path('pyproject.toml').exists() else 2)",
    )

    def test_the_attend_trees_are_prepared_without_being_provisioned(self) -> None:
        bed = _Bed(self, attend=True)
        actor = bed.actor(provision_argv=self._NEEDS_PROJECT)
        preflight = bed.preflight()
        preflight.run_wide()
        with mock.patch.object(provisioning, "provision_tree", wraps=provisioning.provision_tree) as spy:
            preflight(cast(FactoryScheduler, bed.scheduler(actor, [])))
        items = _by_id(preflight.items)
        self.assertEqual(items["attend-tree:operator"].status, rp.VERIFIED, items["attend-tree:operator"].detail)
        self.assertEqual(items["attend-tree:reviewer"].status, rp.VERIFIED, items["attend-tree:reviewer"].detail)
        spy.assert_not_called()
        # No pane: the launcher was never reached.
        self.assertEqual(bed.launcher.specs, [])
        self.assertEqual(list((bed.state / "worktrees" / RUN_ID).glob("preflight-*")), [])
        # The attend trees are worktrees; removing them leaves no entry behind.
        import subprocess

        listed = subprocess.run(
            ["git", "-C", str(bed.product), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("preflight-", listed)

    def test_attend_tree_checks_are_skipped_when_attend_is_disabled(self) -> None:
        bed = _Bed(self, attend=False)
        actor = bed.actor()
        preflight = bed.preflight()
        preflight.run_wide()
        preflight(cast(FactoryScheduler, bed.scheduler(actor, [])))
        items = _by_id(preflight.items)
        self.assertEqual(items["attend-tree:operator"].status, rp.SKIPPED)
        self.assertEqual(items["attend-tree:reviewer"].status, rp.SKIPPED)


class RefusedBeforeDispatch(unittest.TestCase):
    def test_the_scheduler_runs_the_preflight_before_anything_else(self) -> None:
        scheduler = FactoryScheduler.__new__(FactoryScheduler)
        scheduler._pool = None
        refusal = RunPreflightRefused((), "refused (0):")
        scheduler._preflight = mock.Mock(side_effect=refusal)
        scheduler._assert_runners_usable = mock.Mock()
        scheduler._recover_legacy_retarget_journal = mock.Mock()
        scheduler._advance_ready = mock.Mock()
        with self.assertRaises(RunPreflightRefused):
            scheduler.run()
        scheduler._preflight.assert_called_once_with(scheduler)
        scheduler._assert_runners_usable.assert_not_called()
        scheduler._recover_legacy_retarget_journal.assert_not_called()
        scheduler._advance_ready.assert_not_called()

    def test_without_a_preflight_the_runner_check_still_runs_first(self) -> None:
        scheduler = FactoryScheduler.__new__(FactoryScheduler)
        scheduler._pool = None
        scheduler._preflight = None
        scheduler._assert_runners_usable = mock.Mock(side_effect=RuntimeError("stop"))
        with self.assertRaises(RuntimeError):
            scheduler.run()
        scheduler._assert_runners_usable.assert_called_once_with()

    def _refusing_preflight(self):
        refusal = RunPreflightRefused(
            (rp.PreflightItem("key:reviewer-hmac", rp.REFUSED, "gone", human_secret=True),),
            "refused (1):",
        )
        fake = mock.Mock()
        fake.return_value.run_wide.side_effect = refusal
        return fake

    def test_run_resume_refuses_before_any_scheduler_exists(self) -> None:
        runtime = mock.Mock()
        store = mock.Mock()
        store.active_projection.return_value = ()
        bound = ({"attend": {}}, runtime, store, {"target_repository_root": "/r", "target_main_ref": "m"},
                 mock.Mock(), mock.Mock())
        with mock.patch.object(maestro, "_bind_existing_run", return_value=bound), \
                mock.patch.object(maestro, "maybe_autoload_dashboard"), \
                mock.patch.object(maestro.step_log, "RunReporter"), \
                mock.patch.object(maestro, "_RunPreflight", self._refusing_preflight()), \
                mock.patch.object(maestro, "FactoryScheduler") as scheduler, \
                mock.patch.object(maestro, "_actor_for") as actor_for:
            with self.assertRaises(RunPreflightRefused):
                maestro._run_resume(SimpleNamespace(run_id=RUN_ID))
        scheduler.assert_not_called()
        actor_for.assert_not_called()

    def test_run_start_refuses_before_any_scheduler_exists(self) -> None:
        compiled = SimpleNamespace(lanes=())
        target = mock.Mock()
        with mock.patch.multiple(
            maestro,
            _executing_maestro_file=mock.Mock(return_value=Path("/x/adws/maestro.py")),
            _load_deployment_config=mock.Mock(return_value={"attend": {}}),
            require_deployment=mock.Mock(),
            _open_runtime=mock.Mock(return_value=SimpleNamespace(
                path=Path("/state"), ensure_layout=lambda: None, close=lambda: None,
                ledger_path=lambda: Path("/state/l"),
            )),
            _compile_plan=mock.Mock(return_value=compiled),
            _require_approved_plan=mock.Mock(),
            _open_store=mock.Mock(),
            create_factory_run=mock.Mock(),
            target_from_binding=mock.Mock(return_value=target),
            _pin_plan_artifact=mock.Mock(),
            register_installation=mock.Mock(),
            maybe_autoload_dashboard=mock.Mock(),
            _RunPreflight=self._refusing_preflight(),
            FactoryScheduler=mock.Mock(),
            _actor_for=mock.Mock(),
        ), mock.patch.object(maestro.gitpub, "bind_target_worktree"), \
                mock.patch.object(maestro.step_log, "RunReporter"):
            with self.assertRaises(RunPreflightRefused):
                maestro._run_start(
                    SimpleNamespace(repo="/r", main_ref="refs/heads/main", plan="/p", run_id=RUN_ID)
                )
            maestro.FactoryScheduler.assert_not_called()
            maestro._actor_for.assert_not_called()

    def test_the_cli_prints_every_refused_item(self) -> None:
        refusal = RunPreflightRefused(
            (
                rp.PreflightItem("key:reviewer-hmac", rp.REFUSED, "gone", human_secret=True),
                rp.PreflightItem("gh:auth", rp.REFUSED, "logged out", human_secret=True),
            ),
            "refused (2):",
        )
        with mock.patch.object(maestro, "_run_resume", side_effect=refusal), \
                mock.patch("sys.stdout") as out:
            code = maestro.main(["run", "resume", RUN_ID])
        self.assertEqual(code, 3)
        printed = json.loads("".join(call.args[0] for call in out.write.call_args_list))
        self.assertEqual(printed["outcome"], "RUN_PREFLIGHT_REFUSED")
        self.assertEqual([item["item_id"] for item in printed["refused"]], ["key:reviewer-hmac", "gh:auth"])


class TheLedgerRecord(unittest.TestCase):
    def test_a_preflight_is_recorded_as_a_run_artifact(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        store = ArtifactStore(Path(holder.name) / "lifecycle.sqlite3")
        self.addCleanup(store.close)
        store.create_run(RUN_ID, make_plan(make_lane("A")), make_binding())
        store.record_run_preflight(RUN_ID, preflight_id="p-1", payload={"items": []})
        store.record_run_preflight(RUN_ID, preflight_id="p-2", payload={"items": []})
        rows = store.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.RUN_PREFLIGHT)
        self.assertEqual([row["payload"]["preflight_id"] for row in rows], ["p-1", "p-2"])

    def test_a_v7_ledger_is_widened_for_the_record_and_keeps_its_rows(self) -> None:
        import sqlite3

        from adw_modules import lifecycle

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        path = Path(holder.name) / "lifecycle.sqlite3"
        store = ArtifactStore(path)
        store.create_run(RUN_ID, make_plan(make_lane("A")), make_binding())
        store.record_attend_session(RUN_ID, session_id="s-0", phase=st.ATTEND_PHASE_START, payload={})
        store.close()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        cols = ", ".join(lifecycle.RUN_ARTIFACT_COLUMNS)
        conn.execute("ALTER TABLE run_artifacts RENAME TO ra_old")
        conn.execute(lifecycle.V7_RUN_ARTIFACTS_SQL)
        conn.execute(f"INSERT INTO run_artifacts ({cols}) SELECT {cols} FROM ra_old")
        conn.execute("DROP TABLE ra_old")
        conn.execute("UPDATE ledger_meta SET schema_version=?", (st.LEDGER_SCHEMA_VERSION_V7,))
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run_artifacts VALUES "
                "('x', ?, 99, 'RUN_PREFLIGHT', 1, 'd', 'o', 'r', '{}', 'now')",
                (RUN_ID,),
            )
        conn.close()
        migrated = ArtifactStore(path)
        self.addCleanup(migrated.close)
        version = migrated.conn.execute("SELECT schema_version FROM ledger_meta").fetchone()[0]
        self.assertEqual(version, st.LEDGER_SCHEMA_VERSION)
        self.assertEqual(len(migrated.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.ATTEND_SESSION)), 1)
        migrated.record_run_preflight(RUN_ID, preflight_id="p-1", payload={})
        self.assertEqual(len(migrated.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.RUN_PREFLIGHT)), 1)


class TreeLocalPackages(unittest.TestCase):
    def test_provision_tree_installs_the_recorded_packages_after_its_argv(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tree = Path(holder.name)
        provisioning.set_tree_packages(("requests",))
        self.addCleanup(provisioning.set_tree_packages, ())
        seen = []

        def harness(argv, *, cwd, env=None, timeout=None):
            seen.append(tuple(argv))
            if argv[0] == sys.executable:
                (tree / ".venv" / "bin").mkdir(parents=True)
                (tree / ".venv" / "bin" / "python").write_text("")
            return SimpleNamespace(returncode=0, stderr="")

        with mock.patch.object(provisioning, "run_harness_process", side_effect=harness):
            provisioning.provision_tree(tree, (sys.executable, "-c", "pass"))
        self.assertEqual(seen[1][:3], ("uv", "pip", "install"))
        self.assertEqual(seen[1][-1], "requests")

    def test_no_venv_to_install_into_refuses_typed(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        with self.assertRaises(provisioning.ReviewProvisioningError):
            provisioning.install_tree_packages(Path(holder.name), ("requests",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
