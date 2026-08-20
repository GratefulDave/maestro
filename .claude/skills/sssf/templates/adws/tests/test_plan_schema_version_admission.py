"""Executable proof that a plan projected before §19 M26's fix cannot run.

M26 is the projection that *narrowed* a field instead of dropping it.
`plan_contract_ingress.project_draft` mapped a lane's `title` onto
`nodes[].instruction` and dropped `requirements[].text`, so every builder
prompt and every reviewer contract carried a summary of the lane's contract
in place of the contract. `test_node_instruction_requirement_text.py` closes
the projection. This file closes what the projection could not: the plans
already shipped under the broken one.

The gap it exists for. The widened projection went on emitting
`maestro-plan.v1` — the same version string the degenerate projection emitted
— so a plan shipped before the fix and a plan shipped after it were
indistinguishable *in version*, and a runtime carrying every fix would execute
the degenerate one and say nothing. Four executable plans and 51 agent nodes
in the lexgenius-pipeline deployment are in that state;
`lane-p5-gap-policy` burned 23 attempts with its reviewer judging 573 lines
against a 150-byte goal. `code_review._node_goal` cannot catch it either: it
raises `InstructionNotCarried` only for an *empty* instruction, and a title is
not empty. A populated field cannot be audited by its consumers, so the only
channel left is the version string.

What is settled here:

  §6.3  the projection emits `maestro-plan.v2`, and v2 dispatches to its own
        registered class rather than parsing as v1
  §6.3  a v1 plan still parses, still validates, still holds its digest — the
        change is to what may *run*, not to what may be read
  §19 M26  the version bump and the instruction fix are asserted together, so
        reverting either one alone fails here
  §19 M6  the guard sits at `_load_runnable_plan`, which `_execute_run` is the
        sole caller of and which `run start` and `run resume` both enter, so
        its coverage is a property of the call graph rather than of a list of
        verbs somebody has to maintain
  §1.2  the refusal keys on a typed version field read from the stored bytes
        before they become a model — never on prose, pane text, or a claim
  §3.6 B15  the new constant has a reader, and the reader is on the run path

Run with:  uv run adws/adw_test.py -k plan_schema_version_admission
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import maestro  # noqa: E402

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_contract_ingress as pci  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402

from test_finalization import (  # noqa: E402
    DIGEST, WindowFactory, clean_report, make_store, matrix_for)
from test_node_instruction_requirement_text import (  # noqa: E402
    REAL_LANE_TITLE, REAL_REQUIREMENT_TEXT, _authored_plan, _ir, _repo)
from test_receipt_set_aside import finalize  # noqa: E402


def _emit(call):
    """One handler invocation, and the single JSON object it printed."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = call()
    return code, json.loads(output.getvalue())


def _shipped_v2_bytes(root: Path) -> bytes:
    """Canonical bytes of a plan shipped through the real ingress path.

    It builds inside its own subdirectory because `_repo` mints `<root>/repo`
    and `make_store` mints one of its own at the same name.
    """
    shipped = Path(root) / "shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    plan = _authored_plan(shipped, _repo(shipped), _ir())
    return pc.canonicalize(plan)


def _downgraded_to_v1(stored: bytes) -> bytes:
    """The same plan wearing v1's marker, as a plan shipped last month wears it.

    It is round-tripped through `pm.parse_mapping` deliberately: this must be
    a *valid* v1 plan, not a malformed one, or the refusal below would be
    proving something about broken bytes instead of about a version.
    """
    data = json.loads(stored.decode("utf-8"))
    data["schema_version"] = pm.SCHEMA_V1
    plan = pm.parse_mapping(data)
    assert type(plan) is pm.Plan, "the downgraded fixture must parse as v1"
    return pc.canonicalize(plan)


# ── the projection emits v2, and it moved with the instruction fix ──────────

class TheProjectionEmitsV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_projected_draft_declares_v2(self):
        draft = pci.project_draft(_ir(), self.root)
        self.assertEqual(pm.SCHEMA_V2, draft["schema_version"])

    def test_the_shipped_bytes_parse_as_v2_not_as_v1(self):
        """Not `assertIsInstance`: `PlanV2` subclasses `Plan`, so an isinstance
        check would pass on a v1 plan and prove nothing."""
        stored = _shipped_v2_bytes(self.root)
        self.assertIs(type(pm.parse_bytes(stored)), pm.PlanV2)

    def test_the_version_moved_with_the_instruction_it_describes(self):
        """The two halves asserted together, because either one alone is the
        defect. A v2 marker over a title-only instruction is a lie about the
        contents; the widened instruction under a v1 marker is M26's own
        indistinguishability. Reverting either fails here.
        """
        draft = pci.project_draft(_ir(), self.root)
        instruction = draft["nodes"][0]["instruction"]
        self.assertEqual(pm.SCHEMA_V2, draft["schema_version"])
        self.assertNotEqual(REAL_LANE_TITLE, instruction)
        self.assertIn(REAL_REQUIREMENT_TEXT, instruction)

    def test_no_v1_marker_survives_anywhere_in_the_shipped_bytes(self):
        stored = _shipped_v2_bytes(self.root)
        self.assertNotIn(pm.SCHEMA_V1.encode(), stored)


# ── v1 is still readable; only running it is refused ────────────────────────

class AV1PlanIsStillAPlanTest(unittest.TestCase):
    """The negative control §13.4 requires beside the refusal.

    A fix that made v1 unreadable would break `plan show`, `plan list`, every
    receipt already signed over v1 bytes, and any forensic read of the four
    shipped plans this exists because of. The change is to what may run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_v1_plan_still_parses_under_its_frozen_class(self):
        stored = _downgraded_to_v1(_shipped_v2_bytes(self.root))
        self.assertIs(type(pm.parse_bytes(stored)), pm.Plan)

    def test_a_v1_plan_is_still_canonical(self):
        stored = _downgraded_to_v1(_shipped_v2_bytes(self.root))
        self.assertTrue(pc.is_canonical(stored))

    def test_validation_did_not_acquire_a_version_opinion(self):
        """The refusal belongs to the run, not to `plan validate`. Asserted
        over the module rather than over one call because the claim is that
        `plan_validate` reads the version *nowhere* — one passing call would
        only show it did not read it on that path."""
        source = (ADWS / "adw_modules" / "plan_validate.py").read_text(
            encoding="utf-8")
        self.assertNotIn("schema_version", source)
        self.assertNotIn("SCHEMA_V", source)


# ── the run-start refusal ───────────────────────────────────────────────────

class TheRunStartVersionGuardTest(unittest.TestCase):
    """`_load_runnable_plan` against a real receipt store.

    Only `pv.validate_plan` is stubbed, exactly as
    `test_run_refusal_vocabulary.py` stubs it: these tests ask what the
    *version* arm does, so the plan validator is held eligible rather than
    reimplemented.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store, _repo_path, _data, self.seed = make_store(self.root)
        self.v2 = _shipped_v2_bytes(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _arguments(self, stored: bytes) -> argparse.Namespace:
        plan_file = self.root / "plan.json"
        plan_file.write_bytes(stored)
        return argparse.Namespace(
            plan_file=str(plan_file), digest=DIGEST,
            repo=str(self.root / "repo"), receipt_dir=str(self.store.root),
            data_dir=str(self.root / "sssf-data"),
            verify_key=[rc.seed_to_public_key(self.seed).hex()], runners=None)

    @contextlib.contextmanager
    def _eligible(self):
        validation = mock.Mock(eligible=True, digest=DIGEST)
        with mock.patch.object(maestro.pv, "validate_plan",
                               return_value=validation), \
                mock.patch.object(maestro, "_plan_collector",
                                  return_value=None):
            yield

    def _refusal_for(self, stored: bytes) -> maestro._RunRefused:
        args = self._arguments(stored)
        with self._eligible():
            with self.assertRaises(maestro._RunRefused) as caught:
                maestro._load_runnable_plan(args)
        return caught.exception

    def test_a_v1_plan_is_refused_under_its_own_outcome(self):
        refusal = self._refusal_for(_downgraded_to_v1(self.v2))
        self.assertEqual("RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE",
                         refusal.outcome)

    def test_the_refusal_carries_the_version_as_a_typed_field(self):
        """§1.2 — a fact a caller must branch on travels as a field, never as
        a sentence. An operator tool deciding which plans to re-ship reads
        these two, not the prose."""
        refusal = self._refusal_for(_downgraded_to_v1(self.v2))
        self.assertEqual(pm.SCHEMA_V1,
                         refusal.fields["declared_schema_version"])
        self.assertEqual([pm.SCHEMA_V2],
                         refusal.fields["runnable_schema_versions"])

    def test_the_refusal_names_the_plan_the_version_and_the_remedy(self):
        """All three, because a refusal an operator cannot act on sends them
        to the source. The remedy is re-shipping from the IR — there is no
        upgrade function and no in-place edit (§6.3)."""
        args = self._arguments(_downgraded_to_v1(self.v2))
        with self._eligible():
            with self.assertRaises(maestro._RunRefused) as caught:
                maestro._load_runnable_plan(args)
        detail = caught.exception.detail
        self.assertIn(args.plan_file, detail)
        self.assertIn(pm.SCHEMA_V1, detail)
        self.assertIn(pm.SCHEMA_V2, detail)
        self.assertIn("plan ship", detail)
        self.assertIn("docs/plan-authoring.md", detail)

    def test_a_v2_plan_is_not_refused_and_loads_as_v2(self):
        """The positive control. Without it the guard could be refusing
        everything and every assertion above would still pass."""
        args = self._arguments(self.v2)
        finalize(self.store, WindowFactory(report=clean_report(matrix_for())))
        with self._eligible():
            plan = maestro._load_runnable_plan(args)
        self.assertIs(type(plan), pm.PlanV2)

    def test_an_unregistered_future_version_is_refused_too(self):
        """An allowlist, not a denylist: a v3 registered later and not added to
        `_RUNNABLE_PLAN_SCHEMA_VERSIONS` must refuse rather than run."""
        data = json.loads(self.v2.decode("utf-8"))
        data["schema_version"] = "maestro-plan.v7"
        stored = json.dumps(data, sort_keys=True,
                            separators=(",", ":")).encode("utf-8") + b"\n"
        self.assertEqual("RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE",
                         self._refusal_for(stored).outcome)

    def test_bytes_with_no_version_are_left_to_the_parser(self):
        """One defect, one vocabulary. `parse_bytes` already refuses a missing
        `schema_version` as `SchemaVersionUnknown`, and a second name for it
        here would teach an operator that one of them is optional. Nothing
        that could run escapes: bytes that run have parsed, and bytes that
        have parsed declared a registered version.
        """
        args = self._arguments(b"{}\n")
        finalize(self.store, WindowFactory(report=clean_report(matrix_for())))
        with self._eligible():
            with self.assertRaises(pm.SchemaVersionUnknown):
                maestro._load_runnable_plan(args)


# ── both run verbs cross it, with nothing stubbed ───────────────────────────

class BothRunVerbsRefuseAV1PlanTest(unittest.TestCase):
    """The handler end, driven for real: no mock anywhere in this class.

    The guard raises before the receipt store, the validator, or the scheduler
    is reached, so `run start` and `run resume` can be invoked exactly as an
    operator invokes them and the refusal is the whole observable behaviour.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        plan_file = self.root / "plan.json"
        plan_file.write_bytes(_downgraded_to_v1(_shipped_v2_bytes(self.root)))
        self.plan_file = plan_file

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_file=str(self.plan_file), repo=str(self.root / "repo"),
            receipt_dir=str(self.root / "receipts"),
            data_dir=str(self.root / "sssf-data"), verify_key=["ab" * 32],
            digest=DIGEST, db=str(self.root / "lifecycle.db"),
            run_id="run-1", selector=None,
            integration_path=str(self.root / "integration"),
            worktrees_root=str(self.root / "worktrees"),
            scratch_root=str(self.root / "scratch"), concurrency=1,
            node_timeout_s=60, turn_timeout_s=60,
            final_acceptance_timeout_s=60, backstop_t_s=600,
            semantic_ceiling=2, runners=None)

    def test_run_start_refuses_it(self):
        code, payload = _emit(lambda: maestro._run_start(self._args()))
        self.assertNotEqual(0, code)
        self.assertEqual("RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE",
                         payload["outcome"])
        self.assertEqual(pm.SCHEMA_V1, payload["declared_schema_version"])

    def test_run_resume_refuses_it_too(self):
        """The route M6 is about. A guard on `run start` alone would let a run
        that began under v1 come back through `run resume` untouched."""
        code, payload = _emit(lambda: maestro._run_resume(self._args()))
        self.assertNotEqual(0, code)
        self.assertEqual("RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE",
                         payload["outcome"])


# ── the coverage claim is a property of the call graph (§19 M6) ─────────────

class TheGuardSitsAtTheChokepointTest(unittest.TestCase):
    """M6's lesson, asserted rather than described.

    B13's handoff size preflight was installed on one launch path; a second
    route reached the same agent without crossing it, and the guarantee decayed
    with nothing going red. What stops that here is not this file's list of
    verbs but the shape of the call graph: one loader, one caller of it, two
    verbs entering that caller. A third verb added tomorrow inherits the guard
    without being enumerated anywhere — and if someone gives the loader a
    second caller that bypasses it, or the module a second loader, this fails.
    """

    @staticmethod
    def _callers(name: str):
        tree = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == name):
                    found.add(node.name)
        return found

    def test_the_loader_has_exactly_one_caller_and_it_is_execute_run(self):
        self.assertEqual({"_execute_run"},
                         self._callers("_load_runnable_plan"))

    def test_execute_run_is_entered_only_by_the_two_run_verbs(self):
        self.assertEqual({"_run_start", "_run_resume"},
                         self._callers("_execute_run"))

    def test_the_guard_is_read_by_the_loader(self):
        """§3.6 B15 — a check with no reader is a build failure. This is the
        reader, and it is on the run path rather than beside it."""
        self.assertEqual({"_load_runnable_plan"},
                         self._callers("_refuse_unrunnable_plan_schema"))

    def test_the_allowlist_has_a_reader(self):
        """A substring count would also count the assignment and the comment.
        This counts `Load` references — the definition of a reader."""
        tree = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        reads = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Name)
                 and node.id == "_RUNNABLE_PLAN_SCHEMA_VERSIONS"
                 and isinstance(node.ctx, ast.Load)]
        self.assertGreaterEqual(len(reads), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
