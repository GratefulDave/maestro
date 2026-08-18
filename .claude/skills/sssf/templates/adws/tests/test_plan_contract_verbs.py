"""`plan gate`, `plan review`, and `plan ship`: cmd + verb + name, nothing typed.

Every path, identity, and key the plan-contract pipeline needs is derived from
the plan name and the repository configuration. These tests hold that line: the
derivations, planctl's resolution order, the author/reviewer key separation, the
stop-at-first-failure contract, the reviewer key Maestro mints and owns, and the
visible Herdr pane every verb refuses to work without.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import receipt_crypto
from adw_modules import route_admission


FAKE_PLANCTL = '''#!/usr/bin/env python3
"""A planctl stand-in that records how Maestro drove it."""
import hashlib
import json
import os
import pathlib
import sys

control = json.loads(pathlib.Path(os.environ["PLANCTL_FAKE_CONTROL"]).read_text())
log = pathlib.Path(os.environ["PLANCTL_FAKE_LOG"])
verb = sys.argv[1] if len(sys.argv) > 1 else ""
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "verb": verb,
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "key": os.environ.get("PLANCTL_REVIEWER_HMAC_KEY"),
    }) + "\\n")

if "--help" in sys.argv:
    sys.stdout.write("usage: planctl.py " + verb + " [-h] [--rendered RENDERED]\\n")
    if control.get("repo_root", True):
        sys.stdout.write("  --repo-root REPO_ROOT\\n")
    raise SystemExit(0)

if verb == "render" and "--out" in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index("--out") + 1]).write_text(
        "<html>rendered</html>", encoding="utf-8")
if verb == "review" and "--receipt-out" in sys.argv:
    # A plan-contract-review.v1 receipt, bound to the bytes it was shown.
    # `{"approved": true}` was accepted here for as long as nothing read the
    # file; `plan ship` reads it now, and a receipt that cannot verify is not
    # a review a downstream verb may act on.
    payload = {"schema_version": "plan-contract-review.v1", "verdict": "PASS"}
    ir = next((item for item in sys.argv if item.endswith(".plan.json")), None)
    if ir:
        payload["ir_sha256"] = hashlib.sha256(
            pathlib.Path(ir).read_bytes()).hexdigest()
    if "--rendered" in sys.argv:
        payload["rendered_sha256"] = hashlib.sha256(
            pathlib.Path(
                sys.argv[sys.argv.index("--rendered") + 1]).read_bytes()
        ).hexdigest()
    pathlib.Path(sys.argv[sys.argv.index("--receipt-out") + 1]).write_text(
        json.dumps(payload), encoding="utf-8")

status = int(control.get("fail", {}).get(verb, 0))
if status:
    sys.stdout.write(json.dumps({"valid": False, "diagnostics": ["boom in " + verb]}) + "\\n")
    sys.stderr.write("planctl " + verb + " failed with key "
                     + str(os.environ.get("PLANCTL_REVIEWER_HMAC_KEY")) + "\\n")
raise SystemExit(status)
'''


FAKE_HERDR = '''#!/usr/bin/env python3
"""A Herdr stand-in that records every pane call Maestro made."""
import json
import os
import pathlib
import sys

log = pathlib.Path(os.environ["HERDR_FAKE_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "environment": {key: value for key, value in os.environ.items()
                        if "HMAC" in key or "PLANCTL" in key},
    }) + "\\n")

if os.environ.get("HERDR_FAKE_REFUSE"):
    sys.stderr.write("herdr: no running session\\n")
    raise SystemExit(1)
if sys.argv[1:3] == ["pane", "split"]:
    sys.stdout.write(json.dumps({"result": {"pane": {"pane_id": "pane-7"}}}) + "\\n")
else:
    sys.stdout.write(json.dumps({"result": {}}) + "\\n")
raise SystemExit(0)
'''


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if result.returncode != 0:
        raise AssertionError("git {0} -> {1}: {2}".format(
            " ".join(args), result.returncode, result.stderr))
    return result.stdout.strip()


class PlanContractVerbFixture(unittest.TestCase):
    """A repository whose plan pipeline is fully configured and fully derived."""

    maxDiff = None

    @contextlib.contextmanager
    def _repository_cwd(self, repo):
        previous = Path.cwd()
        os.chdir(repo)
        try:
            yield
        finally:
            os.chdir(previous)

    def _fixture(self, root, *, plans_dir="specs", reviewer_identity=True,
                 plan_contract=None, control=None):
        repo = root / "repo"
        (repo / "adws").mkdir(parents=True)
        (repo / plans_dir).mkdir(parents=True, exist_ok=True)
        # A real repository, not a bare `.git` directory. `plan ship` projects
        # the canonical plan before it authors anything, and that projection
        # pins every declared source artifact against `git hash-object`, so a
        # fixture with no objects can only ever exercise the refusal arm.
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "harness@example.invalid")
        _git(repo, "config", "user.name", "Harness")
        _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))

        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = root / name
            binary.write_text(
                FAKE_HERDR if name == "herdr" else "#!/bin/sh\nexit 0\n",
                encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        herdr_log = root / "herdr-log.jsonl"

        state_root = "../maestro-state"
        state = (repo / state_root).resolve() / repo.name
        route_dir = state / "route-receipts"
        route_dir.mkdir(parents=True)
        for route in ("omp", "claude"):
            (route_dir / (route + ".json")).write_text("{}", encoding="utf-8")

        signing_seed = receipt_crypto.generate_seed()
        route_seed = receipt_crypto.generate_seed()
        environment = {
            "MAESTRO_TEST_VERIFY_KEY": receipt_crypto.seed_to_public_key(
                signing_seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": signing_seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY": receipt_crypto.seed_to_public_key(
                route_seed).hex(),
        }

        planctl = root / "planctl.py"
        planctl.write_text(FAKE_PLANCTL, encoding="utf-8")
        control_file = root / "planctl-control.json"
        control_file.write_text(json.dumps(control or {}), encoding="utf-8")
        log_file = root / "planctl-log.jsonl"
        environment["PLANCTL_FAKE_CONTROL"] = str(control_file)
        environment["PLANCTL_FAKE_LOG"] = str(log_file)
        environment["PLAN_CONTRACT_SKILL_PATH"] = str(planctl)
        environment["HERDR_FAKE_LOG"] = str(herdr_log)

        reviewer = {
            "route": "claude",
            "model": "review-model",
            "effort": "high",
            "finalization_timeout_s": 60,
            "turn_timeout_s": 20,
            "poll_interval_s": 1,
        }
        if reviewer_identity:
            reviewer["id"] = "maestro-independent-reviewer"
            reviewer["vendor"] = "anthropic"
        config = {
            "schema": "maestro-config.v1",
            "plans_dir": plans_dir,
            "state_root": state_root,
            "keys": {
                "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
            },
            "executables": binaries,
            "route_receipts": {
                "omp": "route-receipts/omp.json",
                "claude": "route-receipts/claude.json",
            },
            "reviewer": reviewer,
            "execution": {
                "route": "omp",
                "model": "execution-model",
                "effort": "medium",
                "concurrency": 2,
                "node_timeout_s": 120,
                "turn_timeout_s": 30,
                "final_acceptance_timeout_s": 45,
                "backstop_t_s": 600,
                "semantic_ceiling": 3,
            },
        }
        if plan_contract is not None:
            config["plan_contract"] = plan_contract
        (repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8")

        return {
            "control": control_file,
            "environment": environment,
            "herdr_log": herdr_log,
            "log": log_file,
            "planctl": planctl,
            "plans_dir": repo / plans_dir,
            "repo": repo,
            "root": root,
            "state": state,
        }

    def _approved(self, fixture, name="demo"):
        """An IR that really projects, and a receipt that really verifies.

        `{"approved": true}` beside a one-key IR served for as long as nothing
        read either file. `plan ship` now projects the canonical plan before it
        authors anything -- that is what makes a half-finished ship resumable
        -- so the placeholder pair stopped standing for an approved plan and
        started standing for `RECEIPT_SCHEMA`. The receipt is built from the
        digests of the bytes actually written, never pasted, because a pasted
        digest is the same hole one edit later.
        """
        repo = fixture["repo"]
        readme = "# fixture repository\n"
        (repo / "README.md").write_text(readme, encoding="utf-8")
        tests_dir = repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_existing.py").write_text(
            "def test_one():\n    assert True\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "fixture base")

        ir_bytes = json.dumps({
            "schema_version": "plan-contract.v1",
            "plan_id": name,
            "title": "Ship " + name,
            "plan_kind": "brownfield",
            "source_artifacts": [{
                "source_id": "src-readme",
                "path": "README.md",
                "sha256": hashlib.sha256(readme.encode("utf-8")).hexdigest(),
                "required": True,
            }],
            "lanes": [{
                "lane_id": "lane-ship",
                "title": "Ship it",
                "execution_context": ".",
                "depends_on": [],
                "verifier_ids": ["verify-ship"],
            }],
            "verifiers": [{
                "verifier_id": "verify-ship",
                "lane_ids": ["lane-ship"],
                "source_ids": ["src-readme"],
                "command": "python3 -m pytest tests/test_existing.py",
                "min_executed": 1,
            }],
            "extensions": {
                "maestro": {
                    "repo": "example",
                    "outputs": {"lane-ship": ["src/shipped.py"]},
                    "integration_branch": "main",
                    "integration_gate": {
                        "runner": "pytest",
                        "argv": ["tests"],
                        "cwd": ".",
                        "min_cases": 1,
                    },
                },
            },
        }).encode("utf-8")
        (fixture["plans_dir"] / (name + ".plan.json")).write_bytes(ir_bytes)
        rendered_bytes = b"<html/>"
        (fixture["plans_dir"] / (name + ".html")).write_bytes(rendered_bytes)
        (fixture["plans_dir"] / (name + ".plan-review.json")).write_text(
            json.dumps({
                "schema_version": "plan-contract-review.v1",
                "verdict": "PASS",
                "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
                "rendered_sha256": hashlib.sha256(rendered_bytes).hexdigest(),
            }), encoding="utf-8")

    def _authors(self):
        """What a real `plan author` leaves behind, since it is mocked here.

        `plan validate` and `plan finalize` bind to the named plan on disk, so
        an author step that returns 0 without writing one leaves the next step
        refusing `MAESTRO_CONFIGURATION_INVALID`. Returning the status alone
        modelled half of what the step does.
        """
        def author(args):
            destination = Path(args.plan_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'{"plan":"stored"}\n')
            return 0
        return author

    def _write_plan_ir(self, fixture, name="demo"):
        path = fixture["plans_dir"] / (name + ".plan.json")
        path.write_text('{"plan_id": "' + name + '"}', encoding="utf-8")
        return path

    def _invocations(self, fixture):
        if fixture["log"].is_file():
            return self._records(fixture["log"])
        return fixture.get("_planctl_calls", [])

    def _pane_calls(self, fixture):
        if fixture["herdr_log"].is_file():
            return self._records(fixture["herdr_log"])
        return fixture.get("_pane_calls", [])

    def _records(self, path):
        if not path.is_file():
            return []
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _run(self, fixture, argv, *, environment=None):
        merged = dict(fixture["environment"])
        merged.update(environment or {})
        removed = [name for name, value in merged.items() if value is None]
        for name in removed:
            merged.pop(name)
        stream = io.StringIO()
        with mock.patch.dict(os.environ, merged, clear=False), \
                self._repository_cwd(fixture["repo"]), \
                contextlib.redirect_stdout(stream):
            for name in removed:
                os.environ.pop(name, None)
            status = maestro.main(argv)
        # Snapshot both logs now: a caller may assert after the fixture's
        # temporary directory is gone.
        fixture["_planctl_calls"] = self._records(fixture["log"])
        fixture["_pane_calls"] = self._records(fixture["herdr_log"])
        payloads = [json.loads(line) for line in stream.getvalue().splitlines()
                    if line.strip()]
        return status, payloads, stream.getvalue()


class PlanContractDerivationTest(PlanContractVerbFixture):
    def test_every_artifact_path_derives_from_the_plan_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with self._repository_cwd(fixture["repo"]):
                layout = maestro._plan_contract_layout()
                artifacts = maestro._plan_contract_artifacts(layout, "demo")
            plans = fixture["plans_dir"].resolve()
            self.assertEqual(artifacts["plan_ir"], plans / "demo.plan.json")
            self.assertEqual(artifacts["rendered"], plans / "demo.html")
            self.assertEqual(artifacts["receipt"], plans / "demo.plan-review.json")

    def test_a_plan_name_may_never_escape_the_plans_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with self._repository_cwd(fixture["repo"]):
                layout = maestro._plan_contract_layout()
                for hostile in ("../escape", "nested/plan", ".."):
                    with self.assertRaises(maestro._MaestroConfigurationError):
                        maestro._plan_contract_artifacts(layout, hostile)

    def test_the_verbs_take_the_plan_name_as_their_only_positional(self):
        parser = maestro.build_parser()
        verbs = maestro.parser_verbs(parser)
        for verb in ("plan gate", "plan review", "plan ship"):
            self.assertIn(verb, verbs)
        for verb in ("gate", "review", "ship"):
            args = parser.parse_args(["plan", verb, "demo"])
            self.assertEqual(args.plan_name, "demo")
            positionals = [action.dest
                           for action in self._verb_parser(parser, verb)._actions
                           if not action.option_strings]
            self.assertEqual(positionals, ["plan_name"])

    def _verb_parser(self, parser, verb):
        def subparsers(current):
            for action in current._actions:
                if isinstance(action, __import__("argparse")._SubParsersAction):
                    return action
            raise AssertionError("no subparsers on " + str(current.prog))

        return subparsers(subparsers(parser).choices["plan"]).choices[verb]


class PlanctlResolutionTest(PlanContractVerbFixture):
    def test_configuration_outranks_the_repository_skill_and_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "configured-planctl.py"
            configured.write_text(FAKE_PLANCTL, encoding="utf-8")
            fixture = self._fixture(root, plan_contract=str(configured))
            repository_skill = (fixture["repo"]
                                / maestro._PLAN_CONTRACT_REPOSITORY_SKILL)
            repository_skill.parent.mkdir(parents=True)
            repository_skill.write_text(FAKE_PLANCTL, encoding="utf-8")
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(os.environ, fixture["environment"]):
                layout = maestro._plan_contract_layout()
                self.assertEqual(maestro._resolve_planctl(layout),
                                 configured.resolve())

    def test_the_repository_skill_outranks_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            repository_skill = (fixture["repo"]
                                / maestro._PLAN_CONTRACT_REPOSITORY_SKILL)
            repository_skill.parent.mkdir(parents=True)
            repository_skill.write_text(FAKE_PLANCTL, encoding="utf-8")
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(os.environ, fixture["environment"]):
                layout = maestro._plan_contract_layout()
                self.assertEqual(maestro._resolve_planctl(layout),
                                 repository_skill.resolve())

    def test_the_environment_skill_directory_is_the_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture(root)
            skill = root / "skill"
            script = skill / "scripts" / "planctl.py"
            script.parent.mkdir(parents=True)
            script.write_text(FAKE_PLANCTL, encoding="utf-8")
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(
                        os.environ,
                        {"PLAN_CONTRACT_SKILL_PATH": str(skill)}):
                layout = maestro._plan_contract_layout()
                self.assertEqual(maestro._resolve_planctl(layout),
                                 script.resolve())

    def test_an_absent_planctl_names_every_way_to_supply_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(os.environ,
                                    {"PLAN_CONTRACT_SKILL_PATH": ""}):
                layout = maestro._plan_contract_layout()
                with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                    maestro._resolve_planctl(layout)
        detail = str(caught.exception)
        self.assertIn("plan_contract", detail)
        self.assertIn("PLAN_CONTRACT_SKILL_PATH", detail)
        self.assertIn(".claude/skills/plan-contract", detail)

    def test_repo_root_is_passed_only_when_the_installed_planctl_takes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"repo_root": True})
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(
                fixture, ["plan", "gate", "demo"])
        self.assertEqual(status, 0, payloads)
        render = [row for row in self._invocations(fixture)
                  if row["verb"] == "render" and "--help" not in row["argv"]]
        self.assertEqual(len(render), 1)
        self.assertIn("--repo-root", render[0]["argv"])
        self.assertEqual(
            render[0]["argv"][render[0]["argv"].index("--repo-root") + 1],
            str(fixture["repo"].resolve()))

    def test_a_planctl_without_repo_root_refuses_a_plan_below_plans_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"repo_root": False})
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(
                fixture, ["plan", "gate", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "MAESTRO_CONFIGURATION_INVALID")
        self.assertIn("--repo-root", payloads[-1]["detail"])
        self.assertIn("plans_dir", payloads[-1]["detail"])


class PlanGateTest(PlanContractVerbFixture):
    def test_gate_runs_render_then_validate_then_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "gate", "demo"])
        self.assertEqual(status, 0, payloads)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_GATED")
        self.assertEqual(payloads[-1]["steps"], ["render", "validate", "mutate"])
        verbs = [row["verb"] for row in self._invocations(fixture)
                 if "--help" not in row["argv"]]
        self.assertEqual(verbs, ["render", "validate", "mutate"])

    def test_gate_refuses_while_the_reviewer_key_is_in_its_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, payloads, raw = self._run(
                fixture, ["plan", "gate", "demo"],
                environment={"PLANCTL_REVIEWER_HMAC_KEY": "k" * 64})
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "REVIEWER_KEY_PRESENT")
        self.assertIn("PLANCTL_REVIEWER_HMAC_KEY", payloads[-1]["detail"])
        self.assertNotIn("k" * 64, raw)
        self.assertEqual(self._invocations(fixture), [])

    def test_gate_never_hands_planctl_a_reviewer_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, _payloads, _raw = self._run(fixture, ["plan", "gate", "demo"])
            self.assertEqual(status, 0)
            self.assertTrue(
                all(not row["key"] for row in self._invocations(fixture)))

    def test_gate_stops_at_the_first_failing_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"fail": {"render": 3}})
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "gate", "demo"])
        self.assertEqual(status, 2)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_GATE_FAILED")
        self.assertEqual(payloads[-1]["step"], "render")
        self.assertEqual(payloads[-1]["status"], 3)
        self.assertIn("boom in render", payloads[-1]["stdout"])
        verbs = [row["verb"] for row in self._invocations(fixture)
                 if "--help" not in row["argv"]]
        self.assertEqual(verbs, ["render"])

    def test_gate_stops_before_mutate_when_validate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"fail": {"validate": 2}})
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "gate", "demo"])
        self.assertEqual(status, 2)
        self.assertEqual(payloads[-1]["step"], "validate")
        verbs = [row["verb"] for row in self._invocations(fixture)
                 if "--help" not in row["argv"]]
        self.assertEqual(verbs, ["render", "validate"])

    def test_gate_refuses_a_plan_name_with_no_plan_ir(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            status, payloads, _raw = self._run(fixture, ["plan", "gate", "absent"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_CONTRACT_IR_MISSING")
        self.assertIn("absent.plan.json", payloads[-1]["detail"])


class PlanReviewTest(PlanContractVerbFixture):
    def test_review_signs_then_revalidates_against_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            gate_status, _payloads, _raw = self._run(
                fixture, ["plan", "gate", "demo"])
            self.assertEqual(gate_status, 0)
            status, payloads, _raw = self._run(fixture, ["plan", "review", "demo"])
        self.assertEqual(status, 0, payloads)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_REVIEWED")
        self.assertEqual(payloads[-1]["reviewer"], "maestro-independent-reviewer")
        self.assertEqual(payloads[-1]["reviewer_vendor"], "anthropic")
        rows = [row for row in self._invocations(fixture)
                if "--help" not in row["argv"]]
        self.assertEqual([row["verb"] for row in rows],
                         ["render", "validate", "mutate", "review", "validate"])
        review = rows[3]
        self.assertIn("--require-approved", rows[4]["argv"])
        self.assertEqual(
            review["argv"][review["argv"].index("--reviewer") + 1],
            "maestro-independent-reviewer")

    def test_gate_and_review_run_from_one_shell_without_any_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            shell = {"PLANCTL_REVIEWER_HMAC_KEY": None}
            gate_status, _gate, _raw = self._run(
                fixture, ["plan", "gate", "demo"], environment=shell)
            review_status, _review, _raw2 = self._run(
                fixture, ["plan", "review", "demo"], environment=shell)
        self.assertEqual((gate_status, review_status), (0, 0))
        rows = [row for row in self._invocations(fixture)
                if "--help" not in row["argv"]]
        gate_rows = rows[:3]
        review_rows = rows[3:]
        self.assertTrue(all(not row["key"] for row in gate_rows))
        self.assertTrue(all(row["key"] for row in review_rows))

    def test_review_stops_at_the_first_failing_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"fail": {"review": 4}})
            self._write_plan_ir(fixture)
            (fixture["plans_dir"] / "demo.html").write_text("<html/>",
                                                            encoding="utf-8")
            status, payloads, _raw = self._run(fixture, ["plan", "review", "demo"])
        self.assertEqual(status, 2)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_REVIEW_FAILED")
        self.assertEqual(payloads[-1]["step"], "review")
        verbs = [row["verb"] for row in self._invocations(fixture)
                 if "--help" not in row["argv"]]
        self.assertEqual(verbs, ["review"])

    def test_review_refuses_before_the_plan_has_been_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "review", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_CONTRACT_RENDER_MISSING")
        self.assertIn("plan gate demo", payloads[-1]["detail"])

    def test_review_refuses_without_a_configured_reviewer_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), reviewer_identity=False)
            self._write_plan_ir(fixture)
            (fixture["plans_dir"] / "demo.html").write_text("<html/>",
                                                            encoding="utf-8")
            status, payloads, _raw = self._run(fixture, ["plan", "review", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "REVIEWER_IDENTITY_UNCONFIGURED")


class ReviewerKeyCustodyTest(PlanContractVerbFixture):
    def _layout(self, fixture):
        with self._repository_cwd(fixture["repo"]), \
                mock.patch.dict(os.environ, fixture["environment"]):
            return maestro._plan_contract_layout()

    def test_the_key_is_minted_once_and_reused_afterwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            with mock.patch.dict(os.environ,
                                 {"PLANCTL_REVIEWER_HMAC_KEY": ""}, clear=False):
                os.environ.pop("PLANCTL_REVIEWER_HMAC_KEY", None)
                first = maestro._reviewer_hmac_key(layout)
                second = maestro._reviewer_hmac_key(layout)
            stored = maestro._reviewer_hmac_key_file(layout).read_text(
                encoding="ascii").strip()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first.encode("utf-8")), 32)
        self.assertEqual(stored, first)

    def test_the_key_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            os.environ.pop("PLANCTL_REVIEWER_HMAC_KEY", None)
            maestro._reviewer_hmac_key(layout)
            mode = stat.S_IMODE(
                maestro._reviewer_hmac_key_file(layout).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_an_existing_key_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            path = maestro._reviewer_hmac_key_file(layout)
            path.parent.mkdir(parents=True, exist_ok=True)
            planted = "p" * 64
            path.write_text(planted + "\n", encoding="ascii")
            os.environ.pop("PLANCTL_REVIEWER_HMAC_KEY", None)
            resolved = maestro._reviewer_hmac_key(layout)
            self.assertEqual(resolved, planted)
            self.assertEqual(path.read_text(encoding="ascii").strip(), planted)

    def test_a_too_short_key_file_is_refused_not_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            path = maestro._reviewer_hmac_key_file(layout)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("too-short\n", encoding="ascii")
            os.environ.pop("PLANCTL_REVIEWER_HMAC_KEY", None)
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                maestro._reviewer_hmac_key(layout)
            self.assertEqual(path.read_text(encoding="ascii").strip(), "too-short")
        self.assertIn("invalidate", str(caught.exception))

    def test_an_operator_supplied_key_wins_over_the_minted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            supplied = "s" * 64
            with mock.patch.dict(
                    os.environ, {"PLANCTL_REVIEWER_HMAC_KEY": supplied}):
                self.assertEqual(maestro._reviewer_hmac_key(layout), supplied)
            self.assertFalse(maestro._reviewer_hmac_key_file(layout).exists())

    def test_the_key_is_stored_outside_every_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            layout = self._layout(fixture)
            path = maestro._reviewer_hmac_key_file(layout)
        self.assertFalse(maestro._path_is_within(path, fixture["repo"].resolve()))
        for ancestor in (path.parent, *path.parent.parents):
            self.assertFalse((ancestor / ".git").exists(),
                             "the reviewer key must not sit in a git work tree")

    def test_no_emitted_byte_carries_the_reviewer_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp), control={"fail": {"review": 5}})
            self._write_plan_ir(fixture)
            (fixture["plans_dir"] / "demo.html").write_text("<html/>",
                                                            encoding="utf-8")
            status, payloads, raw = self._run(
                fixture, ["plan", "review", "demo"],
                environment={"PLANCTL_REVIEWER_HMAC_KEY": None})
            layout = self._layout(fixture)
            key = maestro._reviewer_hmac_key_file(layout).read_text(
                encoding="ascii").strip()
        self.assertEqual(status, 2)
        self.assertTrue(key)
        self.assertNotIn(key, raw)
        self.assertIn("[redacted]", payloads[-1]["stdout"])
        self.assertNotIn(key, json.dumps(payloads))


class VisiblePaneTest(PlanContractVerbFixture):
    """Nothing happens invisibly, and no pane means no work at all."""

    def _pane_argv(self, fixture):
        return [row["argv"] for row in self._pane_calls(fixture)]

    def test_gate_opens_a_visible_pane_and_streams_its_log_into_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "gate", "demo"])
            streamed = Path(payloads[-1]["log"]).read_text(encoding="utf-8")
        self.assertEqual(status, 0, payloads)
        self.assertEqual(payloads[-1]["pane"], "pane-7")
        argv = self._pane_argv(fixture)
        self.assertEqual(argv[0][:2], ["pane", "split"])
        self.assertIn("--cwd", argv[0])
        run = [row for row in argv if row[:2] == ["pane", "run"]]
        self.assertEqual(len(run), 1)
        self.assertEqual(run[0][:3], ["pane", "run", "pane-7"])
        self.assertEqual(run[0][3], "tail")
        self.assertEqual(run[0][-1], payloads[-1]["log"])
        self.assertIn(["pane", "close", "pane-7"], argv)
        self.assertIn("planctl render", streamed)

    def test_review_and_ship_open_a_visible_pane_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._approved(fixture)
            self._run(fixture, ["plan", "gate", "demo"])
            status, payloads, _raw = self._run(fixture, ["plan", "review", "demo"])
            self.assertEqual(status, 0, payloads)
            self.assertEqual(payloads[-1]["pane"], "pane-7")
            with mock.patch.object(
                    maestro, "_plan_author_options",
                    return_value={
                        "--from-plan-contract": "from_plan_contract",
                        "--plan-contract-receipt": "plan_contract_receipt",
                        "--plan-contract-rendered": "plan_contract_rendered"}), \
                    mock.patch.object(maestro, "_plan_author",
                                      side_effect=self._authors()), \
                    mock.patch.object(maestro, "_plan_validate", return_value=0), \
                    mock.patch.object(maestro, "_plan_finalize", return_value=0):
                ship_status, ship_payloads, _raw = self._run(
                    fixture, ["plan", "ship", "demo"])
        self.assertEqual(ship_status, 0, ship_payloads)
        self.assertEqual(ship_payloads[-1]["pane"], "pane-7")

    def test_the_reviewer_key_never_reaches_a_pane_argv_or_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            self._run(fixture, ["plan", "gate", "demo"])
            status, payloads, _raw = self._run(
                fixture, ["plan", "review", "demo"],
                environment={"PLANCTL_REVIEWER_HMAC_KEY": None})
            self.assertEqual(status, 0, payloads)
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(os.environ, fixture["environment"]):
                layout = maestro._plan_contract_layout()
            key = maestro._reviewer_hmac_key_file(layout).read_text(
                encoding="ascii").strip()
            pane_log = Path(payloads[-1]["log"]).read_text(encoding="utf-8")
        self.assertTrue(key)
        for row in self._pane_calls(fixture):
            self.assertNotIn(key, json.dumps(row["argv"]))
            self.assertNotIn(key, json.dumps(row["environment"]))
        self.assertNotIn(key, pane_log)

    def _refusing_herdr(self):
        return {"HERDR_FAKE_REFUSE": "1"}

    def test_every_verb_refuses_when_herdr_cannot_open_a_pane(self):
        for verb, prepare in (
                ("gate", lambda fixture: None),
                ("review", lambda fixture: (fixture["plans_dir"] / "demo.html")
                 .write_text("<html/>", encoding="utf-8")),
                ("ship", lambda fixture: [
                    (fixture["plans_dir"] / "demo.html").write_text(
                        "<html/>", encoding="utf-8"),
                    (fixture["plans_dir"] / "demo.plan-review.json").write_text(
                        "{}", encoding="utf-8")])):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(Path(tmp))
                self._write_plan_ir(fixture)
                prepare(fixture)
                with mock.patch.object(
                        maestro, "_plan_author_options",
                        return_value={
                            "--from-plan-contract": "from_plan_contract",
                            "--plan-contract-receipt": "plan_contract_receipt",
                            "--plan-contract-rendered":
                                "plan_contract_rendered"}):
                    status, payloads, _raw = self._run(
                        fixture, ["plan", verb, "demo"],
                        environment=self._refusing_herdr())
                self.assertNotEqual(status, 0)
                self.assertEqual(payloads[-1]["outcome"],
                                 "HERDR_PANE_UNAVAILABLE")
                self.assertIn("Herdr", payloads[-1]["detail"])
                self.assertEqual(self._invocations(fixture), [],
                                 "no planctl step may run without a pane")

    def test_a_refused_pane_leaves_no_artifact_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            plan_ir = self._write_plan_ir(fixture)
            before = sorted(path.name for path in fixture["plans_dir"].iterdir())
            status, payloads, _raw = self._run(
                fixture, ["plan", "gate", "demo"],
                environment=self._refusing_herdr())
            after = sorted(path.name for path in fixture["plans_dir"].iterdir())
            state = fixture["state"] / "plan-contract"
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "HERDR_PANE_UNAVAILABLE")
        self.assertEqual(before, after)
        self.assertEqual(after, [plan_ir.name])
        self.assertFalse(state.exists(),
                         "a refused verb must not leave a step log behind")


class BootstrapReviewerKeyTest(unittest.TestCase):
    """`maestro bootstrap` mints the reviewer key, so nobody ever types one.

    The key is needed before `maestro plan review` exists: /arch-review drives
    `planctl review` through the skill directly and only needs the env file
    bootstrap already writes.
    """

    def test_bootstrap_writes_the_reviewer_key_into_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = route_admission.provision_keys(Path(tmp) / "keys")
            env_file = route_admission.write_env_file(
                keys, verify_key_env="MAESTRO_VERIFY_KEY",
                signing_seed_env="MAESTRO_SIGNING_SEED",
                route_verify_key_env="MAESTRO_ROUTE_VERIFY_KEY")
            body = env_file.read_text(encoding="ascii")
            bindings = dict(
                line.split("=", 1) for line in body.splitlines() if line.strip())
            mode = stat.S_IMODE(env_file.stat().st_mode)
        self.assertIn("PLANCTL_REVIEWER_HMAC_KEY", bindings)
        for name in ("MAESTRO_VERIFY_KEY", "MAESTRO_SIGNING_SEED",
                     "MAESTRO_ROUTE_VERIFY_KEY"):
            self.assertIn(name, bindings)
        self.assertEqual(bindings["PLANCTL_REVIEWER_HMAC_KEY"],
                         keys.reviewer_hmac.hex())
        self.assertEqual(mode, 0o600)

    def test_the_minted_key_clears_planctl_s_minimum_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = route_admission.provision_keys(Path(tmp) / "keys")
        self.assertGreaterEqual(
            len(keys.reviewer_hmac.hex().encode("utf-8")),
            maestro._REVIEWER_HMAC_KEY_MINIMUM_BYTES)

    def test_bootstrapping_twice_never_changes_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys_dir = Path(tmp) / "keys"
            first = route_admission.provision_keys(keys_dir)
            second = route_admission.provision_keys(keys_dir)
            key_file = keys_dir / route_admission.REVIEWER_HMAC_KEY_FILE
            stored = key_file.read_text(encoding="ascii").strip()
            mode = stat.S_IMODE(key_file.stat().st_mode)
        self.assertEqual(first.reviewer_hmac, second.reviewer_hmac)
        self.assertEqual(second.created, ())
        self.assertEqual(stored, first.reviewer_hmac.hex())
        self.assertEqual(mode, 0o600)

    def test_the_reviewer_key_is_minted_on_the_first_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = route_admission.provision_keys(Path(tmp) / "keys")
        self.assertIn(route_admission.REVIEWER_HMAC_KEY_FILE, first.created)


class BootstrapAndReviewShareOneKeyTest(PlanContractVerbFixture):
    def test_plan_review_uses_exactly_the_key_bootstrap_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            # Bootstrap runs first, as it does in a real repository.
            keys = route_admission.provision_keys(fixture["state"] / "keys")
            bootstrapped = keys.reviewer_hmac.hex()
            self._write_plan_ir(fixture)
            (fixture["plans_dir"] / "demo.html").write_text(
                "<html/>", encoding="utf-8")
            status, payloads, raw = self._run(
                fixture, ["plan", "review", "demo"],
                environment={"PLANCTL_REVIEWER_HMAC_KEY": None})
            keys_used = {row["key"] for row in self._invocations(fixture)
                         if "--help" not in row["argv"]}
        self.assertEqual(status, 0, payloads)
        self.assertEqual(keys_used, {bootstrapped})
        self.assertNotIn(bootstrapped, raw)

    def test_plan_review_mints_the_same_key_when_bootstrap_has_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            with self._repository_cwd(fixture["repo"]), \
                    mock.patch.dict(os.environ, fixture["environment"]):
                os.environ.pop("PLANCTL_REVIEWER_HMAC_KEY", None)
                layout = maestro._plan_contract_layout()
                minted = maestro._reviewer_hmac_key(layout)
            # A later bootstrap must adopt it rather than replace it.
            keys = route_admission.provision_keys(fixture["state"] / "keys")
        self.assertEqual(keys.reviewer_hmac.hex(), minted)


class PlanShipTest(PlanContractVerbFixture):
    def test_ship_refuses_without_a_review_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            (fixture["plans_dir"] / "demo.html").write_text("<html/>",
                                                            encoding="utf-8")
            status, payloads, _raw = self._run(fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_CONTRACT_RECEIPT_MISSING")
        self.assertIn("plan review demo", payloads[-1]["detail"])

    def test_ship_refuses_without_a_rendered_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._write_plan_ir(fixture)
            status, payloads, _raw = self._run(fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_CONTRACT_RENDER_MISSING")

    def test_ship_refuses_when_the_author_verb_cannot_ingest_a_plan_ir(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._approved(fixture)
            with mock.patch.object(maestro, "_plan_author_options",
                                   return_value={}):
                status, payloads, _raw = self._run(
                    fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"],
                         "PLAN_CONTRACT_INGRESS_UNAVAILABLE")
        self.assertIn("--from-plan-contract", payloads[-1]["detail"])

    def _ingest_options(self):
        return {
            "--from-plan-contract": "from_plan_contract",
            "--plan-contract-receipt": "plan_contract_receipt",
            "--plan-contract-rendered": "plan_contract_rendered",
        }

    def test_ship_hands_the_author_every_derived_plan_contract_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._approved(fixture)
            with mock.patch.object(maestro, "_plan_author_options",
                                   return_value=self._ingest_options()), \
                    mock.patch.object(maestro, "_plan_author",
                                      side_effect=self._authors()) as author, \
                    mock.patch.object(maestro, "_plan_validate",
                                      return_value=0), \
                    mock.patch.object(maestro, "_plan_finalize",
                                      return_value=0):
                status, payloads, _raw = self._run(
                    fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 0, payloads)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_SHIPPED")
        self.assertEqual(payloads[-1]["steps"], ["author", "validate", "finalize"])
        authored = author.call_args.args[0]
        plans = fixture["plans_dir"].resolve()
        self.assertEqual(Path(authored.from_plan_contract), plans / "demo.plan.json")
        self.assertEqual(Path(authored.plan_contract_receipt),
                         plans / "demo.plan-review.json")
        self.assertEqual(Path(authored.plan_contract_rendered),
                         plans / "demo.html")

    def test_ship_stops_at_the_first_failing_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._approved(fixture)
            with mock.patch.object(maestro, "_plan_author_options",
                                   return_value=self._ingest_options()), \
                    mock.patch.object(maestro, "_plan_author",
                                      return_value=3), \
                    mock.patch.object(maestro, "_plan_validate",
                                      return_value=0) as validate, \
                    mock.patch.object(maestro, "_plan_finalize",
                                      return_value=0) as finalize:
                status, payloads, _raw = self._run(
                    fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 3)
        self.assertEqual(payloads[-1]["outcome"], "PLAN_SHIP_FAILED")
        self.assertEqual(payloads[-1]["step"], "author")
        validate.assert_not_called()
        finalize.assert_not_called()

    def test_ship_never_finalizes_a_plan_that_failed_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(Path(tmp))
            self._approved(fixture)
            with mock.patch.object(maestro, "_plan_author_options",
                                   return_value=self._ingest_options()), \
                    mock.patch.object(maestro, "_plan_author",
                                      side_effect=self._authors()), \
                    mock.patch.object(maestro, "_plan_validate",
                                      return_value=2), \
                    mock.patch.object(maestro, "_plan_finalize",
                                      return_value=0) as finalize:
                status, payloads, _raw = self._run(
                    fixture, ["plan", "ship", "demo"])
        self.assertEqual(status, 2)
        self.assertEqual(payloads[-1]["step"], "validate")
        finalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
