"""`maestro plan finalize` dispatches no reviewer (§1.2, §6.5, §11.1).

A plan used to become runnable because a reviewer said so. The reviewer was
launched in a pane, answered `clear` or `finding` per (check x object) cell,
and code derived a verdict from its answers — and the prompt it was given was
the matrix, the plan digest, and a path to write to. It was never shown the
plan. So the thing standing between authored bytes and an executing DAG was a
model's opinion about a list of check ids, which is exactly the lifecycle
transition §1.2 forbids: caused by prompt text and an agent's claim about its
own work rather than by a counted, re-derivable fact.

The reviewer is removed rather than repaired. Eligibility is `plan_validate`'s
deterministic obligations, every one computed from git objects alone, and the
receipt records who actually judged: route `deterministic`, model
`plan_validate`, session id the digest, and no cells, because no cell was
answered.

What this file proves, in the order it matters:

  1. A plan that would previously have been finalization-gated now ships with
     no reviewer route, model, session, or report configured at all — and no
     launcher is constructed while it does.
  2. The receipt is a real signed receipt in the real store, carrying
     `maestro-deterministic.v1` and the deterministic identity.
  3. §6.5's replay is intact: a second `plan finalize` over the same bytes
     writes nothing and reports `replayed`.
  4. `run start`'s receipt check accepts it, through the production function
     `_load_runnable_plan` that `_execute_run` calls.
  5. Structurally, the `plan finalize` call graph cannot reach a launch path.
     Checked by walking maestro.py's own AST rather than by trusting 1-4, so
     re-wiring a reviewer into the verb fails here even if the four
     behavioural cases were left passing.

Run:  uv run adw_test.py -k plan_finalize_deterministic
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
for _path in (str(ADWS), str(Path(__file__).resolve().parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import maestro                                          # noqa: E402
from adw_modules import finalization as fin             # noqa: E402
from adw_modules import plan_canonical as pc            # noqa: E402
from adw_modules import plan_digest as pd               # noqa: E402
from adw_modules import plan_model as pm                # noqa: E402
from adw_modules import receipt_crypto as rc            # noqa: E402

from test_step2_plan_validation import make_repo, plan_mapping  # noqa: E402


class _NoLauncher:
    """Constructing this is the failure. `plan finalize` launches nothing."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "plan finalize constructed a HerdrLauncher; it dispatches no "
            "reviewer")


class DeterministicFinalizeTestCase(unittest.TestCase):
    """One real repository, one real receipt store, one real plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        base = maestro.subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo),
            capture_output=True, text=True, check=True).stdout.strip()
        mapping = plan_mapping(base)
        # v2 because `run start` refuses a v1 plan outright (#104), and case 4
        # below is about the receipt check rather than about the schema gate.
        mapping["schema_version"] = pm.SCHEMA_V2
        self.stored = pc.canonicalize(pm.parse_mapping(mapping))
        self.digest = pd.digest_of(self.stored)
        self.plan_file = self.root / "plans" / "maestro-plan.v2"
        self.plan_file.parent.mkdir(parents=True)
        self.plan_file.write_bytes(self.stored)
        self.receipt_dir = self.root / "receipts"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.seed = rc.generate_seed()
        self.public_key = rc.seed_to_public_key(self.seed)

    # -- drivers ------------------------------------------------------------

    def finalize(self) -> "tuple[int, dict]":
        """`maestro plan finalize <path> …` through `main`, with the whole
        reviewer surface absent: no route, no model, no session directory, no
        report file, and no executable for any of the three launchers."""
        argv = [
            "plan", "finalize", str(self.plan_file),
            "--repo", str(self.repo),
            "--receipt-dir", str(self.receipt_dir),
            "--data-dir", str(self.data_dir),
            "--verify-key", self.public_key.hex(),
            "--signing-seed", self.seed.hex(),
        ]
        out = io.StringIO()
        with mock.patch.object(maestro.launcher, "HerdrLauncher", _NoLauncher), \
                contextlib.redirect_stdout(out):
            code = maestro.main(argv)
        printed = out.getvalue().strip()
        return code, (json.loads(printed) if printed else {})

    def store(self) -> fin.ReceiptStore:
        return fin.ReceiptStore(
            self.receipt_dir, repo_paths=(self.repo,), data_dir=self.data_dir,
            verify_keys=(self.public_key,))

    def run_start_args(self) -> SimpleNamespace:
        """Exactly what `_execute_run` hands `_load_runnable_plan`."""
        return SimpleNamespace(
            plan_file=str(self.plan_file), repo=str(self.repo),
            digest=self.digest, receipt_dir=str(self.receipt_dir),
            data_dir=str(self.data_dir),
            verify_key=[self.public_key.hex()], runners={})


class APreviouslyGatedPlanShips(DeterministicFinalizeTestCase):

    def test_finalize_passes_with_no_reviewer_configured_and_no_pane(self):
        code, payload = self.finalize()

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload, {
            "digest": self.digest,
            "outcome": "FINALIZED",
            "replayed": False,
            "verdict": "PASS",
        })

    def test_the_receipt_names_the_deterministic_authority(self):
        self.finalize()
        receipt = self.store().load(self.digest)

        self.assertEqual(receipt.rubric_version, "maestro-deterministic.v1")
        self.assertIs(receipt.verdict, fin.Verdict.PASS)
        self.assertEqual(receipt.cells, ())
        self.assertIsNone(receipt.reject_at)
        self.assertEqual(
            (receipt.reviewer.route, receipt.reviewer.model,
             receipt.reviewer.session_id),
            ("deterministic", "plan_validate", self.digest))

    def test_the_receipt_is_signed_and_verifies_like_any_other(self):
        """§6.6 is untouched: the store that wrote it is the store that
        refuses to sit inside the repository, and the detached signature is
        checked on every load."""
        self.finalize()
        signature = self.store().signature_path_for(self.digest)
        self.assertTrue(signature.is_file())
        signature.write_bytes(b"\x00" * len(signature.read_bytes()))
        with self.assertRaises(fin.SignatureInvalid):
            self.store().load(self.digest)

    def test_a_second_finalize_replays_and_writes_nothing(self):
        """§6.5: replay is keyed on the digest alone."""
        self.finalize()
        written = self.store().path_for(self.digest).read_bytes()

        code, payload = self.finalize()

        self.assertEqual(code, 0)
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(self.store().path_for(self.digest).read_bytes(),
                         written)

    def test_an_ineligible_plan_still_earns_no_receipt(self):
        """The deterministic obligations are the whole gate, so removing the
        reviewer must not have removed the refusal with it."""
        broken = json.loads(self.stored.decode("utf-8"))
        broken["base_commit"] = "0" * 40
        self.plan_file.write_bytes(
            pc.canonicalize(pm.parse_mapping(broken)))

        code, payload = self.finalize()

        self.assertEqual(code, 2)
        self.assertEqual(payload["outcome"], "AUTHORING_BLOCKED")
        self.assertTrue(payload["blockers"])
        self.assertFalse(self.receipt_dir.exists()
                         and any(self.receipt_dir.rglob("*.json")))

    def test_run_start_accepts_the_digest_it_finalized(self):
        """`_load_runnable_plan` is the production receipt check `run start`
        and `run resume` both reach through `_execute_run`."""
        self.finalize()

        plan = maestro._load_runnable_plan(self.run_start_args())

        self.assertEqual(plan.schema_version, pm.SCHEMA_V2)

    def test_run_start_refuses_the_same_plan_before_it_is_finalized(self):
        """The control for the case above: without the deterministic receipt
        the run is refused, so the acceptance proves the receipt and not the
        bytes."""
        with self.assertRaises(maestro._RunRefused) as refused:
            maestro._load_runnable_plan(self.run_start_args())
        self.assertEqual(refused.exception.outcome, "RUN_RECEIPT_ABSENT")


class TheFinalizeVerbCannotReachALaunchPath(unittest.TestCase):
    """The structural half, in the shape of §6.4's
    `ValidationLaunchesNoReviewer`: what `plan finalize` can *reach*, read off
    maestro.py's AST rather than off a passing run.

    A behavioural test proves no reviewer ran in the cases it exercised. This
    proves no reviewer can run, which is the claim the removal actually makes.
    """

    #: Names that only exist to launch, bound, or poll an agent pane.
    LAUNCH_NAMES = frozenset({
        "HerdrLauncher", "LaunchSpec", "FinalizationWindow", "ReviewerSession",
        "_typed_launch_pane", "_preflight_prompt", "_poll_reviewer_report",
        "_clear_stale_reviewer_report", "_reviewer_occupancy",
        "_route_context_window", "agent_status",
    })

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        cls.functions = {
            node.name: node for node in cls.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def _reachable(self, entry: str) -> "set[str]":
        """Every module-level function `entry` can call, transitively."""
        seen: "set[str]" = set()
        pending = [entry]
        while pending:
            name = pending.pop()
            if name in seen or name not in self.functions:
                continue
            seen.add(name)
            for node in ast.walk(self.functions[name]):
                if isinstance(node, ast.Name):
                    pending.append(node.id)
        return seen

    def _names_used_by(self, entry: str) -> "set[str]":
        used: "set[str]" = set()
        for name in self._reachable(entry):
            for node in ast.walk(self.functions[name]):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
        return used

    def test_the_entry_point_exists_and_the_walk_reaches_its_callees(self):
        """The control for the check below. A reachability sweep that reaches
        nothing acquits everything, which is §13.4's whole complaint."""
        reachable = self._reachable("_plan_finalize")
        self.assertIn("_plan_finalize", reachable)
        self.assertIn("_finalization_store", reachable)
        self.assertIn("_deterministic_receipt", reachable)

    def test_plan_finalize_reaches_no_launch_name(self):
        used = self._names_used_by("_plan_finalize")
        self.assertEqual(sorted(used & self.LAUNCH_NAMES), [])

    def test_the_sweep_convicts_a_planted_launch(self):
        """§13.4: the detector is shown to convict before it is believed."""
        used = self._names_used_by("_code_review_runner")
        self.assertTrue(used & self.LAUNCH_NAMES)

    def test_the_finalize_parser_carries_no_reviewer_flag(self):
        # Reached through the parser's own choices rather than by name-matching
        # the source, so a flag re-added anywhere in the subparser is caught.
        plan = _subparser(maestro.build_parser(), "plan")
        finalize = _subparser(plan, "finalize")
        flags = {option for action in finalize._actions
                 for option in action.option_strings}
        self.assertEqual(flags & {
            "--herdr", "--omp", "--claude", "--reviewer-route",
            "--reviewer-model", "--reviewer-effort", "--reviewer-profile",
            "--reviewer-session-dir", "--reviewer-report-file",
            "--route-receipt", "--route-verify-key",
            "--finalization-timeout-s", "--reviewer-turn-timeout-s",
            "--reviewer-poll-interval-s"}, set())
        # ...and still carries everything the receipt needs.
        self.assertLessEqual(
            {"--repo", "--receipt-dir", "--data-dir", "--verify-key",
             "--signing-seed"}, flags)


def _subparser(parser, name):
    """The subparser `name` dispatches to, from the parser itself."""
    for action in parser._actions:
        choices = getattr(action, "choices", None) or {}
        if name in choices:
            return choices[name]
    raise AssertionError("no subcommand named " + name)


if __name__ == "__main__":
    unittest.main()
