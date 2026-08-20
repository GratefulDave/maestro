"""Executable proof that a node which cannot satisfy its own instruction
fails at plan review rather than eight attempts into a run.

The incident (§19, run `run-2a44d226e75a4be391a14f02b78a6d25`, node
`lane-p4-enrichment-ordering`): the node's instruction stated an end-to-end
behavioural property over production dispatch. Its declared `outputs` were a
brand-new module and that module's own test, and nothing else. The production
call sites the instruction required were the declared outputs of a different,
already-merged node. Single-producer ownership forbade this node declaring
them and the attempt permission check convicts any diff touching a path the
node does not declare, so every reviewer correctly rejected a diff that did
not wire production while the permission check would have rejected any diff
that did. The node was unsatisfiable from attempt 1 and ended `BLOCKED` with
`REVIEW_BUDGET_EXHAUSTED` at attempt 8, having spent six reviews, six builder
sessions, a launcher failure and a turn timeout to discover a property of the
authored bytes.

`node.reads_are_sufficient` already asked whether a node can do its work from
what it is allowed to *read*. Nothing asked the same question of what it is
allowed to *write*, so a node could be given everything it needed to read and
still be forbidden from writing the only file that would satisfy its
instruction. `node.writes_are_sufficient` is that counterpart, BLOCKING, and
this file is its red test, its green control, and the receipt-compatibility
proof the version bump owes every already-signed receipt.

What each block settles:

  the gap       -- the write counterpart exists, is NODE-scoped, is BLOCKING,
                   and is asked of every node the projection emits.
  the incident  -- the incident's exact shape reaches a BLOCKING finding on
                   the new check through `finalize`, and the receipt it writes
                   records that cell with the severity code stamped.
  the guard     -- a legitimate new-module node wired by a downstream node
                   still PASSES, and nothing deterministic refuses it. That
                   half matters more than the first: a false refusal here
                   blocks authoring outright.
  the version   -- `maestro-rubric.v3` adds a question, not a receipt key.
                   Signed v1 and pre-grading v2 receipts still parse, still
                   verify, and still replay with zero reviewer launches
                   (§19 M21, §3.6 B8).

Run with:  uv run adws/adw_test.py -k write_scope
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_finalization as pf  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import plan_validate as pv  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402

WRITE_CHECK = "node.writes_are_sufficient"
READ_CHECK = "node.reads_are_sufficient"

README = "fixture repository\n"
DISPATCH = """
def dispatch(document):
    return validate(document)
"""
DISPATCH_TEST = """
import unittest


class T(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)
"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if result.returncode != 0:
        raise AssertionError("git {0} -> {1}: {2}".format(
            " ".join(args), result.returncode, result.stderr))
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    """A repository holding a real pre-existing production dispatch.

    `src/dispatch.py` exists at base and is what the incident's node had to
    change and was not permitted to. `src/enrichment_gate.py` does not exist
    at base — the new module both plans produce.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "README.md").write_text(README)
    (repo / "src").mkdir()
    (repo / "src" / "dispatch.py").write_text(DISPATCH)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_dispatch.py").write_text(DISPATCH_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


class Collector:
    """The gate collector seam — running a runner is an environment fact,
    not a git fact (§6.4)."""

    def __init__(self) -> None:
        self.counts = {"tests/test_dispatch.py": 2, "tests": 3}

    def collect(self, gate, tree):
        return self.counts.get(pm.selector_of(gate), 0)


class Receipts:
    def has_receipt(self, digest):
        return False


def _node(node_id, *, outputs, instruction, gate_argv, needs=()):
    return {
        "kind": "agent", "node_id": node_id, "needs": list(needs),
        "reads": ["e-readme"], "outputs": list(outputs),
        "instruction": instruction,
        "gate": {"runner": "pytest", "cwd": ".", "argv": list(gate_argv),
                 "min_cases": 1},
        "prompt_assets": [],
    }


def _plan(base_commit: str, nodes) -> dict:
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": "plan-write-scope",
        "repo": "example/repo",
        "base_commit": base_commit,
        "intent": "enrich only after validation",
        "evidence": [
            {"kind": "observed", "evidence_id": "e-readme",
             "path": "README.md", "sha256": sha256_text(README)},
        ],
        "nodes": nodes,
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "cwd": ".",
                                 "argv": ["tests"], "min_cases": 1},
        },
        "supersedes": None,
    }


#: The incident: a node whose instruction is a property of production
#: dispatch, whose outputs are a new module and its own test, and whose
#: dispatch file belongs to another node.
UNSATISFIABLE_NODE = "lane-p4-enrichment-ordering"
UNSATISFIABLE_INSTRUCTION = (
    "Add the enrichment gate that runs markdown extraction, classification, "
    "deadlines, SALI and leadership only after binary and identity "
    "validation, and never lets classification modify an identity field")


def unsatisfiable_plan(base_commit: str) -> dict:
    return _plan(base_commit, [
        _node("lane-p1-dispatch",
              outputs=["src/dispatch.py", "tests/test_dispatch.py"],
              instruction="own the dispatch order and its coverage",
              gate_argv=["tests/test_dispatch.py"]),
        _node(UNSATISFIABLE_NODE,
              outputs=["src/enrichment_gate.py",
                       "tests/test_enrichment_gate.py"],
              instruction=UNSATISFIABLE_INSTRUCTION,
              gate_argv=["tests/test_enrichment_gate.py"]),
    ])


#: The control: the same new module, produced by a node whose instruction
#: stops at the module it owns, and wired by a downstream node that owns the
#: production file.
def wired_plan(base_commit: str) -> dict:
    return _plan(base_commit, [
        _node("lane-enrichment-module",
              outputs=["src/enrichment_gate.py",
                       "tests/test_enrichment_gate.py"],
              instruction=("Add src/enrichment_gate.py exposing "
                           "enrich_after_validation(document) and cover it. "
                           "Wiring it into dispatch belongs to "
                           "lane-dispatch-wiring."),
              gate_argv=["tests/test_enrichment_gate.py"]),
        _node("lane-dispatch-wiring",
              outputs=["src/dispatch.py", "tests/test_dispatch.py"],
              instruction=("Call enrich_after_validation from dispatch, "
                           "after validation, and cover the ordering."),
              gate_argv=["tests/test_dispatch.py"],
              needs=["lane-enrichment-module"]),
    ])


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class WindowFactory:
    """The real `FinalizationWindow` over injected collaborators — never a
    mock of it."""

    def __init__(self, report=None) -> None:
        self.report = report
        self.launches = 0
        self.clock = FakeClock()

    def __call__(self, matrix) -> fw.FinalizationWindow:
        self.matrix = matrix
        return fw.FinalizationWindow(
            config=fw.FinalizationConfig(finalization_timeout_s=10.0,
                                         turn_timeout_s=5.0,
                                         poll_interval_s=1.0),
            launch=self._launch,
            poll_report=lambda: self.report,
            record_reviewer_session=lambda session: None,
            kill=lambda session: None,
            time_source=self.clock,
            wall_clock=lambda: 1_760_000_000.0)

    def _launch(self):
        self.launches += 1
        return fw.ReviewerSession(route="omp", model="opus",
                                  session_id="sess-1",
                                  harness_owned_group=True)

    def sleep(self, seconds):
        self.clock.advance(4.0)


def report_for(matrix, findings=()):
    """A report answering every cell: `clear` everywhere except the
    known-bad canary and whatever `findings` names."""
    cells = []
    for cell in matrix.cells:
        finding = ((cell.check_id, cell.object_id) in findings
                   or cell.canary is fin.CanaryKind.KNOWN_BAD)
        cells.append({
            "check_id": cell.check_id,
            "object_id": cell.object_id,
            "status": "finding" if finding else "clear",
            "message": ("the instruction is a property of src/dispatch.py, "
                        "which this node may not write") if finding else "",
        })
    return {"plan_digest": matrix.plan_digest,
            "pair_count": matrix.pair_count,
            "cells": cells}


class WriteScopeTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def validate(self, mapping):
        stored = pc.canonicalize(pm.parse_mapping(mapping))
        return stored, pv.validate_plan(stored, self.repo,
                                        receipts=Receipts(),
                                        collector=Collector())

    def store(self):
        data_dir = self.root / "sssf-data"
        data_dir.mkdir(exist_ok=True)
        seed = rc.generate_seed()
        store = fin.ReceiptStore(self.root / "receipts",
                                 repo_paths=(self.repo,), data_dir=data_dir,
                                 verify_keys=[rc.seed_to_public_key(seed)],
                                 signing_seed=seed)
        return store, seed

    def finalize(self, store, digest, objects, factory):
        return fin.finalize(
            plan_digest=digest, objects=objects,
            rubric=fin.DEFAULT_RUBRIC, store=store,
            validate=lambda: (), window_factory=factory,
            occupancy_reader=lambda session: 0.4, sleep=factory.sleep,
            clock=lambda: 1_760_000_000.0)


class TheWriteCounterpartExists(WriteScopeTestCase):
    """The gap itself: one axis asked, the other silently unconstrained."""

    def test_the_rubric_asks_about_writes_as_well_as_reads(self):
        read = fin.DEFAULT_RUBRIC.check(READ_CHECK)
        write = fin.DEFAULT_RUBRIC.check(WRITE_CHECK)
        self.assertEqual(write.applies_to, read.applies_to)
        self.assertEqual(write.applies_to, (fin.ObjectKind.NODE,))
        self.assertIs(write.severity, fin.Severity.BLOCKING)
        self.assertIn("outputs", write.question)

    def test_the_question_names_the_instruction_not_the_shape_of_outputs(self):
        """A check phrased over `outputs` alone would convict every node that
        creates a new module. It is phrased over the instruction."""
        question = fin.DEFAULT_RUBRIC.check(WRITE_CHECK).question
        self.assertIn("instruction", question)

    def test_every_node_the_projection_emits_is_asked_it(self):
        plan = pm.parse_mapping(unsatisfiable_plan(self.base))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, "a" * 64, objects)
        asked = {c.object_id for c in matrix.cells
                 if c.check_id == WRITE_CHECK}
        nodes = {o.object_id for o in objects
                 if o.kind is fin.ObjectKind.NODE}
        self.assertEqual(asked, nodes)
        self.assertTrue(nodes)

    def test_it_is_asked_of_nodes_and_of_nothing_else(self):
        plan = pm.parse_mapping(unsatisfiable_plan(self.base))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, "a" * 64, objects)
        by_id = {o.object_id: o.kind for o in objects}
        for cell in matrix.cells:
            if cell.check_id != WRITE_CHECK:
                continue
            self.assertIs(by_id[cell.object_id], fin.ObjectKind.NODE)


class TheIncidentFailsAtPlanReview(WriteScopeTestCase):
    """run-2a44d226e75a4be391a14f02b78a6d25's shape, one review early."""

    def test_the_unsatisfiable_node_is_deterministically_eligible(self):
        """The half that makes this a rubric question at all.

        Every mechanical signal available at the plan's `base_commit` clears
        this plan: the node's outputs are well-formed, uniquely owned, and
        its gate is executable. Nothing in §6.4 can see that the instruction
        names a file the node may not write, which is why the check is a
        reviewer's and not an obligation's.
        """
        _stored, result = self.validate(unsatisfiable_plan(self.base))
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.outcome, pv.Outcome.FINALIZATION_ELIGIBLE)

    def test_a_blocking_finding_on_the_new_check_fails_the_plan(self):
        stored, result = self.validate(unsatisfiable_plan(self.base))
        digest = result.digest
        self.assertIsNotNone(digest)
        plan = pm.parse_mapping(json.loads(stored.decode("utf-8")))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, digest, objects)
        target = (WRITE_CHECK, "node:" + UNSATISFIABLE_NODE)
        self.assertIn(target, [(c.check_id, c.object_id)
                               for c in matrix.cells])

        store, _seed = self.store()
        factory = WindowFactory(report=report_for(matrix, findings=(target,)))
        outcome = self.finalize(store, digest, objects, factory)

        self.assertEqual(outcome.verdict, fin.Verdict.FAIL)
        self.assertFalse(outcome.replayed)
        convicting = [c for c in outcome.receipt.cells
                      if (c.check_id, c.object_id) == target]
        self.assertEqual(len(convicting), 1)
        self.assertIs(convicting[0].status, fin.CellStatus.FINDING)
        self.assertIs(convicting[0].severity, fin.Severity.BLOCKING)
        self.assertEqual(outcome.receipt.rubric_version, "maestro-rubric.v3")

    def test_the_same_plan_passes_when_only_this_check_is_answered_clear(self):
        """§13.4's pair: a check that convicts everything has convicted
        nothing. The identical plan, identical report, one cell changed."""
        stored, result = self.validate(unsatisfiable_plan(self.base))
        plan = pm.parse_mapping(json.loads(stored.decode("utf-8")))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, result.digest, objects)
        store, _seed = self.store()
        factory = WindowFactory(report=report_for(matrix))
        outcome = self.finalize(store, result.digest, objects, factory)
        self.assertEqual(outcome.verdict, fin.Verdict.PASS)

    def test_the_run_that_blocked_would_never_have_started(self):
        """The whole point: a FAIL receipt is what `run start` refuses with
        `RUN_RECEIPT_NOT_PASS`, so the eight attempts never happen."""
        stored, result = self.validate(unsatisfiable_plan(self.base))
        plan = pm.parse_mapping(json.loads(stored.decode("utf-8")))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, result.digest, objects)
        target = (WRITE_CHECK, "node:" + UNSATISFIABLE_NODE)
        store, _seed = self.store()
        factory = WindowFactory(report=report_for(matrix, findings=(target,)))
        self.finalize(store, result.digest, objects, factory)
        self.assertIs(store.load(result.digest).verdict, fin.Verdict.FAIL)


class ALegitimateNewModuleNodeStillPasses(WriteScopeTestCase):
    """The false-positive guard, which outranks the red test.

    Ten of the twelve nodes in the incident's own plan declared only
    `new_module.py` + `test_new_module.py`. So did the node that failed. The
    shape of `outputs` therefore separates nothing, and a check that read it
    would refuse the ordinary case — a module produced here and wired by a
    downstream node — and block authoring outright.
    """

    def test_nothing_deterministic_refuses_a_new_module_node(self):
        _stored, result = self.validate(wired_plan(self.base))
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.outcome, pv.Outcome.FINALIZATION_ELIGIBLE)

    def test_a_node_creating_only_new_files_is_not_pre_judged(self):
        """The producer's outputs are absent at `base_commit` — the exact
        shape a mechanical refusal would have keyed on. It is asked the
        question like any other node, and nothing answers it in advance."""
        plan = pm.parse_mapping(wired_plan(self.base))
        producer = plan.node_by_id()["lane-enrichment-module"]
        for path in producer.outputs:
            missing = subprocess.run(
                ["git", "-C", str(self.repo), "cat-file", "-e",
                 "{0}:{1}".format(self.base, path)], capture_output=True)
            self.assertNotEqual(missing.returncode, 0, path)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, "b" * 64,
                                    pf.review_objects(plan))
        self.assertIn((WRITE_CHECK, "node:lane-enrichment-module"),
                      [(c.check_id, c.object_id) for c in matrix.cells])

    def test_the_wired_plan_finalizes_pass(self):
        stored, result = self.validate(wired_plan(self.base))
        plan = pm.parse_mapping(json.loads(stored.decode("utf-8")))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, result.digest, objects)
        store, _seed = self.store()
        factory = WindowFactory(report=report_for(matrix))
        outcome = self.finalize(store, result.digest, objects, factory)
        self.assertEqual(outcome.verdict, fin.Verdict.PASS)
        self.assertEqual(factory.launches, 1)


class TheVersionBumpKeepsOlderReceiptsReadable(WriteScopeTestCase):
    """§19 M21: a rubric version is a promise about which checks applied,
    and it is not the receipt's schema discriminator. Adding a question must
    therefore cost an already-signed receipt nothing — not its bytes, not its
    signature, not its replay.
    """

    def _finalized_receipt_bytes(self, store):
        stored, result = self.validate(wired_plan(self.base))
        plan = pm.parse_mapping(json.loads(stored.decode("utf-8")))
        objects = pf.review_objects(plan)
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, result.digest, objects)
        factory = WindowFactory(report=report_for(matrix))
        self.finalize(store, result.digest, objects, factory)
        return result.digest, objects, store.path_for(result.digest).read_bytes()

    def _install(self, store, seed, digest, payload):
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        store.path_for(digest).write_bytes(data)
        store.signature_path_for(digest).write_text(
            rc.sign(seed, data).hex() + "\n", encoding="ascii")
        return data

    def test_the_default_rubric_is_v3(self):
        self.assertEqual(fin.DEFAULT_RUBRIC.version, "maestro-rubric.v3")

    def test_a_signed_v1_receipt_still_parses_verifies_and_replays(self):
        store, seed = self.store()
        digest, objects, written = self._finalized_receipt_bytes(store)
        payload = json.loads(written.decode("utf-8"))
        payload.pop("reject_at", None)
        payload["rubric_version"] = "maestro-rubric.v1"
        for cell in payload["cells"]:
            cell.pop("grade", None)
        historical = self._install(store, seed, digest, payload)

        parsed = fin.Receipt.from_bytes(historical)
        self.assertEqual(parsed.rubric_version, "maestro-rubric.v1")
        self.assertIsNone(parsed.reject_at)
        self.assertEqual(store.load(digest), parsed)

        replay = WindowFactory(report=None)
        outcome = self.finalize(store, digest, objects, replay)
        self.assertTrue(outcome.replayed)
        self.assertEqual(replay.launches, 0)
        self.assertEqual(outcome.receipt.rubric_version, "maestro-rubric.v1")

    def test_a_signed_pre_grading_v2_receipt_still_parses_and_replays(self):
        """The 110-cell PASS of §19 M21, one rubric version later."""
        store, seed = self.store()
        digest, objects, written = self._finalized_receipt_bytes(store)
        payload = json.loads(written.decode("utf-8"))
        payload.pop("reject_at", None)
        payload["rubric_version"] = "maestro-rubric.v2"
        for cell in payload["cells"]:
            cell.pop("grade", None)
        historical = self._install(store, seed, digest, payload)

        parsed = fin.Receipt.from_bytes(historical)
        self.assertEqual(parsed.rubric_version, "maestro-rubric.v2")
        self.assertIsNone(parsed.reject_at)
        self.assertTrue(parsed.cells)
        for cell in parsed.cells:
            self.assertIsNone(cell.grade)
        self.assertEqual(store.load(digest), parsed)

        replay = WindowFactory(report=None)
        outcome = self.finalize(store, digest, objects, replay)
        self.assertTrue(outcome.replayed)
        self.assertEqual(replay.launches, 0)
        self.assertEqual(outcome.receipt.rubric_version, "maestro-rubric.v2")

    def test_a_v2_receipt_keeps_the_matrix_it_was_reviewed_under(self):
        """A v2 receipt records a review in which the write question was
        never asked. Replay reports that matrix, not today's."""
        store, seed = self.store()
        digest, _objects, written = self._finalized_receipt_bytes(store)
        payload = json.loads(written.decode("utf-8"))
        payload.pop("reject_at", None)
        payload["rubric_version"] = "maestro-rubric.v2"
        payload["cells"] = [cell for cell in payload["cells"]
                            if cell["check_id"] != WRITE_CHECK]
        for cell in payload["cells"]:
            cell.pop("grade", None)
        historical = self._install(store, seed, digest, payload)

        parsed = fin.Receipt.from_bytes(historical)
        self.assertEqual(store.load(digest), parsed)
        self.assertEqual([c for c in parsed.cells
                          if c.check_id == WRITE_CHECK], [])

    def test_the_version_bump_adds_no_receipt_key(self):
        """The M21 discriminator stays the payload, not the label: a receipt
        written today carries exactly the keys a v2 graded receipt did."""
        store, _seed = self.store()
        _digest, _objects, written = self._finalized_receipt_bytes(store)
        payload = json.loads(written.decode("utf-8"))
        self.assertEqual(set(payload), {
            "plan_digest", "rubric_version", "verdict", "created_at_epoch",
            "reviewer", "cells", "reject_at"})
        for cell in payload["cells"]:
            self.assertEqual(set(cell), {
                "check_id", "object_id", "status", "severity", "message",
                "canary", "grade"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
